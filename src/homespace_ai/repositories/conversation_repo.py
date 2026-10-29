"""Private AI conversations in the existing MongoDB instance."""

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any
from uuid import uuid4

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, ReturnDocument

from homespace_ai.application.renter_search_state import latest_state, sanitize
from homespace_ai.core.config import get_settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ConversationRepository:
    def __init__(self, mongo_uri: str) -> None:
        self.client = AsyncMongoClient(mongo_uri, serverSelectionTimeoutMS=3000, tz_aware=True)
        self.collection = self.client.get_default_database()["ai_conversations"]
        self.guest_limits = self.client.get_default_database()["ai_guest_limits"]

    async def ensure_indexes(self) -> None:
        await self.collection.create_index(
            [("ownerId", ASCENDING), ("updatedAt", DESCENDING)]
        )
        # Legacy guest records expire; new guest transcripts are never persisted.
        await self.collection.create_index("expiresAt", expireAfterSeconds=0)
        await self.guest_limits.create_index("expiresAt", expireAfterSeconds=0)

    @staticmethod
    def owner_filter(owner_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {"ownerId": owner_id}
        if owner_id.startswith("guest:"):
            result["expiresAt"] = {"$gt": _now()}
        return result

    async def consume_guest_quota(self, identity: str) -> int:
        """Atomic shared IP limits: changing a guest cookie cannot reset the budget."""
        now = _now()
        settings = get_settings()
        for seconds, limit in ((60, settings.guest_chat_requests_per_minute),
                               (86400, settings.guest_chat_requests_per_day)):
            bucket = int(now.timestamp()) // seconds
            end = datetime.fromtimestamp((bucket + 1) * seconds, tz=timezone.utc)
            counter = await self.guest_limits.find_one_and_update(
                {"_id": f"{identity}:{seconds}:{bucket}"},
                {"$inc": {"count": 1}, "$setOnInsert": {"expiresAt": end}},
                upsert=True, return_document=ReturnDocument.AFTER,
            )
            if counter["count"] > limit:
                return max(1, int((end - now).total_seconds()))
        return 0

    async def ping(self) -> None:
        await self.client.admin.command("ping")

    async def close(self) -> None:
        await self.client.close()

    async def create(self, owner_id: str) -> dict[str, Any]:
        if owner_id.startswith("guest:"):
            raise ValueError("Guest transcripts must never be persisted.")
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

    async def start_guest(self, owner: str, identifier: str) -> dict:
        """No transcript. Cookie budget survives refresh/new conversations."""
        now = _now()
        await self.guest_limits.update_one(
            {"_id": "trial:" + owner},
            {"$setOnInsert": {"count": 0, "expiresAt": now + timedelta(days=1)}}, upsert=True)
        return await self.guest_limits.find_one_and_update(
            {"_id": "trial:" + owner}, {"$set": {"activeSession": identifier}}, return_document=ReturnDocument.AFTER)

    async def reserve_guest_question(self, owner: str, identifier: str, count: int) -> dict | None:
        return await self.guest_limits.find_one_and_update(
            {"_id": "trial:" + owner, "activeSession": identifier,
             "count": {"$eq": count, "$lt": get_settings().guest_chat_max_questions},
             "expiresAt": {"$gt": _now()}, "claimedOwner": {"$exists": False}},
            {"$inc": {"count": 1}}, return_document=ReturnDocument.AFTER)

    async def release_guest_question(self, owner: str, identifier: str, count: int) -> None:
        await self.guest_limits.update_one(
            {"_id": "trial:" + owner, "activeSession": identifier, "count": count + 1,
             "claimedOwner": {"$exists": False}}, {"$inc": {"count": -1}})

    async def claim_guest(self, state: dict, account_id: str) -> dict | None:
        """Atomic owner-bound receipt + idempotent account insert."""
        receipt = await self.guest_limits.find_one_and_update(
            {"_id": "trial:" + state["owner"], "activeSession": state["id"],
             "count": state["count"], "expiresAt": {"$gt": _now()},
             "$or": [{"claimedOwner": {"$exists": False}}, {"claimedOwner": account_id}]},
            {"$set": {"claimedOwner": account_id}}, return_document=ReturnDocument.AFTER)
        if receipt is None:
            return None
        now = _now()
        document = {"ownerId": account_id, "title": state["messages"][0]["content"][:60],
                    "isPinned": False, "createdAt": now, "updatedAt": now, "messages": state["messages"]}
        return await self.collection.find_one_and_update(
            {"_id": state["id"], "ownerId": account_id}, {"$setOnInsert": document},
            upsert=True, return_document=ReturnDocument.AFTER)

    async def list_for_user(self, owner_id: str) -> list[dict[str, Any]]:
        cursor = self.collection.find(
            self.owner_filter(owner_id), {"messages": 0, "ownerId": 0}
        ).sort([("isPinned", DESCENDING), ("updatedAt", DESCENDING)])
        return [document async for document in cursor]

    async def get(self, owner_id: str, conversation_id: str) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {"_id": conversation_id, **self.owner_filter(owner_id)}
        )

    async def append_message(
        self, owner_id: str, conversation_id: str, *, role: str,
        content: str, status: str | None = None, listing_context: dict[str, Any] | None = None,
        listing_ids: list[str] | None = None, listing_positions: dict[str, int] | None = None,
        contract_context: dict[str, Any] | None = None, contract_cards: list[dict[str, Any]] | None = None,
        data_sources: list[str] | None = None,
        appointment_context: dict[str, Any] | None = None, appointment_cards: list[dict[str, Any]] | None = None,
    ) -> bool:
        now = _now()
        message = {
            "id": str(uuid4()),
            "role": role,
            "content": content,
            "createdAt": now,
            "status": status,
        }
        if role == "assistant" and listing_context is not None:
            message["listingContext"] = listing_context
        if role == "assistant":
            message["listingIds"] = listing_ids or []
            message["listingPositions"] = listing_positions or {}
            if contract_context is not None:
                message["contractContext"] = contract_context  # references only, never facts
            if contract_cards:
                message["contractCards"] = contract_cards
            if appointment_context is not None:
                message["appointmentContext"] = appointment_context  # references only, never facts
            if appointment_cards:
                message["appointmentCards"] = appointment_cards  # snapshot at readAt
            if data_sources:
                message["dataSources"] = data_sources
        search_state = (listing_context or {}).get("searchState") if role == "assistant" else None
        owned = {"_id": conversation_id, **self.owner_filter(owner_id)}
        if isinstance(search_state, dict) and isinstance(search_state.get("revision"), int):
            # Optimistic revision: a slower concurrent request must not overwrite newer criteria.
            result = await self.collection.update_one(
                {**owned, "$or": [{"renterSearchState": {"$exists": False}},
                                  {"renterSearchState.revision": {"$lt": search_state["revision"]}},
                                  {"renterSearchState": search_state}]},
                {"$push": {"messages": message}, "$set": {"updatedAt": now, "renterSearchState": search_state}},
            )
            if result.matched_count:
                return True
            message["listingContext"] = {key: value for key, value in listing_context.items() if key != "searchState"} | {
                "stateConflict": True}
        result = await self.collection.update_one(
            owned, {"$push": {"messages": message}, "$set": {"updatedAt": now}},
        )
        if result.matched_count and role == "user":
            await self.collection.update_one(
                {"_id": conversation_id, "ownerId": owner_id, "title": "Đoạn chat mới"},
                {"$set": {"title": content[:60]}},
            )
        return bool(result.matched_count)

    async def set_pinned(self, owner_id: str, conversation_id: str, pinned: bool) -> bool:
        result = await self.collection.update_one(
            {"_id": conversation_id, **self.owner_filter(owner_id)},
            {"$set": {"isPinned": pinned, "updatedAt": _now()}},
        )
        return bool(result.matched_count)

    async def delete(self, owner_id: str, conversation_id: str) -> bool:
        result = await self.collection.delete_one(
            {"_id": conversation_id, **self.owner_filter(owner_id)}
        )
        return bool(result.deleted_count)


def conversation_search_state(conversation: dict[str, Any]) -> dict[str, Any] | None:
    """Conversation-level criteria first; else the newest message state over the full transcript."""
    return sanitize(conversation.get("renterSearchState")) or latest_state(conversation.get("messages") or [])


@lru_cache
def get_conversation_repository() -> ConversationRepository:
    return ConversationRepository(get_settings().ai_mongodb_uri)
