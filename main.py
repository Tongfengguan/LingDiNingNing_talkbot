import asyncio
import os
import random
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from loguru import logger

from config import config
from engine.aiEngine.aiEngine import ai
from engine.dashboard.dashboard import router as dashboard_router
from engine.dispatcher import dispatcher
from engine.qqAdapter.qqAdapter import router as qq_router, send_msg, _reply
from engine.scheduleManager import automation_manager


async def proactive_message(task):
    if config.ADMIN_QQ:
        last_active = await asyncio.to_thread(ai.memory.last_active, config.ADMIN_QQ)
        last_sent = task.get("last_sent_at")
        if last_active and last_sent:
            active_at = datetime.fromisoformat(last_active).replace(tzinfo=timezone.utc)
            sent_at = datetime.fromisoformat(last_sent).astimezone(timezone.utc)
            inactive = datetime.now(timezone.utc) - active_at
            since_sent = datetime.now(timezone.utc) - sent_at
            if inactive.days >= 7 and since_sent.days < 3:
                return False
            if inactive.days >= 3 and since_sent.days < 1:
                return False
        message = random.choice([
            "(哈啊……) 喂，你在干嘛呢？", "还在忙吗？记得休息一下！",
            "刚才看到个有趣的东西，回我一下嘛。", "你是失踪了吗？还是把我忘了？",
        ])
        await dispatcher.submit(config.ADMIN_QQ, lambda: send_msg(config.ADMIN_QQ, message))
        return True
    return False


async def bilibili_trending_push(task):
    if config.ADMIN_QQ:
        await dispatcher.submit(
            config.ADMIN_QQ,
            lambda: _reply(config.ADMIN_QQ, "看看", "推荐一个B站热门视频", int(config.ADMIN_QQ)),
        )
        return True
    return False


async def daily_summary_push(task):
    if not config.ADMIN_QQ or not await asyncio.to_thread(ai.memory.recent_history, config.ADMIN_QQ, 24, 1):
        return False
    async def create_and_send():
        summary = await ai.daily_summary(config.ADMIN_QQ)
        if summary:
            await send_msg(config.ADMIN_QQ, "【今日回顾】\n" + summary)
    await dispatcher.submit(config.ADMIN_QQ, create_and_send)
    return True


async def reminder_push(task):
    if not config.ADMIN_QQ:
        return False
    await dispatcher.submit(config.ADMIN_QQ, lambda: send_msg(config.ADMIN_QQ, task["message"]))
    return True


async def sync_documents():
    try:
        await ai.sync_all()
    except Exception as exc:
        logger.error("知识库同步失败 ({})", type(exc).__name__)


@asynccontextmanager
async def lifespan(app):
    config.validate()
    os.makedirs("logs", exist_ok=True)
    log_id = logger.add("logs/bot_{time:YYYY-MM-DD}.log", rotation="1 day", retention="7 days")
    scheduler = AsyncIOScheduler(timezone=automation_manager.settings["timezone"])
    initial_sync = None
    try:
        await dispatcher.start()
        initial_sync = asyncio.create_task(sync_documents())
        scheduler.add_job(sync_documents, "interval", minutes=5, max_instances=1, coalesce=True)
        await automation_manager.bind(scheduler, {
            "proactive": proactive_message,
            "bilibili": bilibili_trending_push,
            "daily_summary": daily_summary_push,
            "reminder": reminder_push,
        })
        scheduler.start()
        app.state.ready = True
        logger.info("Bot started on {}:{}", config.BOT_HOST, config.BOT_PORT)
        yield
    finally:
        app.state.ready = False
        if scheduler.running:
            scheduler.shutdown(wait=False)
        if initial_sync is not None:
            initial_sync.cancel()
            await asyncio.gather(initial_sync, return_exceptions=True)
        await dispatcher.stop()
        await ai.close()
        logger.remove(log_id)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(qq_router)
app.include_router(dashboard_router)


@app.get("/healthz")
async def health():
    ready = getattr(app.state, "ready", False)
    return JSONResponse({"status": "ok" if ready else "starting"}, status_code=200 if ready else 503)


if __name__ == "__main__":
    uvicorn.run(app, host=config.BOT_HOST, port=config.BOT_PORT, reload=False,
                loop="asyncio", proxy_headers=False, limit_concurrency=64, timeout_keep_alive=5)
