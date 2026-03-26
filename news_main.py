import asyncio
import os
import json
import time
from aiogram.exceptions import TelegramBadRequest
from aiohttp import web
import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiojobs.aiohttp import setup as setup_aiojobs
from aiojobs.aiohttp import get_scheduler_from_app

from news_db import NewsDatabase
from handlers.news_handlers import run_sync_db_loop, run_sync_membership_loop

from news_config import X_MAN_BOT_ID,BOT_TOKEN, DB_DSN, AES_KEY, BOT_MODE, WEBHOOK_PATH, WEBHOOK_HOST,SWITCHBOT_CHAT_ID,SWITCHBOT_THREAD_ID,SWITCHBOT_TOKEN
from utils.aes_crypto import AESCrypto
from utils.base62_converter import Base62Converter



bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
db = NewsDatabase(DB_DSN, max_size=2)

_bot_username: str = ""

lz_var_start_time = time.time()
lz_var_cold_start_flag = True
crypto = AESCrypto(AES_KEY)


switch_bot = Bot(token=SWITCHBOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
async def say_hello(text:str = 'Started news bot!'):
    me = await bot.get_me()
    bot_name = me.username if me and me.username else "UnknownSwitchBot"
    bot_id = me.id if me and me.id else 0
    try:
        await switch_bot.send_message(
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


async def safe_reply(message: Message, text: str, **kwargs):
    """
    尝试使用 reply 回复消息，失败时 fallback 为 send_message

    :param message: 原始消息对象
    :param text: 要发送的文本内容
    :param kwargs: 可传递 parse_mode、reply_markup 等其他参数
    """
    try:
        await message.reply(text, **kwargs)
    except TelegramBadRequest as e:
        if "message to be replied not found" in str(e):
            await message.bot.send_message(
                chat_id=message.chat.id,
                text=text,
                **kwargs
            )
        else:
            raise



async def close_resources():
    try:
        await db.close()
    except Exception as e:
        print(f"⚠️ 关闭数据库连接失败: {e}", flush=True)

    for session_name, current_bot in (("bot", bot), ("switch_bot", switch_bot)):
        try:
            await current_bot.session.close()
        except Exception as e:
            print(f"⚠️ 关闭 {session_name} session 失败: {e}", flush=True)

# 等待老板(12343)回传媒体的挂起请求：token -> {"future": Future, "news_id": int, "file_unique_id": str}
pending_fuid_requests: dict[str, dict] = {}




def parse_button_str(button_str: str) -> InlineKeyboardMarkup | None:
    """
    解析格式：
    按钮1 - http://t.me/... && 按钮2 - http://t.me/...
    按钮3 - http://t.me/...
    """
    if not button_str:
        return None
    keyboard: list[list[InlineKeyboardButton]] = []
    for line in button_str.strip().split("\n"):
        row: list[InlineKeyboardButton] = []
        for part in line.split("&&"):
            part = part.strip()
            if " - " in part:
                text, url = part.split(" - ", 1)
                row.append(InlineKeyboardButton(text=text.strip(), url=url.strip()))
        if row:
            keyboard.append(row)
    return InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None


@dp.message(Command("start"))
async def start_handler(message: Message, command: CommandObject):
    args = command.args
    if args and args.startswith("s_"):
        encrypted = args[2:]
        try:
            decrypted = crypto.aes_decode(encrypted)
            parts = decrypted.split(";")
            if len(parts) != 3:
                raise ValueError("格式不正确")

            business_type = {"yz": "stone", "sl": "salai"}.get(parts[0], "unknown")
            expire_ts = Base62Converter.base62_to_decimal(parts[1])
            user_id = Base62Converter.base62_to_decimal(parts[2])
            # 你的编码是从 2025-01-01 00:00:00 起点（1735689600）
            expire_ts = int(expire_ts) + 1735689600

            if expire_ts < time.time():
                await message.answer("⚠️ 此订阅链接已过期。")
                return

            await db.init()
            await db.upsert_user_and_seed_latest_task(user_id, business_type, expire_ts)

            await message.answer(
                "✅ 你已成功订阅！\r\n📅 有效期至："
                f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expire_ts))}。"
            )
        except Exception as e:
            await message.answer(f"⚠️ 链接解析失败：{str(e)}")
    else:
        await message.answer("🤖 哥哥您好，我是鲁仔")


