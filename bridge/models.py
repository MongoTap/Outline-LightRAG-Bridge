"""数据模型定义

包含任务队列数据结构（TaskType, QueueTask）和 Outline Webhook 的 Pydantic 模型。
"""

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel


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


class OutlineDocument(BaseModel):
    """Outline 文档对象模型。"""
    id: str
    title: str = ""
    text: str = ""


class OutlineDocumentPayload(BaseModel):
    """Outline Webhook payload 字段中的文档数据包装。"""
    id: str
    model: OutlineDocument


class OutlineWebhookPayload(BaseModel):
    """Outline Webhook 的顶层数据结构。"""
    event: str
    createdAt: str = ""
    payload: OutlineDocumentPayload
