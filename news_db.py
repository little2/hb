# news_db.py
import asyncio
import asyncpg
from typing import Optional, List, Any, Dict


class NewsDatabase:
    # —— 全局单例池 & 锁 ——
    _pool: Optional[asyncpg.Pool] = None
    _lock = asyncio.Lock()

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8):
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self.pool: Optional[asyncpg.Pool] = None

    async def init(self):
        """幂等初始化：只在第一次真正创建连接池，其余复用。"""
        if NewsDatabase._pool is not None:
            self.pool = NewsDatabase._pool
            return

        async with NewsDatabase._lock:
            if NewsDatabase._pool is None:
                NewsDatabase._pool = await asyncpg.create_pool(
                    dsn=self.dsn,
                    min_size=self.min_size,
                    max_size=self.max_size,
                    command_timeout=60,
                    max_inactive_connection_lifetime=300,
                    init=self._on_connect,
                )
        self.pool = NewsDatabase._pool

    @staticmethod
    async def _on_connect(conn: asyncpg.Connection):
        # 会话级安全设置（防长事务、超时）
        await conn.execute("SET idle_in_transaction_session_timeout = 30000")  # 30s
        await conn.execute("SET statement_timeout = 60000")                    # 60s

    async def close(self):
        """优雅关闭连接池（在应用关闭时调用）"""
        if NewsDatabase._pool is not None:
            await NewsDatabase._pool.close()
            NewsDatabase._pool = None
            self.pool = None

    async def ensure_schema(self) -> None:
        """
        启动时幂等建表/建索引/补字段：
        - send_state enum
        - sequences: news_content_id_seq / news_send_queue_id_seq / news_user_id_seq
        - tables: news_content / news_send_queue / news_user
        - columns: news_content.retry
        - indexes/unique indexes
        """
        if self.pool is None:
            raise RuntimeError("DB pool not initialized. Call await db.init() first.")

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 1) enum type: send_state
                await conn.execute(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'send_state') THEN
                            CREATE TYPE public.send_state AS ENUM ('pending','sent','failed');
                        END IF;
                    END$$;
                    """
                )

                # 2) sequences (match your DEFAULT nextval('...'))
                await conn.execute("CREATE SEQUENCE IF NOT EXISTS public.news_content_id_seq;")
                await conn.execute("CREATE SEQUENCE IF NOT EXISTS public.news_send_queue_id_seq;")
                await conn.execute("CREATE SEQUENCE IF NOT EXISTS public.news_user_id_seq;")

                # 3) tables
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS public.news_content
                    (
                        id integer NOT NULL DEFAULT nextval('public.news_content_id_seq'::regclass),
                        title character varying NOT NULL,
                        text text,
                        file_id character varying,
                        file_type character varying,
                        button_str text,
                        created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
                        bot_name character varying(30),
                        business_type character varying(30),
                        content_id bigint,
                        retry integer DEFAULT 0,
                        thumb_file_unique_id character varying(100),
                        CONSTRAINT news_content_pkey PRIMARY KEY (id),
                        CONSTRAINT news_content_file_unique_id_key UNIQUE (thumb_file_unique_id)
                    );
                    """
                )

                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS public.news_user
                    (
                        id integer NOT NULL DEFAULT nextval('public.news_user_id_seq'::regclass),
                        user_id bigint NOT NULL,
                        business_type character varying(30) NOT NULL,
                        created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
                        last_sent_at timestamp without time zone,
                        expire_at timestamp without time zone,
                        CONSTRAINT news_user_pkey PRIMARY KEY (id),
                        CONSTRAINT news_user_user_id_business_type_key UNIQUE (user_id, business_type)
                    );
                    """
                )

                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS public.news_send_queue
                    (
                        id integer NOT NULL DEFAULT nextval('public.news_send_queue_id_seq'::regclass),
                        user_ref_id integer NOT NULL,
                        news_id integer NOT NULL,
                        bot_id integer,
                        state public.send_state DEFAULT 'pending'::public.send_state,
                        retry_count integer DEFAULT 0,
                        last_try_at timestamp without time zone,
                        sent_at timestamp without time zone,
                        fail_reason text,
                        created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT news_send_queue_pkey PRIMARY KEY (id),
                        CONSTRAINT news_send_queue_user_ref_id_news_id_key UNIQUE (user_ref_id, news_id),
                        CONSTRAINT news_send_queue_news_id_fkey FOREIGN KEY (news_id)
                            REFERENCES public.news_content (id)
                            ON UPDATE NO ACTION
                            ON DELETE CASCADE,
                        CONSTRAINT news_send_queue_user_ref_id_fkey FOREIGN KEY (user_ref_id)
                            REFERENCES public.news_user (id)
                            ON UPDATE NO ACTION
                            ON DELETE CASCADE
                    );
                    """
                )



                # 5) indexes / unique indexes
                # (a) ensure logical dedupe key for news: (business_type, content_id)
                await conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS ux_news_content_business_contentid
                    ON public.news_content (business_type, content_id);
                    """
                )

                # (b) send_queue indexes (you already had state/state+bot/user_ref; add (state, created_at desc))
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_send_queue_state
                    ON public.news_send_queue (state);
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_send_queue_state_bot
                    ON public.news_send_queue (state, bot_id);
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_send_queue_user_ref
                    ON public.news_send_queue (user_ref_id);
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_send_queue_state_createdat
                    ON public.news_send_queue (state, created_at DESC);
                    """
                )

                # (c) optional: speed up UPDATE news_content by (bot_name, thumb_file_unique_id) if you keep that WHERE
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_news_content_bot_fuid
                    ON public.news_content (bot_name, thumb_file_unique_id);
                    """
                )

    # ------------------------
    # 新闻内容 CRUD
    # ------------------------

    async def insert_news(
        self,
        title: str,
        text: str,
        content_id: Optional[int] = None,
        file_id: Optional[str] = None,
        thumb_file_unique_id: Optional[str] = None,
        file_type: Optional[str] = None,
        button_str: Optional[str] = None,
        bot_name: Optional[str] = None,
        business_type: Optional[str] = None,
    ) -> int:
        """插入一条 news_content 并返回 id"""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO news_content
                    (title, text, content_id, file_id, thumb_file_unique_id, file_type, button_str, bot_name, business_type, retry)
                VALUES
                    ($1,   $2,   CAST($3 AS BIGINT), $4,   $5,           $6,        $7,         $8,       $9,       0)
                RETURNING id
                """,
                title, text, content_id, file_id, thumb_file_unique_id, file_type, button_str, bot_name, business_type,
            )

    async def update_news_by_id(
        self,
        news_id: int,
        text: Optional[str] = None,
        content_id: Optional[int] = None,
        file_id: Optional[str] = None,
        thumb_file_unique_id: Optional[str] = None,
        file_type: Optional[str] = None,
        button_str: Optional[str] = None,
        bot_name: Optional[str] = None,
        business_type: Optional[str] = None,
    ) -> None:
        """按 id 更新 news_content 的多个字段"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE news_content
                SET text = $2,
                    content_id = CAST($3 AS BIGINT),
                    file_id = $4,
                    thumb_file_unique_id = $5,
                    file_type = $6,
                    button_str = $7,
                    bot_name = $8,
                    business_type = $9
                WHERE id = $1
                """,
                news_id, text, content_id, file_id, thumb_file_unique_id, file_type, button_str, bot_name, business_type,
            )

    async def set_news_file_id(self, thumb_file_unique_id: str, file_id: str, bot_username: str) -> None:
        """仅更新 file_id，避免误改其它字段"""
        async with self.pool.acquire() as conn:
            sql = """
            UPDATE news_content
            SET file_id = $1
            WHERE bot_name = $2 AND thumb_file_unique_id = $3;
            """
            # print("EXEC:", sql, "PARAMS:", (file_id, bot_username, thumb_file_unique_id))

            r = await conn.execute(sql, file_id, bot_username, thumb_file_unique_id)
            # print(r)

    async def get_news_media_by_id(self, news_id: int) -> Optional[asyncpg.Record]:
        """show 用：取回媒体字段"""
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT file_id, text, file_type, button_str
                FROM news_content
                WHERE id = $1
                """,
                news_id
            )

    async def get_business_type_by_news_id(self, news_id: int) -> Optional[str]:
        """push 用：拿 business_type"""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT business_type FROM news_content WHERE id = $1",
                news_id
            )

    async def get_news_id_by_content_business(
        self, content_id: Optional[int], business_type: Optional[str]
    ) -> Optional[int]:
        """receive_media 用：按 content_id + business_type 查是否已有"""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT id
                FROM news_content
                WHERE content_id = CAST($1 AS BIGINT)
                AND business_type = $2
                LIMIT 1
                """,
                content_id, business_type
            )



    async def get_news_id_by_thumb_file_unique_id(
        self, thumb_file_unique_id: Optional[str]
    ) -> Optional[int]:
        """receive_media 用：按 thumb_file_unique_id+bot_name 查是否已有"""
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT id,business_type 
                FROM news_content
                WHERE thumb_file_unique_id = $1 
                LIMIT 1
                """,
                thumb_file_unique_id
            )

               

    async def find_missing_media_records(self, limit: int = 5) -> List[asyncpg.Record]:
        """
        补档用：找 file_id 为空但有 thumb_file_unique_id 的新闻
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT id, thumb_file_unique_id
                FROM news_content
                WHERE file_id IS NULL
                AND thumb_file_unique_id IS NOT NULL AND retry < 3
                ORDER BY RANDOM() 
                LIMIT $1;
                """,
                limit
            )

    # ------------------------
    # 用户与任务
    # ------------------------

    async def upsert_user_and_seed_latest_task(
        self, user_id: int, business_type: str, expire_ts: int
    ) -> None:
        """
        /start 用：在一个事务里 upsert 用户并（若有）插入“最新新闻”的第一条发送任务
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO news_user (user_id, business_type, expire_at)
                    VALUES ($1, $2, to_timestamp($3))
                    ON CONFLICT (user_id, business_type)
                    DO UPDATE SET expire_at = to_timestamp($3)
                    """,
                    user_id, business_type, expire_ts
                )
                latest_id = await conn.fetchval(
                    """
                    SELECT id FROM news_content
                    WHERE business_type = $1
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    business_type
                )
                if latest_id:
                    await conn.execute(
                        """
                        INSERT INTO news_send_queue (user_ref_id, news_id)
                        SELECT id, $1
                        FROM news_user
                        WHERE user_id = $2 AND business_type = $3
                        ON CONFLICT DO NOTHING
                        """,
                        latest_id, user_id, business_type
                    )

    async def get_active_user_refs(self, business_type: str):
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT id
                FROM news_user
                WHERE business_type = $1
                  AND (expire_at IS NULL OR expire_at > NOW())
                """,
                business_type,
            )

    async def create_send_tasks(self, news_id: int, business_type: str) -> None:
        """批量把该 business_type 的有效用户塞到发送队列"""
        print(f"🆕 为新闻 NewsID={news_id} 创建发送任务，业务类型={business_type}", flush=True)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO news_send_queue (user_ref_id, news_id)
                SELECT u.id, $1
                FROM news_user u
                WHERE u.business_type = $2
                  AND (u.expire_at IS NULL OR u.expire_at > NOW())
                ON CONFLICT DO NOTHING
                """,
                news_id, business_type,
            )

    async def get_pending_tasks(self, limit: int = 10):
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT q.id AS task_id, q.user_ref_id, u.user_id,
                       c.text, c.file_id, c.file_type, c.button_str
                FROM news_send_queue q
                JOIN news_user u   ON q.user_ref_id = u.id
                JOIN news_content c ON q.news_id = c.id
                WHERE q.state = 'pending'
                ORDER BY q.created_at DESC 
                LIMIT $1
                """,
                limit,
            )

    async def mark_sent(self, task_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE news_send_queue
                SET state='sent', sent_at=NOW()
                WHERE id = $1
                """,
                task_id,
            )

    async def mark_failed(self, task_id: int, reason: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE news_send_queue
                SET state='failed',
                    fail_reason=$2,
                    last_try_at=NOW()
                WHERE id = $1
                """,
                task_id, reason,
            )

    
    async def remove_news_user_by_ref_id(self, user_ref_id: int) -> None:
        """通过 user_ref_id 删除 news_user 记录"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM news_user WHERE id = $1; ",
                user_ref_id
            )
            print(f"🗑️ 已删除 user_ref_id={user_ref_id} 的 news_user 记录", flush=True)

            await conn.execute(
                "DELETE FROM news_send_queue WHERE user_ref_id = $1 and state = 'pending';",
                user_ref_id
            )
            print(f"🗑️ 已删除 user_ref_id={user_ref_id} 的 news_send_queue 记录", flush=True)

    async def add_retry_count_for_news_id(
            self,            
            news_id: int,
        ) -> None:
            """按 id 增加重试次数"""
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE news_content
                    SET retry = retry+1
                    WHERE id = $1
                    """,
                    news_id,
                )