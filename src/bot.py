import asyncio
import io
import json
import logging
import re
from pathlib import Path
from typing import Any

from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, User
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .campaign_generator import generate_campaign
from .config import settings
from .db import (
    LOG_AD_TYPE,
    LOG_ORDER,
    LOG_ORDER_DONE,
    LOG_START,
    add_balance,
    create_payment_record,
    create_request,
    create_results,
    deduct_balance,
    ensure_user,
    get_last_requests,
    get_results_for_request,
    get_user_balance,
    log_action,
)
from .models import AdVariant, CampaignDraft
from .vk_client import fetch_group_analysis
from .yookassa_client import create_payment as yookassa_create_payment

logger = logging.getLogger(__name__)

CREATING_MESSAGE = "Ваше объявление создаётся. Вы получите результат, когда всё будет готово (около 3 минут)."

AD_TYPE_SUBSCRIBERS = "subscribers"
AD_TYPE_MESSAGES = "messages"

AD_TYPE_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("Привлечь подписчиков", callback_data=AD_TYPE_SUBSCRIBERS)],
    [InlineKeyboardButton("Принять заказы в сообщениях", callback_data=AD_TYPE_MESSAGES)],
])

REGION_IDS_TO_NAMES: dict[str, str] = {
    "1": "Вся Россия",
    "77": "Москва",
    "78": "Санкт-Петербург",
    "1019": "Московская область",
    "2": "Санкт-Петербург и ЛО",
    "11119": "Краснодарский край",
    "11029": "Свердловская область",
    "54": "Татарстан",
    "10995": "Нижегородская область",
}

VK_LINK_PATTERN = re.compile(
    r"(https?://)?(www\.)?vk\.(com|ru)/[^\s]+",
    re.IGNORECASE,
)

BUSY_MESSAGE = "Дождись окончания генерации"
GENERATION_STATE_KEY = "generation_state"
GENERATION_COST_RUB = 500
INSUFFICIENT_BALANCE_MESSAGE = (
    f"Недостаточно средств на балансе. Стоимость одной генерации — {GENERATION_COST_RUB} ₽. "
    "Пополните баланс командой /balance."
)

WELCOME_MESSAGE = """🚀 Добро пожаловать в AI-ассистент по рекламе ВКонтакте

Этот бот помогает владельцам групп и бизнесу во ВКонтакте создавать эффективные рекламные кампании за несколько минут.

Вы отправляете ссылку на группу —
мы анализируем её контент, вовлеченность и позиционирование —
и выдаём готовые рекламные решения, которые можно сразу запускать.

🎁 Первая генерация — бесплатно

Чтобы вы могли оценить качество работы, 1 генерация доступна бесплатно.
Дальнейшие генерации стоят 500 ₽.

🔍 Что входит в генерацию кампании:

✔ Анализ вашей группы
✔ Рекомендации по улучшению контента и повышению вовлеченности
✔ 2 готовые рекламные кампании, каждая из которых включает:
рекламный текст
заголовок
ключевые слова / сегменты аудитории
обоснование выбранной стратегии
уникальное рекламное изображение

Вы получаете не просто текст, а готовый рекламный инструмент с логикой и расчетом.

✍️ Можно добавить уточнения
Вместе со ссылкой на группу вы можете указать дополнительные пожелания.

Например:
«используй новогоднюю тематику»
«сделай акцент на наличии диплома у специалиста»
«сфокусируйся на скидке»
«ориентируйся на молодую аудиторию»

Бот учтёт эти детали при анализе и создании кампаний.

📌 Почему это полезно?

Экономит время на анализ и подготовку рекламы
Помогает находить новые аудитории
Позволяет тестировать разные рекламные подходы
Стоит дешевле, чем услуги таргетолога

Подходит для:
малого бизнеса
администраторов групп
самозанятых
SMM-специалистов

📋 Меню бота

/start — показать это сообщение
/create — создать рекламную кампанию
/info — список ваших созданных кампаний
/balance — проверить баланс и пополнить счёт

💰 Стоимость одной генерации: 500 ₽

Оплата происходит с внутреннего баланса бота.

Если у вас есть вопросы или предложения — пишите напрямую:
📩 @anagam1n

Готовы протестировать рекламу для своей группы?
Введите команду /create и начнём 🚀"""
INFO_REQUEST_IDS_KEY = "info_request_ids"
BALANCE_TOPUP_CALLBACK = "balance:topup"
BALANCE_AMOUNT_PREFIX = "balance:amount:"
BALANCE_AMOUNT_CUSTOM = "balance:amount:custom"
EXPECT_BALANCE_AMOUNT_KEY = "expect_balance_amount"

