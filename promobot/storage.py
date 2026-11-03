"""Persistence layer. MongoStore for production, MemoryStore for local tests only."""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


class DuplicatePayment(Exception):
    """Raised when a telegram_payment_charge_id was already processed."""


def utcnow() -> datetime:
    """Timezone-aware UTC now, truncated to milliseconds (MongoDB's BSON date precision)."""
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _norm(doc: dict | None) -> dict | None:
    if doc is None:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    for k, v in doc.items():
        if isinstance(v, datetime):
            doc[k] = as_utc(v)
    return doc


class BaseStore:
    async def init(self) -> None: ...
    async def close(self) -> None: ...

    # users
    async def upsert_user(self, user_id: int, username: str | None, full_name: str) -> None: raise NotImplementedError
    async def get_user(self, user_id: int) -> dict | None: raise NotImplementedError
    async def set_banned(self, user_id: int, banned: bool) -> bool: raise NotImplementedError
    async def count_users(self) -> int: raise NotImplementedError
    # promotions
    async def insert_promotion(self, promo: dict) -> None: raise NotImplementedError
    async def get_promotion(self, promo_id: str) -> dict | None: raise NotImplementedError
    async def update_promotion(self, promo_id: str, fields: dict, expect_status: str | None = None) -> bool: raise NotImplementedError
    async def find_promotions(self, query: dict, limit: int = 50, newest_first: bool = True) -> list[dict]: raise NotImplementedError
    async def count_promotions(self, query: dict) -> int: raise NotImplementedError
    # payments
    async def insert_payment(self, payment: dict) -> None: raise NotImplementedError
    async def recent_payments(self, limit: int = 10) -> list[dict]: raise NotImplementedError
    async def mark_refunded(self, charge_id: str) -> None: raise NotImplementedError
    async def payment_stats(self, since: datetime | None = None) -> tuple[int, int]: raise NotImplementedError
    async def payment_by_package(self) -> dict[str, tuple[int, int]]: raise NotImplementedError
    # settings
    async def get_setting(self, key: str, default: Any = None) -> Any: raise NotImplementedError
    async def set_setting(self, key: str, value: Any) -> None: raise NotImplementedError


