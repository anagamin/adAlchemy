import asyncio
import io
import json
import logging
from pathlib import Path

from PIL import Image
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .campaign_generator import generate_campaign
from .config import settings
from .models import AdVariant, CampaignDraft
from .vk_ads_requests import build_vk_ads_requests
from .vk_client import fetch_group_analysis

logger = logging.getLogger(__name__)

CREATING_MESSAGE = "Ваше объявление создаётся. Вы получите результат, когда всё будет готово."


async def _run_campaign_task(chat_id: int, link: str, app: Application) -> None:
    logger.info("task start chat_id=%s link=%s", chat_id, link)
    try:
        logger.info("task: fetching VK group analysis")
        analysis = fetch_group_analysis(link, posts_count=50)
        logger.info("task: VK done group=%s posts=%s", analysis.group.name, len(analysis.posts))
        draft = await generate_campaign(analysis)
        logger.info("task: campaign generated, sending to user")
        await _send_campaign(chat_id, draft, app)
        logger.info("task done chat_id=%s", chat_id)
    except ValueError as e:
        logger.warning("task error (ValueError): %s", e)
        await app.bot.send_message(chat_id=chat_id, text=f"Ошибка: {e}")
    except Exception as e:
        logger.exception("task failed: %s", e)
        await app.bot.send_message(chat_id=chat_id, text=f"Произошла ошибка: {e}")


def _format_ad_block(ad: AdVariant, index: int) -> str:
    lines = [
        f"━━━ Вариант {index} · {ad.segment_name} ━━━",
        "",
        f"📌 Заголовок: {ad.headline}",
        "",
        "Текст:",
        ad.body_text,
        "",
        f"CTA: {ad.cta}",
        "",
        f"Визуальная концепция: {ad.visual_concept}",
        "",
        "🖼 Промпт для генерации изображения:",
        ad.image_prompt,
        "",
    ]
    return "\n".join(lines)


def _format_campaign_message(draft: CampaignDraft) -> list[str]:
    chunks = []
    summary = draft.analysis_result.get("project_summary")
    if summary:
        chunks.append("📊 Анализ группы\n\n" + summary)
    if draft.keywords:
        chunks.append("🏷 Ключевые слова для таргета: " + ", ".join(draft.keywords[:20]))

    for i, ad in enumerate(draft.ads, 1):
        block = _format_ad_block(ad, i)
        chunks.append(block)

    return chunks


CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
PHOTO_MAX_SIZE = 1280
PHOTO_JPEG_QUALITY = 88


def _prepare_photo_for_telegram(path: str) -> bytes:
    with Image.open(path) as img:
        img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > PHOTO_MAX_SIZE:
            ratio = PHOTO_MAX_SIZE / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=PHOTO_JPEG_QUALITY, optimize=True)
        return buf.getvalue()


async def _send_campaign(chat_id: int, draft: CampaignDraft, app: Application) -> None:
    chunks = _format_campaign_message(draft)
    summary_count = 2 if draft.keywords else 1
    for i, part in enumerate(chunks[:summary_count]):
        if len(part) > MESSAGE_LIMIT:
            start = 0
            while start < len(part):
                await app.bot.send_message(chat_id=chat_id, text=part[start : start + MESSAGE_LIMIT])
                start += MESSAGE_LIMIT
        else:
            await app.bot.send_message(chat_id=chat_id, text=part)

    for i, ad in enumerate(draft.ads):
        block = chunks[summary_count + i] if summary_count + i < len(chunks) else _format_ad_block(ad, i + 1)
        caption = block[:CAPTION_LIMIT]
        if ad.image_path:
            try:
                photo_bytes = _prepare_photo_for_telegram(ad.image_path)
                await app.bot.send_photo(chat_id=chat_id, photo=photo_bytes, caption=caption)
            except Exception as e:
                logger.warning("send_photo for ad %s failed: %s", i + 1, e)
                await app.bot.send_message(chat_id=chat_id, text=block)
        else:
            if len(block) > MESSAGE_LIMIT:
                start = 0
                while start < len(block):
                    await app.bot.send_message(chat_id=chat_id, text=block[start : start + MESSAGE_LIMIT])
                    start += MESSAGE_LIMIT
            else:
                await app.bot.send_message(chat_id=chat_id, text=block)

    vk_requests = build_vk_ads_requests(draft)
    api_payload = {"vk_ads_api_requests": vk_requests}
    json_text = json.dumps(api_payload, ensure_ascii=False, indent=2)
    await app.bot.send_message(chat_id=chat_id, text="📤 Запросы в VK Ads API (JSON):")
    if len(json_text) > MESSAGE_LIMIT:
        start = 0
        while start < len(json_text):
            segment = json_text[start : start + MESSAGE_LIMIT]
            await app.bot.send_message(chat_id=chat_id, text=segment)
            start += MESSAGE_LIMIT
    else:
        await app.bot.send_message(chat_id=chat_id, text=json_text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Отправьте ссылку на группу ВКонтакте (например, vk.com/group_name или vk.com/club123). "
        "Я проанализирую группу и последние 50 постов и подготовлю данные для рекламной кампании."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    link = (update.message.text or "").strip()
    if not link or "vk.com" not in link.lower():
        await update.message.reply_text("Отправьте корректную ссылку на группу ВКонтакте (содержит vk.com).")
        return

    chat_id = update.effective_chat.id
    logger.info("handle_link chat_id=%s link=%s", chat_id, link)
    await update.message.reply_text(CREATING_MESSAGE)

    app = context.application
    asyncio.create_task(_run_campaign_task(chat_id, link, app))


def build_application() -> Application:
    from telegram.request import HTTPXRequest

    request = HTTPXRequest(
        read_timeout=30,
        write_timeout=30,
        connect_timeout=10,
        media_write_timeout=90,
    )
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .request(request)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    return app


def run_bot() -> None:
    if not settings.telegram_bot_token:
        raise ValueError("Укажите TELEGRAM_BOT_TOKEN в .env")
    if not settings.vk_access_token:
        raise ValueError("Укажите VK_ACCESS_TOKEN в .env")
    if not settings.llm_api_key:
        raise ValueError("Укажите LLM_API_KEY в .env (OpenAI / DeepSeek / Qwen)")

    app = build_application()
    app.run_polling(allowed_updates=Update.ALL_TYPES)