BALANCE_AMOUNTS = [500, 1000, 2000, 5000]
MIN_TOPUP = 100
MAX_TOPUP = 100_000


def _ensure_user_kwargs(user: User | None) -> dict[str, Any]:
    if user is None:
        return {}
    return {
        "first_name": user.first_name,
        "last_name": user.last_name,
        "username": user.username,
        "language_code": user.language_code,
        "is_bot": user.is_bot,
        "is_premium": getattr(user, "is_premium", None),
    }


def _get_generation_state(app: Application) -> dict[int, dict[str, Any]]:
    state = app.bot_data.get(GENERATION_STATE_KEY)
    if not isinstance(state, dict):
        state = {}
        app.bot_data[GENERATION_STATE_KEY] = state
    return state


def _is_generation_active(app: Application, chat_id: int) -> bool:
    state = _get_generation_state(app)
    data = state.get(chat_id)
    return bool(data and data.get("active"))


def _register_generation(app: Application, chat_id: int, request_id: int | None) -> None:
    state = _get_generation_state(app)
    state[chat_id] = {"active": True, "request_id": request_id, "result_sent": False}


def _should_send_results(app: Application, chat_id: int, request_id: int | None) -> bool:
    state = _get_generation_state(app)
    data = state.get(chat_id)
    if not data:
        return True
    if data.get("result_sent"):
        return False
    stored_request_id = data.get("request_id")
    if stored_request_id is not None and request_id is not None and stored_request_id != request_id:
        return False
    return True


def _mark_results_sent(app: Application, chat_id: int) -> None:
    state = _get_generation_state(app)
    data = state.get(chat_id)
    if data is not None:
        data["result_sent"] = True


def _clear_generation_state(app: Application, chat_id: int) -> None:
    state = _get_generation_state(app)
    state.pop(chat_id, None)


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


def _region_ids_to_text(region_ids: str | None) -> str:
    if not region_ids:
        return "—"
    parts = [p.strip() for p in str(region_ids).split(",") if p.strip()]
    names = [REGION_IDS_TO_NAMES.get(p, p) for p in parts]
    return ", ".join(names) if names else region_ids


async def _run_campaign_task(
    chat_id: int,
    link: str,
    app: Application,
    user_wishes: str | None = None,
    ad_type: str = AD_TYPE_SUBSCRIBERS,
    user_id: int | None = None,
    request_id: int | None = None,
    telegram_id: int | None = None,
) -> None:
    logger.info("task start chat_id=%s link=%s ad_type=%s", chat_id, link, ad_type)
    try:
        if not link or not str(link).strip():
            raise ValueError("Ссылка на группу не указана. Отправьте ссылку на группу ВКонтакте (например, vk.com/group_name).")
        logger.info("task: fetching VK group analysis")
        analysis = fetch_group_analysis(link, posts_count=50)
        logger.info("task: VK done group=%s posts=%s", analysis.group.name, len(analysis.posts))
        draft = await generate_campaign(analysis, user_wishes=user_wishes, ad_objective=ad_type)
        if request_id is not None:
            await create_results(request_id, draft)
        if user_id is not None:
            await log_action(user_id, LOG_ORDER_DONE)
        logger.info("task: campaign generated, evaluating send guard")
        if _should_send_results(app, chat_id, request_id):
            logger.info("task: sending campaign to chat_id=%s", chat_id)
            await _send_campaign(chat_id, draft, app)
            _mark_results_sent(app, chat_id)
        else:
            logger.info("task: duplicate results suppressed chat_id=%s request_id=%s", chat_id, request_id)
        logger.info("task done chat_id=%s", chat_id)
    except ValueError as e:
        logger.warning("task error (ValueError): %s", e)
        await app.bot.send_message(chat_id=chat_id, text=f"Ошибка: {e}")
        if telegram_id is not None:
            await add_balance(telegram_id, GENERATION_COST_RUB)
            await app.bot.send_message(chat_id=chat_id, text=f"Сумма {GENERATION_COST_RUB} ₽ возвращена на баланс.")
    except Exception as e:
        logger.exception("task failed: %s", e)
        await app.bot.send_message(chat_id=chat_id, text=f"Произошла ошибка: {e}")
        if telegram_id is not None:
            await add_balance(telegram_id, GENERATION_COST_RUB)
            await app.bot.send_message(chat_id=chat_id, text=f"Сумма {GENERATION_COST_RUB} ₽ возвращена на баланс.")
    finally:
        _clear_generation_state(app, chat_id)


