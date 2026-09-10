"""Offline regression tests. All files live in a temporary directory; no real credentials are loaded."""
import asyncio
import gc
import hashlib
import hmac
import importlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ORIGINAL_CWD = Path.cwd()
SUITE = tempfile.TemporaryDirectory(prefix="talkbot-regression-")
os.chdir(SUITE.name)
with patch("dotenv.load_dotenv"), patch.dict(os.environ, {
    "DEEPSEEK_API_KEY": "test-only", "DEEPSEEK_BASE_URL": "https://model.invalid",
    "DASHSCOPE_API_KEY": "", "ADMIN_QQ": "10001", "ALLOWED_QQ": "10001,10002",
    "WEBHOOK_SECRET": "s" * 64, "DASHBOARD_TOKEN": "d" * 64,
    "NAPCAT_URL": "http://napcat.invalid", "NAPCAT_TOKEN": "test-napcat",
    "SMTP_USER": "bot@example.invalid", "SMTP_PASSWORD": "test-only",
    "RECEIVER_EMAIL": "owner@example.invalid", "SMTP_HOST": "smtp.invalid", "SMTP_PORT": "465",
}):
    from config import config
    qq = importlib.import_module("engine.qqAdapter.qqAdapter")
    rag = importlib.import_module("engine.ragEngine.ragEngine")
    mm = importlib.import_module("engine.memoryManager.memoryManager")
    pdf = importlib.import_module("engine.pdfEngine.pdfEngine")
    dash = importlib.import_module("engine.dashboard.dashboard")
    imgs = importlib.import_module("engine.imageUtils.imageUtils")
    email = importlib.import_module("engine.emailEngine.emailEngine")
    aim = importlib.import_module("engine.aiEngine.aiEngine")
    schedules = importlib.import_module("engine.scheduleManager.scheduleManager")
    from engine.dispatcher import Dispatcher

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException
from PIL import Image


def tearDownModule():
    os.chdir(ORIGINAL_CWD)
    gc.collect()
    SUITE.cleanup()


class RegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=SUITE.name)
        self.cwd = Path.cwd()
        os.chdir(self.tmp.name)
        self.rag = rag.RAGEngine()
        for module in (rag, mm, pdf, dash):
            self.start_patch(patch.object(module, "rag_engine", self.rag))
        self.memory = mm.MemoryManager()
        self.dispatcher = Dispatcher(self.memory)
        self.docs = pdf.PDFEngine()
        self.automations = schedules.AutomationManager()
        self.start_patch(patch.object(qq, "dispatcher", self.dispatcher))
        self.start_patch(patch.object(qq, "memory", self.memory))
        self.start_patch(patch.object(qq, "automation_manager", self.automations))
        self.start_patch(patch.object(dash, "pdf_engine", self.docs))
        self.start_patch(patch.object(dash, "ai", SimpleNamespace(memory=self.memory)))
        self.start_patch(patch.object(dash, "dispatcher", self.dispatcher))
        self.start_patch(patch.object(dash, "automation_manager", self.automations))
        self.real_send = qq.send_msg
        self.send = self.start_patch(patch.object(qq, "send_msg", new=AsyncMock()))
        self.reply = self.start_patch(patch.object(qq, "_reply", new=AsyncMock()))
        self.embed = self.start_patch(patch.object(rag, "get_embedding", new=AsyncMock(return_value=[1.0, 0.0])))
        self.bot = aim.AIGirlfriend()
        self.start_patch(patch.object(aim, "pdf_engine", self.docs))
        email.drafts.clear()
        await self.dispatcher.start()
        self.app = FastAPI()
        self.app.include_router(qq.router)
        self.app.include_router(dash.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    async def asyncTearDown(self):
        await self.dispatcher.stop()
        await self.bot.close()
        await self.client.aclose()
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def event(self, **changes):
        return dict({"post_type": "message", "message_type": "private", "user_id": 10001,
                     "self_id": 20001, "message_id": 1, "time": int(time.time()),
                     "raw_message": "/help", "sender": {"nickname": "Test"}}, **changes)

    async def post(self, event, signed=True):
        raw = json.dumps(event, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json"}
        if signed:
            headers["X-Signature"] = "sha1=" + hmac.new(config.WEBHOOK_SECRET.encode(), raw, hashlib.sha1).hexdigest()
        return await self.client.post("/qq/webhook", content=raw, headers=headers)

    async def test_unsigned_and_wrong_signature_cannot_clear_history(self):
        self.memory.save("10001", "user", "keep me")
        self.assertEqual((await self.post(self.event(raw_message="/清除记忆"), signed=False)).status_code, 401)
        response = await self.client.post("/qq/webhook", content=b"{}", headers={"X-Signature": "sha1=" + "0" * 40})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(self.memory.get_history("10001")), 1)

    async def test_signed_event_processed_once_and_dedup_persisted(self):
        event = self.event()
        self.assertEqual((await self.post(event)).status_code, 200)
        self.assertEqual((await self.post(event)).json()["status"], "duplicate")
        await self.dispatcher.queue.join()
        self.send.assert_awaited_once()
        reopened = mm.MemoryManager()
        key = hashlib.sha256(f"20001:10001:1:{event['time']}".encode()).hexdigest()
        self.assertFalse(reopened.claim_event(key, int(time.time())))

    async def test_user_whitelist_and_timestamp(self):
        self.assertEqual((await self.post(self.event(user_id=90001))).status_code, 403)
        self.assertEqual((await self.post(self.event(time=0))).status_code, 400)
        self.assertEqual((await self.post(self.event(message_id=None))).status_code, 400)
        self.assertEqual((await self.post(self.event(raw_message=123))).status_code, 400)
        self.assertEqual((await self.post([])).status_code, 400)

    async def test_missing_security_config_fails_closed(self):
        with patch.object(config, "WEBHOOK_SECRET", ""):
            self.assertEqual((await self.post(self.event())).status_code, 503)
        with patch.object(config, "ALLOWED_QQ", frozenset()):
            self.assertEqual((await self.post(self.event())).status_code, 503)

    async def test_request_size_is_bounded(self):
        response = await self.post(self.event(raw_message="x" * (config.MAX_WEBHOOK_BYTES + 1)))
        self.assertEqual(response.status_code, 413)
        self.assertEqual((await self.post(self.event(raw_message="x" * (config.MAX_MESSAGE_CHARS + 1)))).status_code, 400)

    async def test_dashboard_auth_escape_and_no_fake_sync(self):
        marker = '<script>alert("stored")</script>'
        await self.rag.index_chunks([marker], "chat_10001", kind="chat", user_id="10001")
        self.assertEqual((await self.client.get("/dashboard/")).status_code, 401)
        auth = ("admin", config.DASHBOARD_TOKEN)
        response = await self.client.get("/dashboard/", auth=auth)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(marker, response.text)
        memories = await self.client.get("/dashboard/api/memories?kind=chat", auth=auth)
        self.assertEqual(memories.status_code, 200)
        self.assertEqual(memories.json()["items"][0]["text"], marker)
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual((await self.client.get("/dashboard/static/dashboard.css", auth=auth)).status_code, 200)
        with patch.object(config, "DASHBOARD_TOKEN", ""):
            self.assertEqual((await self.client.get("/dashboard/", auth=auth)).status_code, 503)

    async def test_structured_and_cq_image_parsing(self):
        message, url = qq.parse_message({"message": [{"type": "text", "data": {"text": "/学这个 cat"}},
                        {"type": "image", "data": {"url": "https://example.com/a.png"}}]})
        self.assertEqual(message, "/学这个 cat")
        self.assertEqual(url, "https://example.com/a.png")
        message, url = qq.parse_message({"raw_message": "hi[CQ:image,file=abc,url=https://example.com/a?x=1&amp;y=2,type=show]"})
        self.assertEqual(message, "hi")
        self.assertEqual(url, "https://example.com/a?x=1&y=2")

    async def test_clear_removes_both_stores_only_for_owner(self):
        for user in ("10001", "10002"):
            self.memory.save(user, "user", "a private record")
            await self.memory.index_chat(user, "user", "a private record")
        response = await self.post(self.event(raw_message="/清除记忆"))
        self.assertEqual(response.status_code, 200)
        await self.dispatcher.queue.join()
        self.assertEqual(self.memory.get_history("10001"), [])
        self.assertTrue(self.memory.get_history("10002"))
        self.assertFalse(await self.rag.search("record", kind="chat", user_id="10001"))
        self.assertTrue(await self.rag.search("record", kind="chat", user_id="10002"))

    async def test_same_second_history_has_correct_order(self):
        self.memory.save_turn("10001", "question", "answer")
        self.memory._exec("UPDATE chat_history SET timestamp='2020-01-01 00:00:00'")
        self.assertEqual([m["role"] for m in self.memory.get_history("10001")], ["user", "assistant"])

    async def test_rag_filters_before_ranking_and_dedup_keeps_owners(self):
        await self.rag.index_chunks(["same content"], "chat_10001", kind="chat", user_id="10001")
        await self.rag.index_chunks(["same content"], "chat_10002", kind="chat", user_id="10002")
        await self.rag.replace_document("notes.txt", ["shared document"], "digest")
        results = await self.rag.search("query", top_k=1, kind="document")
        self.assertEqual(results[0]["text"], "shared document")
        self.assertEqual(len(self.rag.data), 3)
        own = await self.memory.get_long_term_memory("10002", "query")
        self.assertEqual(own, "same content")
        with self.assertRaises(ValueError):
            await self.rag.search("query", kind="chat")
        with self.assertRaises(ValueError):
            await self.rag.index_chunks(["bad"], "chat_10001", kind="chat", user_id="10002")

    async def test_inflight_embedding_cannot_restore_cleared_memory(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def embedding(_):
            started.set()
            await release.wait()
            return [1, 0]
        with patch.object(rag, "get_embedding", side_effect=embedding):
            pending = asyncio.create_task(self.memory.index_chat("10001", "user", "long enough private record"))
            await started.wait()
            await self.memory.clear("10001")
            release.set()
            await pending
        self.assertEqual(self.rag.data, [])

    async def test_atomic_save_preserves_previous_file_on_failure(self):
        await self.rag.replace_document("a.txt", ["initial content"], "one")
        before = Path("data/vector_store.json").read_bytes()
        with patch.object(rag.os, "replace", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                await self.rag.replace_document("a.txt", ["updated content"], "two")
        self.assertEqual(Path("data/vector_store.json").read_bytes(), before)
        self.assertEqual(self.rag.document_fingerprint("a.txt"), "one")
        self.assertFalse(list(Path("data").glob(".vectors-*.tmp")))

    async def test_legacy_migration_and_corruption_fail_closed(self):
        legacy = [{"source": "chat_10001", "text": "private", "vec": [1, 0]},
                  {"source": "guide.txt", "text": "shared", "vec": [1, 0]}]
        Path("legacy.json").write_text(json.dumps(legacy), encoding="utf-8")
        migrated = rag.RAGEngine("legacy.json")
        self.assertEqual([r["kind"] for r in migrated.data], ["chat", "document"])
        Path("broken.json").write_text("{", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            rag.RAGEngine("broken.json")
        self.assertEqual(Path("broken.json").read_text(), "{")

    async def test_document_update_delete_and_keyword_fallback(self):
        engine = pdf.PDFEngine()
        path = engine.docs_dir / "notes.txt"
        path.write_text("Original useful document content.", encoding="utf-8")
        await engine.sync_docs()
        first = self.rag.document_fingerprint("notes.txt")
        path.write_text("Updated useful document content.", encoding="utf-8")
        with patch.object(rag, "get_embedding", new=AsyncMock(return_value=None)):
            await engine.sync_docs()
        self.assertNotEqual(self.rag.document_fingerprint("notes.txt"), first)
        self.assertEqual(self.rag.data[0]["vec"], [])
        self.assertTrue(all("Original" not in d["text"] for d in self.rag.data))
        path.unlink()
        await engine.sync_docs()
        self.assertEqual(self.rag.data, [])

    async def test_docx_plain_paragraph_extraction(self):
        engine = pdf.PDFEngine()
        with zipfile.ZipFile(engine.docs_dir / "test.docx", "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Document paragraph to index.</w:t></w:r></w:p></w:body></w:document>')
        await engine.sync_docs()
        self.assertIn("test.docx", self.rag.documents)
        self.assertIn("Document paragraph", (await engine.search_docs("paragraph")))

    async def test_structured_hybrid_retrieval_groups_and_chunk_deletion(self):
        path = self.docs.docs_dir / "种植手册.md"
        path.write_text("# 番茄管理\n番茄施肥应少量多次，并根据土壤湿度调整浇水。\n\n"
                        "## 病害处理\n发现叶片病斑后，应及时隔离并记录发生区域。", encoding="utf-8")
        with patch.object(rag, "get_embedding", new=AsyncMock(return_value=None)):
            result = await self.docs.sync_docs()
            matches = await self.docs.search("番茄施肥")
        self.assertEqual(result["indexed"], ["种植手册.md"])
        self.assertTrue(matches)
        self.assertEqual(matches[0]["source"], "种植手册.md")
        self.assertEqual(matches[0]["section"], "番茄管理")
        self.assertGreater(matches[0]["keyword_score"], 0)
        self.assertEqual(matches[0]["vec"], [])
        self.assertTrue(await self.rag.set_document_group("种植手册.md", "agriculture"))
        self.assertFalse(await self.docs.search("番茄施肥", group="shared"))
        self.assertTrue(await self.docs.search("番茄施肥", group="agriculture"))

        rows = [row for row in self.rag.data if row["source"] == "种植手册.md"]
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(await self.rag.remove_item(rows[0]["id"]))
        self.assertEqual(self.rag.documents["种植手册.md"]["chunks"], len(rows) - 1)
        for row in rows[1:]:
            self.assertTrue(await self.rag.remove_item(row["id"]))
        self.assertNotIn("种植手册.md", self.rag.documents)

    async def test_automation_persistence_reload_quiet_hours_and_force(self):
        scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        callbacks = {task_id: AsyncMock(return_value=True) for task_id in self.automations.TASK_IDS}
        await self.automations.bind(scheduler, callbacks)
        self.assertEqual(len([job for job in scheduler.get_jobs() if job.id.startswith("active:")]), 5)

        updated = await self.automations.update({"quiet_hours": {"start": "22:30", "end": "07:30"},
            "tasks": {"proactive": {"enabled": False}, "bilibili": {"enabled": False},
                      "reminder": {"enabled": True, "times": ["20:15"], "message": "检查今日计划"}}})
        self.assertFalse(updated["tasks"]["proactive"]["enabled"])
        self.assertEqual([job.id for job in scheduler.get_jobs()], ["active:reminder:0"])
        self.assertTrue(self.automations._in_quiet_hours(datetime.fromisoformat("2026-01-01T23:00:00")))
        self.assertFalse(self.automations._in_quiet_hours(datetime.fromisoformat("2026-01-01T12:00:00")))
        self.automations.settings["quiet_hours"] = {"start": "00:00", "end": "00:00"}
        self.assertFalse(self.automations._in_quiet_hours(datetime.fromisoformat("2026-01-01T12:00:00")))

        sent = await self.automations.execute("reminder", force=True)
        self.assertEqual(sent["status"], "sent")
        callbacks["reminder"].assert_awaited_once()
        self.assertEqual((await self.automations.execute("reminder"))["reason"], "minimum_interval")
        reopened = schedules.AutomationManager()
        self.assertIsNotNone(reopened.settings["tasks"]["reminder"]["last_sent_at"])
        with self.assertRaises(ValueError):
            await self.automations.update({"tasks": {"reminder": {"typo": True}}})
        with self.assertRaises(ValueError):
            await self.automations.update({"quiet_hours": {"start": "25:00", "end": "08:00"}})

    async def test_qq_admin_can_toggle_proactive_messages(self):
        await qq.handle_message("10002", "Guest", "/主动消息 关闭")
        self.assertIn("仅管理员", self.send.await_args.args[1])
        self.assertTrue(self.automations.settings["tasks"]["proactive"]["enabled"])

        await qq.handle_message("10001", "Owner", "/主动消息 关闭")
        self.assertFalse(self.automations.settings["tasks"]["proactive"]["enabled"])
        self.assertFalse(self.automations.settings["tasks"]["bilibili"]["enabled"])
        await qq.handle_message("10001", "Owner", "/主动消息 状态")
        self.assertIn("【主动消息状态】", self.send.await_args.args[1])
        reopened = schedules.AutomationManager()
        self.assertFalse(reopened.settings["tasks"]["proactive"]["enabled"])

    async def test_management_api_crud_and_mutation_guard(self):
        auth = ("admin", config.DASHBOARD_TOKEN)
        mutation = {"X-Dashboard-Request": "Talkbot"}
        body = "# 操作规范\n控制台上传的知识文档会立即建立检索索引。".encode()
        denied = await self.client.put("/dashboard/api/documents/guide.md", auth=auth, content=body)
        self.assertEqual(denied.status_code, 403)
        response = await self.client.put("/dashboard/api/documents/guide.md", auth=auth,
                                         headers={**mutation, "Content-Type": "application/octet-stream"}, content=body)
        self.assertEqual(response.status_code, 200)
        docs = (await self.client.get("/dashboard/api/documents", auth=auth)).json()
        self.assertEqual(docs[0]["status"], "indexed")
        grouped = await self.client.put("/dashboard/api/documents/guide.md/group", auth=auth,
                                        headers={**mutation, "Content-Type": "application/json"},
                                        json={"group": "manuals"})
        self.assertEqual(grouped.status_code, 200)
        invalid = await self.client.put("/dashboard/api/documents/bad.exe", auth=auth,
                                        headers=mutation, content=b"invalid")
        self.assertEqual(invalid.status_code, 400)

        self.memory.upsert_user("10001", "Owner")
        self.memory.save("10001", "user", "管理后台中的原始消息")
        await self.memory.index_chat("10001", "user", "管理后台中的长期聊天记忆")
        users = (await self.client.get("/dashboard/api/users", auth=auth)).json()
        self.assertEqual(users[0]["user_id"], "10001")
        history = (await self.client.get("/dashboard/api/users/10001/history", auth=auth)).json()
        self.assertEqual(history["total"], 1)
        deleted = await self.client.delete(f"/dashboard/api/users/10001/history/{history['items'][0]['id']}",
                                           auth=auth, headers=mutation)
        self.assertEqual(deleted.status_code, 200)
        memories = (await self.client.get("/dashboard/api/memories?kind=chat&user_id=10001", auth=auth)).json()
        self.assertEqual(memories["total"], 1)
        self.assertEqual((await self.client.delete(f"/dashboard/api/memories/{memories['items'][0]['id']}",
                                                   auth=auth, headers=mutation)).status_code, 200)

        source = io.BytesIO()
        Image.new("RGB", (4, 4), "green").save(source, format="PNG")
        imgs._save_image(imgs.normalize_image(source.getvalue()), "green")
        image_row = (await self.client.get("/dashboard/api/images", auth=auth)).json()[0]
        image_url = f"/dashboard/api/images/{image_row['filename']}"
        self.assertEqual((await self.client.get(image_url + "/content", auth=auth)).headers["content-type"], "image/jpeg")
        renamed = await self.client.put(image_url + "/name", auth=auth,
                                        headers={**mutation, "Content-Type": "application/json"}, json={"name": "leaf"})
        self.assertEqual(renamed.json()["name"], "leaf")
        self.assertIsNotNone(imgs.find_local_image("leaf"))
        self.assertEqual((await self.client.delete(image_url, auth=auth, headers=mutation)).status_code, 200)

        scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        callbacks = {task_id: AsyncMock(return_value=True) for task_id in self.automations.TASK_IDS}
        await self.automations.bind(scheduler, callbacks)
        settings = await self.client.put("/dashboard/api/automations", auth=auth,
                                         headers={**mutation, "Content-Type": "application/json"},
                                         json={"tasks": {"reminder": {"enabled": True, "times": ["20:20"],
                                                                          "message": "喝水"}}})
        self.assertEqual(settings.status_code, 200)
        run_now = await self.client.post("/dashboard/api/automations/reminder/run", auth=auth,
                                         headers=mutation, content=b"{}")
        self.assertEqual(run_now.json()["status"], "sent")
        callbacks["reminder"].assert_awaited_once()

        removed = await self.client.delete("/dashboard/api/documents/guide.md", auth=auth, headers=mutation)
        self.assertEqual(removed.status_code, 200)
        self.assertNotIn("guide.md", self.rag.documents)

    async def test_image_names_and_legacy_lookup_cannot_escape(self):
        Path("outside.jpg").write_bytes(b"private bytes")
        for name in ("../../outside", "..\\outside", "C:\\outside", "", "bad:name"):
            self.assertIsNone(imgs.find_local_image(name))
            with self.assertRaises(ValueError):
                imgs.image_key(name)
        with patch.object(imgs, "fetch_image_bytes", new=AsyncMock()) as fetch:
            self.assertIsNone(await imgs.download_image("https://example.com/a", "../../outside"))
            fetch.assert_not_awaited()

    async def test_ssrf_private_mixed_dns_ports_and_schemes(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"):
            with patch.object(socket, "getaddrinfo", return_value=[(2, 1, 6, "", (address, 443))]):
                with self.assertRaises(ValueError):
                    await imgs.public_target("https://example.com/a")
        with patch.object(socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("10.0.0.1", 443))]):
            with self.assertRaises(ValueError):
                await imgs.public_target("https://example.com/a")
        for url in ("file:///secret", "http://user:pass@example.com/a", "http://example.com:3000/a"):
            with self.assertRaises(ValueError):
                await imgs.public_target(url)

    async def test_images_must_decode_and_have_bounded_pixels(self):
        with self.assertRaises(Exception):
            imgs.normalize_image(b"this is not an image")
        source = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(source, format="PNG")
        encoded = imgs.normalize_image(source.getvalue())
        self.assertTrue(encoded.startswith(b"\xff\xd8"))
        with patch.object(config, "MAX_IMAGE_PIXELS", 1):
            with self.assertRaises(ValueError):
                imgs.normalize_image(source.getvalue())

    async def test_email_requires_owner_matching_confirmation_and_sends_once(self):
        with patch.object(email, "send_email_to_user", return_value=True) as smtp:
            denied = email.create_draft("10002", "subject", "body")
            self.assertIn("仅管理员", denied)
            email.create_draft("10001", "subject", "body")
            token = email.drafts["10001"].token
            smtp.assert_not_called()
            await email.confirm_draft("10002", token)
            await email.confirm_draft("10001", "wrong")
            smtp.assert_not_called()
            self.assertIn("已提交", await email.confirm_draft("10001", token))
            await email.confirm_draft("10001", token)
            smtp.assert_called_once_with("subject", "body", config.RECEIVER_EMAIL)

    async def test_email_expiry_failure_and_subject_header_validation(self):
        with self.assertRaises(ValueError):
            email.create_draft("10001", "subject\nBcc: attacker", "body")
        email.create_draft("10001", "subject", "body")
        email.drafts["10001"].expires = 0
        with patch.object(email, "send_email_to_user", return_value=False) as smtp:
            await email.confirm_draft("10001", "expired")
            smtp.assert_not_called()
            email.create_draft("10001", "subject", "body")
            token = email.drafts["10001"].token
            self.assertIn("未确认成功", await email.confirm_draft("10001", token))
            self.assertNotIn("10001", email.drafts)

    async def test_injected_legacy_action_is_inert(self):
        with patch.object(self.bot, "_ai_call", new=AsyncMock(side_effect=["NO", "[ACTION: SEND_EMAIL|subject|body]"])), patch.object(email, "send_email_to_user") as smtp:
            reply = await self.bot.chat("10001", "test", "hello")
            self.assertNotIn("ACTION", reply)
            self.assertFalse(email.drafts)
            smtp.assert_not_called()

    async def test_email_generation_is_structured_draft_only(self):
        with patch.object(self.bot, "_ai_call", new=AsyncMock(return_value=json.dumps({"subject": "topic", "body": "content"}))) as model, patch.object(email, "send_email_to_user") as smtp:
            response = await self.bot.chat("10001", "test", "发邮件给我")
            self.assertIn("尚未发送", response)
            self.assertTrue(model.call_args.kwargs["json_mode"])
            smtp.assert_not_called()

    async def test_queue_orders_each_user_and_limits_admission(self):
        release, started = asyncio.Event(), asyncio.Event()
        order = []
        async def first():
            order.append(1)
            started.set()
            await release.wait()
            order.append(2)
        async def second():
            order.append(3)
        await self.dispatcher.submit("10001", first)
        await started.wait()
        await self.dispatcher.submit("10001", second)
        await asyncio.sleep(0.01)
        self.assertEqual(order, [1])
        with patch.object(config, "RATE_LIMIT", 2):
            with self.assertRaises(HTTPException) as error:
                await self.dispatcher.submit("10001", second)
            self.assertEqual(error.exception.status_code, 429)
        release.set()
        await self.dispatcher.queue.join()
        self.assertEqual(order, [1, 2, 3])

    async def test_download_pins_checked_ip_and_preserves_tls_host(self):
        source = io.BytesIO()
        Image.new("RGB", (4, 4), "blue").save(source, format="PNG")
        seen = []
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield source.getvalue()
        def handler(request):
            seen.append(request)
            return httpx.Response(200, headers={"Content-Type": "image/png"}, stream=Stream())
        client_class = httpx.AsyncClient
        def factory(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            return client_class(transport=httpx.MockTransport(handler), **kwargs)
        with patch.object(imgs.httpx, "AsyncClient", side_effect=factory), patch.object(socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
            result = await imgs.fetch_image_bytes("https://example.com/picture.png?q=1")
        self.assertTrue(result.startswith(b"\xff\xd8"))
        self.assertEqual(seen[0].url.host, "93.184.216.34")
        self.assertEqual(seen[0].headers["Host"], "example.com")
        self.assertEqual(seen[0].extensions["sni_hostname"], "example.com")
        self.assertEqual(seen[0].url.query, b"q=1")

    async def test_download_rejects_redirect_compression_and_large_streams(self):
        client_class = httpx.AsyncClient
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"a" * 5
                yield b"b" * 6
        cases = [(302, {"Location": "http://127.0.0.1/"}),
                 (200, {"Content-Type": "text/html"}),
                 (200, {"Content-Type": "image/png", "Content-Encoding": "gzip"}),
                 (200, {"Content-Type": "image/png", "Content-Length": "1000"}),
                 (200, {"Content-Type": "image/png"})]
        for status, headers in cases:
            def handler(request):
                return httpx.Response(status, headers=headers, stream=Stream())
            def factory(**kwargs):
                return client_class(transport=httpx.MockTransport(handler), **kwargs)
            with patch.object(imgs.httpx, "AsyncClient", side_effect=factory), patch.object(socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), patch.object(config, "MAX_IMAGE_BYTES", 10):
                with self.assertRaises((ValueError, httpx.HTTPStatusError)):
                    await imgs.fetch_image_bytes("https://example.com/a")

    async def test_image_store_quota_and_safe_name_lookup(self):
        source = io.BytesIO()
        Image.new("RGB", (4, 4), "blue").save(source, format="PNG")
        raw = imgs.normalize_image(source.getvalue())
        with patch.object(config, "MAX_IMAGE_STORE_BYTES", len(raw)):
            path = imgs._save_image(raw, "cat")
            self.assertEqual(imgs.find_local_image("cat"), path)
            with self.assertRaises(ValueError):
                imgs._save_image(raw, "second")
            self.assertEqual(imgs._save_image(raw, "cat"), path)

    async def test_qq_treats_model_cq_as_text_and_checks_api_errors(self):
        client_class = httpx.AsyncClient
        calls, answer = [], {"status": "ok", "retcode": 0}
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=answer)
        def factory(**kwargs):
            return client_class(transport=httpx.MockTransport(handler), **kwargs)
        message = "[CQ:image,file=file:///private.jpg]"
        with patch.object(qq.httpx, "AsyncClient", side_effect=factory):
            await self.real_send("10001", message)
            payload = json.loads(calls[0].content)
            self.assertEqual(payload["message"], [{"type": "text", "data": {"text": message}}])
            self.assertEqual(calls[0].headers["Authorization"], "Bearer test-napcat")
            answer = {"status": "failed", "retcode": 100}
            with self.assertRaises(RuntimeError):
                await self.real_send("10001", "hello")
            answer = {"status": "ok", "retcode": 0}
            draft = "【邮件草稿 · 尚未发送】\n[表情: literal body]"
            await self.real_send("10001", draft)
            self.assertEqual(json.loads(calls[-1].content)["message"], [{"type": "text", "data": {"text": draft}}])

    async def test_failed_intent_cancels_sibling_work(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def memory_lookup(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async def failed_model(*args, **kwargs):
            await started.wait()
            raise ValueError("simulated model failure")
        with patch.object(self.bot, "_ai_call", side_effect=failed_model), patch.object(self.bot.memory, "get_long_term_memory", side_effect=memory_lookup):
            response = await self.bot.chat("10001", "test", "hello")
        self.assertTrue(cancelled.is_set())
        self.assertIn("处理失败", response)

    async def test_normal_conversation_always_receives_matching_knowledge(self):
        source = "--- 来自 guide.md（操作规范）；相关度 0.88 ---\n后台入口是 /dashboard/。"
        with patch.object(aim.pdf_engine, "search_docs", new=AsyncMock(return_value=source)) as search_docs, \
             patch.object(self.bot, "_ai_call", new=AsyncMock(side_effect=["NO", "请打开后台入口。"] )) as model:
            reply = await self.bot._conversation("10001", "Owner", "后台入口在哪里", None, [])
        self.assertEqual(reply, "请打开后台入口。")
        search_docs.assert_awaited_once_with("后台入口在哪里")
        final_messages = model.await_args_list[-1].args[0]
        self.assertTrue(any("guide.md" in item["content"] and "/dashboard/" in item["content"]
                            for item in final_messages))

    async def test_smtp_uses_configured_port_and_verifies_tls(self):
        with patch.object(config, "SMTP_PORT", 2525), patch.object(email.smtplib, "SMTP") as smtp:
            server = smtp.return_value
            server.send_message.return_value = {}
            self.assertTrue(email.send_email_to_user("subject", "body", "owner@example.invalid"))
            smtp.assert_called_once_with(config.SMTP_HOST, 2525, timeout=10)
            self.assertIsNotNone(server.starttls.call_args.kwargs["context"])
            server.login.assert_called_once()

    async def test_queue_full_does_not_claim_event_and_duplicate_ack_survives_limit(self):
        blocker = asyncio.Event()
        async def wait_job():
            await blocker.wait()
        try:
            await self.dispatcher.submit("10001", wait_job, "accepted-event")
            with patch.object(config, "RATE_LIMIT", 1):
                self.assertFalse(await self.dispatcher.submit("10001", wait_job, "accepted-event"))
            with patch.object(self.dispatcher.queue, "full", return_value=True):
                with self.assertRaises(HTTPException) as error:
                    await self.dispatcher.submit("10001", wait_job, "not-accepted")
                self.assertEqual(error.exception.status_code, 503)
            self.assertFalse(self.memory.event_seen("not-accepted"))
        finally:
            blocker.set()

    async def test_app_lifespan_starts_workers_and_shuts_down_cleanly(self):
        main = importlib.import_module("main")
        await self.dispatcher.stop()
        with patch.object(main, "dispatcher", self.dispatcher), patch.object(main, "ai", self.bot), \
             patch.object(main, "automation_manager", self.automations), \
             patch.object(self.bot, "sync_all", new=AsyncMock()):
            async with main.lifespan(main.app):
                self.assertTrue(main.app.state.ready)
                self.assertTrue(self.dispatcher.workers)
                self.assertEqual((await main.health()).status_code, 200)
            self.assertFalse(main.app.state.ready)
            self.assertFalse(self.dispatcher.workers)
            self.assertEqual((await main.health()).status_code, 503)

    async def test_root_redirects_to_dashboard_and_favicon_is_empty(self):
        main = importlib.import_module("main")
        root = await main.root()
        self.assertEqual(root.status_code, 307)
        self.assertEqual(root.headers["location"], "/dashboard/")
        self.assertEqual((await main.favicon()).status_code, 204)

        with patch.object(config, "DASHBOARD_TOKEN", ""):
            public_root = await main.root()
        self.assertEqual(public_root.status_code, 200)
        self.assertNotIn(config.DEEPSEEK_API_KEY.encode(), public_root.body)

    async def test_startup_rejects_weak_secrets_and_empty_whitelist(self):
        config.validate()
        for key, value in (("WEBHOOK_SECRET", "weak"), ("DASHBOARD_TOKEN", "weak"),
                           ("ALLOWED_QQ", frozenset()), ("DEEPSEEK_API_KEY", "")):
            with patch.object(config, key, value), self.assertRaises(ValueError):
                config.validate()


if __name__ == "__main__":
    unittest.main()
