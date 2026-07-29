"""Outline Webhook HMAC-SHA256 签名验证。

Outline 的签名格式：
  Header: outline-signature: t=<timestamp>,s=<hex-signature>
  签名内容: HMAC-SHA256(secret, "{timestamp}.{raw_body}")
  其中 timestamp 为 Unix 毫秒时间戳，raw_body 为 HTTP 请求原始体（字符串）。

参考: https://docs.getoutline.com/s/guide/doc/webhooks-gB7HYhS6yq
"""

import hashlib
import hmac
import time

from bridge.config import settings, logger


def verify_outline_signature(raw_body: bytes, signature_header: str) -> bool:
    """验证 Outline Webhook 的 HMAC-SHA256 签名。

    参数:
        raw_body: HTTP 请求的原始 body（字节流）
        signature_header: outline-signature Header 的值，格式为 t=...,s=...

    返回:
        True: 签名验证通过（或 secret 未配置时跳过验证）
        False: 签名不匹配
    """
    secret = settings.outline_webhook_secret
    if not secret:
        logger.warning("OUTLINE_WEBHOOK_SECRET 未配置，跳过签名验证（生产环境请务必配置！）")
        return True

    if not signature_header:
        logger.warning("缺少 outline-signature Header")
        return False

    # 解析 Header: t=<timestamp>,s=<signature>
    try:
        parts = {}
        for pair in signature_header.split(","):
            key, _, value = pair.partition("=")
            parts[key.strip()] = value.strip()

        timestamp = parts.get("t")
        signature = parts.get("s")

        if not timestamp or not signature:
            logger.warning("outline-signature 格式错误: %s", signature_header)
            return False
    except Exception as e:
        logger.warning("outline-signature 解析失败: %s", e)
        return False

    # 构造签名内容: "{timestamp}.{body}"
    # 注意: body 是原始字符串（不是 JSON 解析后的），且 timestamp 保持原始格式
    body_str = raw_body.decode("utf-8")
    data = f"{timestamp}.{body_str}"

    expected_sig = hmac.new(
        secret.encode("utf-8"), data.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected_sig, signature)
