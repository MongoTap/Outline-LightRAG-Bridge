"""Outline Webhook HMAC-SHA256 签名验证。"""

import hashlib
import hmac

from bridge.config import settings, logger


def verify_outline_signature(raw_body: bytes, signature_header: str) -> bool:
    """验证 Outline Webhook 的 HMAC-SHA256 签名。

    参数:
        raw_body: HTTP 请求的原始 body（字节流）
        signature_header: outline-signature Header 的值

    返回:
        True: 签名验证通过（或 secret 未配置时跳过验证）
        False: 签名不匹配
    """
    secret = settings.outline_webhook_secret
    if not secret:
        logger.warning("OUTLINE_WEBHOOK_SECRET 未配置，跳过签名验证（生产环境请务必配置！）")
        return True

    expected_sig = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected_sig, signature_header)
