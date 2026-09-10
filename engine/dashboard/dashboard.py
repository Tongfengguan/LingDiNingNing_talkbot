"""Authenticated dashboard and same-origin administration API."""
import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from config import config
from engine.aiEngine.aiEngine import ai
from engine.dispatcher import dispatcher
from engine.imageUtils.imageUtils import delete_image, get_image_path, list_images, rename_image
from engine.pdfEngine.pdfEngine import pdf_engine
from engine.ragEngine.ragEngine import rag_engine
from engine.scheduleManager import automation_manager
from engine.security import dashboard_auth

router = APIRouter(prefix="/dashboard", dependencies=[Depends(dashboard_auth)])
STATIC_DIR = Path(__file__).with_name("static")


def mutation_guard(request: Request):
    if request.headers.get("x-dashboard-request") != "Talkbot":
        raise HTTPException(403, "缺少同源管理请求标记")


def safe_user(user_id):
    if not isinstance(user_id, str) or not re.fullmatch(r"[0-9]{1,20}", user_id) or int(user_id) <= 0:
        raise HTTPException(400, "无效用户 ID")
    return user_id


async def json_body(request, limit=64 * 1024):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, "请求过大")
        body.extend(chunk)
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "无效 JSON") from None
    if not isinstance(value, dict):
        raise HTTPException(400, "JSON 必须是对象")
    return value