def _format_ad_block(ad: AdVariant, index: int, draft: CampaignDraft) -> str:
    vk = draft.analysis_result.get("vk_campaign") or {}
    segments = draft.analysis_result.get("audience_segments") or []
    seg = segments[index - 1] if index <= len(segments) else {}
    age_range = seg.get("age_range") or f"{vk.get('age_from', 18)}–{vk.get('age_to', 55)}"
    gender_raw = (seg.get("gender") or "all").lower()
    gender_text = "мужской" if gender_raw == "male" else "женский" if gender_raw == "female" else "все"
    regions_text = _region_ids_to_text(vk.get("region_ids"))
    objective_text = (
        "Привлечь подписчиков" if draft.ad_objective == AD_TYPE_SUBSCRIBERS else "Принять заказы в сообщениях"
    )

    lines = [
        f"━━━ Вариант {index} · {ad.segment_name} ━━━",
        "",
        "Целевая аудитория: " + (ad.segment_name or "—"),
        "",
        f"📌 Заголовок: {ad.headline}",
        "",
        "Текст:",
        ad.body_text,
        "",
        "",
        f"Визуальная концепция: {ad.visual_concept}",
        "",
    ]
    if getattr(ad, "reasoning", "") and ad.reasoning.strip():
        lines.extend([
            "💡 Почему этот вариант:",
            ad.reasoning.strip(),
            "",
        ])
    lines.extend([
        "── Параметры для создания объявления ──",
        f"Цель кампании: {objective_text}",
        f"Регионы: {regions_text}",
        f"Возраст: {age_range}",
        f"Пол: {gender_text}",
        "",
    ])
    return "\n".join(lines)


def _format_content_recommendations(recs: list[dict[str, str]]) -> str:
    lines = ["📝 Рекомендации по контенту группы\n"]
    for i, r in enumerate(recs, 1):
        rec = (r.get("recommendation") or "").strip()
        reason = (r.get("reason") or "").strip()
        if rec:
            lines.append(f"{i}. {rec}")
            if reason:
                lines.append(f"   Обоснование: {reason}")
            lines.append("")
    return "\n".join(lines).strip()


def _format_campaign_message(draft: CampaignDraft) -> list[str]:
    chunks = []
    summary = draft.analysis_result.get("project_summary")
    if summary:
        chunks.append("📊 Анализ группы\n\n" + summary)
    content_recs = draft.analysis_result.get("content_recommendations")
    if content_recs:
        chunks.append(_format_content_recommendations(content_recs))
    if draft.keywords:
        chunks.append("🏷 Ключевые слова для таргета: " + ", ".join(draft.keywords[:20]))

    for i, ad in enumerate(draft.ads, 1):
        block = _format_ad_block(ad, i, draft)
        chunks.append(block)

    return chunks


