"""Structured knowledge-document ingestion and source-aware retrieval."""
import asyncio
import csv
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from loguru import logger
from pypdf import PdfReader

from config import config
from engine.ragEngine.ragEngine import rag_engine


class PDFEngine:
    EXTENSIONS = {".pdf", ".txt", ".md", ".docx", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".csv"}

    def __init__(self, docs_dir="assets/docs"):
        self.docs_dir = Path(docs_dir).resolve()
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()

    def safe_path(self, filename):
        if not isinstance(filename, str) or Path(filename).name != filename or len(filename) > 180:
            raise ValueError("无效文档名称")
        path = (self.docs_dir / filename).resolve()
        if path.parent != self.docs_dir or path.suffix.lower() not in self.EXTENSIONS:
            raise ValueError("不支持的文档路径或类型")
        return path

    @staticmethod
    def _blocks_from_markdown(text):
        blocks, section, buffer = [], None, []
        fenced = False
        for line in text.splitlines():
            if line.lstrip().startswith("```"):
                fenced = not fenced
            heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
            if heading and not fenced:
                if buffer:
                    blocks.append({"text": "\n".join(buffer).strip(), "section": section})
                    buffer = []
                section = heading.group(1)[:200]
                buffer.append(line)
            elif not line.strip() and buffer and not fenced:
                blocks.append({"text": "\n".join(buffer).strip(), "section": section})
                buffer = []
            else:
                buffer.append(line)
        if buffer:
            blocks.append({"text": "\n".join(buffer).strip(), "section": section})
        return [block for block in blocks if block["text"]]

    @staticmethod
    def _blocks_from_code(text):
        blocks, current, section = [], [], "module"
        for line in text.splitlines():
            match = re.match(r"^\s*(?:async\s+def|def|class|function|export\s+function)\s+([\w$]+)", line)
            if match and current:
                blocks.append({"text": "\n".join(current).strip(), "section": section})
                current = []
            if match:
                section = match.group(1)[:200]
            current.append(line)
        if current:
            blocks.append({"text": "\n".join(current).strip(), "section": section})
        return [block for block in blocks if block["text"]]

    def _read_document(self, filename, previous_fingerprint=None):
        path = self.safe_path(filename)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > config.MAX_DOC_BYTES:
            raise ValueError("文档路径或体积不允许")
        raw = path.read_bytes()
        if len(raw) > config.MAX_DOC_BYTES:
            raise ValueError("文档过大")
        digest = hashlib.sha256(raw).hexdigest()
        if digest == previous_fingerprint:
            return digest, None
        suffix = path.suffix.lower()
        blocks = []
        if suffix == ".pdf":
            reader = PdfReader(io.BytesIO(raw))
            for page_number, page in enumerate(reader.pages, 1):
                text = (page.extract_text() or "").strip()
                if text:
                    blocks.append({"text": text, "page": page_number, "section": f"第 {page_number} 页"})
        elif suffix == ".docx":
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                member = archive.getinfo("word/document.xml")
                if member.file_size > config.MAX_DOC_BYTES:
                    raise ValueError("DOCX 解压内容过大")
                root = ElementTree.fromstring(archive.read(member))
                ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                section = None
                for paragraph in root.findall(".//w:p", ns):
                    text = "".join(node.text or "" for node in paragraph.findall(".//w:t", ns)).strip()
                    if not text:
                        continue
                    style = paragraph.find("./w:pPr/w:pStyle", ns)
                    style_name = "" if style is None else style.attrib.get(f"{{{ns['w']}}}val", "")
                    if style_name.lower().startswith(("heading", "标题")):
                        section = text[:200]
                    blocks.append({"text": text, "section": section})
        else:
            text = raw.decode("utf-8-sig")
            if suffix == ".md":
                blocks = self._blocks_from_markdown(text)
            elif suffix in {".py", ".js", ".ts"}:
                blocks = self._blocks_from_code(text)
            elif suffix == ".json":
                parsed = json.loads(text)
                blocks = [{"text": json.dumps(parsed, ensure_ascii=False, indent=2), "section": "JSON"}]
            elif suffix == ".csv":
                rows = list(csv.reader(io.StringIO(text)))
                if len(rows) > 10000:
                    raise ValueError("CSV 行数过多")
                blocks = [{"text": " | ".join(row), "section": "CSV"} for row in rows if row]
            else:
                blocks = self._blocks_from_markdown(text)
        total = sum(len(block["text"]) for block in blocks)
        if total > config.MAX_DOC_CHARS:
            raise ValueError("文档文本过长")
        return digest, blocks

    def chunk_blocks(self, blocks):
        result = []
        for block in blocks:
            prefix = f"[{block['section']}]\n" if block.get("section") else ""
            content = block["text"]
            available = max(config.RAG_CHUNK_SIZE - len(prefix), 100)
            pieces = rag_engine.add_text(content, chunk_size=available, overlap=min(config.RAG_CHUNK_OVERLAP, available - 1))
            for piece in pieces:
                result.append({"text": prefix + piece, "page": block.get("page"), "section": block.get("section")})
        return result

    async def sync_docs(self, only=None, force=False):
        async with self.lock:
            files = {path.name for path in self.docs_dir.iterdir()
                     if path.is_file() and not path.is_symlink() and path.suffix.lower() in self.EXTENSIONS}
            if len(files) > config.MAX_DOC_FILES:
                raise ValueError("知识库文件数量超过限制")
            if only is None:
                await rag_engine.prune_documents(files)
                selected = files
            else:
                self.safe_path(only)
                selected = {only} if only in files else set()
            results = {"indexed": [], "unchanged": [], "failed": {}}
            for filename in sorted(selected):
                try:
                    previous = None if force else rag_engine.document_fingerprint(filename)
                    fingerprint, blocks = await asyncio.to_thread(self._read_document, filename, previous)
                    if blocks is None:
                        results["unchanged"].append(filename)
                        continue
                    chunks = self.chunk_blocks(blocks)
                    group = rag_engine._doc_info(rag_engine.documents[filename])["group"] if filename in rag_engine.documents else "shared"
                    if await rag_engine.replace_document(filename, chunks, fingerprint, group):
                        results["indexed"].append(filename)
                    else:
                        results["failed"][filename] = "没有可索引文本"
                except Exception as exc:
                    results["failed"][filename] = type(exc).__name__
                    logger.warning("文档同步失败 {} ({})", filename, type(exc).__name__)
            return results

    async def search(self, query, top_k=5, group=None):
        return await rag_engine.search(query, top_k=top_k, kind="document", group=group)

    async def search_docs(self, query):
        results = await self.search(query, top_k=5)
        references = []
        for item in results:
            location = item["source"]
            if item.get("page"):
                location += f"，第 {item['page']} 页"
            if item.get("section"):
                location += f"，{item['section']}"
            references.append(f"--- 来自 {location}；相关度 {item['score']:.2f} ---\n{item['text']}")
        return "\n\n".join(references)


pdf_engine = PDFEngine()
