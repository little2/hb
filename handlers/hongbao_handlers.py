from __future__ import annotations
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyParameters, InputMediaPhoto, InputMediaVideo
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, BotCommandScopeDefault
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError,TelegramAPIError,TelegramNotFound,TelegramMigrateToChat, TelegramRetryAfter
from aiogram.utils.formatting import Text
import lz_var
import re

import html
import random
from functools import lru_cache

from config import (
    MIN_UNIT, MAX_COUNT, MAX_TOTAL_AMOUNT, DEFAULT_EXPIRE_MINUTES,
    GROUP_NOTICE_THROTTLE, GROUP_NOTICE_PER_SEC, DM_BLOCK_TTL_SEC,
    TARGET_CHAT_ID, TARGET_MESSAGE_THREAD_ID,REVIEW_CHAT_ID
)



from infra.redis_layer import RedisLayer, split_amounts
from services.hongbao_service import HongbaoService
from shared_config import SharedConfig
SharedConfig.load()
chat_cfg = SharedConfig.get("chat") or {}
school = chat_cfg.get("school") or {}

# 直接重新賦值，後續整個檔案用的 TARGET_CHAT_ID 都會是新值
TARGET_CHAT_ID = int(school.get("chat_id") or TARGET_CHAT_ID)
TARGET_MESSAGE_THREAD_ID = int(school.get("thread_id") or TARGET_MESSAGE_THREAD_ID)


from material import RP_SKINS, I18N

def _h(s: str) -> str:
    return html.escape(s or "", quote=False)

def pick_rp_skin() -> dict:
    if not RP_SKINS:
        raise RuntimeError("RP_SKINS is empty")
    return random.choice(RP_SKINS)

def tr(lang: str, key: str, **kwargs) -> str:
    pack = I18N.get(lang) or I18N["lj"]
    s = pack.get(key) or I18N["lj"].get(key) or key
    return s.format(**kwargs)

@lru_cache(maxsize=4)
def get_patterns(lang: str):
    pack = I18N.get(lang) or I18N["lj"]
    return (
        re.compile(pack["re_total"], re.M),
        re.compile(pack["re_count"], re.M),
        re.compile(pack["re_header"], re.M),
        re.compile(pack["re_sn"], re.M),
        re.compile(pack["re_time"], re.M),
        re.compile(pack["re_item"], re.M),
    )

router = Router()
pending_cover_upload_users: set[int] = set()

CALL_MENU_TTL_SEC = 600
CALL_MENU_ACT_ID = "20000008"



def _build_group_message_link(chat_id: int, message_id: int, thread_id: int | None = None) -> str:
    chat_id_str = str(chat_id)
    if not chat_id_str.startswith("-100") or message_id <= 0:
        return ""

    internal_chat_id = chat_id_str[4:]
    if thread_id:
        return f"https://t.me/c/{internal_chat_id}/{thread_id}/{message_id}"
    return f"https://t.me/c/{internal_chat_id}/{message_id}"

# from aiogram import Router, F
# from aiogram.types import Message

@router.message(F.pinned_message)
async def delete_pin_service(message: Message):
    await message.delete()

@router.message(F.chat.type == ChatType.PRIVATE, F.photo | F.video)
async def on_photo(message: Message):
    print(f"Received media message in private chat: {message}", flush=True)
    user_id = message.from_user.id if message.from_user else 0

    if user_id in pending_cover_upload_users:
        if not message.photo:
            await message.reply("请上传图片作为红包封面。", reply_markup=start_menu_back_keyboard())
            return

        media = message.photo[-1]
        file_type = "photo"

        bot_name = getattr(lz_var, "bot_username", "") or str(message.bot.id)
        try:
            await HongbaoService.upsert_file_extension(
                file_type=file_type,
                file_unique_id=media.file_unique_id,
                file_id=media.file_id,
                bot=bot_name,
                user_id=user_id,
            )

            ok = await HongbaoService.upsert_hongbao_user_setting(
                user_id=user_id,
                cover_type=file_type,
                cover_file_id=media.file_id,
                cover_file_unique_id=media.file_unique_id,
            )
        except Exception as e:
            print(f"[HB_USER_SETTING] upsert failed: {e}", flush=True)
            ok = False

        if ok:
            pending_cover_upload_users.discard(user_id)
            await message.reply(
                "✅ 红包封面已更新。\n"
                f"file_unique_id: {media.file_unique_id}",
                reply_markup=start_menu_back_keyboard(),
            )
        else:
            await message.reply("❌ 红包封面保存失败，请稍后再试。", reply_markup=start_menu_back_keyboard())
        return

    media = None
    file_type = ""
    if message.photo:
        media = message.photo[-1]
        file_type = "photo"
    elif message.video:
        media = message.video
        file_type = "video"

    if not media:
        return

    await message.reply(
        "📸 已识别到图片（最大尺寸）\n"
        f"file_unique_id: {media.file_unique_id}\n"
        f"file_type: {file_type}\n"
    )

    if media:
        try:
            bot_name = getattr(lz_var, "bot_username", "") or str(message.bot.id)
            await HongbaoService.upsert_file_extension(
                file_type=file_type,
                file_unique_id=media.file_unique_id,
                file_id=media.file_id,
                bot=bot_name,
                user_id=message.from_user.id if message.from_user else None,
            )
        except Exception as e:
            print(f"[FILE_EXTENSION] upsert failed: {e}", flush=True)

@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.chat.id == REVIEW_CHAT_ID
)
async def on_target_group_media(message: Message):
    media = None
    file_type = ""
    print(f"on_target_group_media -Received media message: {message}",flush=True)

    if message.photo:
        media = message.photo[-1]
        file_type = "photo"
    elif message.video:
        media = message.video
        file_type = "video"

    if media:
        try:
            bot_name = getattr(lz_var, "bot_username", "") or str(message.bot.id)
            await HongbaoService.upsert_file_extension(
                file_type=file_type,
                file_unique_id=media.file_unique_id,
                file_id=media.file_id,
                bot=bot_name,
                user_id=message.from_user.id if message.from_user else None,
            )
        except Exception as e:
            print(f"[FILE_EXTENSION] upsert failed: {e}", flush=True)

    


def kb_claim(hid: int, lang: str, hb_type:str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=tr(lang, "btn_claim"),
        callback_data=f"hb_claim:{hid}:{hb_type}"
    )]])

def kb_redeem(hid: int, amount: int, lang: str, hb_type: str, activity_link: str | None = None) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=tr(lang, "btn_redeem", amount=amount),
            callback_data=f"hb_redeem:{hid}:{hb_type}"
        )
    ]

    if activity_link:
        buttons.append(InlineKeyboardButton(
            text=tr(lang, "btn_activity"),
            url=activity_link
        ))

    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def kb_expired(lang: str, hb_type: str, activity_link: str | None = None) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=tr(lang, "btn_expired"),
            callback_data=f"hb_expired:{hb_type}"
        )
    ]
    if activity_link:
        buttons.append(InlineKeyboardButton(
            text=tr(lang, "btn_activity"),
            url=activity_link
        ))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])

def kb_done(lang: str, hb_type: str, activity_link: str | None = None) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=tr(lang, "btn_done"),
            callback_data=f"hb_done:{hb_type}"
        )
    ]
    if activity_link:
        buttons.append(InlineKeyboardButton(
            text=tr(lang, "btn_activity"),
            url=activity_link
        ))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def _normalize_hb_type(hb_type: str | None) -> str:
    return "hb" if hb_type == "hb" else "lj"


def _get_callback_hb_type(callback_data: str | None) -> str:
    parts = (callback_data or "").split(":")
    if len(parts) >= 3:
        return _normalize_hb_type(parts[2])
    if len(parts) >= 2 and parts[0] in {"hb_done", "hb_expired"}:
        return _normalize_hb_type(parts[1])
    return "lj"

