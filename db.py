import os

from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URI = os.getenv("MONGODB_URI") or os.getenv("MONGO_URL") or os.getenv("MONGODB_URL")
DB_NAME = os.getenv("MONGODB_DB", "peterpostx")

# Posts that still need attention: waiting to be posted, failed, or live with a delete timer.
ACTIVE = {
    "$or": [
        {"status": {"$in": ["scheduled", "failed"]}},
        {"status": "sent", "delete_at": {"$ne": None}},
    ]
}


def _post(doc):
    if doc is None:
        return None
    d = dict(doc)
    d["id"] = d.pop("_id")
    return d


class DB:
    async def init(self):
        if not MONGO_URI:
            raise SystemExit("Set MONGODB_URI (your MongoDB connection string).")
        self.client = AsyncIOMotorClient(MONGO_URI)
        await self.client.admin.command("ping")
        self.db = self.client[DB_NAME]
        await self.db.posts.create_index([("status", 1), ("send_at", 1)])

    # ---- channels ----
    async def add_channel(self, chat_id, title, username):
        await self.db.channels.update_one(
            {"_id": chat_id},
            {"$set": {"title": title, "username": username}},
            upsert=True,
        )

    async def remove_channel(self, chat_id):
        await self.db.channels.delete_one({"_id": chat_id})

    async def channels(self):
        docs = await self.db.channels.find().sort("title", 1).to_list(None)
        return [{"chat_id": d["_id"], "title": d["title"], "username": d.get("username")} for d in docs]

    async def channel(self, chat_id):
        d = await self.db.channels.find_one({"_id": chat_id})
        if not d:
            return None
        return {"chat_id": d["_id"], "title": d["title"], "username": d.get("username")}

    # ---- posts ----
    async def _next_id(self):
        d = await self.db.counters.find_one_and_update(
            {"_id": "posts"}, {"$inc": {"n": 1}}, upsert=True, return_document=True
        )
        return d["n"]

    async def create_post(self, **f):
        pid = await self._next_id()
        doc = {
            "_id": pid,
            "link_preview": 0,
            "silent": 0,
            "delete_after": None,
            "delete_at": None,
            "sent_at": None,
            "message_id": None,
            "status": "scheduled",
            "error": None,
        }
        doc.update(f)
        await self.db.posts.insert_one(doc)
        return pid

    async def post(self, pid):
        return _post(await self.db.posts.find_one({"_id": pid}))

    async def set(self, pid, **f):
        await self.db.posts.update_one({"_id": pid}, {"$set": f})

    async def delete_post(self, pid):
        await self.db.posts.delete_one({"_id": pid})

    async def claim(self, pid):
        r = await self.db.posts.update_one(
            {"_id": pid, "status": "scheduled"}, {"$set": {"status": "sending"}}
        )
        return r.modified_count == 1

    async def reset_sending(self):
        await self.db.posts.update_many({"status": "sending"}, {"$set": {"status": "scheduled"}})

    async def due_posts(self, now):
        docs = await self.db.posts.find(
            {"status": "scheduled", "send_at": {"$lte": now}}
        ).sort("send_at", 1).to_list(None)
        return [_post(d) for d in docs]

    async def due_deletes(self, now):
        docs = await self.db.posts.find(
            {"status": "sent", "delete_at": {"$ne": None, "$lte": now}}
        ).to_list(None)
        return [_post(d) for d in docs]

    async def count_active(self):
        return await self.db.posts.count_documents(ACTIVE)

    async def active(self, limit, offset):
        docs = await self.db.posts.find(ACTIVE).to_list(None)
        posts = [_post(d) for d in docs]
        titles = {c["chat_id"]: c["title"] for c in await self.channels()}
        for p in posts:
            p["ch_title"] = titles.get(p["chat_id"])
        posts.sort(
            key=lambda p: (
                p["status"] == "sent",
                p["delete_at"] if p["status"] == "sent" else p["send_at"],
            )
        )
        return posts[offset : offset + limit]


db = DB()
