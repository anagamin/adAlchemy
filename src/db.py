import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

# Константы для log.desc (единый стиль для аналитики)
LOG_START = "вход в бот"
LOG_AD_TYPE = "выбор типа объявления"
LOG_ORDER = "заказ"
LOG_ORDER_DONE = "заказ выполнен"

INITIAL_BALANCE_RUB = 500

import aiomysql

from .config import settings
from .models import AdVariant, CampaignDraft

logger = logging.getLogger(__name__)

_pool: Optional[aiomysql.Pool] = None


def _truncate(value: str | None, max_length: int) -> Optional[str]:
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    return trimmed[:max_length]


def _bool_to_int(flag: bool | None) -> Optional[int]:
    if flag is None:
        return None
    return 1 if flag else 0


async def init_pool_if_needed() -> None:
    global _pool
    if _pool is not None:
        return
    if not settings.mysql_host or not settings.mysql_database:
        logger.warning("MySQL not configured (MYSQL_HOST/MYSQL_DATABASE). DB features disabled.")
        return
    _pool = await aiomysql.create_pool(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        db=settings.mysql_database,
        charset="utf8mb4",
        autocommit=True,
        minsize=1,
        maxsize=5,
    )
    logger.info("MySQL pool created")


async def init_pool() -> aiomysql.Pool:
    await init_pool_if_needed()
    return _get_pool()


async def close_pool() -> None:
    global _pool
    if _pool:
        _pool.close()
        await _pool.wait_closed()
        _pool = None
        logger.info("MySQL pool closed")


def _get_pool() -> aiomysql.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized. Call init_pool_if_needed() first.")
    return _pool


def _pool_ready() -> bool:
    return _pool is not None


@asynccontextmanager
async def get_conn():
    pool = _get_pool()
    async with pool.acquire() as conn:
        yield conn


async def ensure_user(
    telegram_id: int,
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    username: str | None = None,
    language_code: str | None = None,
    is_bot: bool | None = None,
    is_premium: bool | None = None,
) -> Optional[int]:
    await init_pool_if_needed()
    if not _pool_ready():
        return None
    async with get_conn() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT
                    id,
                    first_name,
                    last_name,
                    username,
                    language_code,
                    is_bot,
                    is_premium
                FROM users WHERE telegram_id = %s""",
                (telegram_id,),
            )
            row = await cur.fetchone()
            if row:
                user_id = int(row["id"])
                updates: dict[str, Any] = {}
                db_first = row.get("first_name")
                db_last = row.get("last_name")
                db_username = row.get("username")
                db_lang = row.get("language_code")
                db_is_bot = row.get("is_bot")
                db_is_premium = row.get("is_premium")

                new_first = _truncate(first_name, 255)
                new_last = _truncate(last_name, 255)
                new_username = _truncate(username, 255)
                new_lang = _truncate(language_code, 16)
                new_is_bot = _bool_to_int(is_bot)
                new_is_premium = _bool_to_int(is_premium)

                if new_first is not None and new_first != db_first:
                    updates["first_name"] = new_first
                if new_last is not None and new_last != db_last:
                    updates["last_name"] = new_last
                if new_username is not None and new_username != db_username:
                    updates["username"] = new_username
                if new_lang is not None and new_lang != db_lang:
                    updates["language_code"] = new_lang
                if new_is_bot is not None and new_is_bot != (db_is_bot if db_is_bot is None else int(db_is_bot)):
                    updates["is_bot"] = new_is_bot
                if new_is_premium is not None and new_is_premium != (
                    db_is_premium if db_is_premium is None else int(db_is_premium)
                ):
                    updates["is_premium"] = new_is_premium

                if updates:
                    set_clause = ", ".join(f"{col} = %s" for col in updates.keys())
                    params = list(updates.values()) + [user_id]
                    await cur.execute(f"UPDATE users SET {set_clause} WHERE id = %s", params)
                return user_id
            await cur.execute(
                """INSERT INTO users (
                    telegram_id,
                    balance,
                    first_name,
                    last_name,
                    username,
                    language_code,
                    is_bot,
                    is_premium
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    telegram_id,
                    INITIAL_BALANCE_RUB,
                    _truncate(first_name, 255),
                    _truncate(last_name, 255),
                    _truncate(username, 255),
                    _truncate(language_code, 16),
                    _bool_to_int(is_bot),
                    _bool_to_int(is_premium),
                ),
            )
            return cur.lastrowid


async def get_user_id_by_telegram(telegram_id: int) -> Optional[int]:
    await init_pool_if_needed()
    if not _pool_ready():
        return None
    async with get_conn() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id FROM users WHERE telegram_id = %s",
                (telegram_id,),
            )
            row = await cur.fetchone()
            return int(row["id"]) if row else None


