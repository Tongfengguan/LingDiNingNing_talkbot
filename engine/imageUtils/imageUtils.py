import asyncio
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import tempfile
from pathlib import Path
from threading import Lock
from datetime import datetime, timezone

import httpx
from PIL import Image
from loguru import logger

from config import config

storage_lock = Lock()


def image_root():
    return Path("assets/images").resolve()


def image_key(name):
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ValueError("图片名应为 1–80 字符")
    if re.search(r'[\\/:.\x00-\x1f<>|?*"]', name):
        raise ValueError("图片名不能含路径或特殊字符")
    return hashlib.sha256(name.strip().encode("utf-8")).hexdigest()


def _manifest_path():
    return Path("data/image_manifest.json").resolve()


def _load_manifest():
    path = _manifest_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("invalid image manifest")
        return {name: filename for name, filename in value.items()
                if isinstance(name, str) and isinstance(filename, str) and Path(filename).name == filename}
    except Exception as exc:
        raise RuntimeError("图片索引损坏，请检查 data/image_manifest.json") from exc


def _write_manifest(value):
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".images-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def public_target(url):
    parsed = httpx.URL(url)
    if parsed.scheme not in {"http", "https"} or not parsed.host or parsed.userinfo:
        raise ValueError("只允许公开 HTTP(S) 图片地址")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in {80, 443}:
        raise ValueError("不允许该端口")
    addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.host, port, type=socket.SOCK_STREAM)
    ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
    if not ips or any(not ip.is_global or ip.is_multicast or ip.is_unspecified
                      or (isinstance(ip, ipaddress.IPv6Address) and
                          (ip.ipv4_mapped is not None or ip.sixtofour is not None or ip.teredo is not None))
                      for ip in ips):
        raise ValueError("不允许本机、内网或保留地址")
    # Connect to the checked IP, preserving TLS SNI and Host. No second DNS lookup.
    target = parsed.copy_with(host=str(ips[0]))
    host = parsed.netloc.decode("ascii")
    return target, host, parsed.host


def normalize_image(raw):
    with Image.open(io.BytesIO(raw)) as img:
        if img.format not in {"JPEG", "PNG", "GIF", "WEBP"}:
            raise ValueError("不支持的图片格式")
        if img.width * img.height > config.MAX_IMAGE_PIXELS:
            raise ValueError("图片像素过大")
        img.load()
        output = io.BytesIO()
        img.convert("RGB").save(output, format="JPEG", quality=88)
        result = output.getvalue()
        if len(result) > config.MAX_IMAGE_BYTES:
            raise ValueError("转换后图片过大")
        return result


async def fetch_image_bytes(url):
    async with asyncio.timeout(20):
        target, host, sni = await public_target(url)
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", target, headers={"Host": host, "Accept-Encoding": "identity"},
                                     extensions={"sni_hostname": sni}) as resp:
                resp.raise_for_status()
                if resp.status_code != 200 or not resp.headers.get("content-type", "").lower().startswith("image/"):
                    raise ValueError("响应不是图片")
                length = resp.headers.get("content-length")
                if length and int(length) > config.MAX_IMAGE_BYTES:
                    raise ValueError("图片过大")
                if resp.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("不接受压缩 HTTP 响应")
                raw = bytearray()
                async for chunk in resp.aiter_raw():
                    if len(raw) + len(chunk) > config.MAX_IMAGE_BYTES:
                        raise ValueError("图片过大")
                    raw.extend(chunk)
        return await asyncio.to_thread(normalize_image, bytes(raw))


def _save_image(raw, name):
    with storage_lock:
        return _save_image_locked(raw, name)


def _save_image_locked(raw, name):
    root = image_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{image_key(name)}.jpg"
    if path.resolve().parent != root:
        raise ValueError("图片路径越界")
    used = sum(p.stat().st_size for p in root.iterdir() if p.is_file() and p != path)
    if used + len(raw) > config.MAX_IMAGE_STORE_BYTES:
        raise ValueError("图片库容量已满，请清理不再使用的图片")
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".image-", delete=False) as handle:
            tmp = Path(handle.name)
            handle.write(raw)
        os.replace(tmp, path)
        manifest = _load_manifest()
        manifest[name.strip()] = path.name
        _write_manifest(manifest)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return str(path)


async def download_image(url, name):
    try:
        image_key(name)
        raw = await fetch_image_bytes(url)
        return await asyncio.to_thread(_save_image, raw, name)
    except Exception as exc:
        logger.warning("图片下载失败 ({})", type(exc).__name__)
        return None


def find_local_image(name):
    try:
        key = image_key(name)
    except ValueError:
        return None
    root = image_root()
    with storage_lock:
        filename = _load_manifest().get(name.strip())
        if filename:
            path = (root / filename).resolve()
            if path.parent == root and path.is_file():
                return str(path)
    # Read existing named images for backwards compatibility, within the same directory.
    for stem in (key, name.strip()):
        for ext in (".jpg", ".png", ".gif", ".jpeg", ".webp"):
            path = (root / f"{stem}{ext}").resolve()
            if path.parent == root and path.is_file():
                return str(path)
    return None


def list_images():
    root = image_root()
    root.mkdir(parents=True, exist_ok=True)
    with storage_lock:
        manifest = _load_manifest()
        aliases = {}
        for logical_name, filename in manifest.items():
            aliases.setdefault(filename, []).append(logical_name)
        items = []
        for path in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
            if not path.is_file() or path.is_symlink() or path.name == ".gitkeep":
                continue
            items.append({"filename": path.name, "names": aliases.get(path.name, [path.stem]),
                          "size": path.stat().st_size,
                          "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()})
        return items


def get_image_path(filename):
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise ValueError("无效图片名称")
    path = (image_root() / filename).resolve()
    if path.parent != image_root() or not path.is_file() or path.is_symlink():
        raise FileNotFoundError(filename)
    return path


def rename_image(filename, logical_name):
    image_key(logical_name)
    path = get_image_path(filename)
    with storage_lock:
        manifest = _load_manifest()
        manifest = {name: stored for name, stored in manifest.items()
                    if stored != path.name and name != logical_name.strip()}
        manifest[logical_name.strip()] = path.name
        _write_manifest(manifest)
    return logical_name.strip()


def delete_image(filename):
    path = get_image_path(filename)
    with storage_lock:
        # Resolve again while holding the mutation lock.
        path = get_image_path(filename)
        path.unlink()
        manifest = {name: stored for name, stored in _load_manifest().items() if stored != filename}
        _write_manifest(manifest)
    return True
