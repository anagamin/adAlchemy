import logging
import uuid
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger(__name__)

YOOKASSA_API = "https://api.yookassa.ru/v3/payments"


def _yookassa_credentials() -> tuple[str, str] | None:
    shop_id = (settings.yookassa_shop_id or "").strip()
    secret_key = (settings.yookassa_secret_key or "").strip()
    if not shop_id or not secret_key:
        return None
    return shop_id, secret_key


async def create_payment(
    amount_rub: float,
    telegram_id: int,
    description: str = "Пополнение баланса AdAlechemy",
    customer_full_name: str | None = None,
) -> tuple[str | None, str | None]:
    """Создаёт платёж в YooKassa. Возвращает (confirmation_url, payment_id) или (None, None)."""
    creds = _yookassa_credentials()
    if not creds:
        logger.warning("YooKassa not configured (empty YOOKASSA_SHOP_ID or YOOKASSA_SECRET_KEY)")
        return None, None
    shop_id, secret_key = creds
    value_str = f"{amount_rub:.2f}"
    return_url = (settings.yookassa_return_url or "https://t.me").strip()
    payload: dict[str, Any] = {
        "amount": {"value": value_str, "currency": "RUB"},
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": return_url,
        },
        "description": description,
        "metadata": {"telegram_id": str(telegram_id)},
        "receipt": {
            "customer": {
                "full_name": (customer_full_name or "Покупатель").strip() or "Покупатель",
                "email": (settings.yookassa_receipt_email or "noreply@adalechemy.local").strip(),
            },
            "items": [
                {
                    "description": description[:128],
                    "quantity": 1.0,
                    "amount": {"value": value_str, "currency": "RUB"},
                    "vat_code": 1,
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }
            ],
            "internet": "true",
        },
    }
    headers = {
        "Idempotence-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
    auth = (shop_id, secret_key)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                YOOKASSA_API,
                json=payload,
                headers=headers,
                auth=auth,
            )
            body = resp.text
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        logger.warning(
            "YooKassa create payment HTTP %s: %s",
            e.response.status_code,
            (e.response.text or "")[:500],
        )
        return None, None
    except Exception as e:
        logger.exception("YooKassa create payment failed: %s", e)
        return None, None
    payment_id = data.get("id")
    confirmation = data.get("confirmation") or {}
    url = confirmation.get("confirmation_url")
    if not url or not payment_id:
        logger.warning(
            "YooKassa response missing confirmation_url or id: keys=%s body=%s",
            list(data.keys()),
            body[:500] if body else "",
        )
        return None, None
    return url, payment_id
