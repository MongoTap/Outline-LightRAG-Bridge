"""OutlineRAGBridge — 端到端集成测试

架构: Mock LightRAG（线程内 uvicorn）+ Bridge（子进程 uvicorn）
每个测试独立重置 Mock 状态并 cleanup。
"""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import httpx
import uvicorn
from fastapi import FastAPI, Request

# ──────────────────────────────────────────────────────────────────────
#  配置
# ──────────────────────────────────────────────────────────────────────

MOCK_LIGHTRAG_PORT = 19621
BRIDGE_PORT = 19641
TEST_DB = "/tmp/test_bridge_e2e.db"

BRIDGE_ENV = {
    "LIGHTRAG_API_URL": f"http://127.0.0.1:{MOCK_LIGHTRAG_PORT}",
    "BRIDGE_HOST": "127.0.0.1",
    "BRIDGE_PORT": str(BRIDGE_PORT),
    "DB_PATH": TEST_DB,
    "LOG_LEVEL": "WARNING",
    "OUTLINE_WEBHOOK_SECRET": "",
    "POLL_INTERVAL": "1",
    "POLL_MAX_ATTEMPTS": "15",
    "DELETE_RETRY_ATTEMPTS": "5",
    "DELETE_RETRY_DELAY": "1",
    "TASK_SCHEDULE_ENABLED": "false",
    "PYTHONUNBUFFERED": "1",
}


# ──────────────────────────────────────────────────────────────────────
#  Mock LightRAG
# ──────────────────────────────────────────────────────────────────────

class MockLightRAG:
    def __init__(self):
        self.reset()

    def reset(self):
        self.inserted_texts: list[dict] = []
        self.deleted_doc_ids: list[str] = []
        self.track_counter = 0
        self.doc_counter = 0
        self.pipeline_busy = False
        self.delete_busy_countdown = 0
        self.insert_fail_countdown = 0
        self.poll_fail_countdown = 0
        self.poll_processing_delay = 0
        self.track_statuses: dict[str, int] = {}

    def track_id(self) -> str:
        self.track_counter += 1
        return f"mock-track-{self.track_counter}"

    def doc_id(self) -> str:
        self.doc_counter += 1
        return f"mock-doc-{self.doc_counter}"


MOCK = MockLightRAG()
mock_app = FastAPI()


@mock_app.post("/documents/text")
async def insert(request: Request):
    body = await request.json()
    if MOCK.insert_fail_countdown > 0:
        MOCK.insert_fail_countdown -= 1
        from starlette.responses import Response
        return Response(status_code=502)
    tid = MOCK.track_id()
    MOCK.track_statuses[tid] = MOCK.poll_processing_delay
    MOCK.inserted_texts.append({"text": body.get("text", ""), "file_source": body.get("file_source", ""), "track_id": tid})
    return {"track_id": tid}


@mock_app.get("/documents/track_status/{track_id}")
async def track_status(track_id: str):
    rem = MOCK.track_statuses.get(track_id, -1)
    if rem < 0:
        return {"documents": []}
    if rem > 0:
        MOCK.track_statuses[track_id] = rem - 1
        return {"documents": [{"id": "", "status": "processing"}]}
    if MOCK.poll_fail_countdown > 0:
        MOCK.poll_fail_countdown -= 1
        from starlette.responses import Response
        return Response(status_code=500)
    return {"documents": [{"id": MOCK.doc_id(), "status": "processed"}]}


@mock_app.delete("/documents/delete_document")
async def delete(request: Request):
    body = await request.json()
    ids = body.get("doc_ids", [])
    if MOCK.delete_busy_countdown > 0:
        MOCK.delete_busy_countdown -= 1
        return {"status": "busy"}
    MOCK.deleted_doc_ids.extend(ids)
    return {"status": "ok"}


@mock_app.get("/documents/pipeline_status")
async def pipeline_status():
    return {"busy": MOCK.pipeline_busy, "request_pending": False}


def run_mock():
    uvicorn.run(mock_app, host="127.0.0.1", port=MOCK_LIGHTRAG_PORT, log_level="error")


# ──────────────────────────────────────────────────────────────────────
#  工具函数
# ──────────────────────────────────────────────────────────────────────

def wb(event: str, doc_id: str, title: str = "", text: str = "") -> dict:
    return {
        "event": f"documents.{event}",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "payload": {"id": doc_id, "model": {"id": doc_id, "title": title, "text": text}},
    }


def wait_for(url: str, timeout: int = 15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=2)
            if r.status_code < 500:
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"服务器启动超时: {url}")


def get_mappings(client: httpx.Client) -> list[dict]:
    return client.get(f"http://127.0.0.1:{BRIDGE_PORT}/mappings").json().get("mappings", [])


def find_mapping(client: httpx.Client, oid: str):
    for m in get_mappings(client):
        if m["outline_doc_id"] == oid:
            return m
    return None