def _draft_from_results(rows: list[dict[str, Any]], ad_objective: str = AD_TYPE_SUBSCRIBERS) -> CampaignDraft | None:
    if not rows:
        return None
    draft = CampaignDraft(ad_objective=ad_objective)
    first_data = rows[0].get("result_data")
    if first_data:
        try:
            data = json.loads(first_data) if isinstance(first_data, str) else first_data
            draft.keywords = data.get("keywords") or []
            draft.analysis_result = data.get("analysis_result") or {}
        except (json.JSONDecodeError, TypeError):
            pass
    for r in rows:
        ad = AdVariant(
            segment_name=(r.get("segment_name") or "") or "—",
            headline=(r.get("headline") or "") or "—",
            body_text=(r.get("body_text") or "") or "",
            cta=(r.get("cta") or "") or "",
            visual_concept=(r.get("visual_concept") or "") or "",
            image_prompt_short=(r.get("image_prompt_short") or "") or "",
            image_prompt=(r.get("image_prompt") or "") or "",
            image_path=r.get("pic"),
        )
        draft.ads.append(ad)
    return draft


CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
PHOTO_MAX_SIZE = 1024
PHOTO_JPEG_QUALITY = 75
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
    summary_count = sum(
        [
            1 if draft.analysis_result.get("project_summary") else 0,
            1 if draft.analysis_result.get("content_recommendations") else 0,
            1 if draft.keywords else 0,
        ]
    )
    for i, part in enumerate(chunks[:summary_count]):
        if len(part) > MESSAGE_LIMIT:
            start = 0
            while start < len(part):
                await app.bot.send_message(chat_id=chat_id, text=part[start : start + MESSAGE_LIMIT])
                start += MESSAGE_LIMIT
        else:
            await app.bot.send_message(chat_id=chat_id, text=part)

    for i, ad in enumerate(draft.ads):
        block = chunks[summary_count + i] if summary_count + i < len(chunks) else _format_ad_block(ad, i + 1, draft)
        if ad.image_path:
            photo_bytes = None
            try:
                photo_bytes = _prepare_photo_for_telegram(ad.image_path)
            except Exception as e:
                logger.warning("prepare_photo for ad %s failed: %s", i + 1, e)
            if photo_bytes:
                short_caption = f"Вариант {i + 1} · {ad.segment_name}"
                last_error = None
                for attempt in range(1, PHOTO_SEND_RETRIES + 1):
                    try:
                        await app.bot.send_photo(chat_id=chat_id, photo=photo_bytes, caption=short_caption)
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
                    if len(block) > MESSAGE_LIMIT:
                        start = 0
                        while start < len(block):
                            part = block[start : start + MESSAGE_LIMIT]
                            await app.bot.send_message(chat_id=chat_id, text=part)
                            start += MESSAGE_LIMIT
                    else:
                        await app.bot.send_message(chat_id=chat_id, text=block)
            else:
                if len(block) > MESSAGE_LIMIT:
                    start = 0
                    while start < len(block):
                        part = block[start : start + MESSAGE_LIMIT]
                        await app.bot.send_message(chat_id=chat_id, text=part)
                        start += MESSAGE_LIMIT
                else:
                    await app.bot.send_message(chat_id=chat_id, text=block)
        else:
            if len(block) > MESSAGE_LIMIT:
                start = 0
                while start < len(block):
                    part = block[start : start + MESSAGE_LIMIT]
                    await app.bot.send_message(chat_id=chat_id, text=part)
                    start += MESSAGE_LIMIT
            else:
                await app.bot.send_message(chat_id=chat_id, text=block)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        await ensure_user(user.id, **_ensure_user_kwargs(user))
    if update.message:
        await update.message.reply_text(WELCOME_MESSAGE)


