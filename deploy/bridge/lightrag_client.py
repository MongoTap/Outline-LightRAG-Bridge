"""LightRAG API 客户端

封装与 LightRAG HTTP API 的所有交互操作：
  - 插入文本（异步，返回 track_id）
  - 轮询处理状态（获取最终的 doc_id）
  - 删除文档（含 pipeline busy 重试逻辑）
  - 等待 pipeline 空闲
"""

import asyncio

import httpx

from bridge.config import settings, logger


class LightRAGClient:
    """封装对 LightRAG API 的所有 HTTP 调用。"""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def insert_text(self, text: str, file_source: str = "") -> str:
        """向 LightRAG 插入文本内容。

        参数:
            text: 文档的 Markdown 文本内容
            file_source: 可选的来源标识

        返回:
            track_id: LightRAG 异步处理的追踪 ID
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

        参数:
            track_id: insert_text 返回的追踪 ID

        返回:
            doc_id: LightRAG 中的文档 ID（格式如 doc-xxx）

        异常:
            RuntimeError: 文档处理失败
            TimeoutError: 轮询超时
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

        参数:
            doc_id: LightRAG 中的文档 ID（doc-xxx 格式）

        返回:
            bool: 是否成功删除

        重试逻辑:
            当 LightRAG 的 pipeline 正在处理其他文档时，会返回
            status="busy"。此时等待 delete_retry_delay 秒后重试。
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
        在 task_worker 派发每个任务前调用。

        参数:
            timeout: 最长等待时间（秒）
            interval: 轮询间隔（秒）

        返回:
            True: pipeline 空闲
            False: 超时
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


# 全局 LightRAG 客户端单例
lightrag = LightRAGClient(settings.lightrag_api_url)
