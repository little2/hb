from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError,TelegramAPIError,TelegramNotFound,TelegramMigrateToChat, TelegramRetryAfter
from aiogram.utils.formatting import Text
import lz_var
import re

import html
from functools import lru_cache

from config import (
    MIN_UNIT, MAX_COUNT, DEFAULT_EXPIRE_MINUTES,
    GROUP_NOTICE_THROTTLE, GROUP_NOTICE_PER_SEC, DM_BLOCK_TTL_SEC,
    TARGET_CHAT_ID, TARGET_MESSAGE_THREAD_ID,REVIEW_CHAT_ID
)



from infra.redis_layer import RedisLayer, split_amounts
from services.hongbao_service import HongbaoService

from material import RP_SKINS, I18N

def _h(s: str) -> str:
    return html.escape(s or "", quote=False)
import random

def pick_rp_skin() -> dict:
    if not RP_SKINS:
        raise RuntimeError("RP_SKINS is empty")
    return random.choice(RP_SKINS)

def tr(lang: str, key: str, **kwargs) -> str:
    pack = I18N.get(lang) or I18N["zh"]
    s = pack.get(key) or I18N["zh"].get(key) or key
    return s.format(**kwargs)

@lru_cache(maxsize=4)
def get_patterns(lang: str):
    pack = I18N.get(lang) or I18N["zh"]
    return (
        re.compile(pack["re_total"], re.M),
        re.compile(pack["re_count"], re.M),
        re.compile(pack["re_header"], re.M),
        re.compile(pack["re_sn"], re.M),
        re.compile(pack["re_time"], re.M),
        re.compile(pack["re_item"], re.M),
    )

router = Router()


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
    if message.photo:
        media = message.photo[-1]
        file_type = "photo"
    elif message.video:
        media = message.video
        file_type = "video"

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

    


def kb_claim(hid: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=tr(lang, "btn_claim"),
        callback_data=f"hb_claim:{hid}"
    )]])

def kb_redeem(hid: int, amount: int, lang: str, activity_link: str | None = None) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=tr(lang, "btn_redeem", amount=amount),
            callback_data=f"hb_redeem:{hid}"
        )
    ]

    if activity_link:
        buttons.append(InlineKeyboardButton(
            text=tr(lang, "btn_activity"),
            url=activity_link
        ))

    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def kb_expired(lang: str, activity_link: str | None = None) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=tr(lang, "btn_expired"),
            callback_data="hb_expired"
        )
    ]
    if activity_link:
        buttons.append(InlineKeyboardButton(
            text=tr(lang, "btn_activity"),
            url=activity_link
        ))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])

def kb_done(lang: str, activity_link: str | None = None) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=tr(lang, "btn_done"),
            callback_data="hb_done"
        )
    ]
    if activity_link:
        buttons.append(InlineKeyboardButton(
            text=tr(lang, "btn_activity"),
            url=activity_link
        ))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])

@dataclass
class AppCtx:
    r: RedisLayer
    bot: any
    lang: str = "zh"   
    skin : dict | None = None


@router.message(Command("start"))
async def handle_start(message: Message,  command: Command = Command("start"), ctx: AppCtx = None):
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


@router.message(Command("rp"))
async def cmd_rp(message: Message, ctx: AppCtx):
    lang = ctx.lang
    if message.chat.type not in ("group", "supergroup"):
        await message.reply(tr(lang, "rp_group_only"))
        return

    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(tr(lang, "rp_usage"))
        return

    try:
        total_amount = int(parts[1])
        total_count = int(parts[2])
        expire_minutes = int(parts[3]) if len(parts) >= 4 else DEFAULT_EXPIRE_MINUTES
    except ValueError:
        await message.reply(tr(lang, "rp_param_int"))
        return

    skin = pick_rp_skin()


    

    
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

    await _do_create_hongbao(ctx, msg, hongbao)

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
        clt_row = await HongbaoService.get_user_collection(id=id)
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
    