async def cmd_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = None
    if user:
        user_id = await ensure_user(user.id, **_ensure_user_kwargs(user))
    chat = update.effective_chat
    chat_id = chat.id if chat else None
    app = context.application
    if chat_id is not None and _is_generation_active(app, chat_id):
        logger.info("cmd_create: generation active chat_id=%s, informing user", chat_id)
        if update.message:
            await update.message.reply_text(BUSY_MESSAGE)
        else:
            await app.bot.send_message(chat_id=chat_id, text=BUSY_MESSAGE)
        return
    if user_id is not None:
        await log_action(user_id, LOG_START)
    balance = await get_user_balance(user.id) if user else None
    if balance is None:
        balance = 0
    try:
        balance_val = float(balance)
    except (TypeError, ValueError):
        balance_val = 0
    balance_str = f"{balance_val:.2f}"
    if balance_val < GENERATION_COST_RUB:
        await update.message.reply_text(
            f"Ваш баланс: {balance_str} ₽\n\n{INSUFFICIENT_BALANCE_MESSAGE}"
        )
        return
    await update.message.reply_text(
        f"Ваш баланс: {balance_str} ₽. Стоимость генерации — {GENERATION_COST_RUB} ₽.\n\n"
        "AdAlechemy проводит многофакторный анализ вашей группы VK — контент, аудитория, ниша — и на основе данных генерирует рекомендации по ведению группы с учетом специфики целевой аудитории, а также персонализированные рекламные кампании с высокой эффективностью. "
        "Выберите тип объявления:",
        reply_markup=AD_TYPE_KEYBOARD,
    )


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    user_id = await ensure_user(user.id, **_ensure_user_kwargs(user))
    if user_id is None:
        await update.message.reply_text("Не удалось определить пользователя.")
        return
    requests_list = await get_last_requests(user_id, limit=50)
    if not requests_list:
        await update.message.reply_text("У вас пока нет заказов.")
        return
    request_ids = [r["id"] for r in requests_list]
    context.user_data[INFO_REQUEST_IDS_KEY] = request_ids
    lines = ["Последние заказы (новые сверху):\n"]
    for i, r in enumerate(requests_list, 1):
        link = r.get("link") or "—"
        created = r.get("created_at")
        date_str = created.strftime("%d.%m.%Y %H:%M") if hasattr(created, "strftime") else str(created)
        desc = (r.get("desc") or "").strip() or "—"
        desc_short = (desc[:80] + "…") if len(desc) > 80 else desc
        lines.append(f"{i}. {link}")
        lines.append(f"   Дата: {date_str}")
        lines.append(f"   Текст: {desc_short}\n")
    lines.append("Наберите порядковый номер (1–50), чтобы повторно получить сгенерированные варианты по этому заказу.")
    text = "\n".join(lines)
    if len(text) > MESSAGE_LIMIT:
        parts = [text[i : i + MESSAGE_LIMIT] for i in range(0, len(text), MESSAGE_LIMIT)]
        for part in parts:
            await update.message.reply_text(part)
    else:
        await update.message.reply_text(text)


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    await ensure_user(user.id, **_ensure_user_kwargs(user))
    balance = await get_user_balance(user.id)
    if balance is None:
        balance = 0
    balance_str = f"{balance:.2f}" if isinstance(balance, (int, float)) else str(balance)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Пополнить баланс", callback_data=BALANCE_TOPUP_CALLBACK)],
    ])
    await update.message.reply_text(
        f"Ваш текущий баланс: {balance_str}",
        reply_markup=keyboard,
    )


def _balance_amount_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{a} ₽", callback_data=f"{BALANCE_AMOUNT_PREFIX}{a}") for a in BALANCE_AMOUNTS],
        [InlineKeyboardButton("Другая сумма", callback_data=BALANCE_AMOUNT_CUSTOM)],
    ]
    return InlineKeyboardMarkup(rows)


async def handle_balance_topup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data != BALANCE_TOPUP_CALLBACK:
        return
    if not settings.yookassa_shop_id or not settings.yookassa_secret_key:
        await query.edit_message_text("Пополнение временно недоступно. Настройте YooKassa в конфигурации.")
        return
    await query.edit_message_text(
        "Выберите сумму пополнения (₽) или укажите свою:",
        reply_markup=_balance_amount_keyboard(),
    )


def _user_full_name(user: User | None) -> str:
    if not user:
        return ""
    parts = [user.first_name or "", user.last_name or ""]
    return " ".join(p for p in parts if p).strip()


async def _do_create_payment_and_send_link(
    chat_id: int,
    user_id: int,
    telegram_id: int,
    amount_rub: float,
    bot,
    customer_full_name: str | None = None,
) -> bool:
    url, payment_id = await yookassa_create_payment(
        amount_rub, telegram_id, customer_full_name=customer_full_name
    )
    if not url or not payment_id:
        await bot.send_message(
            chat_id,
            "Не удалось создать платёж. Проверьте в .env значения YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY "
            "и посмотрите логи бота — там будет причина ошибки.",
        )
        return False
    await create_payment_record(payment_id, user_id, telegram_id, amount_rub)
    await bot.send_message(
        chat_id,
        f"Оплатите {amount_rub:.2f} ₽ по ссылке. После успешной оплаты баланс пополнится автоматически.\n\n{url}",
    )
    return True


