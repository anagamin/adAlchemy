import json
import logging

import httpx
from aiohttp import web

from .config import settings
from .db import (
    add_balance,
    get_payment_by_yookassa_id,
    init_pool_if_needed,
    set_payment_succeeded,
)

logger = logging.getLogger(__name__)

TELEGRAM_SEND_MESSAGE = "https://api.telegram.org/bot{token}/sendMessage"


async def on_startup(app: web.Application) -> None:
    await init_pool_if_needed()


async def on_cleanup(app: web.Application) -> None:
    from .db import close_pool
    await close_pool()


async def handle_yookassa_webhook(request: web.Request) -> web.Response:
    try:
        body = await request.read()
        data = json.loads(body) if body else {}
    except json.JSONDecodeError as e:
        logger.warning("YooKassa webhook invalid JSON: %s", e)
        return web.json_response({"error": "Invalid JSON"}, status=400)
    event = data.get("event")
    if event != "payment.succeeded":
        return web.json_response({"status": "ignored"})
    obj = data.get("object") or {}
    payment_id = obj.get("id")
    if not payment_id:
        return web.json_response({"error": "Missing object.id"}, status=400)
    await init_pool_if_needed()
    record = await get_payment_by_yookassa_id(payment_id)
    if not record:
        logger.warning("YooKassa webhook unknown payment_id=%s", payment_id)
        return web.json_response({"status": "ok"})
    if record.get("status") != "pending":
        return web.json_response({"status": "ok"})
    telegram_id = int(record["telegram_id"])
    amount = float(record["amount_rub"])
    updated = await set_payment_succeeded(payment_id)
    if not updated:
        return web.json_response({"status": "ok"})
    if not await add_balance(telegram_id, amount):
        logger.error("add_balance failed telegram_id=%s amount=%s", telegram_id, amount)
    token = settings.telegram_bot_token
    if token:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    TELEGRAM_SEND_MESSAGE.format(token=token),
                    json={
                        "chat_id": telegram_id,
                        "text": f"Баланс успешно пополнен на {amount:.2f} ₽. Спасибо!",
                    },
                )
        except Exception as e:
            logger.warning("Failed to send Telegram notification: %s", e)
    return web.json_response({"status": "ok"})


def create_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_post(settings.yookassa_webhook_path, handle_yookassa_webhook)
    return app


def run_webhook_server() -> None:
    app = create_app()
    web.run_app(
        app,
        host="0.0.0.0",
        port=settings.yookassa_webhook_port,
        print=None,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    run_webhook_server()
