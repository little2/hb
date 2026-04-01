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
from config import X_MAN_BOT_ID, BOT_TOKEN, REDIS_URL, MYSQL_DB, MYSQL_USER, MYSQL_PASSWORD, MYSQL_UNIX_SOCKET, SWITCHBOT_TOKEN,SWITCHBOT_CHAT_ID,SWITCHBOT_THREAD_ID
import lz_var
from utils.tpl import Tplate

assert BOT_TOKEN, "BOT_TOKEN is required"
assert REDIS_URL, "REDIS_URL is required"
assert MYSQL_DB, "MYSQL_DB_NAME is required"
import lz_var 

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


async def load_templates(ctx: AppCtx):
    import os
    import json

    bot_info = await ctx.bot.get_me()
    bot_name = bot_info.username
    lz_var.bot = ctx.bot
    lz_var.bot_username = bot_name
    lz_var.bot_id = bot_info.id
    lz_var.x_man_bot_id = X_MAN_BOT_ID
    config_path = f"{bot_name}_skins.json"
    # print(f"🔍 载入或生成皮肤配置文件：{config_path}")

    load_result = await Tplate.load_or_create_skins( get_file_ids_fn=MySQLPool.get_file_id_by_file_unique_id)
    if(load_result.get("ok") == 1):
        lz_var.skins = load_result.get("skins", {})
       
    else:
        print(f"⚠️ 加载皮肤失败: {load_result.get('handshake')}", flush=True)


    # 默认注入 PGPool（外部可传入别的实现）
    default_skins = {
        "push_cover": {"file_id": "", "file_unique_id": "AQAD9wtrG3ZWyER-"}
    }

    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                skins = json.load(f)
                # print(f"✅ 载入已有皮肤配置文件：{skins}")
        except Exception as e:
            print(f"⚠️ 无法读取 {config_path}，将重新生成：{e}")
            skins = default_skins.copy()
    else:
        skins = default_skins.copy()

    # --- 写入文件（即便有缺） ---
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(skins, f, ensure_ascii=False, indent=4)
    return {"ok":1, "skins": skins}




async def say_hello(text:str = 'Started bot!'):
    me = await lz_var.bot.get_me()
    bot_name = me.username if me and me.username else "UnknownSwitchBot"
    bot_id = me.id if me and me.id else 0
    try:
        await lz_var.switchbot.send_message(
            chat_id=f"-100{SWITCHBOT_CHAT_ID}",
            message_thread_id=SWITCHBOT_THREAD_ID,
            text=f"[{bot_name} - {bot_id}] {text}",
        )
    except Exception as e:
        print(
            f"⚠️ say_hello 发送失败: chat_id={SWITCHBOT_CHAT_ID}, "
            f"thread_id={SWITCHBOT_THREAD_ID}, error={e}",
            flush=True,
        )


async def build_app() -> tuple[Bot, Dispatcher, RedisLayer]:

    switchbot = Bot(token=SWITCHBOT_TOKEN)
    lz_var.switchbot = switchbot
    switchbot_info = await switchbot.get_me()

    bot = Bot(BOT_TOKEN)
    me = await bot.get_me()
    bot_name = me.username if me and me.username else "UnknownSwitchBot"
    bot_id = me.id if me and me.id else 0

    try:
        await switchbot.send_message(
            chat_id=f"-100{SWITCHBOT_CHAT_ID}",
            message_thread_id=SWITCHBOT_THREAD_ID,
            text=f"[{bot_name} - {bot_id}] Start",
        )
    except Exception as e:
        print(
            f"⚠️ say_hello 发送失败: chat_id={SWITCHBOT_CHAT_ID}, "
            f"thread_id={SWITCHBOT_THREAD_ID}, error={e}",
            flush=True,
        )



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
    except Exception:
        print("Failed to connect to Redis. Please check your configuration.")
        raise

    try:
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
        print("Failed to connect to MySQL. Please check your configuration.")
        raise

    ctx = AppCtx(r=rlayer, bot=bot)

    bot_info = await bot.get_me()
    print(f"Bot started as @{bot_info.username} (id: {bot_info.id})")

    ret = await load_templates(ctx)  # 预先加载模板（可选，首次运行会生成默认文件）
    print(f"Template load result: {ret}")

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

    if BOT_MODE == "polling":
        await run_polling(bot, dp, rlayer)
    elif BOT_MODE == "webhook":
        await run_webhook(bot, dp, rlayer)
    else:
        raise RuntimeError("BOT_MODE must be 'polling' or 'webhook'")


if __name__ == "__main__":
    asyncio.run(main())
