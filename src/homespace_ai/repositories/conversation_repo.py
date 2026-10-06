"""Private AI conversations in the existing MongoDB instance."""

from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from uuid import uuid4

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient

from homespace_ai.core.config import get_settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ConversationRepository:
    def __init__(self, mongo_uri: str) -> None:
        self.client = AsyncMongoClient(mongo_uri, serverSelectionTimeoutMS=3000, tz_aware=True)
        self.collection = self.client.get_default_database()["ai_conversations"]

    async def ensure_indexes(self) -> None:
        await self.collection.create_index(
            [("ownerId", ASCENDING), ("updatedAt", DESCENDING)]
        )

    async def ping(self) -> None:
        await self.client.admin.command("ping")

    async def close(self) -> None:
        await self.client.close()

    async def create(self, owner_id: str) -> dict[str, Any]:
        now = _now()
        conversation = {
            "_id": str(uuid4()),
            "ownerId": owner_id,
            "title": "Đoạn chat mới",
            "isPinned": False,
            "createdAt": now,
            "updatedAt": now,
            "messages": [],
        }
        await self.collection.insert_one(conversation)
        return conversation

    async def list_for_user(self, owner_id: str) -> list[dict[str, Any]]:
        cursor = self.collection.find(
            {"ownerId": owner_id}, {"messages": 0, "ownerId": 0}
        ).sort([("isPinned", DESCENDING), ("updatedAt", DESCENDING)])
        return [document async for document in cursor]

    async def get(self, owner_id: str, conversation_id: str) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {"_id": conversation_id, "ownerId": owner_id}
        )

    async def append_message(
        self, owner_id: str, conversation_id: str, *, role: str,
        content: str, status: str | None = None,
    ) -> bool:
        now = _now()
        message = {
            "id": str(uuid4()),
            "role": role,
            "content": content,
            "createdAt": now,
            "status": status,
        }
        result = await self.collection.update_one(
            {"_id": conversation_id, "ownerId": owner_id},
            {"$push": {"messages": message}, "$set": {"updatedAt": now}},
        )
        if result.matched_count and role == "user":
            await self.collection.update_one(
                {"_id": conversation_id, "ownerId": owner_id, "title": "Đoạn chat mới"},
                {"$set": {"title": content[:60]}},
            )
        return bool(result.matched_count)

    async def set_pinned(self, owner_id: str, conversation_id: str, pinned: bool) -> bool:
        result = await self.collection.update_one(
            {"_id": conversation_id, "ownerId": owner_id},
            {"$set": {"isPinned": pinned, "updatedAt": _now()}},
        )
        return bool(result.matched_count)

    async def delete(self, owner_id: str, conversation_id: str) -> bool:
        result = await self.collection.delete_one(
            {"_id": conversation_id, "ownerId": owner_id}
        )
        return bool(result.deleted_count)


@lru_cache
def get_conversation_repository() -> ConversationRepository:
    return ConversationRepository(get_settings().ai_mongodb_uri)
