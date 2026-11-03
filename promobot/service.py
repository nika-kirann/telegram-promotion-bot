"""Business logic: payments, publication, expiry, recovery. Independent of handlers for testability."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from typing import Protocol

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, MessageEntity
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError

from . import texts
from .config import CURRENCY, PACKAGES, Config
from .storage import BaseStore, DuplicatePayment, as_utc, utcnow

log = logging.getLogger(__name__)

PAYLOAD_PREFIX = "promo:"


class Scheduler(Protocol):
    def schedule_expiry(self, promo_id: str, when: datetime) -> None: ...
    def cancel_expiry(self, promo_id: str) -> None: ...


def new_promo_id() -> str:
    return "PR" + secrets.token_hex(4).upper()


def fmt_dt(dt: datetime | None) -> str:
    return as_utc(dt).strftime("%d %b %Y, %H:%M UTC") if dt else "—"


class PromotionService:
    def __init__(self, store: BaseStore, config: Config, scheduler: Scheduler):
        self.store = store
        self.config = config
        self.scheduler = scheduler

    # ---------- settings ----------
    async def approval_required(self) -> bool:
        return bool(await self.store.get_setting("require_approval", self.config.require_approval_default))

    async def invite_interval(self) -> int:
        return int(await self.store.get_setting("invite_interval_hours", self.config.invite_interval_default))

    # ---------- drafts & invoices ----------
    async def create_draft(self, user, package_key: str, ad: dict) -> dict:
        pkg = PACKAGES[package_key]
        promo = {
            "promo_id": new_promo_id(), "user_id": user.id, "username": user.username,
            "full_name": user.full_name, "package": pkg.key, "price": pkg.price, "hours": pkg.hours,
            "status": "draft", "ad": ad, "created_at": utcnow(), "extended_hours": 0,
        }
        await self.store.insert_promotion(promo)
        return promo

    @staticmethod
    def invoice_params(promo: dict) -> dict:
        pkg = PACKAGES[promo["package"]]
        return {
            "title": f"{pkg.name} Promotion · {pkg.hours}h",
            "description": texts.invoice_description(pkg),
            "payload": PAYLOAD_PREFIX + promo["promo_id"],
            "provider_token": "",  # Telegram Stars: empty provider token
            "currency": CURRENCY,
            "prices": [LabeledPrice(f"{pkg.name} promotion ({pkg.hours}h)", pkg.price)],
        }

    async def send_invoice(self, bot, chat_id: int, promo_id: str) -> bool:
        promo = await self.store.get_promotion(promo_id)
        if not promo or promo["status"] not in ("draft", "awaiting_payment"):
            return False
        await self.store.update_promotion(promo_id, {"status": "awaiting_payment", "invoiced_at": utcnow()})
        await bot.send_invoice(chat_id=chat_id, **self.invoice_params(promo))
        return True

    # ---------- payment validation ----------
    async def validate_checkout(self, user_id: int, payload: str, currency: str, amount: int) -> tuple[bool, str, dict | None]:
        if not payload or not payload.startswith(PAYLOAD_PREFIX):
            return False, "Invalid order reference.", None
        promo = await self.store.get_promotion(payload[len(PAYLOAD_PREFIX):])
        if not promo:
            return False, "Order not found. Please start a new purchase with /buy.", None
        if promo["user_id"] != user_id:
            return False, "This invoice belongs to another user.", None
        if promo["status"] != "awaiting_payment":
            return False, "This order is no longer payable. Please create a new one with /buy.", None
        if currency != CURRENCY:
            return False, "Only Telegram Stars payments are accepted.", None
        pkg = PACKAGES.get(promo["package"])
        if not pkg or amount != pkg.price or promo["price"] != pkg.price:
            return False, "Payment amount mismatch.", None
        user = await self.store.get_user(user_id)
        if user and user.get("banned"):
            return False, "Your account is not allowed to purchase promotions.", None
        return True, "", promo

    async def process_successful_payment(self, bot, user_id: int, sp) -> str:
        """Returns: 'activated' | 'pending_approval' | 'duplicate' | 'invalid' | 'publish_failed'."""
        charge_id = sp.telegram_payment_charge_id
        ok, reason, promo = await self.validate_checkout(user_id, sp.invoice_payload, sp.currency, sp.total_amount)
        payment = {
            "charge_id": charge_id, "provider_charge_id": getattr(sp, "provider_payment_charge_id", None),
            "user_id": user_id, "promo_id": promo["promo_id"] if promo else sp.invoice_payload,
            "package": promo["package"] if promo else "unknown", "amount": int(sp.total_amount),
            "currency": sp.currency, "paid_at": utcnow(), "valid": ok, "refunded": False,
        }
        try:
            await self.store.insert_payment(payment)
        except DuplicatePayment:
            log.warning("Duplicate successful_payment ignored (promo=%s)", payment["promo_id"])
            return "duplicate"

        if not ok:
            log.error("Invalid successful_payment from user %s: %s", user_id, reason)
            refunded = await self._refund(bot, user_id, charge_id)
            await self._safe_send(bot, user_id, f"⚠️ <b>Payment could not be applied.</b>\n{texts.esc(reason)}\n"
                                  + ("Your Stars have been refunded." if refunded else "An admin will review your refund."))
            await self.notify_admins(bot, f"⚠️ Invalid payment from <code>{user_id}</code>: {texts.esc(reason)} "
                                          f"(refunded: {refunded})")
            return "invalid"

        if not await self.store.update_promotion(promo["promo_id"], {"status": "paid", "paid_at": utcnow(), "charge_id": charge_id},
                                                 expect_status="awaiting_payment"):
            return "duplicate"

        pkg = PACKAGES[promo["package"]]
        if await self.approval_required():
            await self.store.update_promotion(promo["promo_id"], {"status": "pending_approval"})
            await self._safe_send(bot, user_id, texts.payment_pending_review(pkg, promo["promo_id"]))
            await self.notify_new_review(bot, promo["promo_id"])
            return "pending_approval"
        return await self.activate(bot, promo["promo_id"])

    # ---------- publication ----------
    @staticmethod
    def _entities(raw: list | None) -> list[MessageEntity] | None:
        if not raw:
            return None
        return [MessageEntity.de_json(e, None) for e in raw]

    async def publish(self, bot, promo: dict) -> int:
        ad = promo["ad"]
        footer = f"\n\n📢 Sponsored · #{promo['promo_id']}"
        if ad["type"] == "photo":
            msg = await bot.send_photo(chat_id=self.config.group_id, photo=ad["photo_file_id"],
                                       caption=(ad.get("text") or "") + footer,
                                       caption_entities=self._entities(ad.get("entities")))
        else:
            msg = await bot.send_message(chat_id=self.config.group_id, text=ad["text"] + footer,
                                         entities=self._entities(ad.get("entities")))
        return msg.message_id

    async def activate(self, bot, promo_id: str) -> str:
        promo = await self.store.get_promotion(promo_id)
        if not promo or promo["status"] not in ("paid", "pending_approval", "publish_failed"):
            return "invalid"
        try:
            message_id = await self.publish(bot, promo)
        except TelegramError as e:
            log.error("Publishing %s failed: %s", promo_id, e)
            await self.store.update_promotion(promo_id, {"status": "publish_failed", "last_error": str(e)})
            await self._safe_send(bot, promo["user_id"],
                                  f"✅ Payment received for <code>{promo_id}</code>.\n⚠️ We couldn't publish your ad yet — "
                                  "our team has been notified and will publish it shortly.")
            await self.notify_admins(bot, f"⚠️ Publishing <code>{promo_id}</code> failed: {texts.esc(e)}\n"
                                          "Check that the bot is a group admin, then retry from /admin → Pending review.")
            return "publish_failed"
        now = utcnow()
        expires = now + timedelta(hours=promo["hours"])
        await self.store.update_promotion(promo_id, {"status": "active", "published_at": now, "expires_at": expires,
                                                     "group_message_id": message_id})
        self.scheduler.schedule_expiry(promo_id, expires)
        await self._safe_send(bot, promo["user_id"], texts.payment_success(PACKAGES[promo["package"]]))
        log.info("Promotion %s active until %s", promo_id, expires.isoformat())
        return "activated"

    # ---------- lifecycle ----------
    async def safe_delete(self, bot, message_id: int | None) -> bool:
        if not message_id:
            return False
        try:
            await bot.delete_message(chat_id=self.config.group_id, message_id=message_id)
            return True
        except (BadRequest, Forbidden) as e:
            log.warning("Could not delete group message %s: %s", message_id, e)
        except TelegramError as e:
            log.error("Telegram error deleting message %s: %s", message_id, e)
        return False

    async def expire(self, bot, promo_id: str) -> bool:
        promo = await self.store.get_promotion(promo_id)
        if not promo or promo["status"] != "active":
            return False
        if as_utc(promo["expires_at"]) > utcnow() + timedelta(seconds=5):
            # Extended meanwhile: reschedule instead
            self.scheduler.schedule_expiry(promo_id, as_utc(promo["expires_at"]))
            return False
        if not await self.store.update_promotion(promo_id, {"status": "expired", "expired_at": utcnow()}, expect_status="active"):
            return False
        await self.safe_delete(bot, promo.get("group_message_id"))
        await self._safe_send(bot, promo["user_id"], texts.EXPIRED)
        log.info("Promotion %s expired", promo_id)
        return True

    async def admin_delete(self, bot, promo_id: str) -> bool:
        promo = await self.store.get_promotion(promo_id)
        if not promo or promo["status"] != "active":
            return False
        self.scheduler.cancel_expiry(promo_id)
        await self.store.update_promotion(promo_id, {"status": "deleted", "deleted_at": utcnow()})
        await self.safe_delete(bot, promo.get("group_message_id"))
        await self._safe_send(bot, promo["user_id"], texts.removed_by_admin(promo_id))
        return True

    async def extend(self, bot, promo_id: str, hours: int) -> datetime | None:
        if not 1 <= hours <= 720:
            return None
        promo = await self.store.get_promotion(promo_id)
        if not promo or promo["status"] != "active":
            return None
        new_exp = as_utc(promo["expires_at"]) + timedelta(hours=hours)
        await self.store.update_promotion(promo_id, {"expires_at": new_exp,
                                                     "extended_hours": promo.get("extended_hours", 0) + hours})
        self.scheduler.schedule_expiry(promo_id, new_exp)
        await self._safe_send(bot, promo["user_id"], texts.extended(promo_id, hours, fmt_dt(new_exp)))
        return new_exp

    async def reject(self, bot, promo_id: str) -> bool:
        promo = await self.store.get_promotion(promo_id)
        if not promo or promo["status"] not in ("pending_approval", "publish_failed"):
            return False
        refunded = await self._refund(bot, promo["user_id"], promo.get("charge_id"))
        await self.store.update_promotion(promo_id, {"status": "rejected", "rejected_at": utcnow(), "refunded": refunded})
        await self._safe_send(bot, promo["user_id"], texts.rejected(promo_id, refunded))
        return True

    async def recover(self, bot) -> dict:
        """Called at startup: expire overdue promotions and reschedule the rest."""
        expired = scheduled = 0
        for p in await self.store.find_promotions({"status": "active"}, limit=10000):
            exp = as_utc(p.get("expires_at"))
            if exp is None or exp <= utcnow():
                if await self.expire(bot, p["promo_id"]):
                    expired += 1
            else:
                self.scheduler.schedule_expiry(p["promo_id"], exp)
                scheduled += 1
        log.info("Recovery complete: %d rescheduled, %d expired", scheduled, expired)
        return {"scheduled": scheduled, "expired": expired}

    # ---------- helpers ----------
    async def _refund(self, bot, user_id: int, charge_id: str | None) -> bool:
        if not charge_id:
            return False
        try:
            await bot.refund_star_payment(user_id=user_id, telegram_payment_charge_id=charge_id)
        except TelegramError as e:
            log.error("Refund failed for user %s: %s", user_id, e)
            return False
        await self.store.mark_refunded(charge_id)
        return True

    async def _safe_send(self, bot, chat_id: int, text: str, **kw) -> None:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, **kw)
        except TelegramError as e:
            log.warning("Could not message %s: %s", chat_id, e)

    async def notify_admins(self, bot, text: str, **kw) -> None:
        for aid in self.config.admin_ids:
            await self._safe_send(bot, aid, text, **kw)

    async def notify_new_review(self, bot, promo_id: str) -> None:
        promo = await self.store.get_promotion(promo_id)
        if not promo:
            return
        pkg = PACKAGES[promo["package"]]
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"apr:{promo_id}"),
                                    InlineKeyboardButton("❌ Reject & refund", callback_data=f"rej:{promo_id}")]])
        for aid in self.config.admin_ids:
            try:
                if promo["ad"]["type"] == "photo":
                    await bot.send_photo(chat_id=aid, photo=promo["ad"]["photo_file_id"], caption=promo["ad"].get("text") or None,
                                         caption_entities=self._entities(promo["ad"].get("entities")))
                else:
                    await bot.send_message(chat_id=aid, text=promo["ad"]["text"], entities=self._entities(promo["ad"].get("entities")))
                await bot.send_message(
                    chat_id=aid, parse_mode=ParseMode.HTML, reply_markup=kb,
                    text=(f"🕒 <b>NEW AD FOR REVIEW</b>\n🆔 <code>{promo_id}</code>\n👤 {texts.esc(promo.get('full_name'))} "
                          f"(@{texts.esc(promo.get('username') or '—')}, <code>{promo['user_id']}</code>)\n"
                          f"📦 {pkg.name} · {pkg.hours}h · ⭐ {pkg.price}"))
            except TelegramError as e:
                log.warning("Could not notify admin %s: %s", aid, e)
