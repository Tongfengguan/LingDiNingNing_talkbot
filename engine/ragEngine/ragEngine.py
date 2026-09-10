"""Hybrid vector/keyword retrieval with scoped metadata and atomic persistence."""
import asyncio
import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
from loguru import logger

from config import config

DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY")


async def get_embedding(text):
    if not DASHSCOPE_API_KEY or not text:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
                headers={"Authorization": f"Bearer {DASHSCOPE_API_KEY}"},
                json={"model": "text-embedding-v2", "input": {"texts": [text[:2000]]}},
            )
            response.raise_for_status()
            return response.json()["output"]["embeddings"][0]["embedding"]
    except Exception as exc:
        logger.warning("Embedding failed ({})", type(exc).__name__)
        return None


def tokenize(text):
    """Small mixed Chinese/Latin tokenizer used for offline keyword retrieval."""
    lowered = text.casefold()
    latin = re.findall(r"[a-z0-9_]{2,}", lowered)
    chinese_runs = re.findall(r"[\u3400-\u9fff]+", lowered)
    chinese = []
    for run in chinese_runs:
        chinese.extend(run if len(run) == 1 else (run[index:index + 2] for index in range(len(run) - 1)))
    return latin + chinese


class RAGEngine:
    VERSION = 3

    def __init__(self, storage_path="data/vector_store.json"):
        self.storage_path = Path(storage_path)
        self.data = []
        self.documents = {}
        self.lock = asyncio.Lock()
        self.generations = {}
        self.load()

    def add_text(self, text, source="unknown", chunk_size=None, overlap=None):
        chunk_size = chunk_size or config.RAG_CHUNK_SIZE
        overlap = config.RAG_CHUNK_OVERLAP if overlap is None else overlap
        if not isinstance(text, str) or not 0 <= overlap < chunk_size:
            raise ValueError("invalid text or chunk overlap")
        chunks = []
        for index in range(0, len(text), chunk_size - overlap):
            part = text[index:index + chunk_size].strip()
            if len(part) > 10:
                chunks.append(part)
        return chunks

    @staticmethod
    def _scope(source, kind, user_id):
        if not isinstance(source, str) or Path(source).name != source:
            raise ValueError("invalid source")
        if kind == "chat":
            if not user_id or source != f"chat_{user_id}":
                raise ValueError("chat source must match its owner")
        elif kind != "document" or user_id is not None:
            raise ValueError("invalid source metadata")
        return {"source": source, "kind": kind, "user_id": user_id}

    @staticmethod
    def _chunk(value, index=0):
        if isinstance(value, str):
            value = {"text": value}
        if not isinstance(value, dict) or not isinstance(value.get("text"), str):
            raise ValueError("invalid chunk")
        text = value["text"].strip()
        if not text:
            raise ValueError("empty chunk")
        page = value.get("page")
        if page is not None and (type(page) is not int or page < 1):
            raise ValueError("invalid page")
        section = value.get("section") or None
        if section is not None and (not isinstance(section, str) or len(section) > 200):
            raise ValueError("invalid section")
        return {"text": text, "page": page, "section": section, "chunk_index": index}

    @staticmethod
    def _id(item):
        identity = json.dumps({key: item.get(key) for key in (
            "source", "kind", "user_id", "text", "page", "section", "chunk_index"
        )}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(identity.encode()).hexdigest()[:24]

    @staticmethod
    def _doc_info(value):
        if isinstance(value, str):
            return {"fingerprint": value, "indexed_at": None, "group": "shared", "chunks": None}
        if not isinstance(value, dict) or not isinstance(value.get("fingerprint"), str):
            raise ValueError("invalid document metadata")
        return {
            "fingerprint": value["fingerprint"],
            "indexed_at": value.get("indexed_at"),
            "group": value.get("group", "shared"),
            "chunks": value.get("chunks"),
        }

    def document_fingerprint(self, source):
        value = self.documents.get(source)
        return self._doc_info(value)["fingerprint"] if value is not None else None

    async def index_chunks(self, chunks, source, *, kind, user_id=None):
        scope = self._scope(source, kind, user_id)
        generation = self.generations.get(source, 0)
        indexed_at = datetime.now(timezone.utc).isoformat()
        existing = {(row["source"], row["kind"], row.get("user_id"), row["text"]) for row in self.data}
        additions = []
        for index, value in enumerate(chunks):
            chunk = self._chunk(value, index)
            key = (source, kind, user_id, chunk["text"])
            if key in existing:
                continue
            vector = await get_embedding(chunk["text"])
            item = dict(scope, **chunk, vec=vector or [], indexed_at=indexed_at, group=None)
            item["id"] = self._id(item)
            additions.append(item)
        if not additions:
            return
        async with self.lock:
            if generation != self.generations.get(source, 0):
                return
            keys = {(row["source"], row["kind"], row.get("user_id"), row["text"]) for row in self.data}
            data = self.data + [row for row in additions
                                if (source, kind, user_id, row["text"]) not in keys]
            if kind == "chat":
                own = [row for row in data if row["kind"] == "chat" and row.get("user_id") == user_id]
                keep = {row["id"] for row in own[-config.MAX_CHAT_CHUNKS_PER_USER:]}
                data = [row for row in data if row["kind"] != "chat" or row.get("user_id") != user_id
                        or row["id"] in keep]
            await self._commit(data, self.documents)

    async def replace_document(self, source, chunks, fingerprint, group="shared"):
        scope = self._scope(source, "document", None)
        if not isinstance(group, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", group):
            raise ValueError("invalid document group")
        indexed_at = datetime.now(timezone.utc).isoformat()
        additions = []
        for index, value in enumerate(chunks):
            chunk = self._chunk(value, index)
            vector = await get_embedding(chunk["text"])
            # Keyword-only chunks are useful when embeddings are unavailable.
            item = dict(scope, **chunk, vec=vector or [], indexed_at=indexed_at, group=group)
            item["id"] = self._id(item)
            additions.append(item)
        if not additions:
            return False
        async with self.lock:
            data = [row for row in self.data if not (row["kind"] == "document" and row["source"] == source)]
            documents = dict(self.documents)
            documents[source] = {
                "fingerprint": fingerprint, "indexed_at": indexed_at,
                "group": group, "chunks": len(additions),
            }
            await self._commit(data + additions, documents)
        return True

    async def remove_chat(self, user_id):
        source = f"chat_{user_id}"
        async with self.lock:
            self.generations[source] = self.generations.get(source, 0) + 1
            data = [row for row in self.data if not (row["kind"] == "chat" and row.get("user_id") == user_id)]
            await self._commit(data, self.documents)

    async def remove_item(self, item_id):
        async with self.lock:
            removed = next((row for row in self.data if row.get("id") == item_id), None)
            data = [row for row in self.data if row.get("id") != item_id]
            if len(data) == len(self.data):
                return False
            documents = dict(self.documents)
            if removed["kind"] == "document":
                source = removed["source"]
                remaining = sum(row["kind"] == "document" and row["source"] == source for row in data)
                if remaining:
                    info = self._doc_info(documents[source])
                    info["chunks"] = remaining
                    documents[source] = info
                else:
                    documents.pop(source, None)
            await self._commit(data, documents)
            return True

    async def remove_document(self, source):
        async with self.lock:
            data = [row for row in self.data if not (row["kind"] == "document" and row["source"] == source)]
            documents = dict(self.documents)
            existed = documents.pop(source, None) is not None
            if existed or len(data) != len(self.data):
                await self._commit(data, documents)
            return existed

    async def set_document_group(self, source, group):
        if not isinstance(group, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", group):
            raise ValueError("知识库分组只能包含字母、数字、下划线和连字符")
        async with self.lock:
            if source not in self.documents:
                return False
            documents = dict(self.documents)
            info = self._doc_info(documents[source])
            info["group"] = group
            documents[source] = info
            data = [dict(row, group=group) if row["kind"] == "document" and row["source"] == source else row
                    for row in self.data]
            await self._commit(data, documents)
            return True

    async def prune_documents(self, present):
        async with self.lock:
            data = [row for row in self.data if row["kind"] != "document" or row["source"] in present]
            documents = {name: info for name, info in self.documents.items() if name in present}
            if len(data) != len(self.data) or documents != self.documents:
                await self._commit(data, documents)

    @staticmethod
    def _keyword_scores(query_tokens, candidates):
        if not query_tokens:
            return [0.0] * len(candidates)
        documents = [Counter(tokenize(row["text"] + " " + (row.get("section") or ""))) for row in candidates]
        lengths = [sum(counter.values()) or 1 for counter in documents]
        average = sum(lengths) / len(lengths)
        scores = []
        for counter, length in zip(documents, lengths):
            score = 0.0
            for token in set(query_tokens):
                frequency = counter[token]
                document_frequency = sum(token in other for other in documents)
                inverse = math.log(1 + (len(documents) - document_frequency + 0.5) / (document_frequency + 0.5))
                score += inverse * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * length / average))
            scores.append(score)
        peak = max(scores, default=0)
        return [score / peak if peak else 0.0 for score in scores]

    @staticmethod
    def _freshness(item):
        value = item.get("indexed_at")
        if not value:
            return 0.0
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(value).astimezone(timezone.utc)
            return math.exp(-max(age.total_seconds(), 0) / (180 * 86400))
        except (TypeError, ValueError):
            return 0.0

    async def search(self, query, top_k=3, *, kind, user_id=None, group=None):
        if kind not in {"chat", "document"} or (kind == "chat" and not user_id):
            raise ValueError("search requires a scope and chat owner")
        candidates = [row for row in self.data if row["kind"] == kind
                      and (kind != "chat" or row.get("user_id") == user_id)
                      and (kind != "document" or group is None or row.get("group", "shared") == group)]
        if not query or not candidates:
            return []
        query_vector = await get_embedding(query)
        keyword_scores = self._keyword_scores(tokenize(query), candidates)
        vector_scores = [0.0] * len(candidates)
        vector_available = False
        if query_vector:
            q = np.asarray(query_vector, dtype=float)
            q_norm = np.linalg.norm(q)
            if np.isfinite(q_norm) and q_norm:
                for index, item in enumerate(candidates):
                    if not item.get("vec"):
                        continue
                    vector = np.asarray(item["vec"], dtype=float)
                    norm = np.linalg.norm(vector)
                    if vector.shape == q.shape and np.isfinite(norm) and norm:
                        vector_scores[index] = max(float(np.dot(q, vector) / (q_norm * norm)), 0.0)
                        vector_available = True
        ranked = []
        for index, item in enumerate(candidates):
            vector_weight = config.RAG_VECTOR_WEIGHT if vector_available else 0.0
            keyword_weight = config.RAG_KEYWORD_WEIGHT + (config.RAG_VECTOR_WEIGHT if not vector_available else 0.0)
            score = (vector_weight * vector_scores[index]
                     + keyword_weight * keyword_scores[index]
                     + config.RAG_FRESHNESS_WEIGHT * self._freshness(item))
            if score >= (0.12 if keyword_scores[index] else 0.35):
                ranked.append((score, dict(item, score=round(score, 4),
                                           vector_score=round(vector_scores[index], 4),
                                           keyword_score=round(keyword_scores[index], 4))))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in ranked[:top_k]]

    def list_items(self, *, kind=None, user_id=None, query="", offset=0, limit=50):
        rows = [row for row in self.data if (kind is None or row["kind"] == kind)
                and (user_id is None or row.get("user_id") == user_id)
                and (not query or query.casefold() in row["text"].casefold()
                     or query.casefold() in row["source"].casefold())]
        rows.reverse()
        return {"total": len(rows), "items": [{key: value for key, value in row.items() if key != "vec"}
                                                for row in rows[offset:offset + limit]]}

    def document_list(self):
        return [{"name": name, **self._doc_info(info)} for name, info in sorted(self.documents.items())]

    def _write(self, data, documents):
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.storage_path.parent,
                                             prefix=".vectors-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump({"version": self.VERSION, "data": data, "documents": documents}, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.storage_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def _commit(self, data, documents):
        task = asyncio.create_task(asyncio.to_thread(self._write, data, documents))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            self.data, self.documents = data, documents
            raise
        self.data, self.documents = data, documents

    def load(self):
        if not self.storage_path.exists():
            return
        try:
            payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
            legacy = isinstance(payload, list)
            version = 1 if legacy else payload.get("version")
            if version not in {1, 2, self.VERSION}:
                raise ValueError("unsupported vector store version")
            rows = payload if legacy else payload["data"]
            for index, original in enumerate(rows):
                row = dict(original)
                source = row["source"]
                if legacy:
                    match = re.fullmatch(r"chat_([0-9]+)", source)
                    if match:
                        row.update(kind="chat", user_id=match.group(1))
                    elif Path(source).name == source:
                        row.update(kind="document", user_id=None)
                    else:
                        continue
                self._scope(source, row["kind"], row.get("user_id"))
                if not isinstance(row.get("text"), str) or not isinstance(row.get("vec", []), list):
                    raise ValueError("invalid vector row")
                row.setdefault("page", None)
                row.setdefault("section", None)
                row.setdefault("chunk_index", index)
                row.setdefault("indexed_at", None)
                row.setdefault("group", "shared" if row["kind"] == "document" else None)
                row["id"] = row.get("id") or self._id(row)
                self.data.append(row)
            raw_documents = {} if legacy else payload.get("documents", {})
            self.documents = {name: self._doc_info(info) for name, info in raw_documents.items()}
        except Exception as exc:
            raise RuntimeError(f"向量库无法加载，请先备份并检查 {self.storage_path}") from exc


rag_engine = RAGEngine()
