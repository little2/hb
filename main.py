import asyncio
from aiohttp import web
import redis.asyncio as redis
from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.types import TelegramObject
from typing import Any, Awaitable, Callable, Dict
from utils.db.tgone_mysql import MySQLPool

from config import BOT_MODE
from config import WEBHOOK_HOST, WEBHOOK_PATH, WEBAPP_HOST, WEBAPP_PORT, WEBHOOK_SECRET
from config import BOT_TOKEN, REDIS_URL, MYSQL_DB, MYSQL_USER, MYSQL_PASSWORD, MYSQL_UNIX_SOCKET
assert BOT_TOKEN, "BOT_TOKEN is required"
assert REDIS_URL, "REDIS_URL is required"
assert MYSQL_DB, "MYSQL_DB_NAME is required"

from infra.redis_layer import RedisLayer
from handlers.hongbao_handlers import router
from handlers.hongbao_handlers import AppCtx

class CtxMiddleware(BaseMiddleware):
    def __init__(self, ctx: AppCtx):
        super().__init__()
        self.ctx = ctx

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        data["ctx"] = self.ctx
        return await handler(event, data)


async def build_app() -> tuple[Bot, Dispatcher, RedisLayer]:
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    # Redis (Render KV)
    try:
        # Redis (Render KV)
        rds = redis.from_url(
            REDIS_URL,
            decode_responses=False,
            health_check_interval=30,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        rlayer = RedisLayer(rds)
        await rlayer.load_scripts()

        await MySQLPool.init_pool(
            unix_socket=MYSQL_UNIX_SOCKET,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            db=MYSQL_DB,
            autocommit=True,
            charset="utf8mb4",
            minsize=1,
            maxsize=10,
        )
    except Exception:
        print("Failed to connect to Redis or MySQL. Please check your configuration.")
        raise

    ctx = AppCtx(r=rlayer, bot=bot)
    dp.update.outer_middleware(CtxMiddleware(ctx))
    dp.include_router(router)
    return bot, dp, rlayer


async def run_polling(bot: Bot, dp: Dispatcher, rlayer: RedisLayer):
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await rlayer.rds.close()
        await bot.session.close()


async def run_webhook(bot: Bot, dp: Dispatcher, rlayer: RedisLayer):
    if not WEBHOOK_HOST:
        raise RuntimeError("WEBHOOK_HOST is required for BOT_MODE=webhook")

    webhook_url = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET or None,
        drop_pending_updates=True,
    )

    app = web.Application()

    # aiogram webhook handler
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET or None,
    ).register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=WEBAPP_HOST, port=WEBAPP_PORT)
    await site.start()

    # keep running
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await bot.delete_webhook(drop_pending_updates=False)
        await runner.cleanup()
        await rlayer.rds.close()
        await bot.session.close()

async def main():
    bot, dp, rlayer = await build_app()

    bot_info = await bot.get_me()
    print(f"Bot started as @{bot_info.username} (id: {bot_info.id})")

    if BOT_MODE == "polling":
        await run_polling(bot, dp, rlayer)
    elif BOT_MODE == "webhook":
        await run_webhook(bot, dp, rlayer)
    else:
        raise RuntimeError("BOT_MODE must be 'polling' or 'webhook'")


if __name__ == "__main__":
    asyncio.run(main())
