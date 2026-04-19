"""
pulsdays_content.py

Контент-завод для Telegram-канала @pulsdays — «Пульс дня».
Ежедневный пост: дата, праздник, именины, событие, луна, рекомендации, цитата с юмором.

Генерация текста — GPT-4o-mini (HTML-форматирование для Telegram).
Картинка — реалистичное фото через Pexels API (по теме/настроению дня).
"""

import asyncio
import datetime
import logging
import os
import random
import re
from typing import Optional

import aiohttp
from openai import AsyncOpenAI

from persistence import (
    record_published,
    was_published_today,
)

logger = logging.getLogger(__name__)

# ────────────────────────────── Константы ──────────────────────────────

PULSDAYS_CHANNEL = (os.getenv("PULSDAYS_CHANNEL") or "@pulsdays").strip()
PEXELS_API_KEY = (os.getenv("PEXELS_API_KEY") or "").strip()

# 07:00 МСК = 04:00 UTC
PULSDAYS_POST_HOUR_UTC = int(os.getenv("PULSDAYS_POST_HOUR_UTC", "4"))
PULSDAYS_POST_MINUTE_UTC = int(os.getenv("PULSDAYS_POST_MINUTE_UTC", "0"))

TG_CAPTION_LIMIT = 1024
TG_MESSAGE_LIMIT = 4096


# ────────────────────────────── Утилиты ──────────────────────────────

def _get_msk_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)


# ────────────────────────────── Pexels фото ──────────────────────────────

# Запросы для реалистичных фото по сезону/настроению
SEASON_PHOTO_QUERIES = {
    "зима": [
        "cozy winter morning coffee", "winter sunrise city", "snowy street lights",
        "warm blanket winter", "hot cocoa window snow", "winter park morning",
    ],
    "весна": [
        "spring flowers morning", "cherry blossom city", "spring sunshine park",
        "morning dew flowers", "spring rain city", "green leaves sunlight",
    ],
    "лето": [
        "summer morning sunrise", "sunny day nature", "summer lake calm",
        "golden hour field", "summer evening city", "sunflowers field",
    ],
    "осень": [
        "autumn leaves park", "cozy autumn morning", "fall colors forest",
        "rainy autumn city", "pumpkin autumn cozy", "foggy autumn morning",
    ],
}


def _get_season(month: int) -> str:
    if month in (12, 1, 2):
        return "зима"
    if month in (3, 4, 5):
        return "весна"
    if month in (6, 7, 8):
        return "лето"
    return "осень"


async def search_pexels_photo(query: str) -> Optional[str]:
    """Ищет реалистичное фото на Pexels, возвращает URL."""
    if not PEXELS_API_KEY:
        return None
    try:
        url = f"https://api.pexels.com/v1/search?query={query}&per_page=15&orientation=landscape"
        headers = {"Authorization": PEXELS_API_KEY}
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    photos = data.get("photos", [])
                    if photos:
                        photo = random.choice(photos[:10])
                        return photo.get("src", {}).get("large2x") or photo.get("src", {}).get("original")
    except Exception as e:
        logger.warning("Pexels photo search error: %s", e)
    return None


async def get_day_photo(month: int) -> Optional[str]:
    """Подбирает реалистичное фото по сезону."""
    season = _get_season(month)
    queries = SEASON_PHOTO_QUERIES.get(season, ["sunrise morning nature"])
    query = random.choice(queries)
    photo_url = await search_pexels_photo(query)
    if photo_url:
        return photo_url
    # Фоллбэк — универсальный запрос
    return await search_pexels_photo("beautiful morning nature")


# ────────────────────────────── Системный промпт ──────────────────────────────

PULSDAYS_SYSTEM = """Ты — автор ежедневных постов для Telegram-канала @pulsdays («Пульс дня»).

Твоя задача — создать один полностью готовый к публикации пост на русском языке.
Пост форматирован в HTML для Telegram (используй <b>, <i> для выделения).

Структура поста строго такая:

<b>☀️ Сегодняшний день</b>
Сегодня, <b>[дата]</b> — <b>[название праздника/памятного дня]</b>
Именины отмечают: <b>[имена]</b>

<b>📜 Событие дня</b>
Коротко опиши одно важное, интересное или вдохновляющее событие, которое произошло именно в этот день в прошлом. 1–2 предложения.

<b>🌙 Луна и энергия дня</b>
Укажи актуальную фазу Луны, знак зодиака Луны и кратко объясни, какая энергия у дня. 2–3 коротких предложения.

<b>✅ Как провести день</b>
Сделай 3–4 коротких пункта списком (каждый пункт начинай с •). Советы практичные, спокойные, позитивные.

<b>💬 Цитата дня</b>
<i>«[цитата]»</i>
— [автор]

ТРЕБОВАНИЯ:
- Пиши только на русском.
- Используй HTML-теги: <b> для заголовков блоков и ключевых слов, <i> для цитаты.
- Не используй Markdown (никаких * или _).
- Не используй слово «гороскоп».
- Не делай текст эзотерическим.
- Тон — дружелюбный, тёплый, современный.
- Визуально удобно для Telegram (короткие абзацы, аккуратные переносы).
- Не добавляй «Пульс дня напоминает».
- Не пиши «Цитата дня с юмором», только «Цитата дня».
- Не ставь хэштеги.
- Эмодзи — только в заголовках блоков (☀️📜🌙✅💬), больше нигде.
- Не добавляй дисклеймеры вроде «по некоторым данным», «возможно».
- Если праздник малоизвестный, выбери самый интересный и понятный.
- Если именин несколько, выбери основные и не перегружай.
- Цитата ОБЯЗАТЕЛЬНО с юмором — лёгкая, добрая, смешная. Без пошлости и грубости.
- Цитата должна быть реальной цитатой известного человека.
- Не добавляй блок «Кот дня» или описание картинки.
- В конце поста ничего не добавляй после цитаты.
""".strip()


