import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Lock

from config import config
from engine.ragEngine.ragEngine import rag_engine


class MemoryManager:
    def __init__(self, db_path="data/memory.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.lock = Lock()
        self._exec("""CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, role TEXT, content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        self._exec("""CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY, nickname TEXT, last_active DATETIME)""")
        self._exec("CREATE INDEX IF NOT EXISTS history_user_id ON chat_history(user_id, id)")
        self._exec("CREATE TABLE IF NOT EXISTS webhook_events (event_key TEXT PRIMARY KEY, received_at INTEGER)")

    def _exec(self, sql, params=()):
        with self.lock, closing(sqlite3.connect(self.db_path, timeout=10)) as conn, conn:
            return conn.execute(sql, params).fetchall()

    def claim_event(self, event_key, now):
        with self.lock, closing(sqlite3.connect(self.db_path, timeout=10)) as conn, conn:
            conn.execute("DELETE FROM webhook_events WHERE received_at < ?", (now - 2 * config.EVENT_MAX_AGE,))
            result = conn.execute("INSERT OR IGNORE INTO webhook_events VALUES (?, ?)", (event_key, now))
            return result.rowcount == 1

    def event_seen(self, event_key):
        return bool(self._exec("SELECT 1 FROM webhook_events WHERE event_key=?", (event_key,)))

    def save(self, user_id, role, content):
        self._exec("INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content))

    def save_turn(self, user_id, message, reply):
        with self.lock, closing(sqlite3.connect(self.db_path, timeout=10)) as conn, conn:
            conn.executemany("INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)",
                             [(user_id, "user", message), (user_id, "assistant", reply)])

    async def index_chat(self, user_id, role, content):
        if content and len(content) > 10:
            chunks = rag_engine.add_text(f"{role}: {content}")
            await rag_engine.index_chunks(chunks, source=f"chat_{user_id}", kind="chat", user_id=user_id)

    def get_history(self, user_id, limit=20):
        rows = self._exec("SELECT role, content FROM chat_history WHERE user_id=? ORDER BY id DESC LIMIT ?",
                          (user_id, limit))
        selected, remaining = [], config.MAX_HISTORY_CHARS
        for role, content in rows:
            if len(content) > remaining:
                break
            selected.append({"role": role, "content": content})
            remaining -= len(content)
        return list(reversed(selected))

    def list_users(self):
        rows = self._exec("""SELECT u.user_id, u.nickname, u.last_active, COUNT(h.id)
            FROM users u LEFT JOIN chat_history h ON h.user_id=u.user_id
            GROUP BY u.user_id ORDER BY u.last_active DESC""")
        return [{"user_id": user_id, "nickname": nickname, "last_active": last_active,
                 "messages": count} for user_id, nickname, last_active, count in rows]

    def list_history(self, user_id, offset=0, limit=50):
        total = self._exec("SELECT COUNT(*) FROM chat_history WHERE user_id=?", (user_id,))[0][0]
        rows = self._exec("""SELECT id, role, content, timestamp FROM chat_history
            WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?""", (user_id, limit, offset))
        return {"total": total, "items": [
            {"id": row_id, "role": role, "content": content, "timestamp": timestamp}
            for row_id, role, content, timestamp in rows
        ]}

    def delete_history_item(self, user_id, item_id):
        with self.lock, closing(sqlite3.connect(self.db_path, timeout=10)) as conn, conn:
            result = conn.execute("DELETE FROM chat_history WHERE user_id=? AND id=?", (user_id, item_id))
            return result.rowcount == 1

    def last_active(self, user_id):
        rows = self._exec("SELECT last_active FROM users WHERE user_id=?", (user_id,))
        return rows[0][0] if rows else None

    def recent_history(self, user_id, hours=24, limit=100):
        rows = self._exec("""SELECT role, content, timestamp FROM chat_history
            WHERE user_id=? AND timestamp >= datetime('now', ?)
            ORDER BY id ASC LIMIT ?""", (user_id, f"-{hours} hours", limit))
        return [{"role": role, "content": content, "timestamp": timestamp} for role, content, timestamp in rows]

    def stats(self):
        history = self._exec("SELECT COUNT(*), COUNT(DISTINCT user_id) FROM chat_history")[0]
        users = self._exec("SELECT COUNT(*) FROM users")[0][0]
        return {"messages": history[0], "active_users": history[1], "known_users": users}

    async def get_long_term_memory(self, user_id, query, top_k=2):
        results = await rag_engine.search(query, top_k=top_k, kind="chat", user_id=user_id)
        return "\n".join(r["text"] for r in results)

    def upsert_user(self, user_id, nickname):
        self._exec("""INSERT INTO users (user_id, nickname, last_active) VALUES (?, ?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET nickname=excluded.nickname, last_active=datetime('now')""",
            (user_id, nickname))

    async def clear(self, user_id):
        await rag_engine.remove_chat(user_id)
        await asyncio.to_thread(self._exec, "DELETE FROM chat_history WHERE user_id=?", (user_id,))
