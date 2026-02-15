from __future__ import annotations
from datetime import datetime
import aiomysql

from utils.db.tgone_mysql import MySQLPool


class HongbaoService:
    @staticmethod
    async def create_hongbao(sender_user_id: int, chat_id: int, total_amount: int, total_count: int, expire_at: datetime, skin: dict | None = None) -> int:

        # {
        #     "hb_key": "sz",
        #     "file_id_cover": "AgACAgEAAxkBAAMPaX8pAgJj9Am5lLwhmpt-IjS15TsAAjkMaxuUU_hHeBD_jjC7RMYBAAMCAAN5AAM4BA",
        #     "file_id_dm":"AgACAgEAAxkBAAMYaX8pli19IbAcSImVz5A9khJR4fQAAo-vMRsB4QlFLQIQ_2kV6iEBAAMCAAN3AAM4BA",
        #     "intro_text": "龙嵬成熟后，虽是正太模样，但精液的生产效率极高，从里面慢慢充满、撑大，变得又硬又紧，碰一下都会疼，不处理的话会越来越难受，需要各位好心撸夫帮忙榨精。",
        #     "dm_text": "谢谢撸夫哥哥帮忙，龙嵬已经舒服多了！\n\n可以点下面的按钮逛逛鲁仔喔",
        #     "activity_link": "https://t.me/luzai03bot?start=rank",
        # },
        hb_key = skin["hb_key"] if skin else None
        file_id_cover = skin["file_id_cover"] if skin else None
        file_id_dm = skin["file_id_dm"] if skin else None
        intro_text = skin["intro_text"] if skin else None
        dm_text = skin["dm_text"] if skin else None
        activity_link = skin["activity_link"] if skin else None


        await MySQLPool.execute(
            """
            INSERT INTO hongbao(sender_user_id, chat_id, total_amount, total_count, expire_at, status, hb_key, file_id_cover, file_id_dm, intro_text, dm_text, activity_link)
            VALUES(%s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s)
            """,
            (sender_user_id, chat_id, total_amount, total_count, expire_at, hb_key, file_id_cover, file_id_dm, intro_text, dm_text, activity_link),
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
            stat_date = datetime.now().strftime("%Y-%m-%d")
            timestamp = int(datetime.now().timestamp())
            await cur.execute(
                """
                INSERT INTO `contribute_today` (user_id, stat_date, update_timestamp, drangon)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE drangon = drangon + %s, update_timestamp = %s
                """,
                (user_id, stat_date, timestamp, amount, amount, timestamp),
            )           

        try:
            await MySQLPool.transaction(_txn)
            return True, "ok"
        except aiomysql.IntegrityError:
            return False, "already_redeemed"
        except Exception as e:
            return False, f"db_error:{e!s}"

    @staticmethod
    async def get_hongbao(hongbao_id: int) -> dict | None:
        row = await MySQLPool.fetchone(
            "SELECT * FROM hongbao WHERE id=%s",
            (hongbao_id,)
        )
        return row  # 返回 dict 或 None

    @staticmethod
    async def get_contribute_today(user_id: int, stat_date: str | None = None) -> dict | None:
        stat_date = stat_date or datetime.now().strftime("%Y-%m-%d")
        row = await MySQLPool.fetchone(
            "SELECT * FROM contribute_today WHERE user_id=%s AND stat_date=%s",
            (user_id, stat_date)
        )
        return row  # 返回 dict 或 None

''''''