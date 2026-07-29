"""后台任务处理 — 核心业务逻辑 + 任务队列

包含三部分：
  1. process_create / process_update / process_delete — 实际的 LightRAG 操作
  2. task_queue / task_worker / process_task — 串行任务队列基础设施
  3. recover_pending_tasks — 崩溃恢复
"""

import asyncio

from bridge.config import settings, logger
from bridge.database import (
    db_delete,
    db_dequeue_task,
    db_enqueue_task,
    db_find_by_outline_id,
    db_list_pending_tasks,
    db_upsert,
)
from bridge.lightrag_client import lightrag
from bridge.models import QueueTask, TaskType

# ═══════════════════════════════════════════════════════════════════════
#  任务队列 — 串行化所有 LightRAG 变更操作
# ═══════════════════════════════════════════════════════════════════════

# 全局串行任务队列（FIFO）
task_queue: asyncio.Queue[QueueTask] = asyncio.Queue()


async def process_task(task: QueueTask) -> None:
    """处理单个队列任务，根据任务类型分发到对应的处理函数。"""
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
    """后台任务工作者：单消费者，串行处理队列中的任务。"""
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
                task.db_id = db_enqueue_task(task)
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
            # 任务完成，清理持久化记录
            if task.db_id > 0:
                db_dequeue_task(task.db_id)
                task.db_id = 0
            task_queue.task_done()


async def recover_pending_tasks() -> int:
    """从 SQLite 加载未完成的任务，重新入队。

    在服务启动时调用。如果上次运行崩溃，pending_tasks 表中
    可能还有未处理的任务，全部重新入队。
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
            db_id=pt["id"],
        )
        await task_queue.put(qtask)
        logger.info("恢复任务: %s %s (重试 %d)", pt["task_type"], pt["outline_doc_id"], pt["retry_count"])
    return len(pending)


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
