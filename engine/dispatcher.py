"""Bounded in-process jobs, ordered per user, with persistent event deduplication."""
import asyncio
import time
from collections import defaultdict, deque

from fastapi import HTTPException
from loguru import logger

from config import config
from engine.memoryManager.memoryManager import MemoryManager


class Dispatcher:
    def __init__(self, memory=None):
        self.memory = memory or MemoryManager()
        self.queue = asyncio.Queue(maxsize=config.QUEUE_SIZE)
        self.workers = []
        self.user_locks = defaultdict(asyncio.Lock)
        self.rate = defaultdict(deque)
        self.admission = asyncio.Lock()
        self.accepting = False

    async def start(self):
        if not self.workers:
            self.workers = [asyncio.create_task(self._worker()) for _ in range(config.WORKERS)]
        self.accepting = True

    async def submit(self, user_id, factory, event_key=None):
        async with self.admission:
            if not self.workers or not self.accepting:
                raise HTTPException(503, "服务尚未就绪")
            if user_id not in config.ALLOWED_QQ:
                raise HTTPException(403, "该用户未被授权")
            if event_key and await asyncio.to_thread(self.memory.event_seen, event_key):
                return False
            now = time.monotonic()
            recent = self.rate[user_id]
            while recent and recent[0] <= now - config.RATE_WINDOW:
                recent.popleft()
            if len(recent) >= config.RATE_LIMIT:
                raise HTTPException(429, "消息过于频繁", headers={"Retry-After": str(config.RATE_WINDOW)})
            if self.queue.full():
                raise HTTPException(503, "任务队列已满", headers={"Retry-After": "5"})
            if event_key and not await asyncio.to_thread(self.memory.claim_event, event_key, int(time.time())):
                return False
            self.queue.put_nowait((user_id, factory))
            recent.append(now)
            return True

    async def _worker(self):
        while True:
            user_id, factory = await self.queue.get()
            try:
                async with self.user_locks[user_id]:
                    await factory()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("后台消息处理失败 ({})", type(exc).__name__)
            finally:
                self.queue.task_done()

    async def stop(self):
        async with self.admission:
            self.accepting = False
        if not self.workers:
            return
        try:
            await asyncio.wait_for(self.queue.join(), timeout=30)
        except TimeoutError:
            logger.warning("关闭时仍有消息未完成")
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()


dispatcher = Dispatcher()