async def handle_balance_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data == BALANCE_AMOUNT_CUSTOM:
        context.user_data[EXPECT_BALANCE_AMOUNT_KEY] = True
        await query.edit_message_text(
            f"Введите сумму пополнения в рублях (от {MIN_TOPUP} до {MAX_TOPUP} ₽):"
        )
        return
    if not data.startswith(BALANCE_AMOUNT_PREFIX):
        return
    try:
        amount = int(data[len(BALANCE_AMOUNT_PREFIX) :])
    except ValueError:
        return
    if amount < MIN_TOPUP or amount > MAX_TOPUP:
        await query.edit_message_text(f"Сумма должна быть от {MIN_TOPUP} до {MAX_TOPUP} ₽.")
        return
    user = update.effective_user
    if not user:
        return
    user_id = await ensure_user(user.id, **_ensure_user_kwargs(user))
    if user_id is None:
        await query.edit_message_text("Ошибка: не удалось определить пользователя.")
        return
    await query.edit_message_text("Создаём платёж...")
    ok = await _do_create_payment_and_send_link(
        update.effective_chat.id,
        user_id,
        user.id,
        float(amount),
        context.bot,
        customer_full_name=_user_full_name(user),
    )
    if not ok:
        await query.edit_message_text("Не удалось создать платёж. Выберите сумму снова:", reply_markup=_balance_amount_keyboard())


async def handle_balance_amount_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.pop(EXPECT_BALANCE_AMOUNT_KEY, False):
        return False
    text = (update.message.text or "").strip().replace(",", ".")
    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text("Введите число (например 1500 или 2000.50):")
        return True
    if amount < MIN_TOPUP or amount > MAX_TOPUP:
        await update.message.reply_text(f"Сумма должна быть от {MIN_TOPUP} до {MAX_TOPUP} ₽.")
        return True
    user = update.effective_user
    if not user:
        return True
    user_id = await ensure_user(user.id, **_ensure_user_kwargs(user))
    if user_id is None:
        await update.message.reply_text("Ошибка: не удалось определить пользователя.")
        return True
    await update.message.reply_text("Создаём платёж...")
    ok = await _do_create_payment_and_send_link(
        update.effective_chat.id,
        user_id,
        user.id,
        amount,
        context.bot,
        customer_full_name=_user_full_name(user),
    )
    if not ok:
        await update.message.reply_text("Введите сумму ещё раз (от 100 до 100000):")
    return True


