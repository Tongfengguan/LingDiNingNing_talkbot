import asyncio
import hashlib
import html
import re
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request

from config import config
from engine.aiEngine.aiEngine import ai
from engine.dispatcher import dispatcher
from engine.emailEngine.emailEngine import cancel_draft, confirm_draft
from engine.messageUtils.messageUtils import typing_delay
from engine.cfEngine.cfEngine import get_cf_info
from engine.searchEngine.searchEngine import search_image_url
from engine.imageUtils.imageUtils import download_image, find_local_image
from engine.security import signed_event
from engine.scheduleManager import automation_manager

router = APIRouter(prefix="/qq")
memory = ai.memory

HELP_TEXT = """🌸 宁宁的使用手册 🌸

· /help 或 /h：查看说明
· /cf [ID]：查询 Codeforces
· /学这个 [名]：随图片发送，保存表情
· /清除记忆：清除当前用户短期与长期聊天记忆
· /确认邮件 [确认码]：核对草稿后发送邮件（仅管理员）
· /取消邮件：取消待发送草稿
· /主动消息 状态、开启、关闭：管理日常问候和热门推送（仅管理员）

可直接聊天、发图、询问知识库或 B 站热门。
邮件先生成完整草稿，确认后才会发送。"""


async def send_msg(user_id, message):
    # OneBot array messages preserve text literally; model-generated CQ codes are never executed.
    segments, offset = [], 0
    matches = [] if message.startswith("【邮件草稿") else list(re.finditer(r"\[表情:\s*([^\]\n]{1,80})\]", message))[:3]
    for match in matches:
        if match.start() > offset:
            segments.append({"type": "text", "data": {"text": message[offset:match.start()]}})
        tag = match.group(1).strip()
        local_path = find_local_image(tag)
        if not local_path:
            url = await asyncio.to_thread(search_image_url, tag)
            if url:
                local_path = await download_image(url, tag)
        if local_path:
            segments.append({"type": "image", "data": {"file": Path(local_path).as_uri()}})
        else:
            segments.append({"type": "text", "data": {"text": f"[{tag}]"}})
        offset = match.end()
    if offset < len(message):
        segments.append({"type": "text", "data": {"text": message[offset:]}})
    if not segments:
        return
    headers = {"Authorization": f"Bearer {config.NAPCAT_TOKEN}"} if config.NAPCAT_TOKEN else {}
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        resp = await client.post(f"{config.NAPCAT_URL}/send_private_msg", headers=headers,
                                 json={"user_id": int(user_id), "message": segments})
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "ok" or data.get("retcode") != 0:
            raise RuntimeError("NapCat rejected the message")


def parse_message(data):
    raw = data.get("raw_message", "")
    if not isinstance(raw, str) or len(raw) > config.MAX_MESSAGE_CHARS:
        raise HTTPException(400, "消息格式或长度不允许")
    image_url = None
    segments = data.get("message")
    if isinstance(segments, list):
        if len(segments) > 64:
            raise HTTPException(400, "消息段过多")
        texts = []
        for segment in segments:
            if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
                raise HTTPException(400, "无效消息段")
            value = segment["data"]
            if segment.get("type") == "text":
                if not isinstance(value.get("text"), str):
                    raise HTTPException(400, "无效文本段")
                texts.append(value["text"])
            elif segment.get("type") == "image" and image_url is None:
                image_url = value.get("url")
        message = "".join(texts).strip()
    else:
        for match in re.finditer(r"\[CQ:image,([^\]]*)\]", raw):
            values = dict(item.split("=", 1) for item in match.group(1).split(",") if "=" in item)
            if image_url is None and "url" in values:
                image_url = html.unescape(values["url"])
        message = html.unescape(re.sub(r"\[CQ:image,[^\]]*\]", "", raw)).strip()
    if len(message) > config.MAX_MESSAGE_CHARS:
        raise HTTPException(400, "消息过长")
    if image_url is not None and (not isinstance(image_url, str) or len(image_url) > 2048):
        raise HTTPException(400, "图片 URL 无效")
    return message, image_url