def start_menu_keyboard() -> InlineKeyboardMarkup:
    guider_bot_name = getattr(lz_var, "guider_bot_name", "") or SharedConfig.get("guider_bot_name") or "unknown_bot"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧧 红包", callback_data="hb_menu:settings")],
            [InlineKeyboardButton(text="✨ 弟弟库打Call区", callback_data="hb_menu:call")],
            [InlineKeyboardButton(text="🐲 小龙阳", url=f"https://t.me/{guider_bot_name}?start=map")],
        ]
    )


def _call_menu_cache_key(user_id: int) -> str:
    return f"hb:call:list:{user_id}"


def _normalize_call_row(row: dict) -> dict:
    return {
        "cutedd_id": int(row.get("cutedd_id") or 0),
        "dd_thread_id": int(row.get("dd_thread_id") or 0),
        "file_unique_id": row.get("file_unique_id") or "",
        "file_id": row.get("file_id") or "",
        "file_type": row.get("file_type") or "photo",
        "file_caption": row.get("file_caption") or "",
        "send_status": int(row.get("send_status") or 0),
    }


async def _request_missing_call_file_ids(rows: list[dict], max_count: int = 20) -> None:
    missing_rows = [_normalize_call_row(row) for row in rows if not (row.get("file_id") or "").strip()]
    if not missing_rows:
        return

    x_man_bot_id = int(getattr(lz_var, "x_man_bot_id", 0) or 0)
    bot = getattr(lz_var, "bot", None)
    if not x_man_bot_id or bot is None:
        print("[HB_CALL] skip requesting missing file_id: x-man bot not ready", flush=True)
        return

    requested: set[str] = set()
    sent_count = 0
    for row in missing_rows:
        file_unique_id = (row.get("file_unique_id") or "").strip()
        if not file_unique_id or file_unique_id in requested:
            continue

        requested.add(file_unique_id)
        try:
            await bot.send_message(chat_id=x_man_bot_id, text=file_unique_id)
            sent_count += 1
            print(f"[HB_CALL] requested missing file_id for {file_unique_id}", flush=True)
        except Exception as e:
            print(f"[HB_CALL] request to x-man failed: {e} - {x_man_bot_id}", flush=True)

        if sent_count >= max_count:
            break


def _run_missing_call_file_ids_in_background(rows: list[dict], max_count: int = 20) -> None:
    task = asyncio.create_task(_request_missing_call_file_ids(rows, max_count=max_count))

    def _log_task_result(done_task: asyncio.Task) -> None:
        try:
            done_task.result()
        except Exception as e:
            print(f"[HB_CALL] background request task failed: {e}", flush=True)

    task.add_done_callback(_log_task_result)


def _sort_call_rows_by_bias(rows: list[dict], bias: int | None) -> list[dict]:
    normalized = [_normalize_call_row(row) for row in rows if row.get("file_id")]
    if not bias:
        return normalized

    preferred = [row for row in normalized if int(row.get("dd_thread_id") or 0) == int(bias)]
    others = [row for row in normalized if int(row.get("dd_thread_id") or 0) != int(bias)]
    return preferred + others


async def _cache_call_rows(ctx: AppCtx, user_id: int, rows: list[dict], ttl_sec: int = CALL_MENU_TTL_SEC):
    await ctx.r.rds.setex(
        _call_menu_cache_key(user_id),
        ttl_sec,
        json.dumps(rows, ensure_ascii=False),
    )


async def _load_cached_call_rows(ctx: AppCtx, user_id: int) -> list[dict]:
    raw = await ctx.r.rds.get(_call_menu_cache_key(user_id))
    if not raw:
        return []
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [_normalize_call_row(row) for row in data if isinstance(row, dict)]


async def _build_call_rows_for_user(ctx: AppCtx, user_id: int) -> list[dict]:
    setting = await HongbaoService.get_hongbao_user_setting(user_id)
    bias = (setting or {}).get("bias")
    bot_username = getattr(lz_var, "bot_username", "") or ""
    rows = await HongbaoService.list_call_cutedd(bot_username, CALL_MENU_ACT_ID)

    _run_missing_call_file_ids_in_background(rows, max_count=20)
    ordered_rows = _sort_call_rows_by_bias(rows, int(bias) if bias is not None else None)
    await _cache_call_rows(ctx, user_id, ordered_rows)
    return ordered_rows


def _build_call_nav_keyboard(rows: list[dict], index: int) -> InlineKeyboardMarkup:
    current = rows[index]
    nav_buttons: list[InlineKeyboardButton] = []
    if index > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ 上一个", callback_data=f"hb_menu:call:nav:{index - 1}"))
    if index < len(rows) - 1:
        nav_buttons.append(InlineKeyboardButton(text="下一个 ➡️", callback_data=f"hb_menu:call:nav:{index + 1}"))

    inline_keyboard: list[list[InlineKeyboardButton]] = []
    if nav_buttons:
        inline_keyboard.append(nav_buttons)

    inline_keyboard.append([
        InlineKeyboardButton(text="📣 打Call", callback_data=f"hb_menu:call:do:{current['cutedd_id']}")
    ])
    inline_keyboard.append([
        InlineKeyboardButton(text="🔙 返回菜单", callback_data="hb_menu:home")
    ])
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


async def _send_call_media(
    ctx: AppCtx,
    chat_id: int,
    user_id: int,
    rows: list[dict],
    index: int,
    delete_message: Message | None = None,
    edit_message: Message | None = None,
):
    if delete_message is not None:
        try:
            await delete_message.delete()
        except (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound):
            pass

    if not rows:
        return await ctx.bot.send_message(chat_id=chat_id, text="暂无可打Call的媒体。", reply_markup=start_menu_keyboard())

    index = max(0, min(index, len(rows) - 1))
    row = rows[index]
    caption = row.get("file_caption") or ""
    reply_markup = _build_call_nav_keyboard(rows, index)

    if edit_message is not None:
        try:
            if row.get("file_type") == "video":
                return await edit_message.edit_media(
                    media=InputMediaVideo(media=row.get("file_id"), caption=caption),
                    reply_markup=reply_markup,
                )

            return await edit_message.edit_media(
                media=InputMediaPhoto(media=row.get("file_id"), caption=caption),
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as e:
            print(f"[HB_CALL] edit media failed: {e}", flush=True)
            raise

    if row.get("file_type") == "video":
        return await ctx.bot.send_video(
            chat_id=chat_id,
            video=row.get("file_id"),
            caption=caption,
            protect_content=True,
            reply_markup=reply_markup,
        )

    return await ctx.bot.send_photo(
        chat_id=chat_id,
        photo=row.get("file_id"),
        caption=caption,
        protect_content=True,
        reply_markup=reply_markup,
    )


async def _start_cutedd_call(
    ctx: AppCtx,
    user_id: int,
    chat_id: int,
    cutedd_id: int,
    message_id: int = 0,
    sender_name: str | None = None,
):
    


    msg = {
        "mode": "cutedd",
        "chat_id": TARGET_CHAT_ID,
        "message_thread_id": TARGET_MESSAGE_THREAD_ID,
        "sender_id": user_id,
        "message_id": message_id,
        "sender_name": _h(sender_name) if sender_name else _h(tr(ctx.lang, "default_someone")),
    }
    ret = await _do_create_promote(cutedd_id, ctx, msg)

    ok = False
    if isinstance(ret, dict):
        ok = ret.get("ok") == "1" or ret.get("status") in {"insert", "exist"}
    elif ret is not None:
        ok = True

    if not ok:
        return await ctx.bot.send_message(chat_id=chat_id, text="打Call失败，请稍后再试。")

    promo_link = ""
    if isinstance(ret, Message):
        promo_link = _build_group_message_link(
            chat_id=TARGET_CHAT_ID,
            message_id=ret.message_id,
            thread_id=TARGET_MESSAGE_THREAD_ID,
        )

    if promo_link:
        return await ctx.bot.send_message(
            chat_id=chat_id,
            text=f"成功推广你的连结 ({cutedd_id})至大群！",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="直达红包", url=promo_link)
            ]]),
        )

    return await ctx.bot.send_message(chat_id=chat_id, text=f"成功推广你的连结 ({cutedd_id})至大群！")

