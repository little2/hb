from __future__ import annotations
import random
from typing import List, Optional, Tuple
import time
import redis.asyncio as redis


def k_list(hid: int) -> str: return f"hb:{hid}:list"
def k_u(hid: int, uid: int) -> str: return f"hb:{hid}:u:{uid}"
def k_st(hid: int, uid: int) -> str: return f"hb:{hid}:st:{uid}"
def k_dm_block(uid: int) -> str: return f"dm_block:{uid}"
def k_notice(hid: int, sec_bucket: int) -> str: return f"hb:{hid}:notice:{sec_bucket}"
def k_skin(hid: int) -> str: return f"hb:{hid}:skin"


def split_amounts(total_amount: int, total_count: int, min_unit: int) -> List[int]:
    if total_count <= 0:
        raise ValueError("total_count must be > 0")
    if total_amount < total_count * min_unit:
        raise ValueError("total_amount must be >= total_count * min_unit")

    base = [min_unit] * total_count
    rest = total_amount - total_count * min_unit
    if rest == 0:
        random.shuffle(base)
        return base

    cuts = sorted(random.randint(0, rest) for _ in range(total_count - 1))
    parts, prev = [], 0
    for c in cuts:
        parts.append(c - prev)
        prev = c
    parts.append(rest - prev)

    arr = [base[i] + parts[i] for i in range(total_count)]
    random.shuffle(arr)
    return arr


LUA_CLAIM = r"""
-- KEYS: list, u, st
-- ARGV[1] ttl_sec (0 => keep existing ttl)
local ttl = tonumber(ARGV[1])

local prev = redis.call("GET", KEYS[2])
if prev then
  return {1, tonumber(prev), 0} -- already
end

local v = redis.call("RPOP", KEYS[1])
if not v then
  if redis.call("EXISTS", KEYS[1]) == 0 then
    return {-2, 0, 0} -- expired/not found
  end
  return {-1, 0, 1} -- empty (already empty)
end

redis.call("SET", KEYS[2], v)
redis.call("SET", KEYS[3], "pending")

if ttl and ttl > 0 then
  redis.call("EXPIRE", KEYS[1], ttl)
  redis.call("EXPIRE", KEYS[2], ttl)
  redis.call("EXPIRE", KEYS[3], ttl)
end

-- ✅ 判断是否已被拿完：RPOP 后 list 剩余长度
local left = redis.call("LLEN", KEYS[1])
local is_empty = 0
if left <= 0 then
  is_empty = 1
end

return {0, tonumber(v), is_empty}
"""



LUA_REDEEM_PREP = r"""
-- KEYS: u, st
-- ARGV[1] claiming_ttl_sec
local lock_ttl = tonumber(ARGV[1]) or 30

local st = redis.call("GET", KEYS[2])
if not st then
  return {-2, 0} -- expired/no ticket
end

if st == "claimed" then
  local amt = redis.call("GET", KEYS[1])
  return {2, tonumber(amt or "0")}
end

if st == "claiming" then
  local amt = redis.call("GET", KEYS[1])
  return {3, tonumber(amt or "0")}
end

if st ~= "pending" then
  return {-3, 0}
end

local amt = redis.call("GET", KEYS[1])
if not amt then
  return {-2, 0}
end

redis.call("SET", KEYS[2], "claiming")
redis.call("EXPIRE", KEYS[2], lock_ttl)

return {0, tonumber(amt)}
"""


class RedisLayer:
    def __init__(self, rds: redis.Redis):
        self.rds = rds
        self.sha_claim: Optional[str] = None
        self.sha_redeem: Optional[str] = None

    async def load_scripts(self) -> None:
        self.sha_claim = await self.rds.script_load(LUA_CLAIM)
        self.sha_redeem = await self.rds.script_load(LUA_REDEEM_PREP)

    async def init_list(self, hid: int, amounts: List[int], ttl_sec: int) -> None:
        key = k_list(hid)
        pipe = self.rds.pipeline()
        for a in amounts:
            pipe.lpush(key, a)
        pipe.expire(key, max(1, ttl_sec))
        await pipe.execute()
        
    async def set_hb_skin(self, hid: int, skin_key: str, ttl_sec: int) -> None:
        """
        绑定红包 hid 的皮肤 key（例如 'classic_red'）
        - ttl_sec：建议与红包一致
        - 使用 NX：只在不存在时写入，避免重试/重复创建导致皮肤乱跳
        """
        if not skin_key:
            return

        ttl_sec = max(1, int(ttl_sec))
        key = k_skin(hid)

        # redis-py asyncio 支持 nx/ex
        await self.rds.set(key, skin_key, ex=ttl_sec, nx=True)

    async def get_hb_skin(self, hid: int) -> Optional[str]:
        """
        读取红包 hid 的皮肤 key；不存在返回 None
        """
        key = k_skin(hid)
        v = await self.rds.get(key)
        if not v:
            return None
        if isinstance(v, (bytes, bytearray)):
            return v.decode("utf-8", "ignore")
        return str(v)



    async def claim(self, hid: int, uid: int) -> Tuple[int, int, bool]:
        keys = [k_list(hid), k_u(hid, uid), k_st(hid, uid)]
        argv = ["0"]
        try:
            res = await self.rds.evalsha(self.sha_claim, len(keys), *keys, *argv)
        except redis.exceptions.NoScriptError:
            self.sha_claim = await self.rds.script_load(LUA_CLAIM)
            res = await self.rds.evalsha(self.sha_claim, len(keys), *keys, *argv)

        code = int(res[0])
        amount = int(res[1])
        is_empty = bool(int(res[2])) if len(res) >= 3 else False
        return code, amount, is_empty


    async def redeem_prep(self, hid: int, uid: int, claiming_ttl: int = 30) -> Tuple[int, int]:
        keys = [k_u(hid, uid), k_st(hid, uid)]
        argv = [str(claiming_ttl)]
        try:
            res = await self.rds.evalsha(self.sha_redeem, len(keys), *keys, *argv)
        except redis.exceptions.NoScriptError:
            self.sha_redeem = await self.rds.script_load(LUA_REDEEM_PREP)
            res = await self.rds.evalsha(self.sha_redeem, len(keys), *keys, *argv)
        return int(res[0]), int(res[1])

    async def set_claimed(self, hid: int, uid: int) -> None:
        await self.rds.set(k_st(hid, uid), "claimed", keepttl=True)

    async def rollback_pending(self, hid: int, uid: int) -> None:
        await self.rds.set(k_st(hid, uid), "pending", keepttl=True)

    async def should_skip_dm(self, uid: int) -> bool:
        return bool(await self.rds.exists(k_dm_block(uid)))

    async def set_dm_block(self, uid: int, ttl_sec: int) -> None:
        await self.rds.setex(k_dm_block(uid), ttl_sec, "1")

    async def allow_group_notice(self, hid: int, per_sec: int) -> bool:
        now = int(time.time())
        key = k_notice(hid, now)
        n = await self.rds.incr(key)
        if n == 1:
            await self.rds.expire(key, 2)
        return n <= per_sec