# ────────────────────────────── Генерация текста ──────────────────────────────

async def generate_pulsdays_post(client: AsyncOpenAI, target_date: datetime.date) -> str:
    """Генерирует пост «Пульс дня» на указанную дату."""
    months_genitive = {
        1: "января", 2: "февраля", 3: "марта", 4: "апреля",
        5: "мая", 6: "июня", 7: "июля", 8: "августа",
        9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
    }
    weekdays = {
        0: "понедельник", 1: "вторник", 2: "среда", 3: "четверг",
        4: "пятница", 5: "суббота", 6: "воскресенье",
    }

    date_str = f"{target_date.day} {months_genitive[target_date.month]} {target_date.year}"
    weekday_str = weekdays[target_date.weekday()]

    prompt = (
        f"Сегодня {weekday_str}, {date_str}.\n\n"
        f"Подготовь полный пост «Пульс дня» именно на эту дату.\n"
        f"Подбери праздник, именины, историческое событие, фазу Луны "
        f"и рекомендации именно на {target_date.day} {months_genitive[target_date.month]}.\n\n"
        f"Цитата дня должна быть смешной и с юмором.\n\n"
        f"Верни только готовый пост в HTML-формате без пояснений до и после."
    )

    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": PULSDAYS_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=1200,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error("Failed to generate pulsdays post: %s", e)
        return (
            f"<b>☀️ Сегодняшний день</b>\n"
            f"Сегодня, <b>{date_str}</b>\n\n"
            f"Пост будет позже — технические шоколадки. Хорошего дня!"
        )


# ────────────────────────────── Публикация ──────────────────────────────

async def publish_pulsdays_post(bot, client: AsyncOpenAI, force: bool = False) -> bool:
    """
    Ежедневная публикация в @pulsdays:
    1. Проверяет дубли (можно обойти через force=True)
    2. Генерирует текст поста (HTML)
    3. Ищет реалистичное фото через Pexels
    4. Публикует фото + текст в канал
    """
    # Защита от дублей (пропускается при force)
    if not force and was_published_today(PULSDAYS_CHANNEL, "daily"):
        logger.info("Pulsdays daily post already published today, skipping")
        return True

    now = _get_msk_now()
    today = now.date()

    logger.info("Publishing pulsdays post for %s", today)

    # Параллельно: генерируем текст и ищем фото
    text_task = generate_pulsdays_post(client, today)
    photo_task = get_day_photo(today.month)

    post_text, photo_url = await asyncio.gather(text_task, photo_task)

    # Публикуем
    try:
        if photo_url and len(post_text) <= TG_CAPTION_LIMIT:
            # Текст влезает в caption к фото
            await bot.send_photo(
                chat_id=PULSDAYS_CHANNEL,
                photo=photo_url,
                caption=post_text,
                parse_mode="HTML",
            )
        elif photo_url:
            # Сначала фото, потом текст отдельным сообщением
            await bot.send_photo(
                chat_id=PULSDAYS_CHANNEL,
                photo=photo_url,
            )
            for i in range(0, len(post_text), TG_MESSAGE_LIMIT):
                await bot.send_message(
                    chat_id=PULSDAYS_CHANNEL,
                    text=post_text[i:i + TG_MESSAGE_LIMIT],
                    parse_mode="HTML",
                )
        else:
            # Без фото — только текст
            for i in range(0, len(post_text), TG_MESSAGE_LIMIT):
                await bot.send_message(
                    chat_id=PULSDAYS_CHANNEL,
                    text=post_text[i:i + TG_MESSAGE_LIMIT],
                    parse_mode="HTML",
                )

        record_published(PULSDAYS_CHANNEL, f"pulsdays_{today.isoformat()}", "daily")
        logger.info("Pulsdays post published for %s", today)
        return True
    except Exception as e:
        logger.error("Failed to publish pulsdays post: %s", e)
        return False