def start_menu_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🖼 上传红包封面", callback_data="hb_menu:upload_cover")],
            [InlineKeyboardButton(text="🔙 返回菜单", callback_data="hb_menu:home")],
        ]
    )


def upload_cover_quiz_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="色色的", callback_data="hb_menu:upload_cover_quiz:incorrect")],
            [InlineKeyboardButton(text="性暗示", callback_data="hb_menu:upload_cover_quiz:incorrect")],
            [InlineKeyboardButton(text="没穿上衣的(含泳衣)", callback_data="hb_menu:upload_cover_quiz:incorrect")],
            [InlineKeyboardButton(text="清水，但祼上半身", callback_data="hb_menu:upload_cover_quiz:incorrect")],
            [InlineKeyboardButton(text="以上皆不行", callback_data="hb_menu:upload_cover_quiz:correct")],
            [InlineKeyboardButton(text="🔙 返回菜单", callback_data="hb_menu:home")],
        ]
    )


async def send_start_menu(ctx: AppCtx, chat_id: int, user_id: int, delete_message: Message | None = None):
    if delete_message is not None:
        try:
            await delete_message.delete()
        except (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound):
            pass

    setting = await HongbaoService.get_hongbao_user_setting(user_id)
    cover_file_id = (setting or {}).get("cover_file_id") or ""

    if cover_file_id:
        try:
            return await ctx.bot.send_photo(
                chat_id=chat_id,
                photo=cover_file_id,
                caption="请选择功能：",
                reply_markup=start_menu_keyboard(),
            )
        except TelegramBadRequest as e:
            print(f"[HB_MENU] send_photo failed, fallback to text: {e}", flush=True)

    return await ctx.bot.send_message(
        chat_id=chat_id,
        text="请选择功能：",
        reply_markup=start_menu_keyboard(),
    )


async def _delete_message_later(message: Message, delay_sec: int = 7):
    await asyncio.sleep(delay_sec)
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound):
        pass


async def _send_temporary_message(
    ctx: AppCtx,
    chat_id: int,
    text: str,
    message_thread_id: int | None = None,
    reply_to_message_id: int | None = None,
    delay_sec: int = 7,
):
    sent = await ctx.bot.send_message(
        chat_id=chat_id,
        message_thread_id=message_thread_id,
        text=text,
        reply_parameters=ReplyParameters(message_id=reply_to_message_id) if reply_to_message_id else None,
    )
    asyncio.create_task(_delete_message_later(sent, delay_sec))
    return sent


async def _reply_temporary_and_delete_source(ctx: AppCtx, message: Message, text: str, delay_sec: int = 7):
    await _send_temporary_message(
        ctx,
        chat_id=message.chat.id,
        message_thread_id=message.message_thread_id,
        reply_to_message_id=message.message_id,
        text=text,
        delay_sec=delay_sec,
    )
    asyncio.create_task(_delete_message_later(message, delay_sec))

@dataclass
class AppCtx:
    r: RedisLayer
    bot: any
    lang: str = "lj"   
    skin : dict | None = None


@router.message(F.chat.type == ChatType.PRIVATE, Command("start"))
async def handle_start(message: Message,  command: Command = Command("start"), ctx: AppCtx = None):
    if message.chat.type != ChatType.PRIVATE:
        return

    lang = ctx.lang
    print(f"Received /start command: {message.text}", flush=True)
    try:
        if message.text and message.text == "/start":
            pass
        else:
            await message.delete()
    except (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramMigrateToChat, TelegramRetryAfter) as e:
        print(f"❌ 删除 /start 消息失败: {e}", flush=True)

    user_id = message.from_user.id
    # 获取 start 后面的参数（如果有）
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        param = args[1].strip()
        parts = param.split("_")
        if parts[0] == "clt":
            cid_str = parts[1].strip() if len(parts) > 1 else ""
            print(f"Received start with clt param: {param} {cid_str}", flush=True)
            if not cid_str.isdigit():
                await ctx.bot.send_message(chat_id=message.chat.id, text="推广失败，参数错误。")
                return
            clt_id = int(cid_str) if cid_str.isdigit() else 0
            print(f"===>message: {message}", flush=True)



            msg = {
                "chat_id": TARGET_CHAT_ID, 
                "message_thread_id": TARGET_MESSAGE_THREAD_ID, 
                "sender_id": message.from_user.id if message.from_user else 0,
                "message_id": message.message_id if message.message_id else 0,
                "sender_name": _h(message.from_user.first_name) if message.from_user else _h(tr(lang, "default_someone"))
            }
            print(f"===>msg: {msg}", flush=True)
           
            ret = await _do_create_promote(clt_id, ctx, msg)
            promo_link = ""
            if isinstance(ret, Message):
                promo_link = _build_group_message_link(
                    chat_id=TARGET_CHAT_ID,
                    message_id=ret.message_id,
                    thread_id=TARGET_MESSAGE_THREAD_ID,
                )

            if promo_link:
                await ctx.bot.send_message(
                    chat_id=message.chat.id,
                    text=f"成功推广你的连结 ({clt_id})至大群！",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                        text="直达红包",
                        url=promo_link,
                    )]]),
                )
            else:
                await ctx.bot.send_message(chat_id=message.chat.id, text=f"成功推广你的连结 ({clt_id})至大群！")
            return
        elif parts[0] == "rl":
            cutedd_id_str = parts[1].strip() if len(parts) > 1 else ""
            print(f"Received start with rl param: {param} {cutedd_id_str}", flush=True)
            if not cutedd_id_str.isdigit():
                await ctx.bot.send_message(chat_id=message.chat.id, text="推广失败，参数错误。")
                return
            cutedd_id = int(cutedd_id_str)
            # print(f"===>message: {message}", flush=True)



            msg = {
                "mode": "cutedd",
                "chat_id": TARGET_CHAT_ID, 
                "message_thread_id": TARGET_MESSAGE_THREAD_ID, 
                "sender_id": message.from_user.id if message.from_user else 0,
                "message_id": message.message_id if message.message_id else 0,
                "sender_name": _h(message.from_user.first_name) if message.from_user else _h(tr(lang, "default_someone"))
            }

            # msg = {
            #     "mode": "cutedd",
            #     "chat_id": message.chat.id, 
            #     "message_thread_id": 0, 
            #     "sender_id": message.from_user.id if message.from_user else 0,
            #     "message_id": message.message_id if message.message_id else 0,
            #     "sender_name": _h(message.from_user.first_name) if message.from_user else _h(tr(lang, "default_someone"))
            # }

            # print(f"===>msg: {msg}", flush=True)
           
            ret = await _do_create_promote(cutedd_id, ctx, msg)
            print(f"ret===>{ret}")

            ok = False
            if isinstance(ret, dict):
                ok = ret.get("ok") == "1" or ret.get("status") in {"insert", "exist"}
            elif ret is not None:
                ok = True

            if ok:
                promo_link = ""
                if isinstance(ret, Message):
                    promo_link = _build_group_message_link(
                        chat_id=TARGET_CHAT_ID,
                        message_id=ret.message_id,
                        thread_id=TARGET_MESSAGE_THREAD_ID,
                    )

                if promo_link:
                    await ctx.bot.send_message(
                        chat_id=message.chat.id,
                        text=f"成功推广你的连结 ({cutedd_id})至大群！",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                            text="直达红包",
                            url=promo_link,
                        )]]),
                    )
                else:
                    await ctx.bot.send_message(chat_id=message.chat.id, text=f"成功推广你的连结 ({cutedd_id})至大群！")
            else:
                await ctx.bot.send_message(chat_id=message.chat.id, text=f"推广失败，可能是参数错误或服务器问题，请稍后再试。")
            return

    await send_start_menu(ctx, message.chat.id, user_id)


