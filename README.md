# Telegram Promotion Bot ⭐

Sell timed promotional posts in your Telegram group, paid with **Telegram Stars (XTR)**.
Ads are published automatically after payment (or after admin approval), removed automatically on expiry,
and everything survives restarts (MongoDB).

## Features
- Packages: 🥉 BASIC 3h / 49 ⭐ · 🥈 STANDARD 12h / 99 ⭐ · 🥇 PREMIUM 24h / 149 ⭐
- `/start` menu: Buy Promotion · My Promotions · Rules & Guidelines · Contact Admin
- Ad submission: text, photo + caption, channel/group/website links (formatting & links preserved), preview with **Confirm / Edit**
- Native Stars invoice (`currency=XTR`, empty provider token). Promotions are activated **only** from `successful_payment`
  after re-validating payload, owner, currency and amount. Pre-checkout performs the same checks.
- Duplicate protection: unique index on `telegram_payment_charge_id`; atomic `awaiting_payment → paid` transition.
  Invalid payments are refunded automatically (`refundStarPayment`).
- Optional manual approval mode (reject = automatic Stars refund)
- Auto-publish to `GROUP_ID`, auto-delete on expiry, customer notifications, full history, UTC timestamps
- Restart recovery: overdue promos are expired immediately, the rest are re-scheduled
- `/admin`: stats, revenue (24h/7d/30d/all, per package), recent transactions, ban/unban, group announcement,
  delete active promotion, extend (+1/+3/+12/+24h), approve/reject, approval mode toggle, invitation frequency
- Periodic group invitation with a **🚀 BUY PROMOTION** deep-link button (Off/2/4/6/12/24h, only the latest invitation is kept)
- `GET /healthz` → `200 {"status":"healthy","service":"telegram-promotion-bot"}` (aiohttp, same asyncio loop as polling);
  `GET /readyz` → 200 only when the bot is polling
- Graceful shutdown on SIGTERM, network-error retries with backoff, bot token & DB URI redacted from logs

Commands: `/start` `/buy` `/myorders` `/packages` `/help` `/admin` (`/cancel` aborts the current step)

## Project layout
```
main.py               entry point (health server + polling, lifecycle)
promobot/config.py    env config, packages
promobot/storage.py   MongoStore (production) / MemoryStore (tests only)
promobot/service.py   payments, publication, expiry, recovery, refunds
promobot/handlers.py  Telegram handlers, keyboards, admin dashboard, jobs
promobot/texts.py     all message copy
promobot/web.py       /healthz server
tests/                pytest suite (mocked Telegram Bot, mongomock)
```

## 1. Telegram setup (BotFather)
1. Open @BotFather → `/newbot` → copy the token (`BOT_TOKEN`).
2. Stars payments need no provider setup — digital goods paid in XTR work for every bot.
3. Optional: `/setdescription`, `/setuserpic`. Commands are registered automatically at startup.
4. Add the bot to your group and **promote it to administrator** with at least:
   **Post messages** and **Delete messages** (needed to publish and remove ads).
5. Get the group ID (e.g. forward a group message to @RawDataBot / @userinfobot); supergroups look like `-100…` → `GROUP_ID`.
6. Get your own user ID from @userinfobot → `ADMIN_IDS` (comma-separated for several admins).
   Each admin must press **Start** in the bot once so it can message them.

## 2. MongoDB Atlas (free M0)
1. https://cloud.mongodb.com → create a free **M0** cluster.
2. Database Access → add a user with password.
3. Network Access → add `0.0.0.0/0` (Koyeb egress IPs are not fixed).
4. Connect → Drivers → copy the `mongodb+srv://…` string, insert the password → `MONGODB_URI`.
   Indexes are created automatically on first start.

> Without `MONGODB_URI` the bot falls back to in-memory storage (logged as a warning). Use only for local testing.

## 3. Deploy on Koyeb (Free Web Service)
1. Push this folder to a GitHub repository (do **not** commit `.env`).
2. Koyeb dashboard → **Create Service** → **Web Service** → **GitHub** → select the repo/branch.
3. Builder: **Dockerfile** (path `Dockerfile`, no build args/command override).
4. Instance: **Free** (eNano), region of your choice, **Scaling: 1 instance** (never more than 1 —
   two pollers on the same token cause `Conflict` errors).
5. Environment variables (use *Secret* type for the sensitive ones):
   | Name | Value |
   |---|---|
   | `BOT_TOKEN` | token from BotFather (secret) |
   | `GROUP_ID` | `-100…` |
   | `ADMIN_IDS` | `111111111,222222222` |
   | `MONGODB_URI` | Atlas connection string (secret) |
   | `PORT` | `8000` |
   | `ADMIN_CONTACT` | optional, username without @ |
6. Ports: **8000 / HTTP**, route path `/`.
7. Health checks: **HTTP**, port **8000**, path **`/healthz`** (grace period 20s+).
8. **Deploy**. Logs should show `Health server listening on 0.0.0.0:8000` and `Bot @yourbot is running`.
9. Test: open `https://<your-app>.koyeb.app/healthz`, then send `/start` to the bot and `/admin` from an admin account.

**Koyeb free-tier note:** free instances may scale to zero after a period without inbound HTTP traffic, which would
pause polling. If you notice this, point a free uptime monitor (e.g. UptimeRobot) at `https://<your-app>.koyeb.app/healthz`
every 5 minutes. Telegram keeps undelivered updates (incl. payments) for 24h, and the bot processes them on wake-up
(`drop_pending_updates=False`); overdue promotions are expired during recovery.

### Koyeb CLI alternative
```bash
koyeb app init telegram-promotion-bot \
  --git github.com/<you>/<repo> --git-branch main --git-builder docker \
  --instance-type free --regions fra --min-scale 1 --max-scale 1 \
  --ports 8000:http --routes /:8000 --checks 8000:http:/healthz \
  --env PORT=8000 --env GROUP_ID=-100... --env ADMIN_IDS=111 \
  --env BOT_TOKEN=@BOT_TOKEN --env MONGODB_URI=@MONGODB_URI   # @NAME = Koyeb secret
```

## Local run
```bash
cp .env.example .env   # fill in values
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python main.py
curl localhost:8000/healthz
pytest -q              # test suite
docker build -t promo-bot . && docker run --env-file .env -p 8000:8000 promo-bot
```

## Payment flow (security)
1. Confirm ad → order stored as `awaiting_payment` → `sendInvoice(currency="XTR", provider_token="", payload="promo:<ID>")`.
2. `pre_checkout_query` → validates order exists, belongs to payer, is still payable, currency XTR, exact amount, user not banned.
3. `successful_payment` → validates again, inserts payment with unique `charge_id` (duplicates ignored),
   atomic status transition, then publishes (or queues for review). Invalid payments are refunded.
4. Invoice creation alone never activates anything.

## Admin notes
- Approval mode ON: paid ads wait in **/admin → Pending review**; the timer starts when published. Reject refunds Stars.
- If publishing fails (bot not admin), the order becomes *Publish failed*; fix permissions, then Approve it from Pending review.
- Ban/unban by numeric user ID; banned users can't use the bot or pay.