def await_mapping(client: httpx.Client, oid: str, timeout: int = 25) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        m = find_mapping(client, oid)
        if m and m["status"] in ("ready", "failed"):
            return m
        time.sleep(0.3)
    raise TimeoutError(f"等待 {oid} 超时 {timeout}s")


def await_mapping_updated(client: httpx.Client, oid: str, old_doc_id: str, timeout: int = 25) -> dict:
    """等待映射的 lightrag_doc_id 发生变化（用于 UPDATE 测试）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        m = find_mapping(client, oid)
        if m and m["lightrag_doc_id"] and m["lightrag_doc_id"] != old_doc_id:
            return m
        time.sleep(0.3)
    raise TimeoutError(f"等待 {oid} 更新超时 {timeout}s, old={old_doc_id}")


def wait_pending_done(client: httpx.Client, timeout: int = 30):
    """等待 bridge 处理完所有待处理任务。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"http://127.0.0.1:{BRIDGE_PORT}/mappings")
        # 检查所有 mapping 都不是 processing 状态
        all_done = True
        for m in r.json().get("mappings", []):
            if m["status"] == "processing":
                all_done = False
                break
        if all_done:
            return
        time.sleep(0.5)
    raise TimeoutError("等待任务处理完成超时")


def await_no_mapping(client: httpx.Client, oid: str, timeout: int = 25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not find_mapping(client, oid):
            return
        time.sleep(0.3)
    raise TimeoutError(f"等待 {oid} 删除超时 {timeout}s")


# ──────────────────────────────────────────────────────────────────────
#  测试（基于 httpx.Client 同步调用）
# ──────────────────────────────────────────────────────────────────────

PASS, FAIL = 0, 0
RESULTS: list[tuple[str, bool, str]] = []
CLIENT: httpx.Client = None


def test(name: str):
    def dec(fn):
        def wrapper():
            global PASS, FAIL
            MOCK.reset()
            print(f"\n  ▶ {name}")
            try:
                fn()
                print(f"    ✓ PASS")
                PASS += 1
                RESULTS.append((name, True, ""))
            except Exception as e:
                print(f"    ✗ FAIL: {e}")
                import traceback
                traceback.print_exc()
                FAIL += 1
                RESULTS.append((name, False, str(e)))
        return wrapper
    return dec


def bridge_post(path: str, json_data: dict) -> httpx.Response:
    return CLIENT.post(f"http://127.0.0.1:{BRIDGE_PORT}{path}", json=json_data, timeout=10)


# ══════════════════════════════════════════════════════════════════════
#  测试用例
# ══════════════════════════════════════════════════════════════════════

@test("CREATE — 新建文档")
def t_create():
    r = bridge_post("/webhook", wb("create", "d1", "标题", "内容"))
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    m = await_mapping(CLIENT, "d1")
    assert m["status"] == "ready"
    assert m["lightrag_doc_id"].startswith("mock-doc-")
    assert len(MOCK.inserted_texts) == 1
    assert MOCK.inserted_texts[0]["text"] == "内容"
    assert MOCK.inserted_texts[0]["file_source"] == "outline:d1"


@test("DELETE — 先创建再删除")
def t_delete():
    r = bridge_post("/webhook", wb("create", "d2", "t", "x"))
    assert r.status_code == 200
    m1 = await_mapping(CLIENT, "d2")
    lid = m1["lightrag_doc_id"]

    r = bridge_post("/webhook", wb("delete", "d2"))
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    await_no_mapping(CLIENT, "d2")
    assert lid in MOCK.deleted_doc_ids


@test("DELETE — 不存在则静默跳过")
def t_delete_none():
    r = bridge_post("/webhook", wb("delete", "d-none"))
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    time.sleep(1)
    assert len(MOCK.deleted_doc_ids) == 0
    assert len(MOCK.inserted_texts) == 0


@test("UPDATE — 先删旧文档再插新文档")
def t_update():
    r = bridge_post("/webhook", wb("create", "d3", "旧", "旧内容"))
    assert r.status_code == 200
    m1 = await_mapping(CLIENT, "d3")
    lid_old = m1["lightrag_doc_id"]

    r = bridge_post("/webhook", wb("update", "d3", "新", "新内容"))
    assert r.status_code == 200 and r.json()["status"] == "accepted"

    # 等待 doc_id 变化（确认 UPDATE 已被 worker 处理）
    m2 = await_mapping_updated(CLIENT, "d3", lid_old)
    assert m2["outline_title"] == "新"
    assert len(MOCK.inserted_texts) == 2
    assert MOCK.inserted_texts[1]["text"] == "新内容"
    assert lid_old in MOCK.deleted_doc_ids


@test("UPDATE — 无旧映射则直接创建")
def t_update_new():
    r = bridge_post("/webhook", wb("update", "d4", "新", "x"))
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    m = await_mapping(CLIENT, "d4")
    assert m["status"] == "ready"
    assert len(MOCK.deleted_doc_ids) == 0


@test("Pipeline Busy — 繁忙时等待空闲再处理")
def t_pipeline_busy():
    MOCK.pipeline_busy = True
    start = time.time()

    r = bridge_post("/webhook", wb("create", "d5", "t", "x"))
    assert r.status_code == 200

    time.sleep(2)  # worker 进入 wait_pipeline_idle
    MOCK.pipeline_busy = False  # 释放

    m = await_mapping(CLIENT, "d5", timeout=20)
    assert m["status"] == "ready"
    elapsed = time.time() - start
    assert elapsed > 1.5, f"未观察到等待: {elapsed:.2f}s"


@test("DELETE w/ Busy — 删除时 busy 重试后成功")
def t_delete_busy():
    r = bridge_post("/webhook", wb("create", "d6", "t", "x"))
    assert r.status_code == 200
    m = await_mapping(CLIENT, "d6")
    lid = m["lightrag_doc_id"]

    MOCK.delete_busy_countdown = 2

    r = bridge_post("/webhook", wb("delete", "d6"))
    assert r.status_code == 200
    await_no_mapping(CLIENT, "d6", timeout=20)
    assert lid in MOCK.deleted_doc_ids


@test("多事件串行 — CREATE+CREATE+DELETE")
def t_multi():
    # 串行处理（每个事件完成后才发下一个），避免同文档合并导致的竞态
    r1 = bridge_post("/webhook", wb("create", "s1", "a", "a"))
    assert r1.status_code == 200
    await_mapping(CLIENT, "s1")

    r2 = bridge_post("/webhook", wb("create", "s2", "b", "b"))
    assert r2.status_code == 200
    await_mapping(CLIENT, "s2")

    r3 = bridge_post("/webhook", wb("delete", "s1"))
    assert r3.status_code == 200
    await_no_mapping(CLIENT, "s1")

    m2 = find_mapping(CLIENT, "s2")
    assert m2 is not None and m2["status"] == "ready"
    assert find_mapping(CLIENT, "s1") is None
    assert len(MOCK.inserted_texts) == 2
    assert len(MOCK.deleted_doc_ids) == 1


@test("空文本跳过 — 不产生任务")
def t_empty():
    r = bridge_post("/webhook", wb("create", "d7", "空", ""))
    assert r.status_code == 200
    assert r.json()["status"] == "skipped"
    time.sleep(0.5)
    assert len(MOCK.inserted_texts) == 0
    assert find_mapping(CLIENT, "d7") is None


@test("UPDATE 原子性 — 新文档插入失败后重试")
def t_atomic():
    r = bridge_post("/webhook", wb("create", "d8", "旧", "旧"))
    assert r.status_code == 200
    m = await_mapping(CLIENT, "d8")
    lid_old = m["lightrag_doc_id"]

    MOCK.insert_fail_countdown = 1

    r = bridge_post("/webhook", wb("update", "d8", "新", "新"))
    assert r.status_code == 200

    # 等待 doc_id 变化（UPDATE 完成）
    m = await_mapping_updated(CLIENT, "d8", lid_old, timeout=30)
    assert m["status"] == "ready"
    # 创建 1 次 + 更新至少 1 次
    assert len(MOCK.inserted_texts) >= 2


# ══════════════════════════════════════════════════════════════════════
#  main
# ══════════════════════════════════════════════════════════════════════

def main():
    global CLIENT, PASS, FAIL

    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

    # 启动 Mock LightRAG
    threading.Thread(target=run_mock, daemon=True).start()
    wait_for(f"http://127.0.0.1:{MOCK_LIGHTRAG_PORT}/documents/pipeline_status")

    # 启动 Bridge 子进程
    proc = subprocess.Popen(
        [sys.executable, "-m", "bridge"],
        env={**os.environ, **BRIDGE_ENV},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    wait_for(f"http://127.0.0.1:{BRIDGE_PORT}/health")

    CLIENT = httpx.Client()

    tests = [
        ("CREATE", t_create),
        ("DELETE", t_delete),
        ("DELETE 不存在", t_delete_none),
        ("UPDATE", t_update),
        ("UPDATE 无旧映射", t_update_new),
        ("Pipeline Busy 等待", t_pipeline_busy),
        ("DELETE w/ Busy 重试", t_delete_busy),
        ("多事件串行", t_multi),
        ("空文本跳过", t_empty),
        ("UPDATE 原子性", t_atomic),
    ]

    print(f"\n{'='*60}")
    print(f"  OutlineRAGBridge 端到端集成测试")
    print(f"  Mock LightRAG: :{MOCK_LIGHTRAG_PORT}  Bridge: :{BRIDGE_PORT}  DB: {TEST_DB}")
    print(f"{'='*60}")

    for name, fn in tests:
        fn()

    CLIENT.close()
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    total = PASS + FAIL
    print(f"\n{'─'*60}")
    print(f"  结果: {PASS}/{total} 通过")
    for name, ok, err in RESULTS:
        print(f"  {'✓' if ok else '✗'} {name}{'  ' + err if err else ''}")
    print(f"{'─'*60}")

    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
