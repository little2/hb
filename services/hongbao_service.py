from __future__ import annotations
from datetime import datetime
import aiomysql

from utils.db.tgone_mysql import MySQLPool


class HongbaoService:
    @staticmethod
    async def create_hongbao(sender_user_id: int, chat_id: int, total_amount: int, total_count: int, expire_at: datetime) -> int:
        await MySQLPool.execute(
            """
            INSERT INTO hongbao(sender_user_id, chat_id, total_amount, total_count, expire_at, status)
            VALUES(%s, %s, %s, %s, %s, 'active')
            """,
            (sender_user_id, chat_id, total_amount, total_count, expire_at),
        )
        row = await MySQLPool.fetchone("SELECT LAST_INSERT_ID() AS id")
        return int(row["id"])

    @staticmethod
    async def bind_message(hongbao_id: int, message_id: int) -> None:
        await MySQLPool.execute("UPDATE hongbao SET message_id=%s WHERE id=%s", (message_id, hongbao_id))

    @staticmethod
    async def redeem_add_points(hongbao_id: int, user_id: int, amount: int) -> tuple[bool, str]:
        async def _txn(cur):


            # 唯一键防重复领取
            await cur.execute(
                "INSERT INTO hongbao_redeem(hongbao_id, user_id, amount) VALUES(%s, %s, %s)",
                (hongbao_id, user_id, amount),
            )

            # # 确保 user 行存在（最小写入，不覆盖其他字段）
            # await cur.execute(
            #     """
            #     INSERT INTO `user` (user_id, active, point, create_time, update_time)
            #     VALUES (%s, 1, %s, NOW(), NOW())
            #     ON DUPLICATE KEY UPDATE point = point + %s,update_time = NOW()
            #     """,
            #     (user_id, amount, amount),
            # )

            

        try:
            await MySQLPool.transaction(_txn)
            return True, "ok"
        except aiomysql.IntegrityError:
            return False, "already_redeemed"
        except Exception as e:
            return False, f"db_error:{e!s}"
