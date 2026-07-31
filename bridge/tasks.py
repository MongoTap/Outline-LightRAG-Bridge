"""后台任务处理 — 核心业务逻辑 + 任务队列

包含三部分：
  1. process_create / process_update / process_delete — 实际的 LightRAG 操作
  2. 定时窗口调度 + 任务合并（Coalescing）+ DB 驱动的串行任务队列
  3. 启动恢复与队列状态

队列模型：以 SQLite pending_tasks 表为唯一事实来源。
  - Webhook 入队 → 按文档合并后写入 DB（每文档至多 1 行）+ 触发唤醒事件
  - Worker → 从 DB 取最早待处理行 → 处理 → 删除该行
  这样窗口外队列不占用内存、崩溃恢复天然生效、合并无需处理内存中过期任务。
"""

import asyncio
from datetime import datetime, timedelta
from typing import Optional

from bridge.config import settings, logger
from bridge.database import (
    db_delete,
    db_dequeue_task,
    db_enqueue_task,
    db_find_by_outline_id,
    db_list_pending_by_doc,
    db_list_pending_tasks,
    db_delete_pending_by_doc,
    db_delete_pending_by_types,
    db_pending_row_exists,
    db_upsert,
)
from bridge.lightrag_client import lightrag
from bridge.models import QueueTask, TaskType

# ═══════════════════════════════════════════════════════════════════════
#  队列唤醒事件
# ═══════════════════════════════════════════════════════════════════════

_wake = asyncio.Event()


def wake_worker():
    """唤醒等待中的 Worker（入队/重试入队后调用）。"""
    _wake.set()


# ═══════════════════════════════════════════════════════════════════════
#  任务合并（Coalescing）— 按文档去重
# ═══════════════════════════════════════════════════════════════════════


def coalesce_pending(task: QueueTask) -> Optional[int]:
    """按文档合并待处理任务。

    规则（同一 outline_doc_id）：
      - CREATE/UPDATE + 已有 CREATE/UPDATE → 只留最新内容
        （已同步到 LightRAG → 转 UPDATE 先删旧再插新，避免重复；未同步 → CREATE）
      - CREATE/UPDATE + 已有 DELETE        → 删掉 DELETE，转 UPDATE
      - DELETE + 已有 CREATE/UPDATE        → 已同步 → 只留 DELETE；从未同步 → 全部丢弃
      - DELETE + 已有 DELETE               → 只留一个 DELETE

    返回插入的 DB 行 ID；返回 None 表示无需执行任何操作（任务被丢弃）。
    """
    doc_id = task.outline_doc_id
    mapping = db_find_by_outline_id(doc_id)
    had_sync = bool(mapping and mapping["lightrag_doc_id"])
    pending = db_list_pending_by_doc(doc_id)
    pending_types = {p["task_type"] for p in pending}

    if task.task_type == TaskType.DELETE:
        # 文档从未同步到 LightRAG → 丢弃该文档全部待处理任务，无需执行
        if not had_sync:
            if pending:
                db_delete_pending_by_doc(doc_id)
            return None
        # 删除旧的内容任务（CREATE/UPDATE 被删除事件取代）
        if pending_types - {TaskType.DELETE.value}:
            db_delete_pending_by_types(doc_id, [TaskType.CREATE.value, TaskType.UPDATE.value])
        # 已有 DELETE 待处理，不重复入队
        if TaskType.DELETE.value in pending_types:
            return None
        return db_enqueue_task(task)

    # CREATE / UPDATE：最新内容胜出，清掉该文档全部旧待处理任务
    if pending:
        db_delete_pending_by_doc(doc_id)
    # 已同步 → 用 UPDATE（先删旧 LightRAG 文档再插新，避免重复）；未同步 → CREATE
    final_type = TaskType.UPDATE if had_sync else TaskType.CREATE
    return db_enqueue_task(QueueTask(final_type, doc_id, task.title, task.text))


def coalesce_all_pending() -> int:
    """启动时清理历史冗余待处理任务（含旧版重试泄漏行）。

    对每个文档，只保留按合并规则计算出的最终状态。返回删除的行数。
    """
    rows = db_list_pending_tasks()
    docs: dict[str, list[dict]] = {}
    for r in rows:
        docs.setdefault(r["outline_doc_id"], []).append(r)

    removed = 0
    for doc_id, doc_rows in docs.items():
        if len(doc_rows) <= 1:
            continue
        last = doc_rows[-1]  # 最高 id = 最新事件
        virtual = QueueTask(
            task_type=TaskType[last["task_type"].upper()],
            outline_doc_id=doc_id,
            title=last["title"],
            text=last["text"],
            retry_count=last["retry_count"],
        )
        for r in doc_rows:
            db_dequeue_task(r["id"])
            removed += 1
        coalesce_pending(virtual)  # 重新决定最终状态（返回 None = 丢弃）
    if removed:
        logger.info("启动合并清理：删除 %d 条冗余待处理任务", removed)
    return removed