@router.message(F.chat.type == ChatType.PRIVATE, Command("home"))
async def handle_home(message: Message, ctx: AppCtx):

    url_school = SharedConfig.get("school_invite_link", "")
    text = "欢迎回家 \r\n\r\n"
    text += f"🔗 <a href='{url_school}'>🐲 龙阳学院</a>\r\n\r\n"
    text += "🎈 <i>任何一个龙阳的机器人，右下角菜单都有回家的指令</i>"

    await ctx.bot.send_message(
        chat_id=message.chat.id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        protect_content=True,
    )


@router.callback_query(F.data == "hb_menu:settings")
async def handle_hb_menu_settings(callback: CallbackQuery, ctx: AppCtx):
    text = (
        "<blockquote>发送红包指令</blockquote>\n"
        "🧧 积分红包：/hb [总积分] [份数] [留言]\n"
        "<i>例如：/hb 100 10 恭喜发财</i>\n\n"
        "💦 龙精红包：/lj [精值] [可射数] [留言]\n"
        "<i>例如：/lj 100 10 帮我榨干他</i>\n\n"
        "🎈 可以在大群各主题版下指令发红，若是私聊机器人，默认发送到闲聊区\n\n"
      
        "<blockquote>设定红包封面</blockquote>\n"
        "点击下方按钮。\n\n"
    )
    try:
        await callback.message.edit_text(text, reply_markup=start_menu_back_keyboard(),parse_mode="HTML")
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=start_menu_back_keyboard(),parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "hb_menu:upload_cover")
async def handle_hb_menu_upload_cover(callback: CallbackQuery, ctx: AppCtx):
    user_id = callback.from_user.id if callback.from_user else 0
    if user_id:
        pending_cover_upload_users.discard(user_id)

    text = (
        "由于 Telegram 的风控严格，以下不允许被发到群组中\n\n"
        "1. 明显是色情资源\n"
        "2. 有漏点，包括乳头，鸡鸡，蛋蛋，菊花等等\n"
        "3. 没漏点，但有性暗示(例如正太上下动)\n"
        "4. 清水，但漏点(只穿泳裤都算)\n\n"
        "下列选择，哪一个正确"
    )
    try:
        await callback.message.edit_text(text, reply_markup=upload_cover_quiz_keyboard())
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=upload_cover_quiz_keyboard())
    await callback.answer()


@router.callback_query(F.data == "hb_menu:call")
async def handle_hb_menu_call(callback: CallbackQuery, ctx: AppCtx):
    user_id = callback.from_user.id if callback.from_user else 0
    
    rows = await _build_call_rows_for_user(ctx, user_id)
   
    await _send_call_media(
        ctx,
        chat_id=callback.message.chat.id,
        user_id=user_id,
        rows=rows,
        index=0,
        delete_message=callback.message,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("hb_menu:call:nav:"))
async def handle_hb_menu_call_nav(callback: CallbackQuery, ctx: AppCtx):
    user_id = callback.from_user.id if callback.from_user else 0
    index_str = (callback.data or "").split(":")[-1]
    try:
        index = int(index_str)
    except ValueError:
        await callback.answer("参数错误", show_alert=True)
        return

    rows = await _load_cached_call_rows(ctx, user_id)
    print(f"{user_id} nav to {index}, cached rows: {rows}", flush=True)
    if not rows:
        rows = await _build_call_rows_for_user(ctx, user_id)

    await _send_call_media(
        ctx,
        chat_id=callback.message.chat.id,
        user_id=user_id,
        rows=rows,
        index=index,
        edit_message=callback.message,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("hb_menu:call:do:"))
async def handle_hb_menu_call_do(callback: CallbackQuery, ctx: AppCtx):
    user_id = callback.from_user.id if callback.from_user else 0
    cutedd_id_str = (callback.data or "").split(":")[-1]
    try:
        cutedd_id = int(cutedd_id_str)
    except ValueError:
        await callback.answer("参数错误", show_alert=True)
        return

    rows = await _load_cached_call_rows(ctx, user_id)
    if not rows:
        rows = await _build_call_rows_for_user(ctx, user_id)

    current = next((row for row in rows if int(row.get("cutedd_id") or 0) == cutedd_id), None)
    if not current:
        await callback.answer("找不到该媒体，请重试", show_alert=True)
        return

    ok = await HongbaoService.upsert_hongbao_user_setting(
        user_id=user_id,
        cover_type=current.get("file_type") or "photo",
        cover_file_id=current.get("file_id") or "",
        cover_file_unique_id=current.get("file_unique_id") or "",
        bias=int(current.get("dd_thread_id") or 0) or None,
    )
    if not ok:
        await callback.answer("保存本命设定失败，请稍后再试", show_alert=True)
        return

    try:
        await callback.message.delete()
    except (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound):
        pass

    await _start_cutedd_call(
        ctx,
        user_id=user_id,
        chat_id=callback.message.chat.id,
        cutedd_id=cutedd_id,
        message_id=callback.message.message_id,
        sender_name=callback.from_user.first_name if callback.from_user else None,
    )
    await callback.answer("已开始打Call")


@router.callback_query(F.data == "hb_menu:upload_cover_quiz:correct")
async def handle_hb_menu_upload_cover_quiz_correct(callback: CallbackQuery, ctx: AppCtx):
    user_id = callback.from_user.id if callback.from_user else 0
    if user_id:
        pending_cover_upload_users.add(user_id)

    text = "请直接上传一张图片，作为你的红包默认封面。"
    try:
        await callback.message.edit_text(text, reply_markup=start_menu_back_keyboard())
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=start_menu_back_keyboard())
    await callback.answer("请上传图片")


@router.callback_query(F.data == "hb_menu:upload_cover_quiz:incorrect")
async def handle_hb_menu_upload_cover_quiz_incorrect(callback: CallbackQuery, ctx: AppCtx):
    user_id = callback.from_user.id if callback.from_user else 0
    if user_id:
        pending_cover_upload_users.discard(user_id)
    await callback.answer("答错了，以上皆不行。", show_alert=True)


@router.callback_query(F.data == "hb_menu:home")
async def handle_hb_menu_home(callback: CallbackQuery, ctx: AppCtx):
    user_id = callback.from_user.id if callback.from_user else 0
    if user_id:
        pending_cover_upload_users.discard(user_id)
    await send_start_menu(ctx, callback.message.chat.id, user_id, delete_message=callback.message)
    await callback.answer()



@router.message(Command(commands=["hb", "rp", "hongbao"]))
async def cmd_hb(message: Message, ctx: AppCtx):
    hb_type = "hb"
    ctx.lang = "hb"  
    lang = ctx.lang
    # if message.chat.type not in ("group", "supergroup"):
    #     await message.reply(tr(lang, "rp_group_only"))
    #     return

    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 3:
        await _reply_temporary_and_delete_source(ctx, message, tr(lang, "rp_usage"))
        return

    try:
        total_amount = int(parts[1])
        total_count = int(parts[2])
    except ValueError:
        await _reply_temporary_and_delete_source(ctx, message, tr(lang, "rp_param_int"))
        return

    expire_minutes = DEFAULT_EXPIRE_MINUTES
    comment = parts[3].strip() if len(parts) >= 4 else ""

    if total_count <= 0 or total_count > MAX_COUNT:
        await _reply_temporary_and_delete_source(ctx, message, tr(lang, "rp_count_range", max_count=MAX_COUNT))
        return
    if total_amount < 1 or total_amount > MAX_TOTAL_AMOUNT:
        await _reply_temporary_and_delete_source(ctx, message, tr(lang, "rp_total_range", max_total=MAX_TOTAL_AMOUNT))
        return
    if total_amount < (total_count * MIN_UNIT):
        await _reply_temporary_and_delete_source(ctx, message, tr(lang, "rp_total_too_small", min_unit=MIN_UNIT))
        return
    if expire_minutes <= 0:
        await _reply_temporary_and_delete_source(ctx, message, tr(lang, "rp_expire_invalid"))
        return

    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound):
        pass

    default_skin = lz_var.skins.get("hb_cover") or {}
    user_setting = await HongbaoService.get_hongbao_user_setting(
        message.from_user.id if message.from_user else 0
    )
    cover_file_id = (user_setting or {}).get("cover_file_id") or default_skin.get("file_id", "")
    cover_type = (user_setting or {}).get("cover_type") or default_skin.get("file_type", "")
    bot_username = getattr(lz_var, "bot_username", "") or ""

    dm_text= f"""\
    记得点击领取才能使用获得的积分！\n
    如果你也想发红包，或有自定义的红包封面，请点击下方 🧧 红包设定 按钮。\n
    """

    activity_link = f"https://t.me/{bot_username}?start=true" if bot_username else ""
    skin = {
        "hb_key": "hb:0",
        "file_id_cover": cover_file_id,
        "file_type_cover": cover_type,
        "file_id_dm": cover_file_id,
        "file_type_dm": cover_type,
        "intro_text": comment if comment else "",
        "dm_text": dm_text,
        "activity_link": activity_link,
    }
       

    
   

    school_chat_id = message.chat.id
    school_chat_thread_id = message.message_thread_id

    if message.chat.type not in ("group", "supergroup"):
        school_chat_id = TARGET_CHAT_ID
        school_chat_thread_id = TARGET_MESSAGE_THREAD_ID
    


    msg = {
        "chat_id": school_chat_id,
        "message_thread_id": school_chat_thread_id,
        "sender_id": message.from_user.id if message.from_user else 0,
        "message_id": message.message_id if message.message_id else 0,
        "sender_name": _h(message.from_user.first_name) if message.from_user else _h(tr(lang, "default_someone"))
    }


    hongbao = {
        "total_count": total_count,
        "total_amount": total_amount,
        "expire_minutes": expire_minutes,
        "skin": skin,
    }

    

    await _do_create_hongbao(ctx, msg, hongbao, hb_type=hb_type)