@dp.message(Command("show"))
async def show_news_handler(message: Message, command: CommandObject):
    try:
        news_id = int((command.args or "").strip())
    except (ValueError, AttributeError):
        await safe_reply(message, "⚠️ 请输入正确的新闻 ID，例如 /show 1")
        return

    await db.init()
    record = await db.get_news_media_by_id(news_id)

    if not record:
        await safe_reply(message, "⚠️ 未找到指定 ID 的新闻")
        return

    keyboard = parse_button_str(record["button_str"])
    if record["file_type"] == "photo" and record["file_id"]:
        await message.bot.send_photo(
            chat_id=message.chat.id,
            photo=record["file_id"],
            caption=record["text"],
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
    else:
        await safe_reply(message, "⚠️ 该新闻没有有效的照片或不支持的媒体类型")


@dp.message(Command("push"))
async def push_news_handler(message: Message, command: CommandObject):
    try:
        news_id = int((command.args or "").strip())
    except (ValueError, AttributeError):
        await safe_reply(message, "⚠️ 请输入正确的新闻 ID，例如 /push 1")
        return

    await db.init()
    business_type = await db.get_business_type_by_news_id(news_id)
    if not business_type:
        await safe_reply(message, "⚠️ 未找到指定 ID 的新闻")
        return

    business_type = business_type or "news"
    await db.create_send_tasks(news_id, business_type)
    await safe_reply(message, f"✅ 已将新闻 ID = {news_id} 加入 {business_type} 业务类型的推送任务队列")



# 当收到消息中包含媒体（照片、视频或文档）且发送者不是老板(12343)时，尝试解析 caption 中的 JSON 来创建或更新新闻记录
@dp.message(lambda msg: (msg.photo or msg.video or msg.document) and msg.from_user.id != X_MAN_BOT_ID)
async def receive_media(message: Message):
    print(f"📥 收到消息：{message.text or '无文本'}", flush=True)
    caption = message.caption or ""
    try:
        result = json.loads(caption)
    except Exception:
        return

    if not isinstance(result, dict) or "caption" not in result:
        return

    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
    elif message.video:
        file_id = message.video.file_id
        file_type = "video"
    elif message.document:
        file_id = message.document.file_id
        file_type = "document"
    else:
        return

    me = await message.bot.get_me()

    # content_id 解析
    content_id_raw = result.get("content_id")
    try:
        content_id = int(content_id_raw) if content_id_raw is not None else None
    except (ValueError, TypeError):
        await safe_reply(message, "⚠️ content_id 不是合法的数字或缺失")
        return

    # 局部 payload，避免并发污染
    await db.init()
    business_type = result.get("business_type", "news")
    title = (result.get("title") or "Untitled").strip() or "Untitled"
    payload = {
        "content_id": content_id,
        "text": result.get("caption", ""),
        "file_id": file_id,
        "file_type": file_type,
        "button_str": result.get("button_str"),
        "bot_name": me.username,
        "business_type": result.get("business_type"),
    }

    existing_news_id = await db.get_news_id_by_content_business(
        payload["content_id"], payload["business_type"]
    )

    if existing_news_id:
        await db.update_news_by_id(news_id=existing_news_id, **payload)
        await safe_reply(message, f"🔁 已更新新闻 ID = {existing_news_id}")
        print(f"🔁 已更新新闻 ID = {existing_news_id}", flush=True)
        await db.create_send_tasks(existing_news_id, business_type)
    else:
        news_id = await db.insert_news(title=title, **payload)
        await safe_reply(message, f"✅ 已新增新闻并建立任务，新闻 ID = {news_id}")
        print(f"✅ 已新增新闻并建立任务，新闻 ID = {news_id}", flush=True)
        await db.create_send_tasks(news_id, business_type)

#收到X_MAN的回覆
@dp.message(lambda msg: (msg.photo or msg.video or msg.document) and msg.from_user.id == X_MAN_BOT_ID)
async def receive_file_material(message: Message):
    # 必须是回复别人的消息
    if not message.reply_to_message:
        print("⛔ 忽略：这不是对任何消息的回复。", flush=True)
        return
    
    # 仅当这是“回复本 Bot 发送的消息”时才继续
    me = await message.bot.get_me()
    bot_username = me.username

    replied = message.reply_to_message
    if not replied.from_user or replied.from_user.id != me.id:
        print(f"⛔ 忽略：这不是对本 Bot 的消息的回复（reply.from_user.id="
              f"{getattr(replied.from_user, 'id', None)}, bot.id={me.id}）。", flush=True)
        return

    # ① 打印被回复的“原消息”的文字（caption 优先，其次 text）
    orig_text = replied.caption or replied.text or "(无文本)"
    print(f"1. 🧵 被回复的原消息文本：{orig_text}", flush=True)

    # （可选）如果原消息也带媒体，这里简单标注一下类型与 file_id
    o_type, o_fid = None, None
    if replied.photo:
        o_type, o_fid = "photo", replied.photo[-1].file_id
    elif replied.video:
        o_type, o_fid = "video", replied.video.file_id
    elif replied.document:
        o_type, o_fid = "document", replied.document.file_id
    if o_type:
        print(f"2. 🧵 原消息媒体：type={o_type}, file_id={o_fid}", flush=True)


    # ② 打印“这条回复消息”的内容（类型、file_id、caption/text）
    m_type, m_fid, m_fuid = None, None, None
    if message.photo:
        m_type, m_fid, m_fuid = "photo", message.photo[-1].file_id, message.photo[-1].file_unique_id
    elif message.video:
        m_type, m_fid, m_fuid = "video", message.video.file_id, message.video.file_unique_id
    elif message.document:
        m_type, m_fid, m_fuid = "document", message.document.file_id, message.document.file_unique_id
    m_text = message.caption or message.text or "(无文本)"
    print(f"3. 📥 回复内容：type={m_type}, file_id={m_fid}, m_fuid='{m_fuid}' , bot_username='{bot_username}'", flush=True)

    await db.set_news_file_id(m_fuid, m_fid, bot_username)
    existing_news = await db.get_news_id_by_thumb_file_unique_id(m_fuid)

    if (existing_news and existing_news.get("id")):
        await db.create_send_tasks(int(existing_news['id']), existing_news['business_type'])



# === 执行正常新闻批次推送 ===
async def periodic_sender(db: NewsDatabase):
    from news_sender import send_news_batch

    while True:
        # === 执行正常新闻批次推送 ===
        try:
            print("  ✅ 派送中", flush=True)
            await send_news_batch(db, bot)
        except Exception as e:
            print(f"❌ send_news_batch 异常: {e}", flush=True)



        # === 执行补档逻辑 ===
        try:
            print("🔍 检查需要补档的新闻(PG)...", flush=True)
            await db.init()
            rows = await db.find_missing_media_records(limit=10)  # 需返回: id, business_type, thumb_file_unique_id
            if len(rows) == 0:
                print("  ✅ 没有需要补档的新闻", flush=True)
            else:
                for row in rows:
                    news_id = row["id"]
                    fuid = row["thumb_file_unique_id"]
                    bt = row.get("business_type") or "news"
                    try:
                        # 记挂起映射：FUID -> {news_id, business_type, ts}
                        pending_fuid_requests[fuid] = {
                            "news_id": news_id,
                            "business_type": bt,
                            "ts": time.time(),
                        }
                        print(f"➡️ 请求老板( {X_MAN_BOT_ID}) 补档 news_id={news_id}, fuid={fuid}", flush=True)
                        await bot.send_message(X_MAN_BOT_ID, fuid)
                        await asyncio.sleep(1)
                        await db.add_retry_count_for_news_id(news_id)
                    except Exception as e:
                        await switch_bot.send_message(chat_id=X_MAN_BOT_ID,  text=f"|_kick_|@{_bot_username}")
                        await switch_bot.send_message(chat_id=SWITCHBOT_CHAT_ID, message_thread_id=SWITCHBOT_THREAD_ID, text=f"⚠️ 无法联系老板( {X_MAN_BOT_ID} ) 已kick,  补档 news_id={news_id}, fuid={fuid}")
                        print(f"⚠️ 发送请求给 {X_MAN_BOT_ID} 失败: {e}", flush=True)
                        # 失败也清掉挂起，避免僵尸条目
                        # pending_fuid_requests.pop(fuid, None)
                        continue
        except Exception as e:
            print(f"❌ periodic_sender 补档流程异常: {e}", flush=True)


        # === 间隔 60 秒再跑下一轮 ===
        await asyncio.sleep(15)

async def sync_db():
    await asyncio.gather(
        run_sync_membership_loop(
            interval_seconds=300,
            course_code="xlj",
            business_type="xlj",
            batch_size=5000,
        ),
        run_sync_db_loop(interval_seconds=300, batch_size=5),
    )

async def on_startup(bot: Bot):
    global lz_var_cold_start_flag
    lz_var_cold_start_flag = False
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(f"{WEBHOOK_HOST}{WEBHOOK_PATH}")


async def health(request):
    uptime = time.time() - lz_var_start_time
    if lz_var_cold_start_flag or uptime < 10:
        return web.Response(text="⏳ Bot 正在唤醒，请稍候...", status=503)
    return web.Response(text="✅ Bot 正常运行", status=200)


async def on_shutdown(app):
    await close_resources()


async def keep_alive_ping():
    url = f"{WEBHOOK_HOST}{WEBHOOK_PATH}" if BOT_MODE == "webhook" else f"{WEBHOOK_HOST}/"
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url) as resp:
                    print(f"🌐 Keep-alive ping {url} status {resp.status}", flush=True)
            except Exception as e:
                print(f"⚠️ Keep-alive ping failed: {e}", flush=True)
            await asyncio.sleep(300)