@router.get("/", response_class=HTMLResponse)
async def index():
    html = (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")
    html = html.replace("{{BOT_NAME}}", config.BOT_NAME.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return HTMLResponse(html, headers={
        "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
    })


@router.get("/static/{filename}")
async def static_asset(filename: str):
    allowed = {"dashboard.css": "text/css; charset=utf-8", "dashboard.js": "text/javascript; charset=utf-8"}
    if filename not in allowed:
        raise HTTPException(404)
    return Response((STATIC_DIR / filename).read_bytes(), media_type=allowed[filename],
                    headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


@router.get("/api/overview")
async def overview():
    memory_stats = await asyncio.to_thread(ai.memory.stats)
    images = await asyncio.to_thread(list_images)
    return {
        **memory_stats,
        "documents": len(rag_engine.documents),
        "document_chunks": sum(row["kind"] == "document" for row in rag_engine.data),
        "memory_chunks": sum(row["kind"] == "chat" for row in rag_engine.data),
        "images": len(images), "image_bytes": sum(item["size"] for item in images),
        "queue": dispatcher.queue.qsize(), "queue_capacity": config.QUEUE_SIZE,
        "enabled_tasks": sum(task["enabled"] for task in automation_manager.settings["tasks"].values()),
    }


@router.get("/api/documents")
async def documents():
    files = {path.name: path.stat() for path in pdf_engine.docs_dir.iterdir()
             if path.is_file() and not path.is_symlink() and path.suffix.lower() in pdf_engine.EXTENSIONS}
    indexed = {item["name"]: item for item in rag_engine.document_list()}
    return [{"name": name, "size": stat.st_size, "modified_at": stat.st_mtime,
             "status": "indexed" if name in indexed else "pending", **indexed.get(name, {})}
            for name, stat in sorted(files.items())]


@router.put("/api/documents/{filename}", dependencies=[Depends(mutation_guard)])
async def upload_document(filename: str, request: Request):
    try:
        path = pdf_engine.safe_path(filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    existing = {item.name for item in pdf_engine.docs_dir.iterdir()
                if item.is_file() and item.suffix.lower() in pdf_engine.EXTENSIONS}
    if filename not in existing and len(existing) >= config.MAX_DOC_FILES:
        raise HTTPException(409, "知识库文件数量已达上限")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=pdf_engine.docs_dir, prefix=".upload-", delete=False) as handle:
            temporary = Path(handle.name)
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > config.MAX_DOC_BYTES:
                    raise HTTPException(413, "文档过大")
                handle.write(chunk)
        if size == 0:
            raise HTTPException(400, "文档为空")
        os.replace(temporary, path)
        temporary = None
        result = await pdf_engine.sync_docs(only=filename, force=True)
        if result["failed"]:
            raise HTTPException(422, f"文件已保存，但索引失败：{result['failed'][filename]}")
        return {"status": "ok", "result": result}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@router.post("/api/documents/{filename}/reindex", dependencies=[Depends(mutation_guard)])
async def reindex_document(filename: str):
    try:
        pdf_engine.safe_path(filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    result = await pdf_engine.sync_docs(only=filename, force=True)
    if not result["indexed"]:
        raise HTTPException(422, "重新索引失败或文档不存在")
    return result


@router.put("/api/documents/{filename}/group", dependencies=[Depends(mutation_guard)])
async def document_group(filename: str, request: Request):
    payload = await json_body(request)
    try:
        if not await rag_engine.set_document_group(filename, payload.get("group")):
            raise HTTPException(404, "文档尚未索引")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"status": "ok"}


@router.delete("/api/documents/{filename}", dependencies=[Depends(mutation_guard)])
async def remove_document(filename: str):
    try:
        path = pdf_engine.safe_path(filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if not path.is_file() or path.is_symlink():
        raise HTTPException(404, "文档不存在")
    temporary = path.with_name(f".delete-{filename}")
    if temporary.exists():
        raise HTTPException(409, "该文档正在被处理")
    path.replace(temporary)
    try:
        await rag_engine.remove_document(filename)
        temporary.unlink()
    except Exception:
        temporary.replace(path)
        raise
    return {"status": "ok"}


@router.get("/api/users")
async def users():
    return await asyncio.to_thread(ai.memory.list_users)


@router.get("/api/users/{user_id}/history")
async def history(user_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
    return await asyncio.to_thread(ai.memory.list_history, safe_user(user_id), offset, limit)


@router.delete("/api/users/{user_id}/history", dependencies=[Depends(mutation_guard)])
async def clear_history(user_id: str):
    await ai.memory.clear(safe_user(user_id))
    return {"status": "ok"}


@router.delete("/api/users/{user_id}/history/{item_id}", dependencies=[Depends(mutation_guard)])
async def remove_history_item(user_id: str, item_id: int):
    if not await asyncio.to_thread(ai.memory.delete_history_item, safe_user(user_id), item_id):
        raise HTTPException(404, "消息不存在")
    return {"status": "ok"}


@router.get("/api/memories")
async def memories(user_id: str | None = None, kind: str | None = None, query: str = "",
                   offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
    if user_id is not None:
        safe_user(user_id)
    if kind not in {None, "chat", "document"}:
        raise HTTPException(400, "无效记忆类型")
    return rag_engine.list_items(kind=kind, user_id=user_id, query=query[:200], offset=offset, limit=limit)


@router.delete("/api/memories/{item_id}", dependencies=[Depends(mutation_guard)])
async def remove_memory(item_id: str):
    if not await rag_engine.remove_item(item_id):
        raise HTTPException(404, "记忆片段不存在")
    return {"status": "ok"}


@router.get("/api/images")
async def images():
    return await asyncio.to_thread(list_images)


@router.get("/api/images/{filename}/content")
async def image_content(filename: str):
    try:
        path = await asyncio.to_thread(get_image_path, filename)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "图片不存在") from None
    media = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
             ".gif": "image/gif", ".webp": "image/webp"}.get(path.suffix.lower(), "application/octet-stream")
    return Response(await asyncio.to_thread(path.read_bytes), media_type=media,
                    headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})


@router.put("/api/images/{filename}/name", dependencies=[Depends(mutation_guard)])
async def update_image_name(filename: str, request: Request):
    payload = await json_body(request)
    try:
        name = await asyncio.to_thread(rename_image, filename, payload.get("name"))
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from None
    return {"status": "ok", "name": name}


@router.delete("/api/images/{filename}", dependencies=[Depends(mutation_guard)])
async def remove_image(filename: str):
    try:
        await asyncio.to_thread(delete_image, filename)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "图片不存在") from None
    return {"status": "ok"}


@router.get("/api/automations")
async def automations():
    return automation_manager.snapshot()


@router.put("/api/automations", dependencies=[Depends(mutation_guard)])
async def update_automations(request: Request):
    try:
        return await automation_manager.update(await json_body(request))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/api/automations/{task_id}/run", dependencies=[Depends(mutation_guard)])
async def run_automation(task_id: str):
    if task_id not in automation_manager.TASK_IDS:
        raise HTTPException(404, "任务不存在")
    try:
        return await automation_manager.execute(task_id, force=True)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(503, str(exc)) from None
