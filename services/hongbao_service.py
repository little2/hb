from __future__ import annotations
from datetime import datetime
import time
import aiomysql
from utils.db.tgone_mysql import MySQLPool

class HongbaoService:
    @staticmethod
    async def _repair_hb_credit_after_duplicate(hongbao_id: int, user_id: int, amount: int, skin: dict | None = None) -> bool:
        """
        兜底补账：当 hongbao_redeem 已存在（重复键）但积分流水缺失时，补写 transaction + user.point。
        目的：避免“显示已领取但实际未到账”。
        """
        async def _txn(cur):
            tx_desc = str((skin or {}).get("id") or hongbao_id)

            await cur.execute(
                """
                SELECT transaction_id
                FROM `transaction`
                WHERE receiver_id=%s
                  AND transaction_type=%s
                  AND transaction_description=%s
                LIMIT 1
                FOR UPDATE
                """,
                (user_id, "hongbao", tx_desc),
            )
            exists = await cur.fetchone()
            if exists:
                return False

            timestamp = int(datetime.now().timestamp())
            sender_id = (skin or {}).get("sender_user_id") or (skin or {}).get("sender_id") or 666666
            memo = (skin or {}).get("hb_key") or f"hb:{hongbao_id}"
            sender_fee = -abs(int(amount or 0))
            receiver_fee = abs(int(amount or 0))

            await cur.execute(
                """
                INSERT INTO `transaction` (sender_id, sender_fee, receiver_id, receiver_fee, transaction_type, transaction_description, transaction_timestamp, memo)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (sender_id, sender_fee, user_id, receiver_fee, "hongbao", tx_desc, timestamp, memo),
            )

            await cur.execute(
                """
                INSERT INTO `user` (user_id, active, point, create_time, update_time)
                VALUES (%s, 1, %s, NOW(), NOW())
                ON DUPLICATE KEY UPDATE point = point + %s,update_time = NOW()
                """,
                (user_id, receiver_fee, receiver_fee),
            )
            return True

        try:
            return bool(await MySQLPool.transaction(_txn))
        except Exception as e:
            print(
                f"[HB_REDEEM_REPAIR] failed: hongbao_id={hongbao_id} user_id={user_id} amount={amount} err={e}",
                flush=True,
            )
            return False

    @staticmethod
    async def create_hongbao(sender_user_id: int, chat_id: int, total_amount: int, total_count: int, expire_at: datetime, skin: dict | None = None, hb_type: str = "lj") -> int:

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
        hb_type = hb_type or "lj"


        await MySQLPool.execute(
            """
            INSERT INTO hongbao(sender_user_id, chat_id, total_amount, total_count, expire_at, status, hb_key, hb_type, file_id_cover, file_id_dm, intro_text, dm_text, activity_link)
            VALUES(%s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s)
            """,
            (sender_user_id, chat_id, total_amount, total_count, expire_at, hb_key, hb_type, file_id_cover, file_id_dm, intro_text, dm_text, activity_link),
        )
        row = await MySQLPool.fetchone("SELECT LAST_INSERT_ID() AS id")
        return int(row["id"])

    @staticmethod
    async def bind_message(hongbao_id: int, message_id: int) -> None:
        await MySQLPool.execute("UPDATE hongbao SET message_id=%s WHERE id=%s", (message_id, hongbao_id))

    @staticmethod
    async def redeem_add_points(hongbao_id: int, user_id: int, amount: int, skin: dict | None = None) -> tuple[bool, str]:
        async def _txn(cur):
            # 唯一键防重复领取
            await cur.execute(
                "INSERT INTO hongbao_redeem(hongbao_id, user_id, amount) VALUES(%s, %s, %s)",
                (hongbao_id, user_id, amount),
            )


            stat_date = datetime.now().strftime("%Y-%m-%d")
            timestamp = int(datetime.now().timestamp())
            hb_type = skin.get("hb_type") if skin else None
            if hb_type=="hb":
                print(f"⚡ 积分红包，直接写入 transaction 表，user_id={user_id}, amount={amount}")
                sender_id = skin.get("sender_user_id") or skin.get("sender_id") or 666666
                transaction_description = skin.get("id") or 0
                memo = skin.get("hb_key") or ""

                sender_fee = -abs(amount)
                receiver_fee = abs(amount)

                await cur.execute(
                    """
                    INSERT INTO `transaction` (sender_id, sender_fee, receiver_id, receiver_fee, transaction_type, transaction_description, transaction_timestamp, memo)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (sender_id, sender_fee, user_id, receiver_fee, 'hongbao', transaction_description, timestamp, memo),
                ) 

                await cur.execute(
                    """
                    INSERT INTO `user` (user_id, active, point, create_time, update_time)
                    VALUES (%s, 1, %s, NOW(), NOW())
                    ON DUPLICATE KEY UPDATE point = point + %s,update_time = NOW()
                    """,
                    (user_id, receiver_fee, receiver_fee),
                )

                # # 再查询一次 user 的积分 point, 并显示出来
                # await cur.execute(
                #     "SELECT point FROM `user` WHERE user_id=%s",
                #     (user_id,)
                # )
                # user_row = await cur.fetchone()
                # current_point = user_row["point"] if user_row else 0
                # print(f"⚡ 用户 {user_id} 当前积分: {current_point}")

            else:    
                print(f"⚡ 龙精红包，写入 contribute_today 表，user_id={user_id}, amount={amount}, stat_date={stat_date}")
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
            # 兜底：极端情况下可能出现“hongbao_redeem 已写入，但积分流水/余额未落库”
            # 此处仅对 hb（积分红包）尝试补账，避免误二次入账。
            hb_type = (skin or {}).get("hb_type") if skin else None
            if hb_type == "hb":
                repaired = await HongbaoService._repair_hb_credit_after_duplicate(
                    hongbao_id=hongbao_id,
                    user_id=user_id,
                    amount=amount,
                    skin=skin,
                )
                if repaired:
                    return True, "ok_repaired"

            return False, "already_redeemed"
        except Exception as e:
            return False, f"db_error:{e!s}"

    @staticmethod
    async def get_hongbao(hongbao_id: int) -> dict | None:
        key = f"hongbao:{hongbao_id}"
        row = MySQLPool.cache.get(key)
        if row is None:
            row = await MySQLPool.fetchone(
                "SELECT * FROM hongbao WHERE id=%s",
                (hongbao_id,)
            )
            MySQLPool.cache.set(key, row, ttl=300)  # 缓存 5 分钟
        return row  # 返回 dict 或 None

    @staticmethod
    async def get_contribute_today(user_id: int, stat_date: str | None = None) -> dict | None:
        stat_date = stat_date or datetime.now().strftime("%Y-%m-%d")
        row = await MySQLPool.fetchone(
            "SELECT * FROM contribute_today WHERE user_id=%s AND stat_date=%s",
            (user_id, stat_date)
        )
        return row  # 返回 dict 或 None


    @staticmethod
    async def get_user_collection(id: int) -> dict | None:
        row = await MySQLPool.fetchone(
            "SELECT * FROM user_collection WHERE id=%s",
            (id,)
        )
        return row  # 返回 dict 或 None

    @staticmethod
    async def get_cutedd(cutedd_id: int, bot_name: str) -> dict | None:
        row = await MySQLPool.fetchone(
            """
            SELECT
                c.board_message_thread_id,
                c.board_chat_id,
                c.board_message_id,
                c.file_caption,
                fe.file_type AS file_type,
                fe.file_id AS file_id
            FROM cutedd c
            LEFT JOIN file_extension fe
                ON fe.file_unique_id = c.file_unique_id
               AND fe.bot = %s
            WHERE c.cutedd_id = %s
            ORDER BY fe.id DESC
            LIMIT 1
            """,
            (bot_name, cutedd_id),
        )
        return row

    @staticmethod
    async def list_call_cutedd(bot_name: str, act_id: str = "20000008") -> list[dict]:
        rows = await MySQLPool.fetchall(
            """
            SELECT
                c.cutedd_id,
                c.dd_thread_id,
                c.file_unique_id,
                COALESCE(e.file_id, '') AS file_id,
                c.file_type,
                c.file_caption,
                c.send_status
            FROM cutedd c
            LEFT JOIN file_extension e
                ON c.file_unique_id = e.file_unique_id
               AND e.bot = %s
            WHERE c.send_status = 1
              AND c.act_id = %s
            ORDER BY c.dd_thread_id, c.file_caption ASC
            """,
            (bot_name, act_id),
        )
        return rows or []

    @staticmethod
    async def upsert_file_extension(
        file_type: str,
        file_unique_id: str,
        file_id: str,
        bot: str,
        user_id: int | None,
    ) -> None:
        await MySQLPool.execute(
            """
            INSERT INTO file_extension(file_type, file_unique_id, file_id, bot, user_id, create_time)
            VALUES(%s, %s, %s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
                file_type = VALUES(file_type),
                file_unique_id = VALUES(file_unique_id),
                user_id = VALUES(user_id),
                create_time = VALUES(create_time)
            """,
            (file_type, file_unique_id, file_id, bot, user_id),
        )

    @staticmethod
    async def upsert_hongbao_user_setting(
        user_id: int,
        cover_type: str,
        cover_file_id: str,
        cover_file_unique_id: str,
        bias: int | None = None,
    ) -> bool:
        async def _txn(cur):
            await cur.execute(
                """
                UPDATE hongbao_user_setting
                SET cover_type=%s, cover_file_id=%s, cover_file_unique_id=%s, bias=COALESCE(%s, bias)
                WHERE user_id=%s
                """,
                (cover_type, cover_file_id, cover_file_unique_id, bias, user_id),
            )

            if cur.rowcount == 0:
                await cur.execute(
                    """
                    INSERT INTO hongbao_user_setting(user_id, cover_type, cover_file_id, cover_file_unique_id, bias)
                    VALUES(%s, %s, %s, %s, %s)
                    """,
                    (user_id, cover_type, cover_file_id, cover_file_unique_id, bias),
                )

            return True

        try:
            return bool(await MySQLPool.transaction(_txn))
        except Exception:
            return False

    @staticmethod
    async def get_hongbao_user_setting(user_id: int) -> dict | None:
        row = await MySQLPool.fetchone(
            "SELECT * FROM hongbao_user_setting WHERE user_id=%s LIMIT 1",
            (user_id,)
        )
        return row

    @staticmethod
    async def get_file_type_by_file_id(file_id: str, bot: str | None = None) -> str:
        if not file_id:
            return ""

        params = [file_id]
        sql = """
            SELECT file_type
            FROM file_extension
            WHERE file_id=%s
        """
        if bot:
            sql += " AND bot=%s"
            params.append(bot)
        sql += " ORDER BY id DESC LIMIT 1"

        row = await MySQLPool.fetchone(sql, tuple(params))
        return (row or {}).get("file_type") or ""

    @classmethod
    async def in_block_list(cls, user_id: int) -> bool:
        row = await MySQLPool.fetchone(
            "SELECT 1 FROM block_list WHERE user_id=%s LIMIT 1",
            (user_id,)
        )
        return bool(row)

    @classmethod
    async def transaction_log(cls, transaction_data: dict):
        user_info_row = None

        # ---------- 1) 入参校验 ----------
        if not isinstance(transaction_data, dict):
            return {"ok": "", "status": "bad_params", "transaction_data": transaction_data}

        desc = (transaction_data.get("transaction_description") or "").strip()
        if not desc:
            return {"ok": "", "status": "no_description", "transaction_data": transaction_data}

        tx_type = (transaction_data.get("transaction_type") or "").strip()
        if not tx_type:
            return {"ok": "", "status": "no_type", "transaction_data": transaction_data}

        sender_id = transaction_data.get("sender_id", "")
        receiver_id = transaction_data.get("receiver_id", "")

        # 统一 fee
        try:
            sender_fee = int(transaction_data.get("sender_fee") or 0)
        except Exception:
            sender_fee = 0

        try:
            receiver_fee = int(transaction_data.get("receiver_fee") or 0)
        except Exception:
            receiver_fee = 0

        # 统一语义
        if sender_fee > 0:
            sender_fee = -abs(sender_fee)
        if receiver_fee < 0:
            receiver_fee = abs(receiver_fee)

        transaction_data["transaction_type"] = tx_type
        transaction_data["transaction_description"] = desc
        transaction_data["sender_fee"] = sender_fee
        transaction_data["receiver_fee"] = receiver_fee

        if sender_id and sender_id == receiver_id:
            return {"ok": "", "status": "reward_self", "transaction_data": transaction_data}

        # ---------- 2) 事务逻辑 ----------
        async def _txn(cur):
            nonlocal user_info_row

            # 2.1 幂等查重
            where = []
            params = []

            if sender_id:
                where.append("sender_id = %s")
                params.append(sender_id)

            if receiver_id:
                where.append("receiver_id = %s")
                params.append(receiver_id)

            where.append("transaction_type = %s")
            params.append(tx_type)

            where.append("transaction_description = %s")
            params.append(desc)

            where_sql = " AND ".join(where)

            await cur.execute(
                f"""
                SELECT transaction_id
                FROM transaction
                WHERE {where_sql}
                LIMIT 1
                FOR UPDATE
                """,
                tuple(params),
            )

            exist = await cur.fetchone()
            if exist:
                return {"ok": "1", "status": "exist", "transaction_data": exist}

            # 2.2 扣 sender
            if sender_id and sender_fee != 0:
                await cur.execute(
                    "SELECT point, credit FROM user WHERE user_id=%s LIMIT 1 FOR UPDATE",
                    (sender_id,),
                )
                user_info_row = await cur.fetchone()

                need = abs(sender_fee)
                if (not user_info_row) or int(user_info_row.get("point") or 0) < need:
                    return {
                        "ok": "",
                        "status": "insufficient_funds",
                        "transaction_data": transaction_data,
                        "user_info": user_info_row,
                    }

                await cur.execute(
                    "UPDATE user SET point = point + %s WHERE user_id = %s",
                    (sender_fee, sender_id),
                )

                await cur.execute(
                    "SELECT point, credit FROM user WHERE user_id=%s LIMIT 1",
                    (sender_id,),
                )
                user_info_row = await cur.fetchone()

            # 2.3 入账 receiver
            if receiver_id and receiver_fee != 0:
                if not await cls.in_block_list(receiver_id):
                    await cur.execute(
                        "UPDATE user SET point = point + %s WHERE user_id = %s",
                        (receiver_fee, receiver_id),
                    )

            # 2.4 写 transaction
            transaction_data["transaction_timestamp"] = int(time.time())

            columns = ", ".join(transaction_data.keys())
            placeholders = ", ".join(["%s"] * len(transaction_data))
            values = list(transaction_data.values())

            await cur.execute(
                f"""
                INSERT INTO transaction ({columns})
                VALUES ({placeholders})
                """,
                values,
            )

            transaction_data["transaction_id"] = cur.lastrowid

            return {
                "ok": "1",
                "status": "insert",
                "transaction_data": transaction_data,
                "user_info": user_info_row,
            }

        # ---------- 3) 执行事务 ----------
        try:
            return await MySQLPool.transaction(_txn)
        except aiomysql.IntegrityError as e:
            return {"ok": "", "status": "integrity_error", "error": str(e)}
        except Exception as e:
            return {"ok": "", "status": "error", "error": str(e)}