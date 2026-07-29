"""
Outline ↔ LightRAG 桥接服务
============================

功能概述：
  监听 Outline 的 Webhook 事件（文档创建/更新/删除），自动同步文档内容到 LightRAG 知识库。
  当 Outline 中的文档发生变化时，LightRAG 中的文档切片和向量数据也会同步更新。

工作流程：
  1. Outline 产生文档事件 → 通过 Webhook POST 到本服务
  2. 本服务验证 Webhook 签名 → 解析事件类型 → 启动后台异步任务
  3. 后台任务调用 LightRAG API 进行对应的插入/删除/更新操作

依赖的外部服务：
  - LightRAG Docker (API: http://localhost:9621)
  - Outline Docker (通过 Webhook 推送事件)
  - SQLite (本地存储文档 ID 映射关系)

事件处理策略：
  - documents.create  → 插入文本到 LightRAG，轮询异步处理完成，存储映射
  - documents.update  → 删除旧文档 → 插入新文本 → 更新映射
  - documents.delete  → 通过映射删除 LightRAG 文档 → 移除映射
  - documents.publish / restore / unarchive → 按 create 逻辑处理
"""

import asyncio
import hashlib
import hmac
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

# ═══════════════════════════════════════════════════════════════════════
#  配置管理
#  使用 pydantic-settings 从环境变量和 .env 文件读取配置
# ═══════════════════════════════════════════════════════════════════════


class Settings(BaseSettings):
    """应用配置集合

    所有配置项都支持通过环境变量覆盖。如果存在 .env 文件，会自动读取。
    例如: LIGHTRAG_API_URL=http://localhost:9621
    """

    # ── LightRAG 连接配置 ──────────────────────────────────────────
    lightrag_api_url: str = Field(
        default="http://localhost:9621",
        description="LightRAG API 的基础 URL。如果 LightRAG 部署在其他主机，修改此值。",
    )

    # ── Outline Webhook 安全配置 ───────────────────────────────────
    outline_webhook_secret: str = Field(
        default="",
        description="Outline Webhook 的共享密钥，用于 HMAC-SHA256 签名验证。"
                    "在 Outline 管理后台配置 Webhook 时设置。为空时跳过验证（仅测试用）。",
    )

    # ── 桥接服务监听配置 ───────────────────────────────────────────
    bridge_host: str = Field(default="0.0.0.0", description="桥接服务监听地址。0.0.0.0 表示监听所有网络接口。")
    bridge_port: int = Field(default=9641, description="桥接服务监听端口。Outline 配置 Webhook 时需指向此端口。")

    # ── LightRAG 异步处理轮询配置 ──────────────────────────────────
    # LightRAG 的文档插入是异步的：/documents/text 返回 track_id，
    # 需要轮询 /documents/track_status/{track_id} 直到处理完成。
    poll_interval: int = Field(
        default=2, description="轮询 LightRAG 处理状态的时间间隔（秒）。"
    )
    poll_max_attempts: int = Field(
        default=60, description="最大轮询次数。与 poll_interval 共同决定最长等待时间（默认 120 秒）。"
    )

    # ── LightRAG 删除重试配置 ──────────────────────────────────────
    # 由于任务队列串行执行 + 派发前 pre-flight pipeline 检查，
    # pipeline busy 冲突已基本消除。保留轻量安全网作为最后兜底。
    delete_retry_attempts: int = Field(
        default=2, description="LightRAG pipeline busy 时的最大重试次数。"
    )
    delete_retry_delay: int = Field(
        default=3, description="每次重试之间的等待时间（秒）。"
    )

    # ── 日志配置 ───────────────────────────────────────────────────
    log_level: str = Field(default="INFO", description="日志级别：DEBUG, INFO, WARNING, ERROR")

    # ── 数据库配置 ──────────────────────────────────────────────────
    db_path: str = Field(
        default="bridge.db",
        description="SQLite 数据库文件路径。Docker 环境下建议设为 /app/data/bridge.db",
    )

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()

# ═══════════════════════════════════════════════════════════════════════
#  任务队列数据结构
#  用于串行化所有 LightRAG 变更操作，避免 pipeline busy 冲突
# ═══════════════════════════════════════════════════════════════════════


class TaskType(Enum):
    """队列任务类型，对应 Outline 的文档变更事件。"""
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


@dataclass
class QueueTask:
    """队列中的单个任务。

    所有 LightRAG 变更操作（create/update/delete）都封装为此结构，
    通过 asyncio.Queue 串行派发给单消费者 Worker 处理。
    """
    task_type: TaskType
    outline_doc_id: str
    title: str = ""
    text: str = ""
    retry_count: int = 0
    max_retries: int = 3
    last_error: str = ""
    db_id: int = 0  # pending_tasks 表中的行 ID，处理完成后用于删除


# ═══════════════════════════════════════════════════════════════════════
#  日志配置
# ═══════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("outline-rag-bridge")

