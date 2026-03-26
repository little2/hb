from typing import Any, Dict, List, Tuple

from utils.db.tgone_mysql import MySQLPool
from utils.db.tgone_pgsql import PGPool
from news_config import DB_DSN

class NewsService:
    @classmethod
    async def in_block_list(cls, user_id: int) -> bool:
        row = await MySQLPool.fetchone(
            "SELECT 1 FROM block_list WHERE user_id=%s LIMIT 1",
            (user_id,)
        )
        return bool(row)

    @classmethod
    async def init_sync_pools(cls) -> None:
        PGPool.DSN = DB_DSN
        PGPool.MAX_SIZE = 5
        await PGPool.init_pool()
        await MySQLPool.init_pool()

    @classmethod
    async def get_pg_news_content_max_id(cls) -> int:
        max_id = await PGPool.fetchval("SELECT COALESCE(MAX(id), 0) FROM news_content")
        return int(max_id or 0)

    @classmethod
    async def fetch_mysql_news_content_rows_after_id(
        cls,
        after_id: int,
        batch_size: int,
    ) -> List[Dict[str, Any]]:
        return await MySQLPool.fetchall(
            """
            SELECT
                id,
                title,
                text,
                file_id,
                file_type,
                button_str,
                created_at,
                bot_name,
                business_type,
                content_id,
                thumb_file_unique_id
            FROM news_content
            WHERE id > %s and thumb_file_unique_id IS NOT NULL
            ORDER BY id ASC
            LIMIT %s
            """,
            (int(after_id), int(batch_size)),
            error_tag="sync_news_content_cache_once",
        )

    @classmethod
    async def bulk_insert_news_content_cache(cls, payload: List[Tuple[Any, ...]]) -> int:
        if not payload:
            return 0

        sql = """
        INSERT INTO news_content (
            id,
            title,
            text,
            file_id,
            file_type,
            button_str,
            created_at,
            bot_name,
            business_type,
            content_id,
            thumb_file_unique_id
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        ON CONFLICT (id) DO NOTHING
        """

        pg_pool = await PGPool.ensure_pool()
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(sql, payload)

        # asyncpg executemany 不回传影响行数，这里用输入量近似。
        return len(payload)

    @classmethod
    async def fetch_active_membership_rows(
        cls,
        course_code: str = "xlj",
        limit: int = 5000,
    ) -> List[Dict[str, Any]]:
        return await MySQLPool.fetchall(
            """
            SELECT
                user_id,
                expire_timestamp
            FROM membership
            WHERE expire_timestamp > UNIX_TIMESTAMP()
              AND course_code = %s
            ORDER BY expire_timestamp DESC
            LIMIT %s
            """,
            (course_code, int(limit)),
            error_tag="sync_membership_once",
        )

    @classmethod
    async def bulk_upsert_news_users_membership(
        cls,
        payload: List[Tuple[int, str, int]],
    ) -> int:
        if not payload:
            return 0

        sql = """
        INSERT INTO news_user (user_id, business_type, expire_at)
        VALUES ($1, $2, to_timestamp($3))
        ON CONFLICT (user_id, business_type)
        DO UPDATE SET expire_at = EXCLUDED.expire_at
        """

        pg_pool = await PGPool.ensure_pool()
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(sql, payload)

        return len(payload)