def numeric_id(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise HTTPException(400, "缺少或无效的事件 ID")
    value = str(value)
    if not re.fullmatch(r"-?[0-9]{1,20}", value) or (positive and int(value) <= 0):
        raise HTTPException(400, "无效事件 ID")
    return str(int(value))


@router.post("/webhook")
async def webhook(request: Request):
    data = await signed_event(request)
    if data.get("post_type") != "message" or data.get("message_type") != "private":
        return {"status": "ignored"}
    user_id = numeric_id(data.get("user_id"), positive=True)
    if user_id not in config.ALLOWED_QQ:
        raise HTTPException(403, "该用户未被授权")
    self_id = numeric_id(data.get("self_id"), positive=True)
    message_id = numeric_id(data.get("message_id"))
    event_time = data.get("time")
    if type(event_time) is not int or abs(time.time() - event_time) > config.EVENT_MAX_AGE:
        raise HTTPException(400, "事件时间无效或已过期")
    message, image_url = parse_message(data)
    if not message and not image_url:
        return {"status": "ignored"}
    sender = data.get("sender", {})
    if not isinstance(sender, dict) or not isinstance(sender.get("nickname", ""), str):
        raise HTTPException(400, "发送者格式无效")
    user_name = sender.get("nickname", "宝贝")[:80]
    key = hashlib.sha256(f"{self_id}:{user_id}:{message_id}:{event_time}".encode()).hexdigest()
    accepted = await dispatcher.submit(
        user_id, lambda: handle_message(user_id, user_name, message, image_url), key
    )
    return {"status": "ok" if accepted else "duplicate"}


async def handle_message(user_id, user_name, message, image_url=None):
    if message.lower() in {"/help", "/h", "帮助", "帮助菜单"}:
        await send_msg(user_id, HELP_TEXT)
    elif message.startswith("/学这个") and image_url:
        name = message[len("/学这个"):].strip()
        path = await download_image(image_url, name)
        await send_msg(user_id, "表情已保存。" if path else "图片保存失败，请检查图片地址和名称。")
    elif message == "/清除记忆":
        cancel_draft(user_id)
        await memory.clear(user_id)
        await send_msg(user_id, "你的短期和长期聊天记忆已清空。")
    elif message == "/取消邮件":
        cancel_draft(user_id)
        await send_msg(user_id, "待发送草稿已取消。")
    elif message.startswith("/确认邮件 "):
        await send_msg(user_id, await confirm_draft(user_id, message[len("/确认邮件 "):].strip()))
    elif message.startswith("/主动消息"):
        if user_id != config.ADMIN_QQ:
            await send_msg(user_id, "主动消息设置仅管理员可用。")
        else:
            action = message[len("/主动消息"):].strip() or "状态"
            if action in {"开启", "打开", "开"}:
                await automation_manager.update({"tasks": {
                    "proactive": {"enabled": True}, "bilibili": {"enabled": True},
                }})
                await send_msg(user_id, "日常问候和 B 站推送已开启。")
            elif action in {"关闭", "关"}:
                await automation_manager.update({"tasks": {
                    "proactive": {"enabled": False}, "bilibili": {"enabled": False},
                }})
                await send_msg(user_id, "日常问候和 B 站推送已关闭。")
            elif action == "状态":
                tasks = automation_manager.snapshot()["tasks"]
                lines = [f"{task['label']}：{'开启' if task['enabled'] else '关闭'}，{', '.join(task['times'])}"
                         for task in tasks.values()]
                await send_msg(user_id, "【主动消息状态】\n" + "\n".join(lines))
            else:
                await send_msg(user_id, "用法：/主动消息 状态、开启、关闭")
    elif message.startswith("/cf "):
        await send_msg(user_id, await get_cf_info(message[4:].strip()))
    else:
        await _reply(user_id, user_name, message, int(user_id), image_url)


async def _reply(u_id, u_name, msg, qq_id, image_url=None):
    reply = await ai.chat(u_id, u_name, msg, image_url)
    await _send_reply(qq_id, reply)
    await ai.index_exchange(u_id, msg, reply)


async def _send_reply(qq_id, reply):
    if any(marker in reply for marker in ("\x60\x60\x60", "📺", "http", "【邮件草稿", "[表情:")):
        await typing_delay(reply)
        await send_msg(qq_id, reply)
        return
    parts = re.split(r"([。！？~！?\n]+)", reply)
    sentences, current = [], ""
    for i, part in enumerate(parts):
        current += part
        if i % 2 == 1 or i == len(parts) - 1:
            if current.strip():
                sentences.append(current.strip())
            current = ""
    for sentence in sentences:
        await typing_delay(sentence)
        await send_msg(qq_id, sentence)
