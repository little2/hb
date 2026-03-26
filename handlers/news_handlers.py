from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Tuple

from services.news_service import NewsService



async def sync_news_content_cache_once(after_id: int | None = None, batch_size: int = 500) -> Dict[str, int]:
	"""
	MySQL(主) -> PostgreSQL(缓存) 增量同步一批 news_content。
	规则：
	1) 先取 PG 当前最大 id
	2) 从 MySQL 读取 id > max_id 的新记录
	3) 批量插入 PG（按 id 冲突忽略）
	"""
	await NewsService.init_sync_pools()

	if after_id is None:
		after_id = await NewsService.get_pg_news_content_max_id()
	print(f"📡 同步 news_content_cache: after_id={after_id}, batch_size={batch_size}", flush=True)

	mysql_rows = await NewsService.fetch_mysql_news_content_rows_after_id(
		after_id=int(after_id),
		batch_size=int(batch_size),
	)
	print(f"📥 从 MySQL 读取到 {len(mysql_rows)} 条 news_content 记录", flush=True)

	fetched = len(mysql_rows)
	source_max_id = int(after_id)
	if not mysql_rows:
		return {"after_id": int(after_id), "source_max_id": source_max_id, "fetched": 0, "inserted": 0}

	payload: List[Tuple[Any, ...]] = []
	for r in mysql_rows:
		source_max_id = max(source_max_id, int(r["id"]))
		payload.append(
			(
				int(r["id"]),
				(r.get("title") or "Untitled"),
				r.get("text"),
				r.get("file_id"),
				r.get("file_type"),
				r.get("button_str"),
				r.get("created_at"),
				r.get("bot_name"),
				r.get("business_type"),
				int(r["content_id"]) if r.get("content_id") is not None else None,
				r.get("thumb_file_unique_id"),
			)
		)

	inserted = await NewsService.bulk_insert_news_content_cache(payload)
	print(f"📤 插入到 PostgreSQL {inserted} 条 news_content 记录", flush=True)

	return {
		"after_id": int(after_id),
		"source_max_id": source_max_id,
		"fetched": fetched,
		"inserted": inserted,
	}


async def run_sync_db_loop(
	interval_seconds: int = 300,
	batch_size: int = 500,
) -> None:
	"""
	定时同步循环：每轮会持续追平到最新，再 sleep。
	"""
	while True:
		try:
			cursor_id = await NewsService.get_pg_news_content_max_id()
			round_inserted = 0
			round_batches = 0
			while True:
				stat = await sync_news_content_cache_once(after_id=cursor_id, batch_size=batch_size)
				round_batches += 1
				round_inserted += int(stat.get("inserted", 0))
				cursor_id = int(stat.get("source_max_id", cursor_id))

				if stat.get("fetched", 0) < int(batch_size):
					break

			print(
				f"🔄 同步数据库完成: batches={round_batches}, inserted~={round_inserted}",
				flush=True,
			)
		except Exception as e:
			print(f"❌ run_sync_db_loop 异常: {e}", flush=True)

		await asyncio.sleep(interval_seconds)


async def sync_membership_once(
	course_code: str = "xlj",
	business_type: str = "xlj",
	batch_size: int = 5000,
) -> Dict[str, int]:
	"""将 MySQL membership 有效会员同步到 PostgreSQL news_user。"""
	await NewsService.init_sync_pools()

	rows = await NewsService.fetch_active_membership_rows(
		course_code=course_code,
		limit=int(batch_size),
	)
	fetched = len(rows)
	if not rows:
		return {"fetched": 0, "upserted": 0}

	payload: List[Tuple[int, str, int]] = []
	for r in rows:
		uid_raw = r.get("user_id")
		expire_ts_raw = r.get("expire_timestamp")
		try:
			uid = int(uid_raw)
			expire_ts = int(expire_ts_raw)
		except (TypeError, ValueError):
			continue
		payload.append((uid, business_type, expire_ts))

	upserted = await NewsService.bulk_update_news_users_membership(payload)
	return {"fetched": fetched, "upserted": upserted}


async def run_sync_membership_loop(
	interval_seconds: int = 300,
	course_code: str = "xlj",
	business_type: str = "xlj",
	batch_size: int = 5000,
) -> None:
	"""定时同步会员到 news_user。"""
	while True:
		try:
			stat = await sync_membership_once(
				course_code=course_code,
				business_type=business_type,
				batch_size=batch_size,
			)
			print(
				f"🔄 同步会员完成: fetched={stat.get('fetched', 0)}, upserted={stat.get('upserted', 0)}",
				flush=True,
			)
		except Exception as e:
			print(f"❌ run_sync_membership_loop 异常: {e}", flush=True)

		await asyncio.sleep(interval_seconds)