@router.message(Command("lj"))
async def cmd_rp(message: Message, ctx: AppCtx):
    hb_type = "lj"
    ctx.lang = "lj"  # 强制中文，后续可根据需要调整
    lang = ctx.lang
    if message.chat.type not in ("group", "supergroup"):
        await message.reply(tr(lang, "rp_group_only"))
        return

    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 3:
        await message.reply(tr(lang, "rp_usage"))
        return

    try:
        total_amount = int(parts[1])
        total_count = int(parts[2])
    except ValueError:
        await message.reply(tr(lang, "rp_param_int"))
        return

    expire_minutes = DEFAULT_EXPIRE_MINUTES
    comment = parts[3].strip() if len(parts) >= 4 else ""



    skin = dict(pick_rp_skin())
    if comment:
        skin["intro_text"] = comment
        skin["dm_text"] = comment

    msg = {
        "chat_id": message.chat.id, 
        "message_thread_id": message.message_thread_id, 
        "sender_id": message.from_user.id if message.from_user else 0,
        "message_id": message.message_id if message.message_id else 0,
        "sender_name": _h(message.from_user.first_name) if message.from_user else _h(tr(lang, "default_someone"))
        }
    
    hongbao = {
        "total_count": total_count,
        "total_amount": total_amount,
        "expire_minutes": expire_minutes,
        "skin": skin,
    }

    await _do_create_hongbao(ctx, msg, hongbao, hb_type=hb_type)

@router.message(Command("pushclt"))
async def cmd_pushclt(message: Message, ctx: AppCtx):

    lang = ctx.lang
    print(f"Received /pushclt command: {message.text}", flush=True)
    # if message.chat.type in ("group", "supergroup"):
    #     await message.reply("私信使用")
    #     return

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply(tr(lang, "rp_usage"))
        return

    try:
        clt_id = int(parts[1])
        


    except ValueError:
        await message.reply(tr(lang, "rp_param_int"))
        return
   
    msg = {
        "chat_id": -1003815882738, 
        "message_thread_id": 0, 
        "sender_id": message.from_user.id if message.from_user else 0,
        "message_id": message.message_id if message.message_id else 0,
        "sender_name": _h(message.from_user.first_name) if message.from_user else _h(tr(lang, "default_someone"))
    }
   
    return await _do_create_promote(clt_id, ctx, msg)
    




async def _do_create_promote(id: int, ctx: AppCtx , msg: dict | None = None):
    if msg and msg.get("mode") == "cutedd":
        if id <= 0:
            return {"ok": "", "status": "bad_cutedd_id"}

        bot_name = getattr(lz_var, "bot_username", "") or ""
        cutedd_row = await HongbaoService.get_cutedd(cutedd_id=id, bot_name=bot_name)
        if not cutedd_row:
            return {"ok": "", "status": "cutedd_not_found"}

        board_chat_id = str(cutedd_row.get("board_chat_id")).replace("-100", "") if cutedd_row.get("board_chat_id") else ""
        board_message_thread_id = cutedd_row.get("board_message_thread_id") if cutedd_row.get("board_message_thread_id") else ""
        board_message_id = cutedd_row.get("board_message_id") if cutedd_row.get("board_message_id") else ""
        dm_text = cutedd_row.get("file_caption") or ""

        dm_text = f"{dm_text}\r\n\r\n👇 喜欢我介绍的弟弟吗? 快点下面的「推广链结」喂他吃香蕉吧！👇"
        push_cover_skin = lz_var.skins.get("push_cover", {})
        cover_file_id = cutedd_row.get("file_id") or push_cover_skin.get("file_id", "")
        cover_file_type = cutedd_row.get("file_type") or push_cover_skin.get("file_type", "")

        skin = {
                "hb_key": f"rl:{id}",
            "file_id_cover": cover_file_id,
            "file_type_cover": cover_file_type,
            "file_id_dm": cover_file_id,
            "file_type_dm": cover_file_type,
                "intro_text": cutedd_row.get("file_caption") or cutedd_row.get("description"),
                "dm_text": dm_text,
                "activity_link": f"https://t.me/c/{board_chat_id}/{board_message_thread_id}/{board_message_id}",
            }
    else:
        print(f"Creating promote for clt_id={id} by user_id={msg['sender_id'] if msg else 'N/A'}", flush=True)
        clt_row = await HongbaoService.get_user_collection(id=id) or {}
        push_cover_skin = lz_var.skins.get("push_cover", {})
        skin = {
                "hb_key": f"clt{id}",
                "file_id_cover": push_cover_skin.get("file_id", ""),
                "file_type_cover": push_cover_skin.get("file_type", ""),
                "file_id_dm": push_cover_skin.get("file_id", ""),
                "file_type_dm": push_cover_skin.get("file_type", ""),
                "intro_text": clt_row.get("description"),
                "dm_text": clt_row.get("description"),
                "activity_link": f"https://t.me/{lz_var.publish_bot_name}?start=clt_{id}",
            }
  
    hongbao = {
        "total_count": 7,
        "total_amount": 34,
        "expire_minutes": 60*24,
        "skin": skin,
    }
    return await _do_create_hongbao(ctx, msg, hongbao)
    

