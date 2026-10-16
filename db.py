import json
import os

import aiosqlite

DB_PATH = os.getenv("DB_PATH", "data/bot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    chat_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    username TEXT
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    link_preview INTEGER NOT NULL DEFAULT 0,
    silent INTEGER NOT NULL DEFAULT 0,
    send_at INTEGER NOT NULL,
    delete_after INTEGER,
    delete_at INTEGER,
    sent_at INTEGER,
    message_id INTEGER,
    status TEXT NOT NULL DEFAULT 'scheduled',
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status, send_at);
"""

# Posts that still need attention: waiting to be posted, failed, or live with a delete timer.
ACTIVE = (
    "(p.status IN ('scheduled','failed') "
    "OR (p.status='sent' AND p.delete_at IS NOT NULL))"
)


def _post(row):
    if row is None:
        return None
    d = dict(row)
    d["content"] = json.loads(d["content"])
    return d


class DB:
    conn: aiosqlite.Connection

    async def init(self):
        folder = os.path.dirname(DB_PATH)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.conn = await aiosqlite.connect(DB_PATH)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def _q(self, sql, args=()):
        cur = await self.conn.execute(sql, args)
        rows = await cur.fetchall()
        await cur.close()
        return rows

    async def _x(self, sql, args=()):
        cur = await self.conn.execute(sql, args)
        await self.conn.commit()
        n, last = cur.rowcount, cur.lastrowid
        await cur.close()
        return n, last

    # ---- channels ----
    async def add_channel(self, chat_id, title, username):
        await self._x(
            "INSERT OR REPLACE INTO channels (chat_id, title, username) VALUES (?,?,?)",
            (chat_id, title, username),
        )

    async def remove_channel(self, chat_id):
        await self._x("DELETE FROM channels WHERE chat_id=?", (chat_id,))

    async def channels(self):
        return [dict(r) for r in await self._q("SELECT * FROM channels ORDER BY title")]

    async def channel(self, chat_id):
        rows = await self._q("SELECT * FROM channels WHERE chat_id=?", (chat_id,))
        return dict(rows[0]) if rows else None

    # ---- posts ----
    async def create_post(self, **f):
        f["content"] = json.dumps(f["content"], ensure_ascii=False)
        cols = ", ".join(f)
        marks = ", ".join("?" for _ in f)
        _, last = await self._x(f"INSERT INTO posts ({cols}) VALUES ({marks})", tuple(f.values()))
        return last

    async def post(self, pid):
        rows = await self._q("SELECT * FROM posts WHERE id=?", (pid,))
        return _post(rows[0]) if rows else None

    async def set(self, pid, **f):
        if "content" in f:
            f["content"] = json.dumps(f["content"], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in f)
        await self._x(f"UPDATE posts SET {cols} WHERE id=?", (*f.values(), pid))

    async def delete_post(self, pid):
        await self._x("DELETE FROM posts WHERE id=?", (pid,))

    async def claim(self, pid):
        n, _ = await self._x(
            "UPDATE posts SET status='sending' WHERE id=? AND status='scheduled'", (pid,)
        )
        return n == 1

    async def reset_sending(self):
        await self._x("UPDATE posts SET status='scheduled' WHERE status='sending'")

    async def due_posts(self, now):
        rows = await self._q(
            "SELECT * FROM posts WHERE status='scheduled' AND send_at<=? ORDER BY send_at", (now,)
        )
        return [_post(r) for r in rows]

    async def due_deletes(self, now):
        rows = await self._q(
            "SELECT * FROM posts WHERE status='sent' AND delete_at IS NOT NULL AND delete_at<=?",
            (now,),
        )
        return [_post(r) for r in rows]

    async def count_active(self):
        rows = await self._q(f"SELECT COUNT(*) AS n FROM posts p WHERE {ACTIVE}")
        return rows[0]["n"]

    async def active(self, limit, offset):
        rows = await self._q(
            f"""SELECT p.*, c.title AS ch_title FROM posts p
                LEFT JOIN channels c ON c.chat_id = p.chat_id
                WHERE {ACTIVE}
                ORDER BY (p.status='sent'),
                         CASE WHEN p.status='sent' THEN p.delete_at ELSE p.send_at END
                LIMIT ? OFFSET ?""",
            (limit, offset),
        )
        return [_post(r) for r in rows]


db = DB()
