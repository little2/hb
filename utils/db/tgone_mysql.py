import aiomysql
import time
from config import (
    MYSQL_HOST, MYSQL_DB_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB, MYSQL_UNIX_SOCKET
)
from typing import Optional, Dict, Any, List
from .lz_memory_cache import MemoryCache
import asyncio
from functools import wraps
from inspect import stack

DBError = aiomysql.Error
DBIntegrityError = aiomysql.IntegrityError
DBOperationalError = aiomysql.OperationalError

def _caller_info():
    frames = stack()
    if len(frames) > 2:
        frame = frames[2]
        return f"{frame.filename.split('/')[-1]}:{frame.function}:{frame.lineno}"
    return "unknown"


def reconnecting(func):
    """
    通用断线重连装饰器：
    - 只针对 aiomysql.OperationalError
    - 若错误码为 2006 / 2013 → 认为是断线，重建连接池 + 自动重试一次
    - 第二次仍失败 / 其它错误 → 直接抛出
    """
    @wraps(func)
    async def wrapper(*args, **kwargs):
        cls = args[0] if args else None
        for attempt in (1, 2):
            try:
                return await func(*args, **kwargs)
            except aiomysql.OperationalError as e:
                code = e.args[0] if e.args else None
                msg = e.args[1] if len(e.args) > 1 else ""

                if not cls or code not in (2006, 2013) or attempt == 2:
                    print(f"❌ [MySQLPool] OperationalError {code}: {msg}", flush=True)
                    raise

                print(f"⚠️ [MySQLPool] 检测到断线 {code}: {msg} → 重建连接池并重试一次", flush=True)
                try:
                    await cls._rebuild_pool()
                except Exception as e2:
                    print(f"❌ [MySQLPool] 重建连接池失败: {e2}", flush=True)
                    raise
    return wrapper

# tgone_mysql.py

