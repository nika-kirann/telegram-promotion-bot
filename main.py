"""Entry point: runs the Telegram bot (long polling) and the /healthz server in one asyncio loop."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from telegram import Update
from telegram.error import InvalidToken, NetworkError, TimedOut
from telegram.ext import Application, ApplicationBuilder, Defaults

from promobot.config import Config
from promobot.handlers import COMMANDS, JobScheduler, invitation_job, register
from promobot.service import PromotionService
from promobot.storage import MemoryStore, MongoStore
from promobot.web import start_web


class RedactFilter(logging.Filter):
    """Strip secrets from every log record (httpx logs full request URLs including the token)."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in self.secrets:
            msg = msg.replace(s, "***")
        record.msg, record.args = msg, None
        return True


def setup_logging(config: Config | None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    if config:
        handler.addFilter(RedactFilter([config.bot_token, config.mongodb_uri]))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


log = logging.getLogger("main")


def build_application(config: Config, service_factory) -> Application:
    app = (ApplicationBuilder().token(config.bot_token)
           .connect_timeout(20).read_timeout(30).write_timeout(30).pool_timeout(20)
           .get_updates_read_timeout(40).build())
    service = service_factory(JobScheduler(app))
    app.bot_data["config"] = config
    app.bot_data["service"] = service
    register(app)
    return app


async def run() -> None:
    try:
        config = Config.from_env()
    except (RuntimeError, ValueError) as e:
        setup_logging(None)
        log.critical("Configuration error: %s", e)
        sys.exit(2)
    setup_logging(config)

    state = {"bot_running": False}
    runner = await start_web(state, config.port)
    log.info("Health server listening on 0.0.0.0:%d (/healthz)", config.port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    if config.mongodb_uri:
        store = MongoStore(config.mongodb_uri, config.mongodb_db)
        state["store"] = "mongodb"
    else:
        log.warning("MONGODB_URI not set: using IN-MEMORY storage. Data will be lost on restart. Not for production!")
        store = MemoryStore()
        state["store"] = "memory"

    app = build_application(config, lambda sched: PromotionService(store, config, sched))
    service: PromotionService = app.bot_data["service"]

    # Bring up DB + Telegram with retry/backoff while /healthz keeps serving.
    delay = 5
    while not stop.is_set():
        try:
            await store.init()
            await app.initialize()
            break
        except InvalidToken:
            log.error("BOT_TOKEN rejected by Telegram. Fix the BOT_TOKEN env var. Retrying in %ds", delay)
        except (NetworkError, TimedOut) as e:
            log.warning("Telegram unreachable (%s). Retrying in %ds", e, delay)
        except Exception as e:  # e.g. MongoDB not reachable
            log.error("Startup failed (%s: %s). Retrying in %ds", type(e).__name__, e, delay)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        delay = min(delay * 2, 300)

    if stop.is_set():
        await runner.cleanup()
        return

    try:
        await app.bot.set_my_commands(COMMANDS)
        await app.start()
        await service.recover(app.bot)
        app.job_queue.run_repeating(invitation_job, interval=300, first=60, name="invitation")
        # drop_pending_updates=False keeps successful_payment updates received while we were offline.
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
        state["bot_running"] = True
        log.info("Bot @%s is running", app.bot.username)
        await stop.wait()
    finally:
        log.info("Shutting down gracefully…")
        state["bot_running"] = False
        if app.updater and app.updater.running:
            await app.updater.stop()
        if app.running:
            await app.stop()
        await app.shutdown()
        await store.close()
        await runner.cleanup()
        log.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(run())