async def _do_create_hongbao(ctx: AppCtx, msg:dict,  hongbao:dict):
    lang = ctx.lang
    total_count = hongbao["total_count"]
    total_amount = hongbao["total_amount"]
    expire_minutes = hongbao["expire_minutes"]
    skin = hongbao["skin"]
    bot_name = getattr(lz_var, "bot_username", "") or ""

    mode = "promote"
    if msg and msg.get("mode"):
        mode = msg["mode"]

    print(f"msg====>{msg}")
    if total_count <= 0 or total_count > MAX_COUNT:
        await ctx.bot.send_message(chat_id=msg["chat_id"], message_thread_id=msg["message_thread_id"], text=tr(lang, "rp_count_range", max_count=MAX_COUNT))
        return
    if total_amount < total_count * MIN_UNIT:
        await ctx.bot.send_message(chat_id=msg["chat_id"], message_thread_id=msg["message_thread_id"], text=tr(lang, "rp_total_too_small", min_unit=MIN_UNIT))
        return
    if expire_minutes <= 0:
        await ctx.bot.send_message(chat_id=msg["chat_id"], message_thread_id=msg["message_thread_id"], text=tr(lang, "rp_expire_invalid"))
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
        hid = await HongbaoService.create_hongbao(sender_id, chat_id, total_amount, total_count, expire_at, skin)
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

            if cover_file_type == "video":
                sent = await ctx.bot.send_video(
                    chat_id=msg['chat_id'],
                    message_thread_id=msg['message_thread_id'],
                    video=cover_file_id,
                    caption=text,
                    parse_mode="HTML",
                    protect_content=True,
                    reply_markup=kb_claim(hid, lang),
                )
            else:
                sent = await ctx.bot.send_photo(
                    chat_id=msg['chat_id'],
                    message_thread_id=msg['message_thread_id'],
                    photo=cover_file_id,
                    caption=text,
                    parse_mode="HTML",
                    protect_content=True,
                    reply_markup=kb_claim(hid, lang),
                )
            

        except TelegramForbiddenError as e:
            print(
                f"[HONGBAO] send_photo forbidden, bot may be kicked or has no permission. "
                f"chat_id={msg.get('chat_id')} thread_id={msg.get('message_thread_id')} err={e}",
                flush=True,
            )
            return
        except Exception as e:
            try:
                await ctx.bot.send_message(
                    chat_id=msg["chat_id"],
                    message_thread_id=msg["message_thread_id"],
                    text=f"❌ 发送红包消息失败：{e}"
                )
            except TelegramForbiddenError as notify_err:
                print(
                    f"[HONGBAO] notify send failure skipped due to forbidden. "
                    f"chat_id={msg.get('chat_id')} thread_id={msg.get('message_thread_id')} err={notify_err}",
                    flush=True,
                )
            return

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
    lang = ctx.lang
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

    # skin_key = await ctx.r.get_hb_skin(hid)

    # skin = next((s for s in RP_SKINS if s["hb_key"] == skin_key), None)
    skin = await HongbaoService.get_hongbao(hid) or {}

    if base_msg:
        old_text = (base_msg.caption or base_msg.text or "")
       

        lang = ctx.lang
        sender, total_amount, total_count, hb_sn, created_at = _parse_base(old_text, lang)

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

            if is_empty:
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
        if (not is_empty) and GROUP_NOTICE_THROTTLE:
            try:
                ok_to_edit = await ctx.r.allow_group_notice(hid, GROUP_NOTICE_PER_SEC)
            except Exception:
                ok_to_edit = True
            if not ok_to_edit:
                new_reply_markup = base_msg.reply_markup  # 不变
                # 直接跳过 edit（避免被刷爆）
                goto_dm = True
            else:
                goto_dm = True
        else:
            goto_dm = True

        if goto_dm:
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

            if dm_file_type == "video":
                await ctx.bot.send_video(
                    chat_id=uid,
                    video=dm_file_id,
                    caption=send_message_text,
                    parse_mode="HTML",
                    protect_content=True,
                    reply_markup=kb_redeem(hid, amount, lang, skin.get("activity_link")),
                )
            else:
                await ctx.bot.send_photo(
                    chat_id=uid,
                    photo=dm_file_id,
                    caption=send_message_text,
                    parse_mode="HTML",
                    protect_content=True,
                    reply_markup=kb_redeem(hid, amount, lang, skin.get("activity_link")),
                )
        except TelegramBadRequest:
            await ctx.bot.send_message(
                uid,
                tr(lang, "dm_got", amount=amount),
                reply_markup=kb_redeem(hid, amount, lang, skin.get("activity_link")),
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
    lang = ctx.lang


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

    code, amount = await ctx.r.redeem_prep(hid, uid, claiming_ttl=30)
    print(f"Redeem prep: code={code} amount={amount}")

    if code == -2 :
        await callback.message.answer(tr(lang, "redeem_fail_expired"))
        try:
            hongbao_info = await HongbaoService.get_hongbao(hid)  # 仅为了日志记录，顺便验证是否真的过期（MySQL 层）
            await callback.message.edit_reply_markup(reply_markup=kb_expired(lang=lang, activity_link=hongbao_info.get("activity_link")))
        except TelegramBadRequest:
            await callback.message.edit_reply_markup(reply_markup=None)
            pass
        return

    if code == 2:
        # 已领取：按钮改“已领取”，提示用“重复点击已忽略/已领取过”二选一
        try:
            await callback.message.edit_reply_markup(reply_markup=kb_done(lang=lang, activity_link=skin.get("activity_link")))
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

    ok, msg = await HongbaoService.redeem_add_points(hid, uid, amount)
    if ok:
        await ctx.r.set_claimed(hid, uid)
        try:
            await callback.message.edit_reply_markup(reply_markup=kb_done(lang=lang, activity_link=skin.get("activity_link")))
        except TelegramBadRequest:
            pass
        await callback.message.answer(tr(lang, "redeem_ok", amount=amount))
        return

    if msg == "already_redeemed":
        await ctx.r.set_claimed(hid, uid)
        try:
            await callback.message.edit_reply_markup(reply_markup=kb_done(lang=lang, activity_link=skin.get("activity_link")))
        except TelegramBadRequest:
            pass
        await callback.message.answer(tr(lang, "redeem_ok_dup"))
        return

    await ctx.r.rollback_pending(hid, uid)
    await callback.message.answer(tr(lang, "redeem_fail", msg=msg))


@router.callback_query(F.data == "hb_done")
async def cb_done(callback: CallbackQuery, ctx: AppCtx):
    lang = ctx.lang
    try:
        await callback.answer(tr(lang, "redeem_ok_dup"), show_alert=False)
    except TelegramBadRequest:
        pass

@router.callback_query(F.data == "hb_expired")
async def cb_expired(callback: CallbackQuery, ctx: AppCtx):
    lang = ctx.lang
    try:
        await callback.answer(tr(lang, "redeem_fail_expired"), show_alert=False)
    except TelegramBadRequest:
        pass


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