class MySQLPool:
    _pool = None
    _lock = asyncio.Lock()
    _cache_ready = False
    cache = None
    _closing = False  # ✅ 新增：标记正在 close/rebuild，避免 acquire 竞态
    _debug_mode = False
    _cfg: dict | None = None   # ✅ 保存“最终生效”的连接池配置（默认来自 env，可被外部覆盖）

    @classmethod
    def show_debug(cls,text):
        if cls._debug_mode:
            print(f"{text}", flush=True)

    # ===== 新增：默认配置 + 合并覆盖 =====
    @classmethod
    def _make_default_cfg(cls) -> dict:
        """
        从 config/env 生成默认 cfg（保持你原有行为）
        """
        cfg = dict(
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            db=MYSQL_DB,
            charset="utf8mb4",
            autocommit=True,
            minsize=2,
            maxsize=32,
            pool_recycle=1800,
            connect_timeout=10,
        )
        if MYSQL_UNIX_SOCKET:
            cfg["unix_socket"] = MYSQL_UNIX_SOCKET
        else:
            cfg["host"] = MYSQL_HOST
            cfg["port"] = MYSQL_DB_PORT
        return cfg


    @classmethod
    def _merge_cfg(cls, overrides: dict | None) -> dict:
        """
        合并默认 cfg + 外部 overrides
        - overrides=None: 纯默认（兼容旧用法）
        - overrides 有值：覆盖默认值
        """
        cfg = cls._make_default_cfg()

        if overrides:
            # 允许用 None 显式清空某些字段（如 unix_socket）
            for k, v in overrides.items():
                if v is None:
                    cfg.pop(k, None)
                else:
                    cfg[k] = v

        # 互斥修正：socket 与 host/port 只能二选一
        if cfg.get("unix_socket"):
            cfg.pop("host", None)
            cfg.pop("port", None)
        else:
            cfg.pop("unix_socket", None)
            # 若外部没给 host/port，确保有默认
            cfg.setdefault("host", MYSQL_HOST)
            cfg.setdefault("port", MYSQL_DB_PORT)

        return cfg


    @classmethod
    def _cfg_fingerprint(cls, cfg: dict) -> tuple:
        """
        用于判断“配置是否变化”
        只抓影响连接池行为的关键字段（避免字典顺序问题）
        """
        keys = (
            "host", "port", "unix_socket",
            "user", "password", "db",
            "charset", "autocommit",
            "minsize", "maxsize",
            "pool_recycle", "connect_timeout",
        )
        return tuple((k, cfg.get(k)) for k in keys)


    # ===== 改造：init_pool 支持 **kwargs 注入 + 变更自动重建 =====
    @classmethod
    async def init_pool(cls, **overrides):
        """
        ✅ 新增：支持外部注入配置（覆盖 env/config）
        - 不传参数：保持旧行为（读 env/config）
        - 传参数：覆盖默认 cfg
        - 若 pool 已存在且 cfg 变化：自动重建，确保新配置生效
        """
        new_cfg = cls._merge_cfg(overrides if overrides else None)

        # 锁外快路径：pool 可用且 cfg 没变
        if cls._pool_usable() and cls._cfg and cls._cfg_fingerprint(cls._cfg) == cls._cfg_fingerprint(new_cfg):
            return cls._pool

        async with cls._lock:
            # 锁内二次检查
            if cls._pool_usable() and cls._cfg and cls._cfg_fingerprint(cls._cfg) == cls._cfg_fingerprint(new_cfg):
                return cls._pool

            # cfg 变化但 pool 已存在 → rebuild
            if cls._pool_usable() and cls._cfg and cls._cfg_fingerprint(cls._cfg) != cls._cfg_fingerprint(new_cfg):
                cls.show_debug("🔁 [MySQLPool] cfg changed → rebuild pool")
                await cls._rebuild_pool(new_cfg)
                return cls._pool

            # pool 不可用/不存在 → init
            cls._cfg = new_cfg
            return await cls._init_pool_locked(new_cfg)


    # ===== 改造：_init_pool_locked 接受 cfg（不再直接读 env）=====
    @classmethod
    async def _init_pool_locked(cls, cfg: dict | None = None):
        # 若 pool 对象存在但不可用，强制置空重建
        if cls._pool is not None and not cls._pool_usable():
            cls._pool = None

        if cfg is None:
            cfg = cls._cfg or cls._merge_cfg(None)

        if cls._pool is None:
            cls._cfg = cfg
            cls._pool = await aiomysql.create_pool(**cfg)
            cls.show_debug(
                f"🔄 MySQL 连接池已创建 (socket={bool(cfg.get('unix_socket'))})"
            )

        if not cls._cache_ready:
            cls.cache = MemoryCache()
            cls._cache_ready = True

        return cls._pool


    # ===== 改造：_rebuild_pool 支持 cfg 注入 =====
    @classmethod
    async def _rebuild_pool(cls, cfg: dict | None = None):
        async with cls._lock:
            cls._closing = True
            if cls._pool:
                try:
                    cls._pool.close()
                    await cls._pool.wait_closed()
                except Exception as e:
                    print(f"⚠️ [MySQLPool] 关闭旧连接池出错: {e}", flush=True)

            cls._pool = None
            cls._closing = False

            if cfg is None:
                cfg = cls._cfg or cls._merge_cfg(None)

            cls._cfg = cfg
            cls.show_debug("🔄 [MySQLPool] 正在重建 MySQL 连接池…")
            await cls._init_pool_locked(cfg)

    @classmethod
    async def ensure_pool(cls):
        if cls._pool_usable():
            cls.show_debug("【MySQLPool】连接池可用，直接返回。")
            return cls._pool

        cls.show_debug("【MySQLPool】连接池不可用，准备加锁重建...")
        async with cls._lock:
            cls.show_debug("【MySQLPool】锁内检查连接池状态...")
            if cls._pool_usable():
                cls.show_debug("【MySQLPool】连接池可用（锁内检查），直接返回。")
                return cls._pool

            cls._closing = False
            cls.show_debug("【MySQLPool】连接池不可用，正在初始化...")
            return await cls._init_pool_locked()
        
    @classmethod
    async def get_conn_cursor(cls):
        """
        ✅ 关键：acquire 前确保 pool 可用。
        这里不直接长时间持锁（避免吞吐下降），但要避免 acquire 与 close 交错。
        """
        cls.show_debug("【MySQLPool】获取连接池连接...")
        await cls.ensure_pool()
        cls.show_debug("【MySQLPool】连接池可用，正在 acquire 连接...")
        # acquire 仍可能在 close 刚发生时抛错 → 捕获并重建一次
        try:
            
            conn = await cls._pool.acquire()
            cls.show_debug("【MySQLPool】连接 acquire 成功。")
        except Exception as e:
            msg = str(e).lower()
            if "after closing pool" in msg or "closing pool" in msg:
                # 说明刚好撞上 close，重建并重试一次
                await cls._rebuild_pool()
                conn = await cls._pool.acquire()
            else:
                raise

        cursor = await conn.cursor(aiomysql.DictCursor)
        return conn, cursor

    @classmethod
    async def release(cls, conn, cursor):
        try:
            if cursor:
                await cursor.close()
        finally:
            if conn and cls._pool:
                cls._pool.release(conn)

    @classmethod
    async def close(cls):
        async with cls._lock:
            if cls._pool:
                cls._closing = True
                try:
                    cls._pool.close()
                    await cls._pool.wait_closed()
                finally:
                    cls._pool = None
                    cls._closing = False
                cls.show_debug("🛑 MySQL 连接池已关闭")

    

    @classmethod
    def _pool_usable(cls) -> bool:
        """
        判断连接池是否可用：
        - _pool 为空不可用
        - 正在 closing 不可用
        - aiomysql pool 处于 closed/closing 不可用（兼容不同版本属性）
        """
        p = cls._pool
        if p is None:
            return False
        if cls._closing:
            return False

        # aiomysql pool 通常有 closed/closing 或 _closed/_closing
        if getattr(p, "closed", False):
            return False
        if getattr(p, "closing", False):
            return False
        if getattr(p, "_closed", False):
            return False
        if getattr(p, "_closing", False):
            return False

        return True

    # ==================================================
    #   ✨ 统一 SQL helper：execute / fetchone / fetchall
    # ==================================================

    @classmethod
    @reconnecting
    async def execute(cls, sql: str, params=None, error_tag: str = "", raise_on_error: bool = False) -> bool:
        conn, cur = await cls.get_conn_cursor()
        try:
            await cur.execute(sql, params or ())
            return True
        except Exception as e:
            if error_tag:
                tag = error_tag
            else:
                tag = _caller_info()   # 自动提取调用来源
            
            print(
                f"⚠️ [{tag}] SQL 执行出错 execute: {e} | \nsql={sql} | \nparams={params}",
                flush=True,
            )
            if raise_on_error:
                raise
            return False
        finally:
            await cls.release(conn, cur)

    @classmethod
    @reconnecting
    async def fetchone(cls, sql: str, params=None, error_tag: str = "") -> Optional[Dict[str, Any]]:
        
        conn, cur = await cls.get_conn_cursor()
        try:
            await cur.execute(sql, params or ())
            return await cur.fetchone()
        except Exception as e:
            print(f"{e}", flush=True)
            if error_tag:
                tag = error_tag
            else:
                tag = _caller_info()   # 自动提取调用来源
            
            print(
                f"⚠️ [{tag}] SQL 执行出错fetchone: {e} | sql={sql} | params={params}",
                flush=True,
            )
            return None
        finally:
            await cls.release(conn, cur)

    @classmethod
    @reconnecting
    async def fetchall(cls, sql: str, params=None, error_tag: str = "") -> List[Dict[str, Any]]:
        conn, cur = await cls.get_conn_cursor()
        try:
            await cur.execute(sql, params or ())
            return await cur.fetchall()
        except Exception as e:
            if error_tag:
                tag = error_tag
            else:
                tag = _caller_info()   # 自动提取调用来源
            
            print(
                f"⚠️ [{tag}] SQL 执行出错 fetchall: {e} | sql={sql} | params={params}",
                flush=True,
            )
            return []
        finally:
            await cls.release(conn, cur)

    @classmethod
    async def transaction(cls, fn):
        """
        通用事务执行器（与现有 _pool / release / DictCursor 对齐）
        fn: async def fn(cur): ...  # cur 为 DictCursor
        """
        await cls.ensure_pool()

        conn = None
        cur = None
        try:
            conn = await cls._pool.acquire()
            cur = await conn.cursor(aiomysql.DictCursor)

            await conn.begin()
            result = await fn(cur)
            await conn.commit()
            return result
        except Exception:
            if conn:
                await conn.rollback()
            raise
        finally:
            # 复用你已有的 release 逻辑
            if conn and cur:
                await cls.release(conn, cur)
            elif conn and cls._pool:
                cls._pool.release(conn)



''''''