# ═══════════════════════════════════════════════════════════════════════
#  SQLite 数据库 — 文档 ID 映射关系存储
#
#  为什么需要 SQLite？
#  LightRAG 插入文档后返回的是 doc_id（如 doc-xxx），而 Outline 的文档
#  有自己的 ID（如 doc-abc123）。我们需要维护这两者之间的映射关系：
#    - 删除时：通过 Outline ID 找到 LightRAG doc_id 才能调用删除 API
#    - 更新时：先删除旧的 LightRAG 文档，再插入新内容
#    - 崩溃恢复：即使服务重启，也能通过映射表知道哪些文档正在处理中
#
#  数据库文件位置：bridge.db（在桥接服务的工作目录下）
# ═══════════════════════════════════════════════════════════════════════

# ── 建表 SQL ────────────────────────────────────────────────────────
# document_mappings 表字段说明：
#   - outline_doc_id: Outline 文档的唯一 ID（主键）
#   - lightrag_doc_id: LightRAG 中文档的 ID（如 doc-xxx），处理完成前为空
#   - track_id: LightRAG 异步处理的追踪 ID，用于轮询处理状态
#   - outline_title: Outline 文档标题（便于调试和人工查看）
#   - status: 同步状态（processing=处理中, ready=已完成, failed=失败）
#   - created_at / updated_at: 记录创建和更新时间
SQL_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS document_mappings (
    outline_doc_id TEXT PRIMARY KEY,
    lightrag_doc_id TEXT DEFAULT '',
    track_id TEXT DEFAULT '',
    outline_title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'processing',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# ── 数据库操作 SQL 语句 ────────────────────────────────────────────

# UPSERT: 如果 outline_doc_id 已存在则更新，不存在则插入
SQL_UPSERT = """
INSERT INTO document_mappings (outline_doc_id, lightrag_doc_id, track_id, outline_title, status, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(outline_doc_id) DO UPDATE SET
    lightrag_doc_id = excluded.lightrag_doc_id,
    track_id = excluded.track_id,
    outline_title = excluded.outline_title,
    status = excluded.status,
    updated_at = excluded.updated_at;
"""

# 数据库迁移：旧版本可能没有 track_id 列，动态添加
SQL_ADD_TRACK_ID = """
ALTER TABLE document_mappings ADD COLUMN track_id TEXT DEFAULT '';
"""
SQL_ADD_TRACK_ID_CHECK = """
SELECT COUNT(*) AS cnt FROM pragma_table_info('document_mappings') WHERE name='track_id';
"""

SQL_FIND_BY_OUTLINE_ID = "SELECT * FROM document_mappings WHERE outline_doc_id = ?;"
SQL_DELETE = "DELETE FROM document_mappings WHERE outline_doc_id = ?;"
SQL_LIST = "SELECT * FROM document_mappings ORDER BY updated_at DESC LIMIT ? OFFSET ?;"

# ── pending_tasks 表 ─────────────────────────────────────────────────
# 用于任务队列的崩溃恢复。每个任务在入队时先写入此表，
# 处理完成后删除。服务重启时从未完成的任务重新入队。
SQL_CREATE_PENDING_TASKS = """
CREATE TABLE IF NOT EXISTS pending_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    outline_doc_id TEXT NOT NULL,
    title TEXT DEFAULT '',
    text TEXT DEFAULT '',
    retry_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
"""

SQL_INSERT_PENDING_TASK = """
INSERT INTO pending_tasks (task_type, outline_doc_id, title, text, retry_count, created_at)
VALUES (?, ?, ?, ?, ?, ?);
"""
SQL_DELETE_PENDING_TASK = "DELETE FROM pending_tasks WHERE id = ?;"
SQL_LIST_PENDING_TASKS = "SELECT * FROM pending_tasks ORDER BY id ASC;"


def init_db():
    """初始化数据库：创建表结构并执行必要的迁移。"""
    conn = sqlite3.connect(settings.db_path)
    conn.execute(SQL_CREATE_TABLE)
    conn.execute(SQL_CREATE_PENDING_TASKS)
    # 检查是否需要迁移（新增 track_id 列）
    # 对于已有的数据库文件，可能需要添加这个列
    row = conn.execute(SQL_ADD_TRACK_ID_CHECK).fetchone()
    if row[0] == 0:
        conn.execute(SQL_ADD_TRACK_ID)
        logger.info("数据库迁移：已添加 track_id 列")
    conn.commit()
    conn.close()
    logger.info("数据库初始化完成：%s", settings.db_path)


def db_upsert(outline_doc_id: str, lightrag_doc_id: str, outline_title: str, status: str, track_id: str = ""):
    """创建或更新文档映射记录。

    这是核心的持久化方法，在以下场景被调用：
    1. 文档创建/更新时 → 先存"processing"状态，处理完成后更新为"ready"
    2. 处理失败时 → 更新为"failed"状态
    这样即使服务在轮询过程中崩溃重启，也不会丢失正在处理的任务状态。
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(settings.db_path)
    conn.execute(SQL_UPSERT, (outline_doc_id, lightrag_doc_id, track_id, outline_title, status, now, now))
    conn.commit()
    conn.close()


def db_find_by_outline_id(outline_doc_id: str) -> Optional[dict]:
    """根据 Outline 文档 ID 查询映射记录。

    主要用于文档更新和删除操作：
      - 更新时需要找到旧文档的 lightrag_doc_id 来执行删除
      - 删除时需要找到 lightrag_doc_id 来调用 LightRAG 删除 API
    """
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(SQL_FIND_BY_OUTLINE_ID, (outline_doc_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def db_delete(outline_doc_id: str):
    """删除文档映射记录。在文档从 LightRAG 中删除成功后调用。"""
    conn = sqlite3.connect(settings.db_path)
    conn.execute(SQL_DELETE, (outline_doc_id,))
    conn.commit()
    conn.close()


def db_list(limit: int = 50, offset: int = 0) -> list[dict]:
    """分页查询所有映射记录，按更新时间倒序排列。"""
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(SQL_LIST, (limit, offset)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── pending_tasks CRUD ────────────────────────────────────────────────


def db_enqueue_task(task: QueueTask) -> int:
    """将任务写入 pending_tasks 表，返回自增 ID。"""
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(settings.db_path)
    conn.execute(
        SQL_INSERT_PENDING_TASK,
        (task.task_type.value, task.outline_doc_id, task.title, task.text, task.retry_count, now),
    )
    conn.commit()
    task_id = conn.execute("SELECT last_insert_rowid();").fetchone()[0]
    conn.close()
    return task_id


def db_dequeue_task(task_id: int):
    """从 pending_tasks 表中删除已处理完成的任务。"""
    conn = sqlite3.connect(settings.db_path)
    conn.execute(SQL_DELETE_PENDING_TASK, (task_id,))
    conn.commit()
    conn.close()


def db_list_pending_tasks() -> list[dict]:
    """查询所有未完成的任务，按入队顺序排列。"""
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(SQL_LIST_PENDING_TASKS).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════
#  LightRAG API 客户端
#  封装与 LightRAG HTTP API 的所有交互操作：
#    - 插入文本（异步，返回 track_id）
#    - 轮询处理状态（获取最终的 doc_id）
#    - 删除文档（含 pipeline busy 重试逻辑）
#
#  注意：LightRAG 的文档插入是异步处理的。
#  调用 /documents/text 后会立即返回一个 track_id，
#  实际的文档处理（分块、向量化、图谱构建）在后台进行，
#  需要通过 /documents/track_status/{track_id} 轮询完成状态。
# ═══════════════════════════════════════════════════════════════════════


class LightRAGClient:
    """封装对 LightRAG API 的所有 HTTP 调用。"""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def insert_text(self, text: str, file_source: str = "") -> str:
        """向 LightRAG 插入文本内容。

        参数:
            text: 文档的 Markdown 文本内容（来自 Outline 的 webhook payload）
            file_source: 可选的来源标识，这里设置为 "outline:{outline_doc_id}" 便于追踪

        返回:
            track_id: LightRAG 异步处理的追踪 ID

        工作流程:
            1. POST /documents/text 发送文本
            2. LightRAG 立即返回 track_id（此时文档可能还在排队处理中）
            3. 调用者需要使用 poll_doc_id() 等待处理完成并获取最终的 doc_id

        注意:
            - 这里设置了 60 秒的 HTTP 超时，防止网络问题导致请求卡死
            - file_source 参数在 LightRAG 中会记录为 file_path 字段，便于调试
        """
        payload = {"text": text}
        if file_source:
            payload["file_source"] = file_source

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{self.base_url}/documents/text", json=payload)
            resp.raise_for_status()
            data = resp.json()
            track_id = data.get("track_id")
            if not track_id:
                raise RuntimeError(f"LightRAG 插入返回数据中没有 track_id: {data}")
            logger.info("文本已插入 LightRAG，track_id=%s", track_id)
            return track_id

    async def poll_doc_id(self, track_id: str) -> str:
        """轮询 LightRAG 直到文档处理完成，获取最终的文档 ID。

        LightRAG 的文档处理流程：
            1. 刚插入时 → status="pending"（排队等待处理）
            2. 处理中 → status="processing"（正在分块和向量化）
            3. 完成 → status="processed"（可以正常查询和删除）
            4. 失败 → status="failed"（记录错误信息）

        参数:
            track_id: insert_text 返回的追踪 ID

        返回:
            doc_id: LightRAG 中的文档 ID（格式如 doc-xxx），用于后续的删除操作

        异常:
            RuntimeError: 文档处理失败（LLM API 限流、格式错误等）
            TimeoutError: 轮询超时（超过 poll_max_attempts 次仍未完成）
        """
        for attempt in range(1, settings.poll_max_attempts + 1):
            await asyncio.sleep(settings.poll_interval)
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{self.base_url}/documents/track_status/{track_id}"
                )
                if resp.status_code != 200:
                    logger.warning("track_status 查询返回 %s（track_id=%s）", resp.status_code, track_id)
                    continue
                data = resp.json()
                docs = data.get("documents", [])
                if not docs:
                    continue
                doc = docs[0]
                doc_status = doc.get("status", "").lower()
                doc_id = doc.get("id", "")

                if doc_status in ("processed", "failed"):
                    if doc_status == "failed":
                        error_msg = doc.get("error_msg", "未知错误")
                        logger.error("文档处理失败：%s", error_msg)
                        raise RuntimeError(f"LightRAG 处理失败：{error_msg}")
                    logger.info(
                        "文档处理完成，doc_id=%s（第 %d 次轮询）", doc_id, attempt
                    )
                    return doc_id

                logger.debug(
                    "文档状态: %s（第 %d/%d 次轮询）",
                    doc_status, attempt, settings.poll_max_attempts,
                )

        raise TimeoutError(
            f"等待文档处理超时（track_id={track_id}，"
            f"共轮询 {settings.poll_max_attempts} 次）"
        )

    async def delete_document(self, doc_id: str) -> bool:
        """从 LightRAG 中删除文档及其所有关联数据。

        当一个文档被删除时，LightRAG 会自动清理：
          - 该文档的所有文本切片（chunks）
          - 所有向量嵌入（embeddings）
          - 图谱中的实体和关系（如果存在）

        参数:
            doc_id: LightRAG 中的文档 ID（doc-xxx 格式）

        返回:
            bool: 是否成功删除

        重试逻辑:
            当 LightRAG 的 pipeline 正在处理其他文档时，删除请求会返回
            status="busy"。此时等待 delete_retry_delay 秒后重试，
            最多重试 delete_retry_attempts 次。
        """
        for attempt in range(1, settings.delete_retry_attempts + 1):
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.request(
                    "DELETE",
                    f"{self.base_url}/documents/delete_document",
                    json={"doc_ids": [doc_id]},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "busy":
                        logger.warning(
                            "LightRAG pipeline 正忙，删除重试 doc=%s（第 %d/%d 次）",
                            doc_id, attempt, settings.delete_retry_attempts,
                        )
                        await asyncio.sleep(settings.delete_retry_delay)
                        continue
                    logger.info("已从 LightRAG 删除文档 doc_id=%s", doc_id)
                    return True

                logger.error(
                    "文档删除失败 doc_id=%s：HTTP %s %s",
                    doc_id, resp.status_code, resp.text,
                )
                return False

        logger.error(
            "文档删除失败 doc_id=%s（已重试 %d 次）",
            doc_id, settings.delete_retry_attempts,
        )
        return False

    async def wait_pipeline_idle(self, timeout: int = 120, interval: int = 2) -> bool:
        """等待 LightRAG pipeline 变为空闲。

        轮询 GET /documents/pipeline_status 直到 busy=false 或超时。
        在 task_worker 派发每个任务前调用，确保不会在 pipeline 繁忙时发请求。

        参数:
            timeout: 最长等待时间（秒），默认 120
            interval: 轮询间隔（秒），默认 2

        返回:
            True: pipeline 空闲，可以发送操作请求
            False: 超时，pipeline 仍然繁忙
        """
        for _ in range(0, timeout, interval):
            async with httpx.AsyncClient(timeout=10) as client:
                try:
                    resp = await client.get(f"{self.base_url}/documents/pipeline_status")
                    if resp.status_code == 200:
                        data = resp.json()
                        if not data.get("busy", True):
                            return True
                        logger.debug("LightRAG pipeline 繁忙，等待中...")
                    else:
                        logger.warning("pipeline_status 返回 %s", resp.status_code)
                except Exception as e:
                    logger.warning("查询 pipeline_status 失败: %s", e)
            await asyncio.sleep(interval)

        logger.warning("LightRAG pipeline 在 %ds 内未变为空闲", timeout)
        return False


# 创建 LightRAG 客户端全局实例
lightrag = LightRAGClient(settings.lightrag_api_url)

# ═══════════════════════════════════════════════════════════════════════
#  任务队列 — 串行化所有 LightRAG 变更操作
#
#  所有 Webhook 触发的 create/update/delete 操作都通过此队列串行执行，
#  避免并发请求导致 LightRAG pipeline busy 冲突。
#
#  关键设计：
#    1. Webhook 处理函数只管把任务入队，立即返回 200
#    2. 单消费者 Worker 从队列取任务，逐一处理
#    3. 派发前先检查 pipeline 状态，确保空闲才发请求
#    4. 任务先写入 SQLite pending_tasks 表（崩溃恢复），处理完成后删除
#    5. 服务启动时从未完成任务重新入队
# ═══════════════════════════════════════════════════════════════════════

# 全局串行任务队列（FIFO）
task_queue: asyncio.Queue[QueueTask] = asyncio.Queue()


async def process_task(task: QueueTask) -> None:
    """处理单个队列任务，根据任务类型分发到对应的处理函数。

    注意：此函数在 task_worker 的串行上下文中调用，不会并发执行。
    """
    logger.info(
        "开始处理队列任务: %s outline_doc_id=%s (重试 %d/%d)",
        task.task_type.value, task.outline_doc_id, task.retry_count, task.max_retries,
    )
    try:
        if task.task_type == TaskType.CREATE:
            await process_create(task.outline_doc_id, task.title, task.text)
        elif task.task_type == TaskType.UPDATE:
            await process_update(task.outline_doc_id, task.title, task.text)
        elif task.task_type == TaskType.DELETE:
            await process_delete(task.outline_doc_id)
    except Exception as e:
        logger.error("任务处理异常 %s: %s", task.outline_doc_id, e)
        raise


async def task_worker() -> None:
    """后台任务工作者：单消费者，串行处理队列中的任务。

    每个任务的处理流程：
      1. 取任务 → 2. 等 pipeline 空闲 → 3. 处理 → 4. 删除持久化记录
    如果处理失败（异常），自动重试（最多 max_retries 次）。
    """
    logger.info("task_worker 启动")
    while True:
        task = await task_queue.get()
        try:
            # 第 1 步：等待 LightRAG pipeline 空闲
            logger.debug("等待 pipeline 空闲 (task=%s)", task.outline_doc_id)
            idle = await lightrag.wait_pipeline_idle()
            if not idle:
                logger.warning("pipeline 长时间繁忙，任务重试: %s", task.outline_doc_id)
                raise TimeoutError("LightRAG pipeline 超时未空闲")

            # 第 2 步：处理任务
            await process_task(task)

        except asyncio.CancelledError:
            # Worker 被取消（服务关闭），将任务重新入队
            logger.warning("task_worker 取消，任务重新入队: %s", task.outline_doc_id)
            await task_queue.put(task)
            break

        except Exception as e:
            # 第 3 步：失败重试
            if task.retry_count < task.max_retries:
                task.retry_count += 1
                task.last_error = str(e)
                task.db_id = db_enqueue_task(task)  # 持久化重试状态
                await task_queue.put(task)
                logger.warning(
                    "任务 %s 失败，重新入队（重试 %d/%d）: %s",
                    task.outline_doc_id, task.retry_count, task.max_retries, e,
                )
            else:
                logger.error(
                    "任务 %s 已达最大重试次数 %d，丢弃: %s",
                    task.outline_doc_id, task.max_retries, e,
                )

        else:
            logger.info("任务处理完成: %s %s", task.task_type.value, task.outline_doc_id)

        finally:
            # 任务处理完成（无论成功失败重试），清理持久化记录
            if task.db_id > 0:
                db_dequeue_task(task.db_id)
                task.db_id = 0
            task_queue.task_done()


async def recover_pending_tasks() -> int:
    """从 SQLite 加载未完成的任务，重新入队。

    在服务启动时调用。如果上次运行崩溃，pending_tasks 表中
    可能还有未处理或处理中的任务，全部重新入队。
    """
    pending = db_list_pending_tasks()
    if not pending:
        logger.info("没有需要恢复的待处理任务")
        return 0

    logger.info("发现 %d 个待恢复任务，准备重新入队...", len(pending))
    for pt in pending:
        qtask = QueueTask(
            task_type=TaskType[pt["task_type"].upper()],
            outline_doc_id=pt["outline_doc_id"],
            title=pt["title"],
            text=pt["text"],
            retry_count=pt["retry_count"],
            db_id=pt["id"],  # 记住 db_id，处理后清理
        )
        await task_queue.put(qtask)
        logger.info("恢复任务: %s %s (重试 %d)", pt["task_type"], pt["outline_doc_id"], pt["retry_count"])
    return len(pending)


# ═══════════════════════════════════════════════════════════════════════
#  Outline Webhook 签名验证
#
#  Outline 发送 Webhook 时，会在 HTTP Header 中添加:
#    outline-signature: <HMAC-SHA256 十六进制摘要>
#
#  验证方式：
#    1. 获取请求的原始 body（必须是原始字节，不能先解析 JSON）
#    2. 使用配置的 secret 计算 HMAC-SHA256(secret, raw_body)
#    3. 与 header 中的签名进行常量时间比较
#
#  为什么用常量时间比较（hmac.compare_digest）？
#    防止时序攻击（Timing Attack）。如果使用普通的 == 比较，
#    攻击者可以根据响应时间推断出签名内容。
# ═══════════════════════════════════════════════════════════════════════


def verify_outline_signature(raw_body: bytes, signature_header: str) -> bool:
    """验证 Outline Webhook 的 HMAC-SHA256 签名。

    参数:
        raw_body: HTTP 请求的原始 body（字节流，必须先读取再解析 JSON）
        signature_header: outline-signature Header 的值

    返回:
        True: 签名验证通过（或 secret 未配置时跳过验证）
        False: 签名不匹配
    """
    secret = settings.outline_webhook_secret
    if not secret:
        logger.warning("OUTLINE_WEBHOOK_SECRET 未配置，跳过签名验证（生产环境请务必配置！）")
        return True

    # 使用相同的 secret 和原始 body 计算期望的签名
    expected_sig = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    # 常量时间比较，防止时序攻击
    return hmac.compare_digest(expected_sig, signature_header)


# ═══════════════════════════════════════════════════════════════════════
#  Webhook 数据模型
#  用于解析和验证 Outline Webhook 的 JSON Payload
#
#  Outline Webhook Payload 结构：
#    {
#      "event": "documents.create",         ← 事件类型
#      "createdAt": "2026-06-22T16:00:00Z", ← 事件发生时间
#      "model": {                           ← 文档对象
#        "id": "doc-abc123",               ← Outline 文档 ID
#        "title": "文档标题",
#        "text": "Markdown 全文内容",       ← ← ← 这是我们要同步到 LightRAG 的文本
#        "content": {...},                  ← Prosemirror JSON 格式（暂不使用）
#        ...
#      }
#    }
# ═══════════════════════════════════════════════════════════════════════


class OutlineDocument(BaseModel):
    """Outline 文档对象模型。

    我们只关心三个字段：
      - id: 文档唯一标识，用于维护映射关系
      - title: 文档标题，存入映射表便于追踪
      - text: Markdown 格式的全文内容，这是要同步到 LightRAG 的核心数据
    """
    id: str
    title: str = ""
    text: str = ""


class OutlineDocumentPayload(BaseModel):
    """Outline Webhook payload 字段中的文档数据包装。"""
    id: str
    model: OutlineDocument


class OutlineWebhookPayload(BaseModel):
    """Outline Webhook 的顶层数据结构。

    实际的 Outline Webhook 格式：
      {
        "id": "事件唯一ID",
        "actorId": "操作者ID",
        "webhookSubscriptionId": "订阅ID",
        "createdAt": "ISO时间",
        "event": "documents.create",
        "payload": {
          "id": "文档ID",
          "model": { "id", "title", "text", ... }
        }
      }
    """
    event: str
    createdAt: str = ""
    payload: OutlineDocumentPayload


# ═══════════════════════════════════════════════════════════════════════
#  后台任务处理 — 核心业务逻辑
#
#  这些函数在 Webhook 接收后作为异步后台任务运行。
#  设计原则：
#    1. Webhook 处理必须快速返回（< 1 秒），耗时操作放后台
#    2. 及时保存状态到 SQLite，支持崩溃恢复
#    3. 所有异常都要捕获记录，不能静默失败
#    4. 删除操作尽可能可靠（自动重试）
#
#  各函数的处理流程：
#    process_create → insert → 存"processing" → poll → 更新为"ready"
#    process_update → 删旧文档 → insert → 存"processing" → poll → 更新映射
#    process_delete → 查映射 → 删 LightRAG 文档 → 删映射
# ═══════════════════════════════════════════════════════════════════════


async def process_create(outline_doc_id: str, title: str, text: str):
    """处理文档创建事件：将文档内容插入 LightRAG 并建立映射。

    流程说明：
      第 1 步：调用 LightRAG 插入 API，获取异步追踪 ID（track_id）
      第 2 步：立即将"processing"状态的映射写入 SQLite
              （这样即使服务崩溃，重启后也知道有任务在处理中）
      第 3 步：轮询 LightRAG 处理状态，直到文档处理完成
      第 4 步：更新映射为"ready"状态，存入完整的 lightrag_doc_id

    参数:
        outline_doc_id: Outline 文档 ID
        title: 文档标题（仅用于映射表记录）
        text: Markdown 全文内容（同步到 LightRAG 的核心数据）
    """
    logger.info("处理文档创建: outline_doc_id=%s title=%s", outline_doc_id, title)

    try:
        # 第 1 步：向 LightRAG 插入文本，获取追踪 ID
        track_id = await lightrag.insert_text(text, file_source=f"outline:{outline_doc_id}")

        # 第 2 步：立即写入数据库，防止崩溃后丢失状态
        db_upsert(outline_doc_id, "", title, "processing", track_id)

        # 第 3 步：轮询等待 LightRAG 处理完成
        lightrag_doc_id = await lightrag.poll_doc_id(track_id)

        # 第 4 步：更新映射状态为"ready"
        db_upsert(outline_doc_id, lightrag_doc_id, title, "ready", track_id)
        logger.info(
            "文档创建同步完成: outline=%s lightrag=%s", outline_doc_id, lightrag_doc_id
        )
    except RuntimeError as e:
        # LightRAG 内部处理失败（如 LLM 限流），标记 failed，不重试
        logger.error("文档创建失败（不重试）outline_doc_id=%s: %s", outline_doc_id, e)
        db_upsert(outline_doc_id, "", title, "failed")
    except Exception as e:
        # 网络错误等可重试异常：标记 failed 后抛出，让 task_worker 重试
        logger.error("文档创建异常（可重试）outline_doc_id=%s: %s", outline_doc_id, e)
        db_upsert(outline_doc_id, "", title, "failed")
        raise


async def process_update(outline_doc_id: str, title: str, new_text: str):
    """处理文档更新事件：先删除旧文档，再插入新内容。

    为什么需要先删后插？
      LightRAG 没有直接"更新"文档的 API。当文档内容改变时，
      旧的切片和向量已经过时，必须：
        1. 删除旧的完整文档（连带所有切片和向量）
        2. 将新内容作为新文档插入
      LightRAG 的 doc_id 在更新后会改变，映射表也需要同步更新。

    原子性保证（串行队列环境下）：
      - 删除旧文档失败 → 抛出异常，task_worker 重试整个操作
      - 插入新文档失败 → 标记 failed，抛出异常，重试
      - 只有新旧文档都成功切换后，才更新映射记录
      由于 task_worker 串行执行，不会出现并发冲突。

    参数:
        outline_doc_id: Outline 文档 ID
        title: 更新后的文档标题
        new_text: 更新后的完整 Markdown 内容
    """
    logger.info("处理文档更新: outline_doc_id=%s title=%s", outline_doc_id, title)

    # 查询旧的映射关系，获取旧的 LightRAG 文档 ID
    mapping = db_find_by_outline_id(outline_doc_id)
    old_lightrag_id = mapping["lightrag_doc_id"] if mapping else None

    # 第 1 步：删除旧文档
    # 如果删除失败，异常直接抛出，task_worker 会重试整个 update
    if old_lightrag_id:
        logger.info("正在删除旧文档: %s", old_lightrag_id)
        success = await lightrag.delete_document(old_lightrag_id)
        if not success:
            raise RuntimeError(f"旧文档删除失败 {old_lightrag_id}，将重试整个更新操作")

    # 第 2 步：插入新文档内容
    # 如果插入或轮询失败，标记映射为 failed 并抛出异常
    try:
        track_id = await lightrag.insert_text(new_text, file_source=f"outline:{outline_doc_id}")
        db_upsert(outline_doc_id, "", title, "processing", track_id)
        new_lightrag_id = await lightrag.poll_doc_id(track_id)
        db_upsert(outline_doc_id, new_lightrag_id, title, "ready", track_id)
        logger.info(
            "文档更新同步完成: outline=%s lightrag=%s", outline_doc_id, new_lightrag_id
        )
    except Exception as e:
        logger.error("新文档插入失败 outline_doc_id=%s: %s", outline_doc_id, e)
        db_upsert(outline_doc_id, "", title, "failed")
        raise


async def process_delete(outline_doc_id: str):
    """处理文档删除事件：从 LightRAG 中删除文档并清理映射。

    删除操作的注意事项：
      - LightRAG 删除 API 会同时清理：文档切片、向量嵌入、图谱实体和关系
      - 如果找不到映射记录，说明该文档之前同步失败或未曾同步，直接跳过
      - 只有 LightRAG 确认删除成功后，才清理 SQLite 映射
      - 删除失败时抛出异常，由 task_worker 的重试机制处理

    参数:
        outline_doc_id: 被删除的 Outline 文档 ID
    """
    logger.info("处理文档删除: outline_doc_id=%s", outline_doc_id)

    # 查询映射关系
    mapping = db_find_by_outline_id(outline_doc_id)
    if not mapping:
        logger.warning("未找到映射记录，跳过删除: outline_doc_id=%s", outline_doc_id)
        return

    lightrag_doc_id = mapping["lightrag_doc_id"]
    if not lightrag_doc_id:
        # 文档从未成功同步到 LightRAG，只需清理映射，不需要调 LightRAG API
        logger.warning("LightRAG doc_id 为空，仅清理映射: outline_doc_id=%s", outline_doc_id)
        db_delete(outline_doc_id)
        return

    # 调用 LightRAG 删除 API
    # 如果删除失败，抛出异常让 task_worker 重试
    # 如果删除成功，清理 SQLite 映射
    success = await lightrag.delete_document(lightrag_doc_id)
    if success:
        db_delete(outline_doc_id)
        logger.info("文档删除处理完成: outline=%s lightrag=%s", outline_doc_id, lightrag_doc_id)
    else:
        raise RuntimeError(
            f"LightRAG 删除失败 doc_id={lightrag_doc_id}，"
            f"映射将保留以支持重试"
        )


# ═══════════════════════════════════════════════════════════════════════
#  FastAPI 应用
#
#  暴露两个 HTTP 端点：
#    1. GET  /health    — 健康检查（供 Docker 和监控使用）
#    2. GET  /mappings  — 查看文档映射关系（调试用）
#    3. POST /webhook   — 接收 Outline Webhook（核心入口）
#
#  应用生命周期：
#    启动时 → 初始化 SQLite 数据库
#    运行时 → 监听 Webhook，启动后台任务
#    关闭时 → 清理资源
# ═══════════════════════════════════════════════════════════════════════


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理。

    startup: 初始化数据库、启动后台 Worker、恢复未完成任务
    shutdown: 取消后台 Worker，等待当前任务完成
    """
    init_db()

    # 启动后台任务 Worker
    worker = asyncio.create_task(task_worker(), name="task-worker")

    # 恢复崩溃前未完成的任务
    recovered = await recover_pending_tasks()

    logger.info(
        "OutlineRAGBridge 启动完成: %s:%s (恢复 %d 个待处理任务)",
        settings.bridge_host, settings.bridge_port, recovered,
    )
    yield

    logger.info("OutlineRAGBridge 正在关闭，等待当前任务完成...")
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass
    logger.info("OutlineRAGBridge 已关闭")


app = FastAPI(
    title="OutlineRAGBridge",
    description="Outline → LightRAG 文档同步桥接服务。"
                "监听 Outline Webhook，自动同步文档变更到 LightRAG 知识库。",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    """健康检查端点。

    返回桥接服务自身的运行状态，以及连接的 LightRAG API 地址。
    可用于 Docker 的健康检查配置或 Prometheus 监控。
    """
    return {
        "status": "ok",
        "lightrag_api": settings.lightrag_api_url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/mappings")
async def list_mappings(limit: int = 50, offset: int = 0):
    """查看文档 ID 映射关系（调试/管理接口）。

    返回 Outline 文档 ID 到 LightRAG 文档 ID 的映射列表。
    可通过 limit 和 offset 参数分页查询。
    """
    return {"mappings": db_list(limit, offset)}

# ── 事件路由表 ──────────────────────────────────────────────────────

# Webhook 事件到处理函数的映射规则：
#
#   documents.create     → process_create  插入新文档到 LightRAG
#   documents.update     → process_update  删除旧版，插入新版
#   documents.delete     → process_delete  从 LightRAG 删除
#   documents.publish    → process_create  发布 ≈ 创建（如果之前是草稿）
#   documents.restore    → process_create  从回收站恢复 ≈ 重新创建
#   documents.unarchive  → process_create  取消归档 ≈ 重新创建
#   其他事件             → 忽略            移动、标星等不影响内容的事件
#
# 为什么用 asyncio.create_task？
#   Webhook 需要快速返回 200 给 Outline（否则 Outline 会重试）。
#   实际的 LightRAG 操作（特别是轮询处理状态）可能耗时 10-60 秒，
#   必须放到后台任务中异步执行。


@app.post("/webhook")
async def handle_webhook(request: Request):
    """接收 Outline Webhook 的主入口。

    处理步骤：
      1. 读取原始请求 body（验证签名前不能解析 JSON）
      2. 验证 HMAC-SHA256 签名
      3. 解析 JSON Payload
      4. 根据事件类型分发到对应的后台处理函数
      5. 立即返回 200（实际处理在后台进行）

    注意：
      - 签名验证使用原始 body 字节流，JSON 解析在验证之后
      - 删除事件不需要检查 text 字段（可能为空，但我们只需要 ID）
      - 所有耗时操作通过 asyncio.create_task 异步执行
    """
    # 第 1 步：读取原始请求体（保持字节流，用于签名验证）
    raw_body = await request.body()

    # 第 2 步：验证 Webhook 签名
    sig_header = request.headers.get("outline-signature", "")
    if not verify_outline_signature(raw_body, sig_header):
        raise HTTPException(status_code=401, detail="Webhook 签名验证失败")

    # 第 3 步：解析 JSON Payload
    try:
        payload = OutlineWebhookPayload.model_validate_json(raw_body)
    except Exception as e:
        logger.error("Webhook Payload 解析失败: %s", e)
        raise HTTPException(status_code=400, detail=f"无效的请求数据: {e}")

    event = payload.event
    doc = payload.payload.model
    logger.info("收到 Webhook: event=%s doc_id=%s title=%s", event, doc.id, doc.title)

    # 第 4 步：将任务加入队列（异步执行，Webhook 立即返回 200）
    # 所有耗时操作通过任务队列串行执行，避免并发请求导致 LightRAG pipeline busy
    task: Optional[QueueTask] = None

    if event == "documents.create":
        if not doc.text:
            logger.warning("跳过创建事件 %s：内容为空", doc.id)
            return {"status": "skipped", "reason": "empty text"}
        task = QueueTask(TaskType.CREATE, doc.id, doc.title, doc.text)

    elif event == "documents.update":
        if not doc.text:
            logger.warning("跳过更新事件 %s：内容为空", doc.id)
            return {"status": "skipped", "reason": "empty text"}
        task = QueueTask(TaskType.UPDATE, doc.id, doc.title, doc.text)

    elif event == "documents.delete":
        task = QueueTask(TaskType.DELETE, doc.id)

    elif event in ("documents.publish", "documents.restore", "documents.unarchive"):
        if doc.text:
            task = QueueTask(TaskType.CREATE, doc.id, doc.title, doc.text)
        else:
            logger.warning("跳过 %s 事件 %s：内容为空", event, doc.id)
            return {"status": "skipped", "reason": "empty text"}

    else:
        # 其他事件（move, duplicate, star 等）不影响文档内容，忽略
        logger.info("忽略未处理的事件类型: %s", event)
        return {"status": "ignored", "event": event}

    # 任务持久化 + 入队
    task.db_id = db_enqueue_task(task)   # 先写 SQLite 防崩溃
    await task_queue.put(task)           # 再入内存队列
    logger.info("任务已入队: %s %s qsize=%d", event, doc.id, task_queue.qsize())

    # 第 5 步：快速返回 200
    return {"status": "accepted", "event": event, "doc_id": doc.id}


# ═══════════════════════════════════════════════════════════════════════
#  启动入口
#
#  两种启动方式：
#    1. python bridge.py          → 直接启动（使用 .env 中的配置）
#    2. uvicorn bridge:app        → 通过 uvicorn 启动（可附加更多参数）
#    3. docker run ...            → 通过 Docker 启动（见 Dockerfile）
#
#  生产环境建议：
#    - 配置 OUTLINE_WEBHOOK_SECRET 环境变量
#    - 使用 systemd / supervisor 管理进程
#    - 配合 nginx 反向代理（如果需要 HTTPS）
# ═══════════════════════════════════════════════════════════════════════


def main():
    """应用入口函数。使用 uvicorn 启动 FastAPI 服务。"""
    import uvicorn

    uvicorn.run(
        "bridge:app",
        host=settings.bridge_host,
        port=settings.bridge_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