async def _do_create_hongbao(ctx: AppCtx, msg:dict,  hongbao:dict, hb_type: str = "lj"):
    hb_type = _normalize_hb_type(hb_type)
    ctx.lang = hb_type
    lang = ctx.lang
    total_count = hongbao["total_count"]
    total_amount = hongbao["total_amount"]
    expire_minutes = hongbao["expire_minutes"]
    skin = hongbao["skin"]
    bot_name = getattr(lz_var, "bot_username", "") or ""

    mode = "promote"
    if msg and msg.get("mode"):
        mode = msg["mode"]

    print(f"msg====>{msg} {total_count} {total_amount} {(total_count * MIN_UNIT)}")
    if total_count <= 0 or total_count > MAX_COUNT:
        print(f"Invalid total_count: {total_count}", flush=True)
        await _send_temporary_message(
            ctx,
            chat_id=msg["chat_id"],
            message_thread_id=msg["message_thread_id"],
            reply_to_message_id=msg.get("message_id"),
            text=tr(lang, "rp_count_range", max_count=MAX_COUNT),
        )
        return
    if hb_type == "hb" and (total_amount < 1 or total_amount > MAX_TOTAL_AMOUNT):
        print(f"Invalid total_amount range for hb: {total_amount}", flush=True)
        await _send_temporary_message(
            ctx,
            chat_id=msg["chat_id"],
            message_thread_id=msg["message_thread_id"],
            reply_to_message_id=msg.get("message_id"),
            text=tr(lang, "rp_total_range", max_total=MAX_TOTAL_AMOUNT),
        )
        return
    if total_amount < (total_count * MIN_UNIT):
        print(f"Invalid total_amount: {total_amount}", flush=True)
    
        await _send_temporary_message(
            ctx,
            chat_id=msg["chat_id"],
            message_thread_id=msg["message_thread_id"],
            reply_to_message_id=msg.get("message_id"),
            text=tr(lang, "rp_total_too_small", min_unit=MIN_UNIT),
        )
        return
    if expire_minutes <= 0:
        await _send_temporary_message(
            ctx,
            chat_id=msg["chat_id"],
            message_thread_id=msg["message_thread_id"],
            reply_to_message_id=msg.get("message_id"),
            text=tr(lang, "rp_expire_invalid"),
        )
        return

    sender_id = msg["sender_id"]
    chat_id = msg["chat_id"]

    now = datetime.now()
    expire_at = now + timedelta(minutes=expire_minutes)
    ttl_sec = max(1, int((expire_at - now).total_seconds()))

    transaction_description = f"{chat_id}_{msg.get('message_id', 0)}"

    ret_refund = await HongbaoService.transaction_log({
        "sender_id": sender_id,
        "sender_fee": -1 * total_amount,
        "receiver_id": 0,
        "receiver_fee": 0,
        "transaction_type": mode,
        "transaction_description": transaction_description,
        "memo": f"{skin['hb_key']}"
    })

    print(f"Transaction log result: {ret_refund}", flush=True)

    if ret_refund["status"] == "insufficient_funds":
        await ctx.bot.send_message(chat_id=sender_id, text=tr(lang, "re_insufficient_funds"))
        return ret_refund
    elif ret_refund['status'] == 'insert' or ret_refund['status'] == 'exist':
        hid = await HongbaoService.create_hongbao(sender_id, chat_id, total_amount, total_count, expire_at, skin, hb_type)
        if hid <= 0:
            await ctx.bot.send_message(chat_id=sender_id, text=tr(lang, "redeem_busy"))
            return

        await ctx.r.set_hb_skin(hid, skin["hb_key"], ttl_sec)

        amounts = split_amounts(total_amount, total_count, MIN_UNIT)
        await ctx.r.init_list(hid, amounts, ttl_sec)


        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sender_name = msg["sender_name"]

        line = [
            "<blockquote>"+tr(lang, "post_title", sender=sender_name)+"</blockquote>",
        ]


        if skin["intro_text"]:
            line += [
                "",
                f"<i>💬 {(skin['intro_text'])}</i>",
                "",
            ]

        line += [
            tr(lang, "post_total", total_amount=total_amount),
            tr(lang, "post_count", total_count=total_count),
            tr(lang, "post_sn", sn=hid),
            tr(lang, "post_time", created_at=created_at),
            "",
            tr(lang, "post_stat_amount", claimed_amount=0, total_amount=total_amount),
            tr(lang, "post_stat_count", claimed_count=0, total_count=total_count),
            "",
            "<blockquote>"+tr(lang, "post_list_title")+"</blockquote>",
            "",
        ]

        text ="\n".join(line)
        print(f"Generated hongbao message:\n{text}", flush=True)
        # sent = await message.answer(text, reply_markup=kb_claim(hid, lang))
        try:
            cover_file_id = skin.get("file_id_cover")
            cover_file_type = skin.get("file_type_cover") or await HongbaoService.get_file_type_by_file_id(cover_file_id, bot_name)

            if cover_file_id:
                if cover_file_type == "video":
                    sent = await ctx.bot.send_video(
                        chat_id=msg['chat_id'],
                        message_thread_id=msg['message_thread_id'],
                        video=cover_file_id,
                        caption=text,
                        parse_mode="HTML",
                        protect_content=True,
                        reply_markup=kb_claim(hid, lang, hb_type),
                    )
                else:
                    sent = await ctx.bot.send_photo(
                        chat_id=msg['chat_id'],
                        message_thread_id=msg['message_thread_id'],
                        photo=cover_file_id,
                        caption=text,
                        parse_mode="HTML",
                        protect_content=True,
                        reply_markup=kb_claim(hid, lang, hb_type),
                    )
            else:
                sent = await ctx.bot.send_message(
                    chat_id=msg['chat_id'],
                    message_thread_id=msg['message_thread_id'],
                    text=text,
                    parse_mode="HTML",
                    protect_content=True,
                    reply_markup=kb_claim(hid, lang, hb_type),
                )
            

        except TelegramForbiddenError as e:
            print(
                f"[HONGBAO] send_photo forbidden, bot may be kicked or has no permission. "
                f"chat_id={msg.get('chat_id')} thread_id={msg.get('message_thread_id')} err={e}",
                flush=True,
            )
            return {"ok": "", "status": "send_forbidden", "error": str(e)}
        except TelegramBadRequest as e:
            print(
                f"[HONGBAO] send message bad request. "
                f"chat_id={msg.get('chat_id')} thread_id={msg.get('message_thread_id')} err={e}",
                flush=True,
            )

            notify_chat_id = sender_id if sender_id and sender_id != msg.get("chat_id") else 0
            if notify_chat_id:
                try:
                    await ctx.bot.send_message(
                        chat_id=notify_chat_id,
                        text=f"❌ 发送红包消息失败：{e}",
                    )
                except (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound) as notify_err:
                    print(
                        f"[HONGBAO] notify sender skipped. "
                        f"chat_id={notify_chat_id} err={notify_err}",
                        flush=True,
                    )
            return {"ok": "", "status": "send_bad_request", "error": str(e)}
        except Exception as e:
            notify_chat_id = sender_id if sender_id and sender_id != msg.get("chat_id") else 0
            try:
                if notify_chat_id:
                    await ctx.bot.send_message(
                        chat_id=notify_chat_id,
                        text=f"❌ 发送红包消息失败：{e}"
                    )
            except (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound) as notify_err:
                print(
                    f"[HONGBAO] notify send failure skipped. "
                    f"chat_id={notify_chat_id} err={notify_err}",
                    flush=True,
                )
            return {"ok": "", "status": "send_error", "error": str(e)}

        await HongbaoService.bind_message(hid, sent.message_id)

        # ======= Pin 消息到群组 =======
        try:
            await ctx.bot.pin_chat_message(
                chat_id=msg["chat_id"],
                message_id=sent.message_id,
                disable_notification=True,  # 不发送通知
            )
        except TelegramBadRequest as e:
            # pin 失败不影响红包功能，仅记录（可选）
            pass
        except Exception as e:
            pass
        return sent
        
    else:
        return ret_refund
    
