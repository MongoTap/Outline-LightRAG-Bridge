"""定时窗口调度 + 同文档任务合并 测试

Part A — 进程内单元测试（临时 DB，直接调用函数）
  - coalesce_pending 合并规则 8 例
  - coalesce_all_pending 折叠历史冗余
  - 窗口数学（注入 now，含跨午夜）

Part B — e2e（Mock LightRAG + Bridge 子进程，窗口关闭场景）
  - 窗口外只入队不处理
  - 同文档 CREATE+CREATE+DELETE → 全部合并丢弃
  - /queue 状态端点

运行: python tests/test_schedule_coalescing.py
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta

# 确保项目根目录在 sys.path（便于直接导入 bridge 包）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 必须在导入 bridge 之前设置环境（settings 在导入时实例化） ──
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["TASK_SCHEDULE_ENABLED"] = "false"

from bridge.config import settings  # noqa: E402
from bridge.database import (  # noqa: E402
    db_delete_pending_by_doc,
    db_enqueue_task,
    db_list_pending_tasks,
    db_upsert,
    init_db,
)
from bridge.models import QueueTask, TaskType  # noqa: E402
from bridge.tasks import (  # noqa: E402
    _seconds_until_window_open,
    _window_remaining_seconds,
    coalesce_all_pending,
    coalesce_pending,
)

PASS, FAIL = 0, 0
RESULTS: list[tuple[str, bool, str]] = []


def approx(a: float, b: float, tol: float = 2):
    assert abs(a - b) < tol, f"{a} != {b} (tol {tol})"


def reset_db():
    conn = sqlite3.connect(settings.db_path)
    conn.execute("DELETE FROM pending_tasks;")
    conn.execute("DELETE FROM document_mappings;")
    conn.commit()
    conn.close()


def rows_for(doc_id: str) -> list[dict]:
    return [r for r in db_list_pending_tasks() if r["outline_doc_id"] == doc_id]


def assert_one(doc_id: str, expected_type: str, expected_text: str = ""):
    rows = rows_for(doc_id)
    assert len(rows) == 1, f"{doc_id} 应只有 1 行，实际 {rows}"
    assert rows[0]["task_type"] == expected_type, f"{doc_id} 类型 {rows[0]['task_type']} != {expected_type}"
    assert rows[0]["text"] == expected_text, f"{doc_id} 文本 {rows[0]['text']!r} != {expected_text!r}"


def test(name: str):
    def dec(fn):
        def wrapper():
            global PASS, FAIL
            reset_db()
            print(f"\n  ▶ {name}")
            try:
                fn()
                print("    ✓ PASS")
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


# ══════════════════════════════════════════════════════════════════════
#  Part A — 合并规则单元测试
# ══════════════════════════════════════════════════════════════════════

@test("CREATE + CREATE（未同步）→ 只留最新 CREATE")
def t_create_create():
    coalesce_pending(QueueTask(TaskType.CREATE, "d1", "t", "v1"))
    coalesce_pending(QueueTask(TaskType.CREATE, "d1", "t", "v2"))
    assert_one("d1", "create", "v2")


@test("CREATE + UPDATE（未同步）→ 转 CREATE 只留最新")
def t_create_update_unsynced():
    coalesce_pending(QueueTask(TaskType.CREATE, "d2", "t", "v1"))
    coalesce_pending(QueueTask(TaskType.UPDATE, "d2", "t", "v2"))
    assert_one("d2", "create", "v2")


@test("UPDATE + UPDATE（已同步）→ 转 UPDATE 只留最新")
def t_update_update_synced():
    db_upsert("d3", "lightrag-3", "t", "ready")
    coalesce_pending(QueueTask(TaskType.UPDATE, "d3", "t", "v1"))
    coalesce_pending(QueueTask(TaskType.UPDATE, "d3", "t", "v2"))
    assert_one("d3", "update", "v2")


@test("CREATE + DELETE（未同步）→ 全部丢弃")
def t_create_delete_unsynced():
    coalesce_pending(QueueTask(TaskType.CREATE, "d4", "t", "v1"))
    rid = coalesce_pending(QueueTask(TaskType.DELETE, "d4"))
    assert rid is None
    assert rows_for("d4") == []


@test("CREATE + DELETE（已同步）→ 只留 DELETE")
def t_create_delete_synced():
    db_upsert("d5", "lightrag-5", "t", "ready")
    coalesce_pending(QueueTask(TaskType.CREATE, "d5", "t", "v1"))
    rid = coalesce_pending(QueueTask(TaskType.DELETE, "d5"))
    assert rid is not None
    assert_one("d5", "delete")


@test("DELETE + CREATE（已同步）→ 转 UPDATE 只留最新")
def t_delete_create_synced():
    db_upsert("d6", "lightrag-6", "t", "ready")
    coalesce_pending(QueueTask(TaskType.DELETE, "d6"))
    rid = coalesce_pending(QueueTask(TaskType.CREATE, "d6", "t", "v2"))
    assert rid is not None
    assert_one("d6", "update", "v2")


@test("DELETE + DELETE（已同步）→ 只留一个 DELETE")
def t_delete_delete():
    db_upsert("d7", "lightrag-7", "t", "ready")
    coalesce_pending(QueueTask(TaskType.DELETE, "d7"))
    rid = coalesce_pending(QueueTask(TaskType.DELETE, "d7"))
    assert rid is None
    assert_one("d7", "delete")


@test("DELETE（未同步，单独）→ 丢弃")
def t_delete_alone_unsynced():
    rid = coalesce_pending(QueueTask(TaskType.DELETE, "d8"))
    assert rid is None
    assert rows_for("d8") == []


@test("coalesce_all_pending 折叠历史冗余行")
def t_coalesce_all():
    db_enqueue_task(QueueTask(TaskType.CREATE, "d9", "t", "v1"))
    db_enqueue_task(QueueTask(TaskType.CREATE, "d9", "t", "v2"))
    removed = coalesce_all_pending()
    assert removed == 2
    assert_one("d9", "create", "v2")


# ══════════════════════════════════════════════════════════════════════
#  Part A — 窗口数学单元测试
# ══════════════════════════════════════════════════════════════════════

@test("窗口数学 — 00:00 开始 / 480min")
def t_window_normal():
    settings.task_schedule_start = "00:00"
    settings.task_schedule_duration_minutes = 480
    base = datetime(2026, 7, 31, 12, 0, 0)
    assert _seconds_until_window_open(base.replace(hour=1)) == 0.0
    approx(_seconds_until_window_open(base.replace(hour=10)), 14 * 3600)
    approx(_window_remaining_seconds(base.replace(hour=1)), 7 * 3600)
    assert _window_remaining_seconds(base.replace(hour=10)) == 0.0


@test("窗口数学 — 23:00 开始 / 240min（跨午夜）")
def t_window_cross_midnight():
    settings.task_schedule_start = "23:00"
    settings.task_schedule_duration_minutes = 240
    assert _seconds_until_window_open(datetime(2026, 7, 31, 1, 0, 0)) == 0.0
    approx(_seconds_until_window_open(datetime(2026, 7, 31, 5, 0, 0)), 18 * 3600)
    approx(_seconds_until_window_open(datetime(2026, 7, 31, 22, 0, 0)), 1 * 3600)
    approx(_window_remaining_seconds(datetime(2026, 7, 31, 1, 0, 0)), 2 * 3600)


# ══════════════════════════════════════════════════════════════════════
#  Part B — e2e（Mock LightRAG + Bridge 子进程，窗口关闭）
# ══════════════════════════════════════════════════════════════════════

MOCK_PORT = 19631
BRIDGE_PORT = 19651
TEST_DB = "/tmp/test_bridge_schedule.db"

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402


class MockLightRAG:
    def __init__(self):
        self.reset()

    def reset(self):
        self.inserted_texts: list[dict] = []
        self.deleted_doc_ids: list[str] = []
        self.track_counter = 0

    def track_id(self):
        self.track_counter += 1
        return f"mock-track-{self.track_counter}"


MOCK = MockLightRAG()
mock_app = FastAPI()


@mock_app.post("/documents/text")
async def insert(request: Request):
    body = await request.json()
    tid = MOCK.track_id()
    MOCK.inserted_texts.append({"text": body.get("text", "")})
    return {"track_id": tid}


@mock_app.get("/documents/track_status/{track_id}")
async def track_status(track_id: str):
    return {"documents": [{"id": f"mock-doc-{track_id}", "status": "processed"}]}


@mock_app.delete("/documents/delete_document")
async def delete(request: Request):
    body = await request.json()
    MOCK.deleted_doc_ids.extend(body.get("doc_ids", []))
    return {"status": "ok"}


@mock_app.get("/documents/pipeline_status")
async def pipeline_status():
    return {"busy": False, "request_pending": False}


def run_mock():
    uvicorn.run(mock_app, host="127.0.0.1", port=MOCK_PORT, log_level="error")


def wb(event: str, doc_id: str, title: str = "", text: str = "") -> dict:
    return {
        "event": f"documents.{event}",
        "createdAt": datetime.now().isoformat(),
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


def main():
    global PASS, FAIL

    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

    # 窗口必然关闭：开始时间设为 30 分钟后的 HH:MM
    start_time = (datetime.now() + timedelta(minutes=30)).strftime("%H:%M")
    env = {
        "LIGHTRAG_API_URL": f"http://127.0.0.1:{MOCK_PORT}",
        "BRIDGE_HOST": "127.0.0.1",
        "BRIDGE_PORT": str(BRIDGE_PORT),
        "DB_PATH": TEST_DB,
        "LOG_LEVEL": "WARNING",
        "OUTLINE_WEBHOOK_SECRET": "",
        "TASK_SCHEDULE_ENABLED": "true",
        "TASK_SCHEDULE_START": start_time,
        "TASK_SCHEDULE_DURATION_MINUTES": "1",
        "PYTHONUNBUFFERED": "1",
    }

    threading.Thread(target=run_mock, daemon=True).start()
    wait_for(f"http://127.0.0.1:{MOCK_PORT}/documents/pipeline_status")

    proc = subprocess.Popen(
        [sys.executable, "-m", "bridge"],
        env={**os.environ, **env},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    wait_for(f"http://127.0.0.1:{BRIDGE_PORT}/health")

    client = httpx.Client()
    base = f"http://127.0.0.1:{BRIDGE_PORT}"
    try:
        # ── 1. /queue 初始状态 ──────────────────────────────────
        q = client.get(f"{base}/queue").json()
        assert q["schedule_enabled"] is True
        assert q["window_state"] == "closed", q
        assert "next_window_at" in q
        assert q["pending_count"] == 0
        print("  ✓ /queue 初始状态正确（窗口关闭）")

        # ── 2. 同文档 CREATE×2 + DELETE → 全部合并丢弃 ──────────
        for ev in [
            wb("create", "s1", "t", "v1"),
            wb("create", "s1", "t", "v2"),
            wb("delete", "s1"),
        ]:
            r = client.post(f"{base}/webhook", json=ev, timeout=10)
            assert r.status_code == 200 and r.json()["status"] == "accepted", r.text
        q = client.get(f"{base}/queue").json()
        assert q["pending_count"] == 0, q
        print("  ✓ CREATE+CREATE+DELETE（未同步）→ 合并丢弃为 0")

        # ── 3. 另一文档 CREATE → 只入队不处理 ───────────────────
        r = client.post(f"{base}/webhook", json=wb("create", "s2", "t", "x"), timeout=10)
        assert r.status_code == 200
        q = client.get(f"{base}/queue").json()
        assert q["pending_count"] == 1, q
        print("  ✓ 窗口外 CREATE 入队，pending_count=1")

        # ── 4. 等待片刻确认零处理 ───────────────────────────────
        time.sleep(2)
        assert len(MOCK.inserted_texts) == 0, MOCK.inserted_texts
        assert len(MOCK.deleted_doc_ids) == 0, MOCK.deleted_doc_ids
        mappings = client.get(f"{base}/mappings").json().get("mappings", [])
        assert mappings == [], mappings
        print("  ✓ 窗口未开启时零处理、无映射")

        PASS += 4
        for name in ["e2e 窗口关闭 + 合并丢弃 + 只入队不处理"]:
            RESULTS.append((name, True, ""))

    except Exception as e:
        print(f"  ✗ FAIL: {e}")
        import traceback
        traceback.print_exc()
        FAIL += 1
        RESULTS.append(("e2e 调度场景", False, str(e)))

    finally:
        client.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)

    total = PASS + FAIL
    print(f"\n{'─'*60}")
    print(f"  结果: {PASS}/{total} 通过")
    for name, ok, err in RESULTS:
        print(f"  {'✓' if ok else '✗'} {name}{'  ' + err if err else ''}")
    print(f"{'─'*60}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    init_db()  # 创建临时 DB 的表结构

    # Part A 单元测试
    unit_tests = [
        t_create_create, t_create_update_unsynced, t_update_update_synced,
        t_create_delete_unsynced, t_create_delete_synced, t_delete_create_synced,
        t_delete_delete, t_delete_alone_unsynced, t_coalesce_all,
        t_window_normal, t_window_cross_midnight,
    ]
    for fn in unit_tests:
        fn()

    # Part B e2e
    sys.exit(main())
