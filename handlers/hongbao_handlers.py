from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

import re

import html
from functools import lru_cache

from config import (
    MIN_UNIT, MAX_COUNT, DEFAULT_EXPIRE_MINUTES,
    GROUP_NOTICE_THROTTLE, GROUP_NOTICE_PER_SEC, DM_BLOCK_TTL_SEC
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

# from aiogram import Router, F
# from aiogram.types import Message

@router.message(F.chat.type == ChatType.PRIVATE, F.photo)
async def on_photo(message: Message):
    # message.photo 是 List[PhotoSize]
    largest = message.photo[-1]

    await message.reply(
        "📸 已识别到图片（最大尺寸）\n"
        f"file_unique_id: {largest.file_unique_id}\n"
        f"file_id: {largest.file_id}\n"
        f"size: {largest.width}x{largest.height}"
    )


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

    if total_count <= 0 or total_count > MAX_COUNT:
        await message.reply(tr(lang, "rp_count_range", max_count=MAX_COUNT))
        return
    if total_amount < total_count * MIN_UNIT:
        await message.reply(tr(lang, "rp_total_too_small", min_unit=MIN_UNIT))
        return
    if expire_minutes <= 0:
        await message.reply(tr(lang, "rp_expire_invalid"))
        return

    sender_id = message.from_user.id if message.from_user else 0
    chat_id = message.chat.id

    now = datetime.now()
    expire_at = now + timedelta(minutes=expire_minutes)
    ttl_sec = max(1, int((expire_at - now).total_seconds()))

    hid = await HongbaoService.create_hongbao(sender_id, chat_id, total_amount, total_count, expire_at)
    if hid <= 0:
        await message.reply(tr(lang, "redeem_busy"))
        return


    amounts = split_amounts(total_amount, total_count, MIN_UNIT)
    await ctx.r.init_list(hid, amounts, ttl_sec)

 
    sender_name = _h(message.from_user.first_name) if message.from_user else _h(tr(lang, "default_someone"))

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    skin = pick_rp_skin()
    await ctx.r.set_hb_skin(hid, skin["key"], ttl_sec)

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




    # sent = await message.answer(text, reply_markup=kb_claim(hid, lang))
    try:
        sent = await message.answer_photo(
            photo=skin["file_id_cover"],
            caption=text,
            reply_markup=kb_claim(hid, lang),
            parse_mode="HTML",
        )
    except Exception as e:
        await message.reply(f"❌ 发送红包消息失败：{e}")
        return

    await HongbaoService.bind_message(hid, sent.message_id)

    # ======= Pin 消息到群组 =======
    try:
        await ctx.bot.pin_chat_message(
            chat_id=message.chat.id,
            message_id=sent.message_id,
            disable_notification=True,  # 不发送通知
        )
    except TelegramBadRequest as e:
        # pin 失败不影响红包功能，仅记录（可选）
        pass
    except Exception as e:
        pass

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

    skin_key = await ctx.r.get_hb_skin(hid)

    skin = next((s for s in RP_SKINS if s["key"] == skin_key), None)

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
            send_message_text += "\n\n" + skin["dm_text"]

        try:
            await ctx.bot.send_photo(
                chat_id=uid,
                photo=skin.get("file_id_dm"),
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
    try:
        await callback.answer(tr(lang, "redeem_processing"), show_alert=False)
    except TelegramBadRequest:
        pass

    uid = callback.from_user.id
    hid = int(callback.data.split(":")[1])
    
    skin_key = await ctx.r.get_hb_skin(hid)
    skin = next((s for s in RP_SKINS if s["key"] == skin_key), None)

    code, amount = await ctx.r.redeem_prep(hid, uid, claiming_ttl=30)

    if code == -2:
        await callback.message.answer(tr(lang, "redeem_fail_expired"))
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