@router.callback_query(F.data.startswith("hb_claim:"))
async def cb_claim(callback: CallbackQuery, ctx: AppCtx):
    # 先秒回，避免 Telegram callback 超时
    hb_type = _get_callback_hb_type(callback.data)
    lang = hb_type
    ctx.lang = hb_type


    # try:
    #     await callback.answer(tr(lang, "cb_processing"), show_alert=False)
    # except TelegramBadRequest:
    #     pass

    uid = callback.from_user.id
    hid = int(callback.data.split(":")[1])

    # 原红包消息（带“🧧 抢红包”按钮的那条）
    base_msg = callback.message

    # 抢红包（由 Redis 保证幂等/原子）
    code, amount, is_empty = await ctx.r.claim(hid, uid)

    claimer = tr(lang, "default_someone")

    # 已抢过（重复点选）：不 edit、不 DM
    if code == 1:
        try:
            await callback.answer(tr(lang, "already_claimed"), show_alert=False)
        except TelegramBadRequest:
            pass
        return


    # 不存在/过期
    if code == -2:
        await callback.answer(tr(lang, "hb_not_found"), show_alert=False)
        print(f"Claim failed: hid={hid} not found or expired")
        if base_msg:
            print(f"Base message: chat_id={base_msg.chat.id} message_id={base_msg.message_id}")
            await ctx.bot.unpin_chat_message(
                chat_id=base_msg.chat.id,
                message_id=base_msg.message_id,
            )

            hangbao_info = await HongbaoService.get_hongbao(hid)  # 仅为了日志记录，顺便验证是否真的过期（MySQL 层）


            if hangbao_info.get("activity_link"):
                new_reply_markup = InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(
                            text=tr(lang, "btn_activity"),
                            url=hangbao_info.get("activity_link"),
                        )
                    ]]
                )
            else:
                new_reply_markup = None


            try:
                

                if base_msg.caption is not None:
                    caption = base_msg.caption or ""
                    
                    entities = base_msg.caption_entities or []
                    new_text = Text.from_entities(caption, entities).as_html()

                    new_text += "\n\n" + tr(lang, "post_expired")

                    await base_msg.edit_caption(
                        caption=new_text,
                        reply_markup=new_reply_markup,
                        parse_mode="HTML",
                    )
                else:
                    text = base_msg.text or ""
                    entities = base_msg.entities or []
                    new_text = Text.from_entities(text, entities).as_html()

                    new_text += "\n\n" + tr(lang, "post_expired")

                    await base_msg.edit_text(
                        new_text,
                        reply_markup=new_reply_markup,
                        parse_mode="HTML",
                    )
            except TelegramBadRequest:
                pass    
        return
        
    # 抢完（手慢了）
    elif code == -1 or amount <= 0:
        await callback.answer(tr(lang, "too_late"), show_alert=False)
       
    else:
        # ======= 首次抢到：编辑群里原消息（不再发新消息）=======
        u = callback.from_user
        if u.first_name:
            claimer_raw = u.first_name
        elif u.username:
            claimer_raw = "@" + u.username
        else:
            claimer_raw = tr(lang, "default_someone")

        claimer = "<code>" + _h(claimer_raw) + "</code>"

        await ctx.r.record_claim_meta(
            hid=hid,
            uid=uid,
            amount=amount,
            name=claimer_raw,
            ts = datetime.now().timestamp(),
        )
        try:
            await callback.answer()
        except TelegramBadRequest:
            pass

    # skin_key = await ctx.r.get_hb_skin(hid)

    # skin = next((s for s in RP_SKINS if s["hb_key"] == skin_key), None)
    skin = await HongbaoService.get_hongbao(hid) or {}



    bot_name = getattr(lz_var, "bot_username", "") or str(ctx.bot.id)

    if base_msg:
        old_text = (base_msg.caption or base_msg.text or "")
    
        parsed_sender, total_amount, total_count, hb_sn, created_at = _parse_base(old_text, lang)
        sender = await _resolve_sender_name(ctx, skin, parsed_sender, lang)

        items = _parse_items(old_text, lang)

        sender = _h(sender)
        created_at = _h(created_at)

        # items：分两种路径
        # - 未抢完：为了最小改动，可继续 parse old_text，再 append 本人
        # - 抢完：必须从 Redis 全量重建（解决“名单盖掉”）
        if is_empty:
            try:
                if base_msg:
                    await ctx.bot.unpin_chat_message(
                        chat_id=base_msg.chat.id,
                        message_id=base_msg.message_id,
                    )
            except TelegramBadRequest:
                pass
            except Exception:
                pass

            rows = await ctx.r.list_claim_meta(hid)  # [(uid, amt, ts, name), ...] ts 升序
            items = []
            # 计算耗时：按 base_msg.date 作为起点
            try:
                dt0_ts = base_msg.date.timestamp()
            except Exception:
                dt0_ts = datetime.now().timestamp()

            for _uid, amt, ts, name_raw in rows:
                    cost_sec = max(0.0, float(ts) - float(dt0_ts))
                    cost_txt = _fmt_cost(cost_sec)
                    name = "<code>" + _h(name_raw) + "</code>"
                    items.append((name, int(amt), cost_txt))
        else:            
            items = _parse_items(old_text, lang)

            # 当前这次耗时
            try:
                dt0 = base_msg.date
                dt1 = datetime.now(dt0.tzinfo) if dt0.tzinfo else datetime.now()
                cost_sec = (dt1 - dt0).total_seconds()
            except Exception:
                cost_sec = 9999.0
            cost_txt = _fmt_cost(cost_sec)

            items.append((claimer, amount, cost_txt))
        
        claimed_amount = sum(a for _, a, _ in items)
        claimed_count = len(items)
        king_name, king_amt, _ = max(items, key=lambda x: x[1]) if items else ("", 0, "")

        lines = [
            "<blockquote>" + tr(lang, "post_title", sender=sender) + "</blockquote>",
        ]

        intro_text = skin.get("intro_text") or ""
        if intro_text:
            lines += ["", f"<i>💬 {_h(intro_text)}</i>", ""]

        lines += [
            tr(lang, "post_total", total_amount=total_amount),
            tr(lang, "post_count", total_count=total_count),
            tr(lang, "post_sn", sn=hb_sn),
            tr(lang, "post_time", created_at=created_at),
            "",
            tr(lang, "post_stat_amount", claimed_amount=claimed_amount, total_amount=total_amount),
            tr(lang, "post_stat_count", claimed_count=claimed_count, total_count=total_count),
            "",
            "<blockquote>" + tr(lang, "post_list_title") + "</blockquote>",
            "",
            tr(lang, "post_king", name=king_name, amt=king_amt),
            "",
        ]

        for name, amt, cost in items:
            lines.append(tr(lang, "post_item", name=name, amt=amt, cost=cost))

        if is_empty:
            lines += ["", tr(lang, "post_finished")]

        new_text = "\n".join(lines)

        # 抢完：隐藏抢按钮，只保留活动按钮（如果有）
        if is_empty:
            if skin.get("activity_link"):
                new_reply_markup = InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(
                            text=tr(lang, "btn_activity"),
                            url=skin.get("activity_link"),
                        )
                    ]]
                )
            else:
                new_reply_markup = None
        else:
            new_reply_markup = base_msg.reply_markup

        # （可选）群消息编辑节流：只节流“展示更新”，不影响抢到/DM
        do_edit = True
        if (not is_empty) and GROUP_NOTICE_THROTTLE:
            try:
                ok_to_edit = await ctx.r.allow_group_notice(hid, GROUP_NOTICE_PER_SEC)
            except Exception:
                ok_to_edit = True
            if not ok_to_edit:
                new_reply_markup = base_msg.reply_markup  # 不变
                do_edit = False  # 节流期间跳过 edit

        if do_edit:
            try:
                if base_msg.caption is not None:
                    await base_msg.edit_caption(
                        caption=new_text,
                        reply_markup=new_reply_markup,
                        parse_mode="HTML",
                    )
                else:
                    await base_msg.edit_text(
                        new_text,
                        reply_markup=new_reply_markup,
                        parse_mode="HTML",
                    )
            except TelegramBadRequest:
                pass

    # ======= 私信通知（成功抢到才会走到这里） =======
    if await ctx.r.should_skip_dm(uid):
        try:
            await callback.answer(tr(lang, "dm_blocked"), show_alert=True)
        except TelegramBadRequest:
            pass
        return

    try:
        send_message_text = tr(lang, "dm_got", amount=amount)
        if skin.get("dm_text"):
            send_message_text += "\n\n<i>" + skin["dm_text"] + "</i>"

        try:
            dm_file_id = skin.get("file_id_dm")
            dm_file_type = skin.get("file_type_dm") or await HongbaoService.get_file_type_by_file_id(dm_file_id, bot_name)

            if dm_file_id:
                if dm_file_type == "video":
                    await ctx.bot.send_video(
                        chat_id=uid,
                        video=dm_file_id,
                        caption=send_message_text,
                        parse_mode="HTML",
                        protect_content=True,
                        reply_markup=kb_redeem(hid, amount, lang, hb_type, skin.get("activity_link")),
                    )
                else:
                    await ctx.bot.send_photo(
                        chat_id=uid,
                        photo=dm_file_id,
                        caption=send_message_text,
                        parse_mode="HTML",
                        protect_content=True,
                        reply_markup=kb_redeem(hid, amount, lang, hb_type, skin.get("activity_link")),
                    )
            else:
                await ctx.bot.send_message(
                    uid,
                    send_message_text,
                    parse_mode="HTML",
                    reply_markup=kb_redeem(hid, amount, lang, hb_type, skin.get("activity_link")),
                )
        except TelegramBadRequest:
            await ctx.bot.send_message(
                uid,
                tr(lang, "dm_got", amount=amount),
                reply_markup=kb_redeem(hid, amount, lang, hb_type, skin.get("activity_link")),
            )

    except TelegramForbiddenError:
        await ctx.r.set_dm_block(uid, DM_BLOCK_TTL_SEC)
        try:
            await callback.answer(tr(lang, "dm_blocked"), show_alert=True)
        except TelegramBadRequest:
            pass
        return



