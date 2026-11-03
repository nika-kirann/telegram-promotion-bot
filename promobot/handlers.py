"""Telegram handlers, keyboards, admin dashboard and scheduled jobs."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from telegram import (BotCommand, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, Message, Update)
from telegram.constants import ChatType, ParseMode
from telegram.error import Conflict, NetworkError, TelegramError, TimedOut
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, PreCheckoutQueryHandler, TypeHandler, filters)

from . import texts
from .config import PACKAGES, Config
from .service import PromotionService, fmt_dt
from .storage import as_utc, utcnow

log = logging.getLogger(__name__)

MAX_TEXT = 3500
MAX_CAPTION = 900
INVITE_CHOICES = [0, 2, 4, 6, 12, 24]
MIN_INVITE_HOURS = 1
URL_RE = re.compile(r"(https?://\S+|t\.me/\S+|@[A-Za-z0-9_]{5,})", re.I)


# ======================= scheduler (JobQueue) =======================
class JobScheduler:
    def __init__(self, app: Application):
        self.app = app

    def _name(self, promo_id: str) -> str:
        return f"expire:{promo_id}"

    def cancel_expiry(self, promo_id: str) -> None:
        for job in self.app.job_queue.get_jobs_by_name(self._name(promo_id)):
            job.schedule_removal()

    def schedule_expiry(self, promo_id: str, when: datetime) -> None:
        self.cancel_expiry(promo_id)
        when = max(as_utc(when), utcnow() + timedelta(seconds=1))
        self.app.job_queue.run_once(expiry_job, when=when, data=promo_id, name=self._name(promo_id))


def svc(context) -> PromotionService:
    return context.application.bot_data["service"]


def cfg(context) -> Config:
    return context.application.bot_data["config"]


async def expiry_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await svc(context).expire(context.bot, context.job.data)
    except Exception:
        log.exception("Expiry job failed for %s; retrying in 60s", context.job.data)
        context.job_queue.run_once(expiry_job, when=60, data=context.job.data, name=f"expire:{context.job.data}")


async def invitation_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    hours = await s.invite_interval()
    if hours <= 0:
        return
    last = await s.store.get_setting("invite_last_sent")
    now = utcnow()
    if last and now - as_utc(datetime.fromisoformat(last)) < timedelta(hours=max(hours, MIN_INVITE_HOURS)):
        return
    await send_invitation(context)


async def send_invitation(context) -> bool:
    s = svc(context)
    username = context.bot.username
    kb = Kb([[Btn("🚀 BUY PROMOTION", url=f"https://t.me/{username}?start=buy")]])
    try:
        msg = await context.bot.send_message(chat_id=cfg(context).group_id, text=texts.invitation(),
                                             parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramError as e:
        log.warning("Invitation post failed: %s", e)
        return False
    prev = await s.store.get_setting("invite_last_message_id")
    if prev:  # keep the group clean: only the latest invitation stays
        await s.safe_delete(context.bot, prev)
    await s.store.set_setting("invite_last_message_id", msg.message_id)
    await s.store.set_setting("invite_last_sent", utcnow().isoformat())
    return True


# ======================= keyboards =======================
def main_menu_kb(config: Config) -> Kb:
    contact = (Btn("📞 Contact Admin", url=f"https://t.me/{config.admin_contact}") if config.admin_contact
               else Btn("📞 Contact Admin", callback_data="contact"))
    return Kb([[Btn("🚀 Buy Promotion", callback_data="buy")],
               [Btn("📦 My Promotions", callback_data="orders"), Btn("📜 Rules & Guidelines", callback_data="rules")],
               [contact]])


def packages_kb() -> Kb:
    rows = [[Btn(f"{p.medal} {p.name} · {p.hours}h · {p.price} ⭐", callback_data=f"pkg:{p.key}")] for p in PACKAGES.values()]
    rows.append([Btn("⬅️ Back", callback_data="home")])
    return Kb(rows)


BACK_KB = Kb([[Btn("⬅️ Main menu", callback_data="home")]])


def preview_kb(promo_id: str) -> Kb:
    return Kb([[Btn("✅ Confirm Advertisement", callback_data=f"confirm:{promo_id}")],
               [Btn("✏️ Edit Advertisement", callback_data=f"edit:{promo_id}")]])


ADMIN_KB = Kb([
    [Btn("📊 Statistics", callback_data="adm:stats"), Btn("📈 Revenue", callback_data="adm:revenue")],
    [Btn("📋 Recent transactions", callback_data="adm:tx")],
    [Btn("🟢 Active promotions", callback_data="adm:active"), Btn("🕒 Pending review", callback_data="adm:pending")],
    [Btn("🚫 Ban user", callback_data="adm:ban"), Btn("✅ Unban user", callback_data="adm:unban")],
    [Btn("📢 Group announcement", callback_data="adm:announce")],
    [Btn("🔔 Invitation settings", callback_data="adm:invite"), Btn("🛡️ Approval mode", callback_data="adm:approval")],
    [Btn("🔄 Refresh", callback_data="adm:home")],
])


# ======================= helpers =======================
async def reply(update: Update, text: str, kb=None, edit: bool = False) -> None:
    q = update.callback_query
    if edit and q and q.message and q.message.text:
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
            return
        except TelegramError:
            pass
    chat = update.effective_chat
    await chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)


def is_private(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == ChatType.PRIVATE)


def extract_ad(msg: Message) -> tuple[dict | None, str]:
    if msg.photo:
        caption = msg.caption or ""
        if len(caption) > MAX_CAPTION:
            return None, f"Caption is too long ({len(caption)}/{MAX_CAPTION} characters). Please shorten it."
        return {"type": "photo", "photo_file_id": msg.photo[-1].file_id, "text": caption,
                "entities": [e.to_dict() for e in (msg.caption_entities or [])]}, ""
    if msg.text:
        text = msg.text.strip()
        if len(text) < 3:
            return None, "Your advertisement is too short."
        if len(msg.text) > MAX_TEXT:
            return None, f"Text is too long ({len(msg.text)}/{MAX_TEXT} characters). Please shorten it."
        return {"type": "text", "text": msg.text, "entities": [e.to_dict() for e in (msg.entities or [])]}, ""
    return None, "Please send a text message or a photo with a caption."


async def send_ad_copy(bot, chat_id: int, ad: dict, footer: str = "") -> None:
    ents = PromotionService._entities(ad.get("entities"))
    if ad["type"] == "photo":
        await bot.send_photo(chat_id=chat_id, photo=ad["photo_file_id"], caption=(ad.get("text") or "") + footer or None,
                             caption_entities=ents)
    else:
        await bot.send_message(chat_id=chat_id, text=ad["text"] + footer, entities=ents)


# ======================= gatekeeping =======================
async def track_and_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.is_bot or not is_private(update):
        return
    s = svc(context)
    try:
        await s.store.upsert_user(user.id, user.username, user.full_name)
        u = await s.store.get_user(user.id)
    except Exception:
        log.exception("User tracking failed")
        return
    if u and u.get("banned") and not cfg(context).is_admin(user.id):
        if update.pre_checkout_query:
            return  # rejected in pre-checkout handler with a reason
        if update.callback_query:
            await update.callback_query.answer("🚫 Your account is restricted.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("🚫 Your account is restricted from using this bot.")
        raise ApplicationHandlerStop


# ======================= user commands =======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        return
    context.user_data.clear()
    if context.args and context.args[0] == "buy":
        await reply(update, texts.packages_text(), packages_kb())
        return
    await reply(update, texts.WELCOME, main_menu_kb(cfg(context)))


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        return
    context.user_data.clear()
    await reply(update, texts.packages_text(), packages_kb())


async def packages_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_private(update):
        await reply(update, texts.packages_text(), packages_kb())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_private(update):
        await reply(update, texts.HELP, BACK_KB)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await reply(update, "✖️ Cancelled. Tap /start to open the Promotion Center.")


async def orders_text(context, user_id: int) -> str:
    items = await svc(context).store.find_promotions({"user_id": user_id, "status": {"$in": [
        "awaiting_payment", "pending_approval", "active", "expired", "deleted", "rejected", "publish_failed", "paid"]}}, limit=10)
    if not items:
        return "<b>📦 MY PROMOTIONS</b>\n\nYou have no promotions yet. Tap 🚀 Buy Promotion to get started."
    lines = ["<b>📦 MY PROMOTIONS</b>", texts.DIV]
    for p in items:
        pkg = PACKAGES[p["package"]]
        line = f"{pkg.medal} <code>{p['promo_id']}</code> · {pkg.name} · ⭐ {pkg.price}\n     {texts.STATUS_LABEL.get(p['status'], p['status'])}"
        if p["status"] == "active":
            line += f" · ends {fmt_dt(p.get('expires_at'))}"
        elif p.get("published_at"):
            line += f" · started {fmt_dt(p['published_at'])}"
        lines.append(line)
    return "\n".join(lines)


async def myorders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_private(update):
        await reply(update, await orders_text(context, update.effective_user.id), BACK_KB)


# ======================= user callbacks =======================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    s = svc(context)
    uid = update.effective_user.id
    if data == "home":
        await q.answer()
        context.user_data.clear()
        await reply(update, texts.WELCOME, main_menu_kb(cfg(context)), edit=True)
    elif data == "buy":
        await q.answer()
        await reply(update, texts.packages_text(), packages_kb(), edit=True)
    elif data == "orders":
        await q.answer()
        await reply(update, await orders_text(context, uid), BACK_KB, edit=True)
    elif data == "rules":
        await q.answer()
        await reply(update, texts.RULES, BACK_KB, edit=True)
    elif data == "contact":
        await q.answer()
        context.user_data["state"] = "contact"
        await reply(update, "<b>📞 CONTACT ADMIN</b>\n\nSend your message now and it will be delivered to our team. "
                            "Send /cancel to abort.", None)
    elif data.startswith("pkg:"):
        key = data[4:]
        if key not in PACKAGES:
            await q.answer("Unknown package", show_alert=True)
            return
        await q.answer()
        context.user_data.clear()
        context.user_data.update(state="await_ad", package=key)
        await reply(update, texts.ask_ad(PACKAGES[key]), None, edit=True)
    elif data.startswith("edit:"):
        promo = await s.store.get_promotion(data[5:])
        if not promo or promo["user_id"] != uid or promo["status"] not in ("draft", "awaiting_payment"):
            await q.answer("This order can no longer be edited.", show_alert=True)
            return
        await q.answer()
        await s.store.update_promotion(promo["promo_id"], {"status": "abandoned"})
        context.user_data.clear()
        context.user_data.update(state="await_ad", package=promo["package"])
        await reply(update, "✏️ <b>Send your updated advertisement.</b>\n\n" + texts.ask_ad(PACKAGES[promo["package"]]))
    elif data.startswith("confirm:"):
        promo = await s.store.get_promotion(data[8:])
        if not promo or promo["user_id"] != uid or promo["status"] not in ("draft", "awaiting_payment"):
            await q.answer("This order is no longer available. Please start again with /buy.", show_alert=True)
            return
        await q.answer("Generating your invoice…")
        context.user_data.clear()
        try:
            await q.edit_message_reply_markup(None)
        except TelegramError:
            pass
        await s.send_invoice(context.bot, uid, promo["promo_id"])
        await reply(update, "💳 <b>Tap Pay above to complete your purchase with Telegram Stars.</b>\n"
                            "Your ad goes live automatically right after payment is confirmed.")
    else:
        await q.answer()


# ======================= private messages (ad / contact / admin inputs) =======================
async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    state = context.user_data.get("state")
    uid = update.effective_user.id
    s = svc(context)
    if state == "await_ad":
        ad, err = extract_ad(msg)
        if err:
            await msg.reply_text(f"⚠️ {err}")
            return
        pkg_key = context.user_data["package"]
        promo = await s.create_draft(update.effective_user, pkg_key, ad)
        context.user_data.clear()
        await send_ad_copy(context.bot, uid, ad)
        await reply(update, texts.preview_header(PACKAGES[pkg_key]), preview_kb(promo["promo_id"]))
    elif state == "contact":
        context.user_data.clear()
        u = update.effective_user
        header = (f"📩 <b>Message from user</b> {texts.esc(u.full_name)} (@{texts.esc(u.username or '—')}, "
                  f"<code>{u.id}</code>)")
        for aid in cfg(context).admin_ids:
            try:
                await context.bot.send_message(aid, header, parse_mode=ParseMode.HTML)
                await msg.copy(aid)
            except TelegramError as e:
                log.warning("Contact relay to %s failed: %s", aid, e)
        await msg.reply_text("✅ Your message has been sent to our team. We'll get back to you soon.")
    elif state and state.startswith("adm_") and cfg(context).is_admin(uid):
        await admin_input(update, context, state)
    else:
        await reply(update, "Tap /start to open the Promotion Center.", main_menu_kb(cfg(context)))


# ======================= payments =======================
async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.pre_checkout_query
    ok, reason, _ = await svc(context).validate_checkout(q.from_user.id, q.invoice_payload, q.currency, q.total_amount)
    if ok:
        await q.answer(ok=True)
    else:
        log.warning("Pre-checkout rejected for user %s: %s", q.from_user.id, reason)
        await q.answer(ok=False, error_message=reason)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sp = update.effective_message.successful_payment
    result = await svc(context).process_successful_payment(context.bot, update.effective_user.id, sp)
    log.info("successful_payment processed: %s", result)


# ======================= admin =======================
async def admin_guard(update: Update, context) -> bool:
    if not cfg(context).is_admin(update.effective_user.id if update.effective_user else None):
        if update.callback_query:
            await update.callback_query.answer("⛔ Admins only.", show_alert=True)
        elif update.effective_message and is_private(update):
            await update.effective_message.reply_text("⛔ This command is restricted to administrators.")
        return False
    return True


async def dashboard_text(s: PromotionService) -> str:
    st = s.store
    count, stars = await st.payment_stats()
    active = await st.count_promotions({"status": "active"})
    expired = await st.count_promotions({"status": "expired"})
    pending = await st.count_promotions({"status": {"$in": ["pending_approval", "publish_failed"]}})
    users = await st.count_users()
    appr = "ON" if await s.approval_required() else "OFF"
    inv = await s.invite_interval()
    return (f"<b>🛠️ ADMIN DASHBOARD</b>\n{texts.DIV}\n"
            f"📊 Promotions sold: <b>{count}</b>\n💰 Stars collected: <b>{stars} ⭐</b>\n👥 Registered users: <b>{users}</b>\n"
            f"🟢 Active promotions: <b>{active}</b>\n⏳ Expired promotions: <b>{expired}</b>\n🕒 Pending review: <b>{pending}</b>\n"
            f"{texts.DIV}\n🛡️ Approval mode: <b>{appr}</b>\n🔔 Invitation: <b>{'every ' + str(inv) + 'h' if inv else 'OFF'}</b>")


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update) or not await admin_guard(update, context):
        return
    context.user_data.clear()
    await reply(update, await dashboard_text(svc(context)), ADMIN_KB)


ADMIN_BACK = Kb([[Btn("⬅️ Dashboard", callback_data="adm:home")]])


async def on_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update, context):
        return
    q = update.callback_query
    data = q.data
    s = svc(context)
    bot = context.bot
    st = s.store
    if data.startswith("apr:"):
        result = await s.activate(bot, data[4:])
        await q.answer({"activated": "✅ Published", "publish_failed": "⚠️ Publishing failed"}.get(result, "Not pending"), show_alert=True)
        try:
            await q.edit_message_reply_markup(None)
        except TelegramError:
            pass
        return
    if data.startswith("rej:"):
        ok = await s.reject(bot, data[4:])
        await q.answer("❌ Rejected" if ok else "Not pending", show_alert=True)
        try:
            await q.edit_message_reply_markup(None)
        except TelegramError:
            pass
        return
    if data.startswith("del:"):
        ok = await s.admin_delete(bot, data[4:])
        await q.answer("🗑️ Deleted" if ok else "Not active", show_alert=True)
        return
    if data.startswith("ext:"):
        _, pid, hrs = data.split(":")
        new = await s.extend(bot, pid, int(hrs))
        await q.answer(f"🔄 Extended to {fmt_dt(new)}" if new else "Not active", show_alert=True)
        return
    if data.startswith("inv:"):
        hours = int(data[4:])
        if hours not in INVITE_CHOICES:
            await q.answer("Invalid", show_alert=True)
            return
        await st.set_setting("invite_interval_hours", hours)
        await q.answer("Saved")
        data = "adm:invite"
    if data == "adm:invite_now":
        ok = await send_invitation(context)
        await q.answer("📨 Invitation posted" if ok else "Failed — check bot permissions", show_alert=True)
        return
    if data == "adm:approval_toggle":
        await st.set_setting("require_approval", not await s.approval_required())
        data = "adm:approval"
    await q.answer()

    if data == "adm:home":
        context.user_data.clear()
        await reply(update, await dashboard_text(s), ADMIN_KB, edit=True)
    elif data == "adm:stats":
        await reply(update, await dashboard_text(s), ADMIN_BACK, edit=True)
    elif data == "adm:revenue":
        now = utcnow()
        rows = [f"<b>📈 REVENUE STATISTICS</b>\n{texts.DIV}"]
        for label, since in (("Last 24h", now - timedelta(days=1)), ("Last 7 days", now - timedelta(days=7)),
                             ("Last 30 days", now - timedelta(days=30)), ("All time", None)):
            c, total = await st.payment_stats(since)
            rows.append(f"{label}: <b>{total} ⭐</b> · {c} sales")
        rows.append(texts.DIV)
        by_pkg = await st.payment_by_package()
        for p in PACKAGES.values():
            c, total = by_pkg.get(p.key, (0, 0))
            rows.append(f"{p.medal} {p.name}: {c} sold · {total} ⭐")
        await reply(update, "\n".join(rows), ADMIN_BACK, edit=True)
    elif data == "adm:tx":
        pays = await st.recent_payments(10)
        rows = [f"<b>📋 RECENT TRANSACTIONS</b>\n{texts.DIV}"]
        for p in pays:
            flag = " · ↩️ refunded" if p.get("refunded") else ("" if p.get("valid", True) else " · ⚠️ invalid")
            rows.append(f"{fmt_dt(p['paid_at'])}\n  ⭐ {p['amount']} · {p['package']} · <code>{texts.esc(p['promo_id'])}</code> · "
                        f"user <code>{p['user_id']}</code>{flag}")
        if not pays:
            rows.append("No transactions yet.")
        await reply(update, "\n".join(rows), ADMIN_BACK, edit=True)
    elif data == "adm:active":
        items = await st.find_promotions({"status": "active"}, limit=15, newest_first=False)
        if not items:
            await reply(update, "🟢 No active promotions right now.", ADMIN_BACK, edit=True)
            return
        await reply(update, f"<b>🟢 ACTIVE PROMOTIONS ({len(items)})</b>", ADMIN_BACK, edit=True)
        for p in items:
            kb = Kb([[Btn("➕1h", callback_data=f"ext:{p['promo_id']}:1"), Btn("➕3h", callback_data=f"ext:{p['promo_id']}:3"),
                      Btn("➕12h", callback_data=f"ext:{p['promo_id']}:12"), Btn("➕24h", callback_data=f"ext:{p['promo_id']}:24")],
                     [Btn("🗑️ Delete promotion", callback_data=f"del:{p['promo_id']}")]])
            await context.bot.send_message(update.effective_chat.id, parse_mode=ParseMode.HTML, reply_markup=kb, text=(
                f"<code>{p['promo_id']}</code> · {PACKAGES[p['package']].name}\n👤 <code>{p['user_id']}</code> "
                f"@{texts.esc(p.get('username') or '—')}\n⏰ Ends {fmt_dt(p.get('expires_at'))}"))
    elif data == "adm:pending":
        items = await st.find_promotions({"status": {"$in": ["pending_approval", "publish_failed"]}}, limit=10, newest_first=False)
        if not items:
            await reply(update, "🕒 Nothing waiting for review.", ADMIN_BACK, edit=True)
            return
        await reply(update, f"<b>🕒 PENDING REVIEW ({len(items)})</b>", ADMIN_BACK, edit=True)
        for p in items:
            try:
                await send_ad_copy(context.bot, update.effective_chat.id, p["ad"])
            except TelegramError:
                pass
            kb = Kb([[Btn("✅ Approve & publish", callback_data=f"apr:{p['promo_id']}"),
                      Btn("❌ Reject & refund", callback_data=f"rej:{p['promo_id']}")]])
            await context.bot.send_message(update.effective_chat.id, parse_mode=ParseMode.HTML, reply_markup=kb, text=(
                f"<code>{p['promo_id']}</code> · {PACKAGES[p['package']].name} · {texts.STATUS_LABEL[p['status']]}\n"
                f"👤 <code>{p['user_id']}</code> @{texts.esc(p.get('username') or '—')}"))
    elif data in ("adm:ban", "adm:unban"):
        context.user_data["state"] = "adm_" + data[4:]
        await reply(update, f"Send the Telegram user ID to <b>{data[4:]}</b>. /cancel to abort.", None)
    elif data == "adm:announce":
        context.user_data["state"] = "adm_announce"
        await reply(update, "📢 Send the announcement (text or photo with caption). It will be posted to the group as-is. "
                            "/cancel to abort.", None)
    elif data == "adm:invite":
        cur = await s.invite_interval()
        rows = [[Btn(("● " if h == cur else "") + ("Off" if h == 0 else f"{h}h"), callback_data=f"inv:{h}") for h in INVITE_CHOICES],
                [Btn("📨 Post invitation now", callback_data="adm:invite_now")], [Btn("⬅️ Dashboard", callback_data="adm:home")]]
        await reply(update, f"<b>🔔 GROUP INVITATION</b>\n{texts.DIV}\nCurrent frequency: <b>{'every ' + str(cur) + 'h' if cur else 'OFF'}</b>\n"
                            "Only the latest invitation is kept in the group to avoid spam.", Kb(rows), edit=True)
    elif data == "adm:approval":
        on = await s.approval_required()
        kb = Kb([[Btn("Turn OFF (auto-publish)" if on else "Turn ON (manual review)", callback_data="adm:approval_toggle")],
                 [Btn("⬅️ Dashboard", callback_data="adm:home")]])
        await reply(update, f"<b>🛡️ APPROVAL MODE: {'ON' if on else 'OFF'}</b>\n{texts.DIV}\n"
                            "ON: paid ads wait for admin approval (rejections are refunded automatically).\n"
                            "OFF: paid ads are published instantly.", kb, edit=True)


async def admin_input(update: Update, context, state: str) -> None:
    msg = update.effective_message
    s = svc(context)
    context.user_data.clear()
    if state in ("adm_ban", "adm_unban"):
        raw = (msg.text or "").strip()
        if not raw.lstrip("-").isdigit():
            await msg.reply_text("⚠️ Please send a numeric user ID.", reply_markup=ADMIN_BACK)
            return
        target = int(raw)
        if state == "adm_ban" and cfg(context).is_admin(target):
            await msg.reply_text("⚠️ You cannot ban an administrator.", reply_markup=ADMIN_BACK)
            return
        await s.store.set_banned(target, state == "adm_ban")
        await msg.reply_text(f"{'🚫 Banned' if state == 'adm_ban' else '✅ Unbanned'} user {target}.", reply_markup=ADMIN_BACK)
    elif state == "adm_announce":
        try:
            await msg.copy(cfg(context).group_id)
            await msg.reply_text("📢 Announcement posted to the group.", reply_markup=ADMIN_BACK)
        except TelegramError as e:
            await msg.reply_text(f"⚠️ Failed to post: {e}", reply_markup=ADMIN_BACK)


# ======================= errors & setup =======================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, Conflict):
        log.error("Conflict: another instance is polling with this BOT_TOKEN. Run exactly one instance.")
    elif isinstance(err, (NetworkError, TimedOut)):
        log.warning("Telegram network issue (will retry): %s", err)
    else:
        log.error("Unhandled error", exc_info=err)


COMMANDS = [BotCommand("start", "Open promotion center"), BotCommand("buy", "Purchase a promotion"),
            BotCommand("myorders", "View promotion history"), BotCommand("packages", "View available packages"),
            BotCommand("help", "Bot instructions"), BotCommand("admin", "Admin dashboard")]


def register(app: Application) -> None:
    private = filters.ChatType.PRIVATE
    app.add_handler(TypeHandler(Update, track_and_gate), group=-1)
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("myorders", myorders))
    app.add_handler(CommandHandler("packages", packages_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel, filters=private))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(on_admin_callback, pattern=r"^(adm:|apr:|rej:|del:|ext:|inv:)"))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(private & (filters.TEXT | filters.PHOTO) & ~filters.COMMAND, on_private_message))
    app.add_error_handler(error_handler)
