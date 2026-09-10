"""Inbound authentication and bounded request parsing."""
import hashlib
import asyncio
import hmac
import json

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from config import config

basic = HTTPBasic(auto_error=False)


def dashboard_auth(credentials: HTTPBasicCredentials | None = Depends(basic)):
    if not config.DASHBOARD_TOKEN:
        raise HTTPException(503, "控制台未启用")
    valid = credentials is not None and hmac.compare_digest(
        credentials.password.encode(), config.DASHBOARD_TOKEN.encode()
    ) and hmac.compare_digest(credentials.username.encode(), b"admin")
    if not valid:
        raise HTTPException(401, "需要登录", headers={"WWW-Authenticate": 'Basic realm="Dashboard"'})


async def signed_event(request: Request):
    if not config.WEBHOOK_SECRET or not config.ALLOWED_QQ:
        raise HTTPException(503, "回调安全配置未完成")
    signature = request.headers.get("x-signature", "")
    if not signature.startswith("sha1=") or len(signature) != 45:
        raise HTTPException(401, "无效签名")
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                if len(body) + len(chunk) > config.MAX_WEBHOOK_BYTES:
                    raise HTTPException(413, "请求过大")
                body.extend(chunk)
    except TimeoutError:
        raise HTTPException(408, "读取请求超时") from None
    expected = "sha1=" + hmac.new(config.WEBHOOK_SECRET.encode(), body, hashlib.sha1).hexdigest()
    if not hmac.compare_digest(signature.encode(), expected.encode()):
        raise HTTPException(401, "无效签名")
    try:
        event = json.loads(body)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "无效 JSON") from None
    if not isinstance(event, dict):
        raise HTTPException(400, "事件必须是对象")
    return event