@router.callback_query(F.data.startswith("hb_redeem:"))
async def cb_redeem(callback: CallbackQuery, ctx: AppCtx):
    hb_type = _get_callback_hb_type(callback.data)
    lang = hb_type


    uid = callback.from_user.id
    hid = int(callback.data.split(":")[1])
    
    user_contribute_today = await HongbaoService.get_contribute_today(uid)
    if user_contribute_today and user_contribute_today.get("count", 0) < 1:
        await callback.answer(tr(lang, "redeem_after_talk"), show_alert=True)
        return

    try:
        await callback.answer(tr(lang, "redeem_processing"), show_alert=False)
    except TelegramBadRequest:
        pass

    skin = await HongbaoService.get_hongbao(hid) or {}


    # skin_key = await ctx.r.get_hb_skin(hid)
    # print(f"Redeem: hid={hid} skin_key={skin_key}")
    # skin = next((s for s in RP_SKINS if s["hb_key"] == skin_key), None)
    # print(f"Skin: {skin}")

    code, amount = await ctx.r.redeem_prep(hid, uid,claiming_ttl=30)
    print(f"Redeem prep: code={code} amount={amount}")

    if code == -2 :
        await callback.message.answer(tr(lang, "redeem_fail_expired"))
        try:
            hongbao_info = await HongbaoService.get_hongbao(hid)  # 仅为了日志记录，顺便验证是否真的过期（MySQL 层）
            expired_hb_type = _normalize_hb_type((hongbao_info or {}).get("hb_type") or hb_type)
            await callback.message.edit_reply_markup(reply_markup=kb_expired(lang=expired_hb_type, hb_type=expired_hb_type, activity_link=hongbao_info.get("activity_link")))
        except TelegramBadRequest:
            await callback.message.edit_reply_markup(reply_markup=None)
            pass
        return

    if code == 2:
        # 已领取：按钮改“已领取”，提示用“重复点击已忽略/已领取过”二选一
        try:
            await callback.message.edit_reply_markup(reply_markup=kb_done(lang=lang, hb_type=hb_type, activity_link=skin.get("activity_link")))
        except TelegramBadRequest:
            pass
        await callback.message.answer(tr(lang, "redeem_ok_dup"))
        return

    if code == 3:
        await callback.message.answer(tr(lang, "redeem_busy"))
        return

    if code != 0 or amount <= 0:
        await callback.message.answer(tr(lang, "redeem_state_bad"))
        return



    print(f"Redeem prep success: hid={hid} uid={uid} amount={amount}")

    ok, msg = await HongbaoService.redeem_add_points(hid, uid, amount, skin)
    if ok:
        await ctx.r.set_claimed(hid, uid)
        try:
            await callback.message.edit_reply_markup(reply_markup=kb_done(lang=lang, hb_type=hb_type, activity_link=skin.get("activity_link")))
        except TelegramBadRequest:
            pass
        await callback.message.answer(tr(lang, "redeem_ok", amount=amount))
        return

    if msg == "already_redeemed":
        await ctx.r.set_claimed(hid, uid)
        try:
            await callback.message.edit_reply_markup(reply_markup=kb_done(lang=lang, hb_type=hb_type, activity_link=skin.get("activity_link")))
        except TelegramBadRequest:
            pass
        await callback.message.answer(tr(lang, "redeem_ok_dup"))
        return

    await ctx.r.rollback_pending(hid, uid)
    await callback.message.answer(tr(lang, "redeem_fail", msg=msg))


@router.callback_query(F.data.startswith("hb_done:"))
async def cb_done(callback: CallbackQuery, ctx: AppCtx):
    lang = _get_callback_hb_type(callback.data)
    try:
        await callback.answer(tr(lang, "redeem_ok_dup"), show_alert=False)
    except TelegramBadRequest:
        pass

@router.callback_query(F.data.startswith("hb_expired:"))
async def cb_expired(callback: CallbackQuery, ctx: AppCtx):
    lang = _get_callback_hb_type(callback.data)
    try:
        await callback.answer(tr(lang, "redeem_fail_expired"), show_alert=False)
    except TelegramBadRequest:
        pass



@router.message(Command("setcommand"))
async def handle_set_comment_command(message: Message, state: FSMContext):
    bot = message.bot

    await bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
    await bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())
    await bot.delete_my_commands(scope=BotCommandScopeDefault())

    await bot.set_my_commands(
        commands=[
            BotCommand(command="start", description="首页菜单"),
            BotCommand(command="hb", description="发送积分红包"),
            BotCommand(command="lj", description="发送龙精红包"),
            BotCommand(command="home", description="回家"),
        ],
        scope=BotCommandScopeAllPrivateChats()
    )

    await bot.set_my_commands(
        commands=[
            BotCommand(command="hb", description="发送积分红包"),
            BotCommand(command="lj", description="发送龙精红包"),
        ],
        scope=BotCommandScopeAllGroupChats()
    )

    print("✅ 已设置 HB 命令列表", flush=True)
    await message.reply("✅ 已更新命令列表")

def _parse_base(old_text: str, lang: str):
    RE_TOTAL, RE_COUNT, RE_HEADER, RE_SN, RE_TIME, _RE_ITEM  = get_patterns(lang)
    m_header = RE_HEADER.search(old_text)
    m_total  = RE_TOTAL.search(old_text)
    m_count  = RE_COUNT.search(old_text)
    m_sn     = RE_SN.search(old_text)
    m_time   = RE_TIME.search(old_text)


    sender = (m_header.group(1).strip() if m_header else tr(lang, "default_someone"))
    total_amount = int(m_total.group(1)) if m_total else 0
    total_count = int(m_count.group(1)) if m_count else 0
    hb_sn = int(m_sn.group(1)) if m_sn else 0
    created_at = m_time.group(1).strip() if m_time else ""


    return sender, total_amount, total_count, hb_sn, created_at


def _parse_items(old_text: str, lang: str):
    RE_TOTAL, RE_COUNT, RE_HEADER, RE_SN, RE_TIME, RE_ITEM = get_patterns(lang)
    items = []
    for m in RE_ITEM.finditer(old_text):
        name = m.group(1).strip()
        amt = int(m.group(2))
        cost = m.group(3).strip()
        items.append((name, amt, cost))
    return items


def _fmt_cost(seconds: float) -> str:
    return ">5s" if seconds >= 5.0 else f"{seconds:.3f}s"


async def _resolve_sender_name(ctx: AppCtx, skin: dict, parsed_sender: str, lang: str) -> str:
    sender_user_id = int(skin.get("sender_user_id") or 0)
    if sender_user_id > 0:
        try:
            chat = await ctx.bot.get_chat(sender_user_id)
            if chat.first_name:
                return chat.first_name
            if chat.username:
                return "@" + chat.username
        except Exception:
            pass

    parsed_sender = (parsed_sender or "").strip()
    if parsed_sender and parsed_sender != tr(lang, "default_someone"):
        return parsed_sender

    return tr(lang, "default_someone")



