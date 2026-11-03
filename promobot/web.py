"""Lightweight aiohttp health server (shares the bot's asyncio loop, never blocks polling)."""
from aiohttp import web

HEALTH = {"status": "healthy", "service": "telegram-promotion-bot"}


def build_web_app(state: dict) -> web.Application:
    async def healthz(_request):
        return web.json_response(HEALTH)

    async def readyz(_request):
        ok = state.get("bot_running", False)
        return web.json_response({"bot_running": ok, "store": state.get("store", "unknown")}, status=200 if ok else 503)

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", healthz)
    app.router.add_get("/readyz", readyz)
    return app


async def start_web(state: dict, port: int) -> web.AppRunner:
    runner = web.AppRunner(build_web_app(state), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host="0.0.0.0", port=port).start()
    return runner
