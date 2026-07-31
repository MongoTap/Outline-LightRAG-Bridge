"""SQLite 数据库操作

维护两个表：
  - document_mappings: Outline 文档 ID 到 LightRAG 文档 ID 的映射
  - pending_tasks: 任务队列的持久化存储（支持崩溃恢复）
"""

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from bridge.config import settings, logger
from bridge.models import QueueTask

# ═══════════════════════════════════════════════════════════════════════
#  document_mappings 表 — 文档 ID 映射关系
# ═══════════════════════════════════════════════════════════════════════

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

SQL_ADD_TRACK_ID = """
ALTER TABLE document_mappings ADD COLUMN track_id TEXT DEFAULT '';
"""
SQL_ADD_TRACK_ID_CHECK = """
SELECT COUNT(*) AS cnt FROM pragma_table_info('document_mappings') WHERE name='track_id';
"""

SQL_FIND_BY_OUTLINE_ID = "SELECT * FROM document_mappings WHERE outline_doc_id = ?;"
SQL_DELETE = "DELETE FROM document_mappings WHERE outline_doc_id = ?;"
SQL_LIST = "SELECT * FROM document_mappings ORDER BY updated_at DESC LIMIT ? OFFSET ?;"

# ═══════════════════════════════════════════════════════════════════════
#  pending_tasks 表 — 任务队列持久化（崩溃恢复）
# ═══════════════════════════════════════════════════════════════════════

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
SQL_LIST_PENDING_BY_DOC = "SELECT * FROM pending_tasks WHERE outline_doc_id = ? ORDER BY id ASC;"
SQL_DELETE_PENDING_BY_DOC = "DELETE FROM pending_tasks WHERE outline_doc_id = ?;"
SQL_DELETE_PENDING_BY_TYPES = (
    "DELETE FROM pending_tasks WHERE outline_doc_id = ? AND task_type IN ({placeholders});"
)
SQL_PENDING_ROW_EXISTS = "SELECT COUNT(*) AS cnt FROM pending_tasks WHERE id = ?;"


# ═══════════════════════════════════════════════════════════════════════
#  初始化与迁移
# ═══════════════════════════════════════════════════════════════════════


def init_db():
    """初始化数据库：创建表结构并执行必要的迁移。"""
    conn = sqlite3.connect(settings.db_path)
    conn.execute(SQL_CREATE_TABLE)
    conn.execute(SQL_CREATE_PENDING_TASKS)
    # 检查是否需要迁移（新增 track_id 列）
    row = conn.execute(SQL_ADD_TRACK_ID_CHECK).fetchone()
    if row[0] == 0:
        conn.execute(SQL_ADD_TRACK_ID)
        logger.info("数据库迁移：已添加 track_id 列")
    conn.commit()
    conn.close()
    logger.info("数据库初始化完成：%s", settings.db_path)


# ═══════════════════════════════════════════════════════════════════════
#  document_mappings CRUD
# ═══════════════════════════════════════════════════════════════════════


def db_upsert(outline_doc_id: str, lightrag_doc_id: str, outline_title: str, status: str, track_id: str = ""):
    """创建或更新文档映射记录。"""
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(settings.db_path)
    conn.execute(SQL_UPSERT, (outline_doc_id, lightrag_doc_id, track_id, outline_title, status, now, now))
    conn.commit()
    conn.close()


def db_find_by_outline_id(outline_doc_id: str) -> Optional[dict]:
    """根据 Outline 文档 ID 查询映射记录。"""
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(SQL_FIND_BY_OUTLINE_ID, (outline_doc_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def db_delete(outline_doc_id: str):
    """删除文档映射记录。"""
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


# ═══════════════════════════════════════════════════════════════════════
#  pending_tasks CRUD
# ═══════════════════════════════════════════════════════════════════════


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


def db_list_pending_by_doc(outline_doc_id: str) -> list[dict]:
    """查询指定文档的所有待处理任务（供同文档合并使用）。"""
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(SQL_LIST_PENDING_BY_DOC, (outline_doc_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def db_delete_pending_by_doc(outline_doc_id: str):
    """删除指定文档的全部待处理任务。"""
    conn = sqlite3.connect(settings.db_path)
    conn.execute(SQL_DELETE_PENDING_BY_DOC, (outline_doc_id,))
    conn.commit()
    conn.close()


def db_delete_pending_by_types(outline_doc_id: str, task_types: list[str]):
    """删除指定文档中指定类型（TaskType.value 字符串）的待处理任务。"""
    placeholders = ",".join("?" for _ in task_types)
    conn = sqlite3.connect(settings.db_path)
    conn.execute(
        SQL_DELETE_PENDING_BY_TYPES.format(placeholders=placeholders),
        (outline_doc_id, *task_types),
    )
    conn.commit()
    conn.close()


def db_pending_row_exists(task_id: int) -> bool:
    """判断指定行 ID 是否仍存在于 pending_tasks（用于重试前校验是否已被合并取代）。"""
    conn = sqlite3.connect(settings.db_path)
    row = conn.execute(SQL_PENDING_ROW_EXISTS, (task_id,)).fetchone()
    conn.close()
    return row[0] > 0
