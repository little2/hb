# tgone_pgsql.py
import asyncpg
import asyncio
from functools import wraps
from inspect import stack
from typing import Optional, Any, Dict, List, Sequence, Tuple, Union


def _caller_info():
    frames = stack()
    if len(frames) > 2:
        frame = frames[2]
        return f"{frame.filename.split('/')[-1]}:{frame.function}:{frame.lineno}"
    return "unknown"


def reconnecting(func):
    """
    通用断线重连装饰器（asyncpg 版）：
    - 捕捉常见连接/接口错误：ConnectionDoesNotExistError, InterfaceError, PostgresConnectionError, CannotConnectNowError
    - 出错时重建连接池并重试一次
    """
    @wraps(func)
    async def wrapper(*args, **kwargs):
        cls = args[0] if args else None
        for attempt in (1, 2):
            try:
                return await func(*args, **kwargs)
            except (
                asyncpg.exceptions.ConnectionDoesNotExistError,
                asyncpg.exceptions.InterfaceError,
                asyncpg.exceptions.PostgresConnectionError,
                asyncpg.exceptions.CannotConnectNowError,
                OSError,
            ) as e:
                if not cls or attempt == 2:
                    print(f"❌ [PGPool] connection error: {type(e).__name__}: {e}", flush=True)
                    raise
                print(f"⚠️ [PGPool] 连接异常 → 重建连接池并重试一次: {type(e).__name__}: {e}", flush=True)
                await cls._rebuild_pool()
    return wrapper


class PGPool:
    """
    PostgreSQL asyncpg 连接池工具：
    - init_pool/ensure_pool/close/_rebuild_pool
    - execute/fetchrow/fetch/fetchval 等统一入口
    """
    _pool: Optional[asyncpg.Pool] = None
    _lock = asyncio.Lock()

    # 你可以用 env 或 config.py 注入
    DSN: str = ""

    # pool params
    MIN_SIZE: int = 2
    MAX_SIZE: int = 20
    COMMAND_TIMEOUT: float = 30.0

    @classmethod
    async def init_pool(cls, dsn: Optional[str] = None):
        if dsn:
            cls.DSN = dsn

        if cls._pool is not None:
            return cls._pool

        async with cls._lock:
            if cls._pool is None:
                if not cls.DSN:
                    raise RuntimeError("PGPool.DSN is empty. Please pass dsn to init_pool() or set PGPool.DSN")

                cls._pool = await asyncpg.create_pool(
                    dsn=cls.DSN,
                    min_size=cls.MIN_SIZE,
                    max_size=cls.MAX_SIZE,
                    command_timeout=cls.COMMAND_TIMEOUT,
                )
                print("✅ PostgreSQL 连接池初始化完成", flush=True)
        return cls._pool

    @classmethod
    async def ensure_pool(cls):
        if cls._pool is None:
            await cls.init_pool()
        return cls._pool

    @classmethod
    async def close(cls):
        async with cls._lock:
            if cls._pool:
                await cls._pool.close()
                cls._pool = None
                print("🛑 PostgreSQL 连接池已关闭", flush=True)

    @classmethod
    async def _rebuild_pool(cls):
        async with cls._lock:
            if cls._pool:
                try:
                    await cls._pool.close()
                except Exception as e:
                    print(f"⚠️ [PGPool] 关闭旧连接池出错: {e}", flush=True)
            cls._pool = None
            print("🔄 [PGPool] 正在重建 PostgreSQL 连接池…", flush=True)
            await cls.init_pool()

    # -------------------------
    # Unified SQL helpers
    # -------------------------

    @classmethod
    @reconnecting
    async def execute(cls, sql: str, *params, error_tag: str = "") -> str:
        """
        返回 asyncpg execute 的状态串，例如 'INSERT 0 1'
        """
        await cls.ensure_pool()
        try:
            async with cls._pool.acquire() as conn:
                return await conn.execute(sql, *params)
        except Exception as e:
            tag = error_tag or _caller_info()
            print(f"⚠️ [{tag}] SQL 执行出错: {e} | sql={sql} | params={params}", flush=True)
            raise

    @classmethod
    @reconnecting
    async def fetchrow(cls, sql: str, *params, error_tag: str = "") -> Optional[asyncpg.Record]:
        await cls.ensure_pool()
        try:
            async with cls._pool.acquire() as conn:
                return await conn.fetchrow(sql, *params)
        except Exception as e:
            tag = error_tag or _caller_info()
            print(f"⚠️ [{tag}] SQL fetchrow 出错: {e} | sql={sql} | params={params}", flush=True)
            raise

    @classmethod
    @reconnecting
    async def fetch(cls, sql: str, *params, error_tag: str = "") -> List[asyncpg.Record]:
        await cls.ensure_pool()
        try:
            async with cls._pool.acquire() as conn:
                return await conn.fetch(sql, *params)
        except Exception as e:
            tag = error_tag or _caller_info()
            print(f"⚠️ [{tag}] SQL fetch 出错: {e} | sql={sql} | params={params}", flush=True)
            raise

    @classmethod
    @reconnecting
    async def fetchval(cls, sql: str, *params, error_tag: str = "") -> Any:
        await cls.ensure_pool()
        try:
            async with cls._pool.acquire() as conn:
                return await conn.fetchval(sql, *params)
        except Exception as e:
            tag = error_tag or _caller_info()
            print(f"⚠️ [{tag}] SQL fetchval 出错: {e} | sql={sql} | params={params}", flush=True)
            raise

    # -------------------------
    # Transaction helper
    # -------------------------

    @classmethod
    async def in_tx(cls, fn, *args, **kwargs):
        """
        用法：
            async def work(conn): ...
            await PGPool.in_tx(work)
        """
        await cls.ensure_pool()
        async with cls._pool.acquire() as conn:
            async with conn.transaction():
                return await fn(conn, *args, **kwargs)