# ═══════════════════════════════════════════════════════════════════════
#  定时窗口计算（支持跨午夜，如 23:00 开始、时长 240min → 次日 03:00 结束）
# ═══════════════════════════════════════════════════════════════════════


def _window_bounds(now: datetime):
    """返回 (start_yesterday, end_yesterday, start_today)。"""
    sh, sm = map(int, settings.task_schedule_start.split(":"))
    start_today = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    start_yesterday = start_today - timedelta(days=1)
    return start_yesterday, start_yesterday + timedelta(minutes=settings.task_schedule_duration_minutes), start_today


def _seconds_until_window_open(now: Optional[datetime] = None) -> float:
    """距离下一次窗口打开还有多少秒；当前在窗口内则返回 0。"""
    now = now or datetime.now()
    _, end_yesterday, start_today = _window_bounds(now)
    if now < end_yesterday:  # 昨夜跨午夜的窗口仍在开放
        return 0.0
    if now < start_today:
        return (start_today - now).total_seconds()
    if now < start_today + timedelta(minutes=settings.task_schedule_duration_minutes):
        return 0.0
    return (start_today + timedelta(days=1) - now).total_seconds()


def _window_remaining_seconds(now: Optional[datetime] = None) -> float:
    """当前窗口还剩多少秒；窗口未开放或已结束返回 0。"""
    now = now or datetime.now()
    _, end_yesterday, start_today = _window_bounds(now)
    if now < end_yesterday:
        end = end_yesterday
    else:
        end = start_today + timedelta(minutes=settings.task_schedule_duration_minutes)
    return max(0.0, (end - now).total_seconds())


# ═══════════════════════════════════════════════════════════════════════
#  任务队列 — 串行化所有 LightRAG 变更操作
# ═══════════════════════════════════════════════════════════════════════


async def process_task(task: QueueTask) -> None:
    """处理单个队列任务，根据任务类型分发到对应的处理函数。"""
    logger.info(
        "开始处理队列任务: %s outline_doc_id=%s (重试 %d/%d)",
        task.task_type.value, task.outline_doc_id, task.retry_count, task.max_retries,
    )
    if task.task_type == TaskType.CREATE:
        await process_create(task.outline_doc_id, task.title, task.text)
    elif task.task_type == TaskType.UPDATE:
        await process_update(task.outline_doc_id, task.title, task.text)
    elif task.task_type == TaskType.DELETE:
        await process_delete(task.outline_doc_id)


def _next_pending_task() -> Optional[QueueTask]:
    """从 DB 取最早待处理任务（FIFO by id）。"""
    rows = db_list_pending_tasks()
    if not rows:
        return None
    r = rows[0]
    return QueueTask(
        task_type=TaskType[r["task_type"].upper()],
        outline_doc_id=r["outline_doc_id"],
        title=r["title"],
        text=r["text"],
        retry_count=r["retry_count"],
        db_id=r["id"],
    )


async def task_worker() -> None:
    """后台任务工作者：单消费者，串行处理 DB 中的待处理任务。"""
    logger.info("task_worker 启动")
    while True:
        # ── 第 1 步：定时窗口闸门 ──────────────────────────────────
        if settings.task_schedule_enabled:
            delay = _seconds_until_window_open()
            if delay > 0:
                logger.info("定时窗口未开启，%.1f 分钟后开始处理", delay / 60)
                await asyncio.sleep(delay)

        # ── 第 2 步：取下一个待处理任务 ───────────────────────────
        task = _next_pending_task()
        if task is None:
            _wake.clear()
            # 双检：防止 clear 与 wait 之间新任务到达而被漏掉
            if _next_pending_task() is not None:
                continue
            if settings.task_schedule_enabled:
                remaining = _window_remaining_seconds()
                if remaining > 0:
                    try:
                        await asyncio.wait_for(_wake.wait(), timeout=remaining)
                    except asyncio.TimeoutError:
                        pass  # 窗口关闭 → 回到循环重新闸门
            else:
                await _wake.wait()
            continue

        # ── 第 3 步：处理任务（含重试） ───────────────────────────
        await _process_with_retry(task)


