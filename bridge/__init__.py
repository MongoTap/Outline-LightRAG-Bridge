"""Outline ↔ LightRAG 桥接服务

监听 Outline 的 Webhook 事件（文档创建/更新/删除），
自动同步文档内容到 LightRAG 知识库。

依赖的外部服务：
  - LightRAG Docker (API: http://localhost:9621)
  - Outline Docker (通过 Webhook 推送事件)
  - SQLite (本地存储文档 ID 映射关系)
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request

from bridge.auth import verify_outline_signature
from bridge.config import settings, logger
from bridge.database import init_db, db_list
from bridge.models import OutlineWebhookPayload, QueueTask, TaskType
from bridge.tasks import (
    coalesce_pending,
    get_queue_status,
    recover_pending_tasks,
    task_worker,
    wake_worker,
)


# ═══════════════════════════════════════════════════════════════════════
#  应用生命周期
# ═══════════════════════════════════════════════════════════════════════


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理。

    startup: 初始化数据库、启动后台 Worker、恢复未完成任务
    shutdown: 取消后台 Worker
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


# ═══════════════════════════════════════════════════════════════════════
#  FastAPI 应用
# ═══════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="OutlineRAGBridge",
    description="Outline → LightRAG 文档同步桥接服务。"
                "监听 Outline Webhook，自动同步文档变更到 LightRAG 知识库。",
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    """健康检查端点。"""
    return {
        "status": "ok",
        "lightrag_api": settings.lightrag_api_url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/mappings")
async def list_mappings(limit: int = 50, offset: int = 0):
    """查看文档 ID 映射关系（调试/管理接口）。"""
    return {"mappings": db_list(limit, offset)}


@app.get("/queue")
async def queue_status():
    """查看待处理任务数与定时窗口状态（运维/调试）。"""
    return get_queue_status()


# ═══════════════════════════════════════════════════════════════════════
#  Webhook 接收端点
# ═══════════════════════════════════════════════════════════════════════

# 事件路由说明：
#   documents.create     → CREATE  插入新文档到 LightRAG
#   documents.update     → UPDATE  删除旧版，插入新版
#   documents.delete     → DELETE  从 LightRAG 删除
#   documents.publish    → CREATE  发布 ≈ 创建
#   documents.restore    → CREATE  恢复 ≈ 重新创建
#   documents.unarchive  → CREATE  取消归档 ≈ 重新创建
#   其他事件             → 忽略   移动、标星等不影响内容的事件


@app.post("/webhook")
async def handle_webhook(request: Request):
    """接收 Outline Webhook 的主入口。

    所有耗时操作通过任务队列串行执行，Webhook 立即返回 200。
    """
    # 第 1 步：读取原始请求体
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

    # 第 4 步：将任务加入队列
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
        logger.info("忽略未处理的事件类型: %s", event)
        return {"status": "ignored", "event": event}

    # 任务持久化 + 入队（按文档合并冗余任务）
    db_id = coalesce_pending(task)
    if db_id is not None:
        wake_worker()
        logger.info("任务已入队: %s %s", event, doc.id)
    else:
        logger.info("任务已合并丢弃（无需执行）: %s %s", event, doc.id)

    return {"status": "accepted", "event": event, "doc_id": doc.id}