def _match(doc: dict, query: dict) -> bool:
    for k, v in query.items():
        if isinstance(v, dict) and "$in" in v:
            if doc.get(k) not in v["$in"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


class MemoryStore(BaseStore):
    """In-memory store. Data is lost on exit: use for tests / local dry runs only."""

    def __init__(self) -> None:
        self.users: dict[int, dict] = {}
        self.promotions: dict[str, dict] = {}
        self.payments: dict[str, dict] = {}
        self.settings: dict[str, Any] = {}

    async def upsert_user(self, user_id, username, full_name):
        u = self.users.setdefault(user_id, {"user_id": user_id, "banned": False, "joined_at": utcnow()})
        u.update(username=username, full_name=full_name, last_seen=utcnow())

    async def get_user(self, user_id):
        u = self.users.get(user_id)
        return copy.deepcopy(u) if u else None

    async def set_banned(self, user_id, banned):
        u = self.users.setdefault(user_id, {"user_id": user_id, "username": None, "full_name": "", "joined_at": utcnow()})
        u["banned"] = banned
        return True

    async def count_users(self):
        return len(self.users)

    async def insert_promotion(self, promo):
        if promo["promo_id"] in self.promotions:
            raise ValueError("duplicate promo_id")
        self.promotions[promo["promo_id"]] = copy.deepcopy(promo)

    async def get_promotion(self, promo_id):
        p = self.promotions.get(promo_id)
        return copy.deepcopy(p) if p else None

    async def update_promotion(self, promo_id, fields, expect_status=None):
        p = self.promotions.get(promo_id)
        if not p or (expect_status is not None and p.get("status") != expect_status):
            return False
        p.update(copy.deepcopy(fields))
        return True

    async def find_promotions(self, query, limit=50, newest_first=True):
        items = [copy.deepcopy(p) for p in self.promotions.values() if _match(p, query)]
        items.sort(key=lambda p: p["created_at"], reverse=newest_first)
        return items[:limit]

    async def count_promotions(self, query):
        return sum(1 for p in self.promotions.values() if _match(p, query))

    async def insert_payment(self, payment):
        cid = payment["charge_id"]
        if cid in self.payments:
            raise DuplicatePayment(cid)
        self.payments[cid] = copy.deepcopy(payment)

    async def mark_refunded(self, charge_id):
        if charge_id in self.payments:
            self.payments[charge_id]["refunded"] = True

    async def recent_payments(self, limit=10):
        return sorted(self.payments.values(), key=lambda p: p["paid_at"], reverse=True)[:limit]

    async def payment_stats(self, since=None):
        ps = [p for p in self.payments.values() if not p.get("refunded") and (since is None or p["paid_at"] >= since)]
        return len(ps), sum(p["amount"] for p in ps)

    async def payment_by_package(self):
        out: dict[str, tuple[int, int]] = {}
        for p in self.payments.values():
            if p.get("refunded"):
                continue
            c, s = out.get(p["package"], (0, 0))
            out[p["package"]] = (c + 1, s + p["amount"])
        return out

    async def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    async def set_setting(self, key, value):
        self.settings[key] = value


class MongoStore(BaseStore):
    def __init__(self, uri: str, db_name: str, client=None) -> None:
        if client is None:
            from motor.motor_asyncio import AsyncIOMotorClient
            client = AsyncIOMotorClient(uri, tz_aware=True, serverSelectionTimeoutMS=15000)
        self.client = client
        self.db = client[db_name]

    async def init(self):
        from pymongo import ASCENDING, DESCENDING
        await self.db.users.create_index([("user_id", ASCENDING)], unique=True)
        await self.db.promotions.create_index([("promo_id", ASCENDING)], unique=True)
        await self.db.promotions.create_index([("status", ASCENDING), ("expires_at", ASCENDING)])
        await self.db.promotions.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
        await self.db.payments.create_index([("charge_id", ASCENDING)], unique=True)
        await self.db.payments.create_index([("paid_at", DESCENDING)])
        await self.db.settings.create_index([("key", ASCENDING)], unique=True)
        log.info("MongoDB indexes ensured")

    async def close(self):
        self.client.close()

    async def upsert_user(self, user_id, username, full_name):
        await self.db.users.update_one(
            {"user_id": user_id},
            {"$set": {"username": username, "full_name": full_name, "last_seen": utcnow()},
             "$setOnInsert": {"user_id": user_id, "banned": False, "joined_at": utcnow()}},
            upsert=True,
        )

    async def get_user(self, user_id):
        return _norm(await self.db.users.find_one({"user_id": user_id}))

    async def set_banned(self, user_id, banned):
        await self.db.users.update_one(
            {"user_id": user_id},
            {"$set": {"banned": banned}, "$setOnInsert": {"user_id": user_id, "username": None, "full_name": "", "joined_at": utcnow()}},
            upsert=True,
        )
        return True

    async def count_users(self):
        return await self.db.users.count_documents({})

    async def insert_promotion(self, promo):
        await self.db.promotions.insert_one(dict(promo))

    async def get_promotion(self, promo_id):
        return _norm(await self.db.promotions.find_one({"promo_id": promo_id}))

    async def update_promotion(self, promo_id, fields, expect_status=None):
        q = {"promo_id": promo_id}
        if expect_status is not None:
            q["status"] = expect_status
        res = await self.db.promotions.update_one(q, {"$set": fields})
        return res.matched_count == 1

    async def find_promotions(self, query, limit=50, newest_first=True):
        cur = self.db.promotions.find(query).sort("created_at", -1 if newest_first else 1).limit(limit)
        return [_norm(d) async for d in cur]

    async def count_promotions(self, query):
        return await self.db.promotions.count_documents(query)

    async def insert_payment(self, payment):
        from pymongo.errors import DuplicateKeyError
        try:
            await self.db.payments.insert_one(dict(payment))
        except DuplicateKeyError as e:
            raise DuplicatePayment(payment["charge_id"]) from e

    async def mark_refunded(self, charge_id):
        await self.db.payments.update_one({"charge_id": charge_id}, {"$set": {"refunded": True}})

    async def recent_payments(self, limit=10):
        cur = self.db.payments.find({}).sort("paid_at", -1).limit(limit)
        return [_norm(d) async for d in cur]

    async def payment_stats(self, since=None):
        q: dict = {"refunded": {"$ne": True}}
        if since is not None:
            q["paid_at"] = {"$gte": since}
        count, total = 0, 0
        async for d in self.db.payments.find(q, {"amount": 1}):
            count += 1
            total += int(d["amount"])
        return count, total

    async def payment_by_package(self):
        out: dict[str, tuple[int, int]] = {}
        async for d in self.db.payments.find({"refunded": {"$ne": True}}, {"amount": 1, "package": 1}):
            c, s = out.get(d["package"], (0, 0))
            out[d["package"]] = (c + 1, s + int(d["amount"]))
        return out

    async def get_setting(self, key, default=None):
        d = await self.db.settings.find_one({"key": key})
        return d["value"] if d else default

    async def set_setting(self, key, value):
        await self.db.settings.update_one({"key": key}, {"$set": {"value": value}}, upsert=True)