async def main():
    should_close_resources = True
    try:
        await db.init()
        await db.ensure_schema()
        global bot, _bot_username
        me = await bot.get_me()
        _bot_username = me.username or ""
        print(f'你的用户名: {me.username}',flush=True)
        print(f'你的ID: {me.id}')
        print(f'你的名字: {me.first_name} {me.last_name or ""}')
    

        await say_hello()

        if BOT_MODE == "webhook":
            dp.startup.register(on_startup)
            app = web.Application()
            app.router.add_get("/", health)

            setup_aiojobs(app)

            SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
            setup_application(app, dp, bot=bot)

            async def on_app_start(app):
                await db.init()
                await get_scheduler_from_app(app).spawn(sync_db())
                await get_scheduler_from_app(app).spawn(periodic_sender(db))

            asyncio.create_task(keep_alive_ping())
            app.on_startup.append(on_app_start)
            app.on_shutdown.append(on_shutdown)

            port = int(os.environ.get("PORT", 8080))
            should_close_resources = False
            await web._run_app(app, host="0.0.0.0", port=port)
        else:
            await bot.delete_webhook(drop_pending_updates=True)
            loop = asyncio.get_event_loop()
            loop.create_task(sync_db())
            loop.create_task(periodic_sender(db))
            should_close_resources = False
            await dp.start_polling(
                bot,
                skip_updates=True,
                timeout=60,
                relax=3.0
            )
    finally:
        if should_close_resources:
            await close_resources()


if __name__ == "__main__":
    asyncio.run(main())
