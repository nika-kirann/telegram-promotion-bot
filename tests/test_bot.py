import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from mongomock_motor import AsyncMongoMockClient
from telegram import SuccessfulPayment
from telegram.error import BadRequest

from promobot import texts
from promobot.config import PACKAGES, Config
from promobot.handlers import JobScheduler, extract_ad
from promobot.service import PromotionService
from promobot.storage import MemoryStore, MongoStore, utcnow
from promobot.web import build_web_app

CFG = Config(bot_token="1:x", group_id=-100123, admin_ids=frozenset({999}))
USER = SimpleNamespace(id=42, username="alice", full_name="Alice A")


class FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def schedule_expiry(self, pid, when):
        self.jobs[pid] = when

    def cancel_expiry(self, pid):
        self.jobs.pop(pid, None)


def make_bot():
    bot = AsyncMock()
    counter = iter(range(1000, 9999))
    bot.send_message.side_effect = lambda **kw: SimpleNamespace(message_id=next(counter))
    bot.send_photo.side_effect = lambda **kw: SimpleNamespace(message_id=next(counter))
    return bot


@pytest.fixture(params=["memory", "mongo"])
def store(request):
    if request.param == "memory":
        return MemoryStore()
    return MongoStore("", "testdb", client=AsyncMongoMockClient(tz_aware=True))


async def new_order(svc, bot, package="standard", ad=None):
    ad = ad or {"type": "text", "text": "Join https://t.me/mychannel today!", "entities": []}
    promo = await svc.create_draft(USER, package, ad)
    await svc.send_invoice(bot, USER.id, promo["promo_id"])
    return promo


def payment(promo_id, amount=99, currency="XTR", charge="ch_1"):
    return SuccessfulPayment(currency=currency, total_amount=amount, invoice_payload=f"promo:{promo_id}",
                             telegram_payment_charge_id=charge, provider_payment_charge_id="")


async def test_invoice_params(store):
    await store.init()
    svc, bot = PromotionService(store, CFG, FakeScheduler()), make_bot()
    promo = await new_order(svc, bot, "premium")
    kw = bot.send_invoice.call_args.kwargs
    assert kw["currency"] == "XTR" and kw["provider_token"] == ""
    assert kw["payload"] == f"promo:{promo['promo_id']}"
    assert len(kw["prices"]) == 1 and kw["prices"][0].amount == 149
    assert (await store.get_promotion(promo["promo_id"]))["status"] == "awaiting_payment"
    # invoice alone never activates
    assert bot.send_message.call_count == 0


async def test_precheckout_validation(store):
    await store.init()
    svc, bot = PromotionService(store, CFG, FakeScheduler()), make_bot()
    promo = await new_order(svc, bot)
    p = f"promo:{promo['promo_id']}"
    assert (await svc.validate_checkout(42, p, "XTR", 99))[0]
    assert not (await svc.validate_checkout(42, p, "USD", 99))[0]
    assert not (await svc.validate_checkout(42, p, "XTR", 1))[0]
    assert not (await svc.validate_checkout(7, p, "XTR", 99))[0]
    assert not (await svc.validate_checkout(42, "promo:NOPE", "XTR", 99))[0]
    await store.set_banned(42, True)
    assert not (await svc.validate_checkout(42, p, "XTR", 99))[0]


async def test_successful_payment_publishes_and_notifies(store):
    await store.init()
    sched = FakeScheduler()
    svc, bot = PromotionService(store, CFG, sched), make_bot()
    promo = await new_order(svc, bot)
    assert await svc.process_successful_payment(bot, 42, payment(promo["promo_id"])) == "activated"
    p = await store.get_promotion(promo["promo_id"])
    assert p["status"] == "active" and p["charge_id"] == "ch_1"
    assert abs((p["expires_at"] - p["published_at"]) - timedelta(hours=12)) < timedelta(seconds=1)
    assert sched.jobs[promo["promo_id"]] == p["expires_at"]
    group_call = bot.send_message.call_args_list[0].kwargs
    assert group_call["chat_id"] == CFG.group_id and "t.me/mychannel" in group_call["text"]
    user_call = bot.send_message.call_args_list[1].kwargs
    assert user_call["chat_id"] == 42 and user_call["text"] == texts.payment_success(PACKAGES["standard"])
    assert "Package: STANDARD" in user_call["text"] and "Paid: 99 Stars" in user_call["text"]
    assert (await store.payment_stats()) == (1, 99)


async def test_duplicate_payment_rejected(store):
    await store.init()
    svc, bot = PromotionService(store, CFG, FakeScheduler()), make_bot()
    promo = await new_order(svc, bot)
    assert await svc.process_successful_payment(bot, 42, payment(promo["promo_id"])) == "activated"
    calls = bot.send_message.call_count
    assert await svc.process_successful_payment(bot, 42, payment(promo["promo_id"])) == "duplicate"
    assert bot.send_message.call_count == calls  # nothing re-published
    assert (await store.payment_stats()) == (1, 99)


async def test_bad_amount_refunded_not_activated(store):
    await store.init()
    svc, bot = PromotionService(store, CFG, FakeScheduler()), make_bot()
    promo = await new_order(svc, bot)
    assert await svc.process_successful_payment(bot, 42, payment(promo["promo_id"], amount=1, charge="bad")) == "invalid"
    bot.refund_star_payment.assert_awaited_once_with(user_id=42, telegram_payment_charge_id="bad")
    assert (await store.get_promotion(promo["promo_id"]))["status"] == "awaiting_payment"
    assert all(c.kwargs.get("chat_id") != CFG.group_id for c in bot.send_message.call_args_list)
    assert (await store.payment_stats()) == (0, 0)


