# news_sender.py
import asyncio
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CopyTextButton
from aiogram.exceptions import TelegramRetryAfter

from news_db import NewsDatabase

RATE_LIMIT_DEFAULT = 20
MAX_RETRIES_DEFAULT = 3


def parse_button_str(button_str: str, juhuacode: str = None) -> InlineKeyboardMarkup | None:
    """
    button_str: 按鈕描述字串
    juhuacode: 若有則額外加一個複製按鈕
    """
    if not button_str and not juhuacode:
        return None
    keyboard: list[list[InlineKeyboardButton]] = []
    if button_str:
        for line in button_str.strip().split("\n"):
            row: list[InlineKeyboardButton] = []
            for part in line.split("&&"):
                part = part.strip()
                if " - " in part:
                    text, url = part.split(" - ", 1)
                    row.append(InlineKeyboardButton(text=text.strip(), url=url.strip()))
            if row:
                keyboard.append(row)
    # 新增複製按鈕
    if juhuacode:
        keyboard.append([
            InlineKeyboardButton(
                text="🌼",
                copy_text=CopyTextButton(text=juhuacode)
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None


async def _send_one(bot: Bot, task: dict, rate_limit: int, max_retries: int):
    """发送单条任务，带速率限制与退避重试。"""
    task_label = task.get("task_id", "preview")
    print(f"📤 发送任务: {task_label} 给用户: {task['user_id']}", flush=True)
    await asyncio.sleep(1 / max(rate_limit, 1))

    user_id = task["user_id"]
    button_str = task.get("button_str")
    juhuacode = task.get("juhuacode")
    comment = task.get("comment")
    send_kwargs = {
        "chat_id": user_id,
        "caption": task["text"],
        "protect_content": True,
        "parse_mode": "HTML",
    }
    keyboard = parse_button_str(button_str, juhuacode) if (button_str or juhuacode) else None
    if keyboard is not None:
        send_kwargs["reply_markup"] = keyboard

    last_err = None
    delay = 1
    sent_message_id: int | None = None
    for attempt in range(max_retries + 1):
        try:
            # 新增: 若有 comment，先發送純 caption，後編輯加按鈕
            if comment:
                # 1. 仅在尚未发送成功时发送一次
                if sent_message_id is None:
                    msg = await bot.send_photo(
                        photo=task["file_id"],
                        chat_id=user_id,
                        caption=task["text"],
                        parse_mode="HTML",
                        protect_content=True,
                    )
                    sent_message_id = msg.message_id
                # 2. 構造按鈕
                reply_markup = keyboard.to_python() if keyboard else {"inline_keyboard": []}
                # 3. 新增評論按鈕
                channel_id = str(user_id)
                if channel_id.startswith("-100"):
                    channel_id = channel_id[4:]
                comment_url = f"https://t.me/{channel_id}/{sent_message_id}?comment=1"
                reply_markup["inline_keyboard"].append([
                    {"text": "💬 评论", "url": comment_url}
                ])
                # 4. 編輯訊息加上 reply_markup
                await bot.edit_message_caption(
                    chat_id=user_id,
                    message_id=sent_message_id,
                    caption=task["text"],
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )
                print(f"✅ 成功发送并加评论按钮给用户 {user_id}", flush=True)
                return
            # 原有邏輯
            if task["file_id"]:
                if task["file_type"] == "photo" or task["file_type"] == "p":
                    retSent = await bot.send_photo(photo=task["file_id"], **send_kwargs)
                elif task["file_type"] == "video" or task["file_type"] == "v":
                    retSent = await bot.send_video(video=task["file_id"], **send_kwargs)
                else:
                    retSent = await bot.send_document(document=task["file_id"], **send_kwargs)
            else:
                message_kwargs = {
                    "chat_id": user_id,
                    "text": task["text"],
                    "protect_content": True,
                }
                if keyboard is not None:
                    message_kwargs["reply_markup"] = keyboard
                retSent = await bot.send_message(**message_kwargs)
            print(f"✅ 成功发送给用户 {user_id}", flush=True)
            return  # 成功
        except TelegramRetryAfter as e:
            print(f"⏳ Telegram 速率限制，等待 {e.retry_after} 秒后重试...", flush=True)
            await asyncio.sleep(e.retry_after + 0.1)
            last_err = e
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                await asyncio.sleep(delay)
                delay *= 2
            else:
                break
            print(f"⚠️ 发送失败，尝试重试 {attempt + 1}/{max_retries}...{last_err}", flush=True)
    raise last_err


async def send_news_batch(db: NewsDatabase, bot: Bot,
                          rate_limit: int = RATE_LIMIT_DEFAULT,
                          max_retries: int = MAX_RETRIES_DEFAULT):
    """批量发送：使用传入的单例 db / bot，不自建连接池和会话。"""
    await db.init()
    tasks = await db.get_pending_tasks(limit=rate_limit)

    for task in tasks:
        # asyncpg.Record 是只读结构，转成 dict 后才能安全修改字段
        task = dict(task)
        print(f"📤 发送任务: {task['task_id']} 给用户: {task['user_id']}", flush=True)
        try:
            #因为预览图肯定是图片
            print(f"📤 任务 {task['task_id']} 的文件类型被强制设置为 photo", flush=True)
            task['file_type'] = "photo"
            print(f"📤 任务 {task['task_id']} 的文件ID: {task['file_id']}", flush=True)
            retSend = await _send_one(bot, task, rate_limit=rate_limit, max_retries=max_retries)
            await db.mark_sent(task["task_id"])
        except Exception as e:
            # 避免数据库里塞过长的错误字符串
            reason = str(e)
            if len(reason) > 500:
                reason = reason[:500]
            await db.mark_failed(task["task_id"], reason)
            if reason == "Telegram server says - Bad Request: chat not found":
                print(f"⚠️ 移除用户user_ref_id {task['user_ref_id']}", flush=True)
                await db.remove_news_user_by_ref_id(int(task["user_ref_id"]))
            print(f"❌ 发送任务 {task['task_id']} 给用户 {task['user_id']} 失败: {reason}", flush=True)