async def _process_with_retry(task: QueueTask) -> None:
    """处理单个任务：等待 pipeline 空闲 → 处理 → 成功/失败清理。"""
    try:
        # 等待 LightRAG pipeline 空闲
        logger.debug("等待 pipeline 空闲 (task=%s)", task.outline_doc_id)
        idle = await lightrag.wait_pipeline_idle()
        if not idle:
            logger.warning("pipeline 长时间繁忙，任务重试: %s", task.outline_doc_id)
            raise TimeoutError("LightRAG pipeline 超时未空闲")

        # 处理任务
        await process_task(task)

    except asyncio.CancelledError:
        # Worker 被取消（服务关闭）：任务行保留在 DB，下次启动自动恢复
        raise

    except Exception as e:
        # 失败重试
        if task.retry_count < task.max_retries:
            task.retry_count += 1
            task.last_error = str(e)
            # 行已被同文档新事件合并取代 → 跳过本次重试（最新事件会覆盖该文档状态）
            if task.db_id and not db_pending_row_exists(task.db_id):
                logger.info("任务 %s 已被更新的同文档事件取代，跳过重试", task.outline_doc_id)
            else:
                db_dequeue_task(task.db_id)  # 修泄漏：删旧行
                task.db_id = db_enqueue_task(task)  # 写新行
                _wake.set()  # 防止 worker 在等待中被漏掉
                logger.warning(
                    "任务 %s 失败，重新入队（重试 %d/%d）: %s",
                    task.outline_doc_id, task.retry_count, task.max_retries, e,
                )
        else:
            logger.error(
                "任务 %s 已达最大重试次数 %d，丢弃: %s",
                task.outline_doc_id, task.max_retries, e,
            )
            if task.db_id:
                db_dequeue_task(task.db_id)

    else:
        # 成功
        logger.info("任务处理完成: %s %s", task.task_type.value, task.outline_doc_id)
        if task.db_id:
            db_dequeue_task(task.db_id)


async def recover_pending_tasks() -> int:
    """启动恢复：清理历史冗余任务，返回剩余待处理任务数。

    在服务启动时调用。由于 Worker 直接以 DB 为队列，剩余任务会自然被处理。
    """
    removed = coalesce_all_pending()
    pending = db_list_pending_tasks()
    logger.info("启动恢复：合并清理 %d 条冗余，剩余 %d 个待处理任务", removed, len(pending))
    return len(pending)


def get_queue_status() -> dict:
    """队列与定时窗口状态（运维/调试）。"""
    info = {
        "pending_count": len(db_list_pending_tasks()),
        "schedule_enabled": settings.task_schedule_enabled,
        "schedule_start": settings.task_schedule_start,
        "schedule_duration_minutes": settings.task_schedule_duration_minutes,
    }
    if settings.task_schedule_enabled:
        delay = _seconds_until_window_open()
        info["window_state"] = "open" if delay <= 0 else "closed"
        if delay > 0:
            info["next_window_at"] = (datetime.now() + timedelta(seconds=delay)).isoformat()
    return info


# ═══════════════════════════════════════════════════════════════════════
#  process_create — 文档创建同步
# ═══════════════════════════════════════════════════════════════════════


async def process_create(outline_doc_id: str, title: str, text: str):
    """处理文档创建事件：将文档内容插入 LightRAG 并建立映射。"""
    logger.info("处理文档创建: outline_doc_id=%s title=%s", outline_doc_id, title)

    try:
        track_id = await lightrag.insert_text(text, file_source=f"outline:{outline_doc_id}")
        db_upsert(outline_doc_id, "", title, "processing", track_id)
        lightrag_doc_id = await lightrag.poll_doc_id(track_id)
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


# ═══════════════════════════════════════════════════════════════════════
#  process_update — 文档更新同步
# ═══════════════════════════════════════════════════════════════════════


async def process_update(outline_doc_id: str, title: str, new_text: str):
    """处理文档更新事件：先删除旧文档，再插入新内容。"""
    logger.info("处理文档更新: outline_doc_id=%s title=%s", outline_doc_id, title)

    # 查询旧的映射关系
    mapping = db_find_by_outline_id(outline_doc_id)
    old_lightrag_id = mapping["lightrag_doc_id"] if mapping else None

    # 第 1 步：删除旧文档
    if old_lightrag_id:
        logger.info("正在删除旧文档: %s", old_lightrag_id)
        success = await lightrag.delete_document(old_lightrag_id)
        if not success:
            raise RuntimeError(f"旧文档删除失败 {old_lightrag_id}，将重试整个更新操作")

    # 第 2 步：插入新文档
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


# ═══════════════════════════════════════════════════════════════════════
#  process_delete — 文档删除同步
# ═══════════════════════════════════════════════════════════════════════


async def process_delete(outline_doc_id: str):
    """处理文档删除事件：从 LightRAG 中删除文档并清理映射。"""
    logger.info("处理文档删除: outline_doc_id=%s", outline_doc_id)

    mapping = db_find_by_outline_id(outline_doc_id)
    if not mapping:
        logger.warning("未找到映射记录，跳过删除: outline_doc_id=%s", outline_doc_id)
        return

    lightrag_doc_id = mapping["lightrag_doc_id"]
    if not lightrag_doc_id:
        logger.warning("LightRAG doc_id 为空，仅清理映射: outline_doc_id=%s", outline_doc_id)
        db_delete(outline_doc_id)
        return

    # 如果删除失败，抛出异常让 task_worker 重试
    success = await lightrag.delete_document(lightrag_doc_id)
    if success:
        db_delete(outline_doc_id)
        logger.info("文档删除处理完成: outline=%s lightrag=%s", outline_doc_id, lightrag_doc_id)
    else:
        raise RuntimeError(
            f"LightRAG 删除失败 doc_id={lightrag_doc_id}，映射将保留以支持重试"
        )