async def test_photo_ad_publication(store):
    await store.init()
    svc, bot = PromotionService(store, CFG, FakeScheduler()), make_bot()
    ad = {"type": "photo", "photo_file_id": "FILE123", "text": "Visit https://example.com",
          "entities": [{"type": "url", "offset": 6, "length": 19}]}
    promo = await new_order(svc, bot, "basic", ad)
    assert await svc.process_successful_payment(bot, 42, payment(promo["promo_id"], amount=49)) == "activated"
    kw = bot.send_photo.call_args.kwargs
    assert kw["chat_id"] == CFG.group_id and kw["photo"] == "FILE123"
    assert kw["caption"].startswith("Visit https://example.com") and kw["caption_entities"][0].type == "url"


async def test_approval_flow_and_reject_refund(store):
    await store.init()
    svc, bot = PromotionService(store, CFG, FakeScheduler()), make_bot()
    await store.set_setting("require_approval", True)
    a = await new_order(svc, bot)
    assert await svc.process_successful_payment(bot, 42, payment(a["promo_id"], charge="c1")) == "pending_approval"
    assert all(c.kwargs.get("chat_id") != CFG.group_id for c in bot.send_message.call_args_list)
    assert await svc.activate(bot, a["promo_id"]) == "activated"
    b = await new_order(svc, bot)
    await svc.process_successful_payment(bot, 42, payment(b["promo_id"], charge="c2"))
    assert await svc.reject(bot, b["promo_id"])
    bot.refund_star_payment.assert_awaited_with(user_id=42, telegram_payment_charge_id="c2")
    assert (await store.payment_stats()) == (1, 99)


async def test_expiry_deletes_and_notifies(store):
    await store.init()
    svc, bot = PromotionService(store, CFG, FakeScheduler()), make_bot()
    promo = await new_order(svc, bot)
    await svc.process_successful_payment(bot, 42, payment(promo["promo_id"]))
    msg_id = (await store.get_promotion(promo["promo_id"]))["group_message_id"]
    assert not await svc.expire(bot, promo["promo_id"])  # not yet due
    await store.update_promotion(promo["promo_id"], {"expires_at": utcnow() - timedelta(seconds=1)})
    assert await svc.expire(bot, promo["promo_id"])
    bot.delete_message.assert_awaited_once_with(chat_id=CFG.group_id, message_id=msg_id)
    assert bot.send_message.call_args.kwargs["text"] == texts.EXPIRED
    assert (await store.get_promotion(promo["promo_id"]))["status"] == "expired"
    assert not await svc.expire(bot, promo["promo_id"])  # idempotent


async def test_safe_delete_tolerates_missing_message(store):
    await store.init()
    svc, bot = PromotionService(store, CFG, FakeScheduler()), make_bot()
    bot.delete_message.side_effect = BadRequest("Message to delete not found")
    assert await svc.safe_delete(bot, 5) is False


async def test_restart_recovery(store):
    await store.init()
    svc, bot = PromotionService(store, CFG, FakeScheduler()), make_bot()
    a, b = await new_order(svc, bot), await new_order(svc, bot)
    await svc.process_successful_payment(bot, 42, payment(a["promo_id"], charge="a"))
    await svc.process_successful_payment(bot, 42, payment(b["promo_id"], charge="b"))
    await store.update_promotion(a["promo_id"], {"expires_at": utcnow() - timedelta(minutes=5)})  # expired while offline
    # "restart": brand new service + scheduler over the same persistent store
    sched2, bot2 = FakeScheduler(), make_bot()
    res = await PromotionService(store, CFG, sched2).recover(bot2)
    assert res == {"scheduled": 1, "expired": 1}
    assert list(sched2.jobs) == [b["promo_id"]]
    assert (await store.get_promotion(a["promo_id"]))["status"] == "expired"
    bot2.delete_message.assert_awaited_once()


async def test_extend_and_admin_delete(store):
    await store.init()
    sched = FakeScheduler()
    svc, bot = PromotionService(store, CFG, sched), make_bot()
    promo = await new_order(svc, bot)
    await svc.process_successful_payment(bot, 42, payment(promo["promo_id"]))
    old = sched.jobs[promo["promo_id"]]
    new = await svc.extend(bot, promo["promo_id"], 3)
    assert new - old == timedelta(hours=3) and sched.jobs[promo["promo_id"]] == new
    assert await svc.admin_delete(bot, promo["promo_id"])
    assert promo["promo_id"] not in sched.jobs
    assert (await store.get_promotion(promo["promo_id"]))["status"] == "deleted"


def test_extract_ad_validation():
    ok, _ = extract_ad(SimpleNamespace(photo=None, text="Check https://site.io", entities=[]))
    assert ok["type"] == "text"
    bad, err = extract_ad(SimpleNamespace(photo=None, text="x" * 4000, entities=[]))
    assert bad is None and "too long" in err
    bad, err = extract_ad(SimpleNamespace(photo=None, text=None))
    assert bad is None


async def test_job_scheduler_real_jobqueue():
    from telegram.ext import ApplicationBuilder
    app = ApplicationBuilder().token("123:abc").build()
    js = JobScheduler(app)
    js.schedule_expiry("PRX", utcnow() + timedelta(hours=1))
    js.schedule_expiry("PRX", utcnow() + timedelta(hours=2))  # reschedule replaces
    await asyncio.sleep(0)
    jobs = [j for j in app.job_queue.jobs() if j.name == "expire:PRX" and not j.removed]
    assert len(jobs) == 1 and jobs[0].data == "PRX"


async def test_healthz():
    async with TestClient(TestServer(build_web_app({}))) as client:
        r = await client.get("/healthz")
        assert r.status == 200
        assert await r.json() == {"status": "healthy", "service": "telegram-promotion-bot"}
