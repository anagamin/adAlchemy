import asyncio
import io
import logging
import re
from pathlib import Path

from PIL import Image
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .campaign_generator import generate_campaign
from .config import settings
from .models import AdVariant, CampaignDraft
from .vk_client import fetch_group_analysis

logger = logging.getLogger(__name__)

CREATING_MESSAGE = "Ваше объявление создаётся. Вы получите результат, когда всё будет готово."

VK_LINK_PATTERN = re.compile(
    r"(https?://)?(www\.)?vk\.com/[^\s]+",
    re.IGNORECASE,
)


def parse_user_input(text: str) -> tuple[str | None, str | None]:
    """Извлекает ссылку на группу VK и опциональный текст пожеланий из сообщения.
    Возвращает (link, user_wishes). Если ссылки нет — (None, None)."""
    raw = (text or "").strip()
    if not raw:
        return None, None
    match = VK_LINK_PATTERN.search(raw)
    if not match:
        return None, None
    link = match.group(0)
    if not link.startswith("http"):
        link = "https://" + link
    rest = (raw[: match.start()] + " " + raw[match.end() :]).strip()
    rest = re.sub(r"\s+", " ", rest) if rest else None
    return link, rest or None


async def _run_campaign_task(
    chat_id: int, link: str, app: Application, user_wishes: str | None = None
) -> None:
    logger.info("task start chat_id=%s link=%s", chat_id, link)
    try:
        logger.info("task: fetching VK group analysis")
        analysis = fetch_group_analysis(link, posts_count=50)
        logger.info("task: VK done group=%s posts=%s", analysis.group.name, len(analysis.posts))
        draft = await generate_campaign(analysis, user_wishes=user_wishes)
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


def _format_campaign_data_for_manual_create(draft: CampaignDraft) -> str:
    """Формирует текстовый блок со всеми данными для ручного создания кампании в VK Рекламе."""
    vk = draft.analysis_result.get("vk_campaign") or {}
    lines = [
        "═══════════════════════════════════════",
        "📋 ДАННЫЕ ДЛЯ РУЧНОГО СОЗДАНИЯ КАМПАНИИ",
        "═══════════════════════════════════════",
        "",
        "── КАМПАНИЯ ──",
        f"Название: {vk.get('campaign_name') or draft.analysis_result.get('project_summary', 'Кампания') or 'Кампания'}",
        f"Дневной бюджет (руб): {vk.get('budget_daily_rub') or 500}",
        f"Общий бюджет (руб, 0 = без лимита): {vk.get('budget_total_rub') or 0}",
        f"Тип ставки: {vk.get('bid_type') or 'cpc'}",
        f"Ставка (руб): {vk.get('bid_rub') or 15}",
        f"Ссылка (куда ведёт реклама): {vk.get('link_url') or 'https://vk.com'}",
        f"Страна (код): {vk.get('country') or '1'}",
        f"Регионы (коды через запятую): {vk.get('region_ids') or '—'}",
        f"Интересы (ID через запятую): {vk.get('interest_ids') or '—'}",
        f"Возраст: от {vk.get('age_from', 18)} до {vk.get('age_to', 55)}",
        "",
    ]
    segments = draft.analysis_result.get("audience_segments") or []
    if not segments and draft.ads:
        segments = [{"segment_name": ad.segment_name, "gender": "", "age_range": ""} for ad in draft.ads]
    if segments:
        lines.append("── ГРУППЫ ОБЪЯВЛЕНИЙ (ТАРГЕТИНГ) ──")
        for i, seg in enumerate(segments, 1):
            name = seg.get("segment_name") or f"Группа {i}"
            age = seg.get("age_range") or f"{vk.get('age_from', 18)}–{vk.get('age_to', 55)}"
            gender = seg.get("gender") or "все"
            lines.append(f"{i}. {name}")
            lines.append(f"   Возраст: {age}, пол: {gender}")
            lines.append("")
    lines.append("── ОБЪЯВЛЕНИЯ (для ввода в кабинете) ──")
    link_url = vk.get("link_url") or "https://vk.com"
    for i, ad in enumerate(draft.ads, 1):
        lines.append(f"{i}. Название: {(ad.headline or ad.segment_name or f'Объявление {i}')[:100]}")
        lines.append(f"   Заголовок: {(ad.headline or '')[:80]}")
        lines.append(f"   Текст: {(ad.body_text or '')[:800]}")
        lines.append(f"   Ссылка: {link_url}")
        lines.append("")
    if not draft.ads:
        name = vk.get("campaign_name") or draft.analysis_result.get("project_summary", "Кампания") or "Кампания"
        lines.append(f"1. Название: {name[:100]}")
        lines.append(f"   Заголовок: {name[:80]}")
        lines.append("   Текст: (введите вручную)")
        lines.append(f"   Ссылка: {link_url}")
        lines.append("")
    lines.append("═══════════════════════════════════════")
    return "\n".join(lines)


CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
PHOTO_MAX_SIZE = 1024
PHOTO_JPEG_QUALITY = 82
PHOTO_SEND_RETRIES = 3
PHOTO_SEND_RETRY_DELAY = 3.0


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
            photo_bytes = None
            try:
                photo_bytes = _prepare_photo_for_telegram(ad.image_path)
            except Exception as e:
                logger.warning("prepare_photo for ad %s failed: %s", i + 1, e)
            if photo_bytes:
                last_error = None
                for attempt in range(1, PHOTO_SEND_RETRIES + 1):
                    try:
                        await app.bot.send_photo(chat_id=chat_id, photo=photo_bytes, caption=caption)
                        last_error = None
                        break
                    except Exception as e:
                        last_error = e
                        logger.warning("send_photo for ad %s attempt %s/%s failed: %s", i + 1, attempt, PHOTO_SEND_RETRIES, e)
                        if attempt < PHOTO_SEND_RETRIES:
                            await asyncio.sleep(PHOTO_SEND_RETRY_DELAY)
                if last_error is not None:
                    await app.bot.send_message(chat_id=chat_id, text=block)
            else:
                await app.bot.send_message(chat_id=chat_id, text=block)
        else:
            if len(block) > MESSAGE_LIMIT:
                start = 0
                while start < len(block):
                    await app.bot.send_message(chat_id=chat_id, text=block[start : start + MESSAGE_LIMIT])
                    start += MESSAGE_LIMIT
            else:
                await app.bot.send_message(chat_id=chat_id, text=block)

    text_block = _format_campaign_data_for_manual_create(draft)
    await app.bot.send_message(chat_id=chat_id, text="📋 Данные для ручного создания кампании в VK Рекламе:")
    if len(text_block) > MESSAGE_LIMIT:
        start = 0
        while start < len(text_block):
            segment = text_block[start : start + MESSAGE_LIMIT]
            await app.bot.send_message(chat_id=chat_id, text=segment)
            start += MESSAGE_LIMIT
    else:
        await app.bot.send_message(chat_id=chat_id, text=text_block)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Отправьте ссылку на группу ВКонтакте (например, vk.com/group_name или vk.com/club123). "
        "Вместе со ссылкой можно добавить текст с пожеланиями и рекомендациями по рекламной кампании — "
        "например, акцент на скидках, тоне сообщений или целевой аудитории. "
        "Я проанализирую группу и последние 50 постов и подготовлю данные для рекламной кампании с учётом ваших пожеланий."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    link, user_wishes = parse_user_input(update.message.text or "")
    if not link:
        await update.message.reply_text(
            "Отправьте корректную ссылку на группу ВКонтакте (содержит vk.com). "
            "Можно добавить к сообщению текст с пожеланиями по кампании."
        )
        return

    chat_id = update.effective_chat.id
    logger.info("handle_link chat_id=%s link=%s wishes=%s", chat_id, link, bool(user_wishes))
    await update.message.reply_text(CREATING_MESSAGE)

    app = context.application
    asyncio.create_task(_run_campaign_task(chat_id, link, app, user_wishes))


def build_application() -> Application:
    from telegram.request import HTTPXRequest

    request = HTTPXRequest(
        read_timeout=30,
        write_timeout=30,
        connect_timeout=10,
        media_write_timeout=120,
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