async def handle_ad_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    ad_type = query.data
    if ad_type not in (AD_TYPE_SUBSCRIBERS, AD_TYPE_MESSAGES):
        ad_type = AD_TYPE_SUBSCRIBERS
    context.user_data["ad_type"] = ad_type
    chat_id = update.effective_chat.id
    app = context.application
    if _is_generation_active(app, chat_id):
        logger.info("handle_ad_type: generation active chat_id=%s, ignoring new selection", chat_id)
        await app.bot.send_message(chat_id=chat_id, text=BUSY_MESSAGE)
        return
    pending_link = context.user_data.pop("pending_link", None)
    pending_wishes = context.user_data.pop("pending_wishes", None)

    user_id = None
    telegram_id = update.effective_user.id if update.effective_user else None
    if update.effective_user:
        user_id = await ensure_user(update.effective_user.id, **_ensure_user_kwargs(update.effective_user))
        if user_id is not None:
            await log_action(user_id, LOG_AD_TYPE if not pending_link else LOG_ORDER)

    if pending_link and user_id is not None and telegram_id is not None:
        if not await deduct_balance(telegram_id, GENERATION_COST_RUB):
            await query.edit_message_text(INSUFFICIENT_BALANCE_MESSAGE)
            context.user_data["pending_link"] = pending_link
            context.user_data["pending_wishes"] = pending_wishes
            return
        request_id = await create_request(user_id, pending_link, pending_wishes)
        await query.edit_message_text(CREATING_MESSAGE)
        _register_generation(app, chat_id, request_id)
        asyncio.create_task(
            _run_campaign_task(
                chat_id, pending_link, app, pending_wishes, ad_type, user_id, request_id, telegram_id
            )
        )
    elif pending_link:
        context.user_data["pending_link"] = pending_link
        context.user_data["pending_wishes"] = pending_wishes
        await query.edit_message_text("Ошибка: не удалось определить пользователя.")
    else:
        await query.edit_message_text(
            "Тип объявления выбран. Отправьте ссылку на группу ВКонтакте (например, vk.com/group_name или vk.ru/club123). "
            "Можно добавить текст с пожеланиями по рекламной кампании."
        )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if context.user_data.get(EXPECT_BALANCE_AMOUNT_KEY):
        await handle_balance_amount_message(update, context)
        return
    request_ids: list[int] = context.user_data.get(INFO_REQUEST_IDS_KEY) or []
    if request_ids and text.isdigit():
        num = int(text)
        if 1 <= num <= len(request_ids):
            request_id = request_ids[num - 1]
            context.user_data.pop(INFO_REQUEST_IDS_KEY, None)
            rows = await get_results_for_request(request_id)
            draft = _draft_from_results(rows) if rows else None
            if draft and draft.ads:
                await update.message.reply_text("Повторная выдача по заказу:")
                await _send_campaign(update.effective_chat.id, draft, context.application)
            else:
                await update.message.reply_text("По этому заказу нет сохранённых вариантов.")
            return
    link, user_wishes = parse_user_input(text)
    if not link:
        await update.message.reply_text(
            "Отправьте корректную ссылку на группу ВКонтакте (vk.com или vk.ru). "
            "Можно добавить к сообщению текст с пожеланиями по кампании."
        )
        return

    chat_id = update.effective_chat.id
    app = context.application
    if _is_generation_active(app, chat_id):
        logger.info("handle_link: generation active chat_id=%s, suppressing new request", chat_id)
        await update.message.reply_text(BUSY_MESSAGE)
        return
    ad_type = context.user_data.get("ad_type")
    if ad_type is None:
        context.user_data["pending_link"] = link
        context.user_data["pending_wishes"] = user_wishes
        await update.message.reply_text(
            "Сначала выберите тип объявления:",
            reply_markup=AD_TYPE_KEYBOARD,
        )
        return

    logger.info("handle_link chat_id=%s link=%s wishes=%s ad_type=%s", chat_id, link, bool(user_wishes), ad_type)

    user = update.effective_user
    if not user:
        await update.message.reply_text("Ошибка: не удалось определить пользователя.")
        return
    user_id = await ensure_user(user.id, **_ensure_user_kwargs(user))
    telegram_id = user.id
    if user_id is None:
        await update.message.reply_text("Ошибка: не удалось определить пользователя.")
        return
    await log_action(user_id, LOG_ORDER)
    if not await deduct_balance(telegram_id, GENERATION_COST_RUB):
        await update.message.reply_text(INSUFFICIENT_BALANCE_MESSAGE)
        return
    request_id = await create_request(user_id, link, user_wishes)

    await update.message.reply_text(CREATING_MESSAGE)
    _register_generation(app, chat_id, request_id)
    asyncio.create_task(
        _run_campaign_task(chat_id, link, app, user_wishes, ad_type, user_id, request_id, telegram_id)
    )


async def _on_shutdown(_app: Application) -> None:
    from .db import close_pool
    await close_pool()


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
        .post_shutdown(_on_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("create", cmd_create))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CallbackQueryHandler(handle_balance_topup, pattern=f"^{re.escape(BALANCE_TOPUP_CALLBACK)}$"))
    app.add_handler(
        CallbackQueryHandler(
            handle_balance_amount,
            pattern=f"^({re.escape(BALANCE_AMOUNT_CUSTOM)}|{re.escape(BALANCE_AMOUNT_PREFIX)}\\d+)$",
        )
    )
    app.add_handler(CallbackQueryHandler(handle_ad_type, pattern=f"^({AD_TYPE_SUBSCRIBERS}|{AD_TYPE_MESSAGES})$"))
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
