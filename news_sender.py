# news_sender.py
import asyncio
import json
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramRetryAfter

from news_db import NewsDatabase

RATE_LIMIT_DEFAULT = 20
MAX_RETRIES_DEFAULT = 3


def parse_button_str(button_str: str) -> InlineKeyboardMarkup | None:
    """
    独立实现，避免从 news_main 导入造成循环依赖。
    格式：
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


async def _send_one(bot: Bot, task: dict, rate_limit: int, max_retries: int):
    """发送单条任务，带速率限制与退避重试。"""
    print(f"📤 发送任务: {task['task_id']} 给用户: {task['user_id']}", flush=True)
    # 速率限制：简单 sleep，避免触发 flood
    await asyncio.sleep(1 / max(rate_limit, 1))

    user_id = task["user_id"]
    keyboard = parse_button_str(task["button_str"])
    send_kwargs = {
        "chat_id": user_id,
        "caption": task["text"],
        "reply_markup": keyboard,
        "protect_content": True,
    }

    last_err = None
    delay = 1
    for attempt in range(max_retries + 1):
        try:
            if task["file_id"]:
                if task["file_type"] == "photo" or task["file_type"] == "p":
                    retSent = await bot.send_photo(photo=task["file_id"], **send_kwargs)
                elif task["file_type"] == "video" or task["file_type"] == "v":
                    retSent = await bot.send_video(video=task["file_id"], **send_kwargs)
                else:
                    retSent = await bot.send_document(document=task["file_id"], **send_kwargs)
            else:
                retSent = await bot.send_message(
                    chat_id=user_id, text=task["text"],
                    reply_markup=keyboard, protect_content=True
                )
            print(f"✅ 成功发送给用户 {user_id}, 消息ID: {retSent}", flush=True)
            return  # 成功
        except TelegramRetryAfter as e:
            # Telegram 提示退避秒数
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
    # 重试耗尽仍失败
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