async def create_request(user_id: int, link: str, desc: Optional[str] = None) -> Optional[int]:
    if not _pool_ready():
        return None
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO requests (user_id, link, `desc`) VALUES (%s, %s, %s)",
                (user_id, link[:512], desc),
            )
            return cur.lastrowid


async def create_results(request_id: int, draft: CampaignDraft) -> None:
    if not _pool_ready():
        return
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            for ad in draft.ads:
                result_data: Optional[str] = None
                if draft.keywords or draft.analysis_result:
                    data: dict[str, Any] = {}
                    if draft.keywords:
                        data["keywords"] = draft.keywords
                    if draft.analysis_result:
                        data["analysis_result"] = draft.analysis_result
                    result_data = json.dumps(data, ensure_ascii=False)
                await cur.execute(
                    """INSERT INTO results (
                        request_id, pic, segment_name, headline, body_text,
                        cta, visual_concept, image_prompt_short, image_prompt, result_data
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        request_id,
                        ad.image_path[:1024] if ad.image_path else None,
                        (ad.segment_name or "")[:255],
                        (ad.headline or "")[:512],
                        ad.body_text,
                        (ad.cta or "")[:255],
                        ad.visual_concept,
                        (ad.image_prompt_short or "")[:512],
                        ad.image_prompt,
                        result_data,
                    ),
                )


async def log_action(user_id: int, desc: str) -> None:
    await init_pool_if_needed()
    if not _pool_ready():
        return
    if not desc or len(desc) > 512:
        desc = desc[:512] if desc else "action"
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO log (user_id, `desc`) VALUES (%s, %s)",
                (user_id, desc),
            )


async def get_user_balance(telegram_id: int) -> Optional[Any]:
    await init_pool_if_needed()
    if not _pool_ready():
        return None
    async with get_conn() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT balance FROM users WHERE telegram_id = %s",
                (telegram_id,),
            )
            row = await cur.fetchone()
            return row["balance"] if row else None


async def add_balance(telegram_id: int, amount: float) -> bool:
    await init_pool_if_needed()
    if not _pool_ready() or amount <= 0:
        return False
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE users SET balance = balance + %s WHERE telegram_id = %s",
                (amount, telegram_id),
            )
            return cur.rowcount > 0


async def deduct_balance(telegram_id: int, amount: float) -> bool:
    """Atomically deduct amount from user balance. Returns True if deduction succeeded (balance was >= amount)."""
    await init_pool_if_needed()
    if not _pool_ready() or amount <= 0:
        return False
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE users SET balance = balance - %s WHERE telegram_id = %s AND balance >= %s",
                (amount, telegram_id, amount),
            )
            return cur.rowcount > 0


async def create_payment_record(
    yookassa_payment_id: str,
    user_id: int,
    telegram_id: int,
    amount_rub: float,
) -> bool:
    await init_pool_if_needed()
    if not _pool_ready():
        return False
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO payments (yookassa_payment_id, user_id, telegram_id, amount_rub, status)
                   VALUES (%s, %s, %s, %s, 'pending')""",
                (yookassa_payment_id, user_id, telegram_id, amount_rub),
            )
            return True


async def get_payment_by_yookassa_id(yookassa_payment_id: str) -> Optional[dict[str, Any]]:
    await init_pool_if_needed()
    if not _pool_ready():
        return None
    async with get_conn() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, user_id, telegram_id, amount_rub, status FROM payments WHERE yookassa_payment_id = %s",
                (yookassa_payment_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def set_payment_succeeded(yookassa_payment_id: str) -> bool:
    await init_pool_if_needed()
    if not _pool_ready():
        return False
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE payments SET status = 'succeeded' WHERE yookassa_payment_id = %s AND status = 'pending'",
                (yookassa_payment_id,),
            )
            return cur.rowcount > 0


async def get_last_requests(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    await init_pool_if_needed()
    if not _pool_ready():
        return []
    async with get_conn() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT id, link, `desc`, created_at
                   FROM requests
                   WHERE user_id = %s
                   ORDER BY created_at DESC
                   LIMIT %s""",
                (user_id, limit),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_results_for_request(request_id: int) -> list[dict[str, Any]]:
    await init_pool_if_needed()
    if not _pool_ready():
        return []
    async with get_conn() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT id, pic, segment_name, headline, body_text, cta,
                          visual_concept, image_prompt_short, image_prompt, result_data
                   FROM results
                   WHERE request_id = %s
                   ORDER BY id ASC""",
                (request_id,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
