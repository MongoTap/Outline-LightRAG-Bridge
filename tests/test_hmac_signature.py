"""验证 Outline HMAC 签名逻辑的独立测试。

模拟 Outline 端的签名生成 + Bridge 端的签名验证。
"""
import hashlib
import hmac
import time

SECRET = "test-secret-123"
BODY_RAW = b'{"event":"documents.create","payload":{"id":"doc-1","model":{"id":"doc-1","title":"Test","text":"Hello"}}}'

# ── 模拟 Outline 端签名 ──────────────────────────────────────────

# Outline 使用毫秒级时间戳
ts = str(int(time.time() * 1000))
data = f"{ts}.{BODY_RAW.decode('utf-8')}"

sig = hmac.new(SECRET.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).hexdigest()
header = f"t={ts},s={sig}"
print(f"Secret:       {SECRET}")
print(f"Timestamp:    {ts}")
print(f"Body:         {BODY_RAW.decode()[:50]}...")
print(f"Content:      {data[:60]}...")
print(f"Signature:    {sig[:20]}...")
print(f"Header:       {header}")

# ── 模拟 Bridge 端验证 ───────────────────────────────────────────

# 此部分应和 bridge/auth.py 中 verify_outline_signature 逻辑完全一致
def verify(raw_body: bytes, signature_header: str, secret: str) -> bool:
    if not secret:
        print("  SKIP: secret empty")
        return True
    if not signature_header:
        print("  FAIL: no header")
        return False

    try:
        parts = {}
        for pair in signature_header.split(","):
            key, _, value = pair.partition("=")
            parts[key.strip()] = value.strip()

        timestamp = parts.get("t")
        signature = parts.get("s")
        if not timestamp or not signature:
            print(f"  FAIL: bad format: {signature_header}")
            return False
    except Exception as e:
        print(f"  FAIL: parse error: {e}")
        return False

    body_str = raw_body.decode("utf-8")
    data = f"{timestamp}.{body_str}"
    expected = hmac.new(secret.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── 测试用例 ──────────────────────────────────────────────────────

tests = [
    ("正确签名", BODY_RAW, header, SECRET, True),
    ("空 Secret", BODY_RAW, header, "", True),
    ("空 Header", BODY_RAW, "", SECRET, False),
    ("错误 Secret", BODY_RAW, header, "wrong-secret", False),
    ("篡改 Body", b'{"bad":"body"}', header, SECRET, False),
    ("错误格式 Header", BODY_RAW, "invalid-format", SECRET, False),
    ("只有 t 无 s", BODY_RAW, "t=123", SECRET, False),
]

print(f"\n{'='*60}")
print(f"  签名验证测试")
print(f"{'='*60}")
passed = 0
for name, body, hdr, secret, expected in tests:
    result = verify(body, hdr, secret)
    ok = result == expected
    status = "✓" if ok else "✗"
    print(f"  {status} {name}: {'PASS' if ok else 'FAIL'}")
    if not ok:
        print(f"      预期={expected}, 实际={result}")
    if ok:
        passed += 1

print(f"{'─'*60}")
print(f"  结果: {passed}/{len(tests)} 通过")
print()
