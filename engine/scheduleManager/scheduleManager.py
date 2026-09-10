"""Persistent, dynamically reloadable proactive-message schedules."""
import asyncio
import copy
import json
import os
import re
import tempfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from config import config


DEFAULTS = {
    "timezone": "Asia/Shanghai",
    "quiet_hours": {"start": "23:00", "end": "08:00"},
    "tasks": {
        "proactive": {
            "label": "日常问候", "enabled": True,
            "times": ["10:30", "14:30", "18:30", "22:30"],
            "minimum_interval_minutes": 180, "last_sent_at": None,
        },
        "bilibili": {
            "label": "B站热门", "enabled": True, "times": ["18:00"],
            "minimum_interval_minutes": 720, "last_sent_at": None,
        },
        "daily_summary": {
            "label": "每日回顾", "enabled": False, "times": ["21:30"],
            "minimum_interval_minutes": 720, "last_sent_at": None,
        },
        "reminder": {
            "label": "自定义提醒", "enabled": False, "times": ["20:00"],
            "minimum_interval_minutes": 720, "last_sent_at": None,
            "message": "今天计划的事情完成了吗？",
        },
    },
}


class AutomationManager:
    TASK_IDS = frozenset(DEFAULTS["tasks"])

    def __init__(self, storage_path=None):
        self.storage_path = Path(storage_path or config.AUTOMATION_PATH)
        self.settings = self._load()
        self.scheduler = None
        self.callbacks = {}
        self.lock = asyncio.Lock()
        self.execution_locks = {task_id: asyncio.Lock() for task_id in self.TASK_IDS}

    @staticmethod
    def _clock(value):
        if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("时间必须使用 HH:MM 格式")
        return value

    def validate(self, value):
        if not isinstance(value, dict):
            raise ValueError("自动任务配置必须是对象")
        result = copy.deepcopy(DEFAULTS)
        zone = value.get("timezone", result["timezone"])
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, TypeError):
            raise ValueError("无效时区") from None
        result["timezone"] = zone
        quiet = value.get("quiet_hours", {})
        result["quiet_hours"] = {
            "start": self._clock(quiet.get("start", result["quiet_hours"]["start"])),
            "end": self._clock(quiet.get("end", result["quiet_hours"]["end"])),
        }
        supplied = value.get("tasks", {})
        if not isinstance(supplied, dict) or set(supplied) - self.TASK_IDS:
            raise ValueError("包含未知自动任务")
        for task_id, default in DEFAULTS["tasks"].items():
            item = supplied.get(task_id, {})
            if not isinstance(item, dict):
                raise ValueError("任务配置必须是对象")
            merged = dict(default)
            merged.update({key: item[key] for key in (
                "enabled", "times", "minimum_interval_minutes", "last_sent_at", "message"
            ) if key in item})
            if type(merged["enabled"]) is not bool:
                raise ValueError("enabled 必须是布尔值")
            if not isinstance(merged["times"], list) or not 1 <= len(merged["times"]) <= 12:
                raise ValueError("每个任务需要 1–12 个执行时间")
            merged["times"] = sorted(set(self._clock(clock) for clock in merged["times"]))
            interval = merged["minimum_interval_minutes"]
            if type(interval) is not int or not 1 <= interval <= 43200:
                raise ValueError("最小间隔必须为 1–43200 分钟")
            if task_id == "reminder":
                message = merged.get("message", "")
                if not isinstance(message, str) or not message.strip() or len(message) > 500:
                    raise ValueError("提醒内容需要 1–500 字")
                merged["message"] = message.strip()
            last = merged.get("last_sent_at")
            if last is not None:
                try:
                    datetime.fromisoformat(last)
                except (TypeError, ValueError):
                    merged["last_sent_at"] = None
            result["tasks"][task_id] = merged
        return result

    def _load(self):
        if not self.storage_path.exists():
            return copy.deepcopy(DEFAULTS)
        try:
            return self.validate(json.loads(self.storage_path.read_text(encoding="utf-8")))
        except Exception as exc:
            raise RuntimeError(f"自动任务配置损坏：{self.storage_path}") from exc

    def _write(self, settings):
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.storage_path.parent,
                                             prefix=".automations-", suffix=".tmp", delete=False) as handle:
                tmp = Path(handle.name)
                json.dump(settings, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.storage_path)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    async def bind(self, scheduler, callbacks):
        missing = self.TASK_IDS - callbacks.keys()
        if missing:
            raise ValueError(f"缺少任务回调：{', '.join(sorted(missing))}")
        self.scheduler = scheduler
        self.callbacks = dict(callbacks)
        await self.reload_jobs()

    async def reload_jobs(self):
        if self.scheduler is None:
            return
        for job in list(self.scheduler.get_jobs()):
            if job.id.startswith("active:"):
                self.scheduler.remove_job(job.id)
        zone = self.settings["timezone"]
        for task_id, task in self.settings["tasks"].items():
            if not task["enabled"]:
                continue
            for index, clock in enumerate(task["times"]):
                hour, minute = map(int, clock.split(":"))
                self.scheduler.add_job(
                    self.execute, CronTrigger(hour=hour, minute=minute, timezone=zone),
                    args=[task_id], id=f"active:{task_id}:{index}", replace_existing=True,
                    max_instances=1, coalesce=True, misfire_grace_time=120,
                )

    def snapshot(self):
        result = copy.deepcopy(self.settings)
        if self.scheduler is not None:
            next_runs = {}
            for job in self.scheduler.get_jobs():
                next_run = getattr(job, "next_run_time", None)
                if job.id.startswith("active:") and next_run:
                    task_id = job.id.split(":")[1]
                    value = next_run.isoformat()
                    if task_id not in next_runs or value < next_runs[task_id]:
                        next_runs[task_id] = value
            for task_id, task in result["tasks"].items():
                task["next_run_at"] = next_runs.get(task_id)
        return result

    async def update(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"timezone", "quiet_hours", "tasks"}:
            raise ValueError("包含未知的自动任务设置")
        if "tasks" in payload and (not isinstance(payload["tasks"], dict)
                                   or set(payload["tasks"]) - self.TASK_IDS
                                   or any(not isinstance(value, dict) for value in payload["tasks"].values())):
            raise ValueError("任务更新格式无效")
        allowed = {"enabled", "times", "minimum_interval_minutes", "message"}
        if "tasks" in payload and any(set(value) - allowed for value in payload["tasks"].values()):
            raise ValueError("任务更新包含未知字段")
        if "quiet_hours" in payload and (not isinstance(payload["quiet_hours"], dict)
                                          or set(payload["quiet_hours"]) - {"start", "end"}):
            raise ValueError("免打扰设置格式无效")
        async with self.lock:
            previous = self.settings
            merged = copy.deepcopy(previous)
            if "timezone" in payload:
                merged["timezone"] = payload["timezone"]
            if "quiet_hours" in payload:
                merged["quiet_hours"].update(payload["quiet_hours"])
            if "tasks" in payload:
                for task_id, values in payload["tasks"].items():
                    merged["tasks"][task_id].update(values)
            updated = self.validate(merged)
            await asyncio.to_thread(self._write, updated)
            self.settings = updated
            try:
                await self.reload_jobs()
            except Exception:
                self.settings = previous
                await asyncio.to_thread(self._write, previous)
                await self.reload_jobs()
                raise
            return self.snapshot()

    def _in_quiet_hours(self, now):
        start = time.fromisoformat(self.settings["quiet_hours"]["start"])
        end = time.fromisoformat(self.settings["quiet_hours"]["end"])
        current = now.timetz().replace(tzinfo=None)
        if start == end:
            return False
        return start <= current < end if start < end else current >= start or current < end

    async def execute(self, task_id, force=False):
        if task_id not in self.TASK_IDS or task_id not in self.callbacks:
            raise ValueError("未知或尚未绑定的任务")
        async with self.execution_locks[task_id]:
            task = copy.deepcopy(self.settings["tasks"][task_id])
            now = datetime.now(ZoneInfo(self.settings["timezone"]))
            if not force:
                if not task["enabled"] or self._in_quiet_hours(now):
                    return {"status": "skipped", "reason": "disabled_or_quiet"}
                if task.get("last_sent_at"):
                    last = datetime.fromisoformat(task["last_sent_at"])
                    if now.astimezone(timezone.utc) - last.astimezone(timezone.utc) < timedelta(minutes=task["minimum_interval_minutes"]):
                        return {"status": "skipped", "reason": "minimum_interval"}
            completed = await self.callbacks[task_id](task)
            if completed is False:
                return {"status": "skipped", "reason": "no_content"}
            async with self.lock:
                self.settings["tasks"][task_id]["last_sent_at"] = now.isoformat()
                await asyncio.to_thread(self._write, self.settings)
            return {"status": "sent", "sent_at": now.isoformat()}


automation_manager = AutomationManager()
