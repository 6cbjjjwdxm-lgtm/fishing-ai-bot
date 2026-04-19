"""
pulsdays_content.py

Контент-завод для Telegram-канала @pulsdays — «Пульс дня».
Ежедневный пост: дата, праздник, именины, событие, луна, рекомендации, цитата с юмором.

Генерация текста — GPT-4o-mini (HTML-форматирование для Telegram).
Именины и лунный календарь — из проверенных справочников (pulsdays_data.py).
Картинка — реалистичное фото кота через Pexels API.
"""

import asyncio
import datetime
import logging
import os
import random
from typing import Optional

import aiohttp
from openai import AsyncOpenAI

from persistence import (
    record_published,
    was_published_today,
)
from pulsdays_data import get_moon_info, get_nameday

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


def _get_season(month: int) -> str:
    if month in (12, 1, 2):
        return "зима"
    if month in (3, 4, 5):
        return "весна"
    if month in (6, 7, 8):
        return "лето"
    return "осень"


# ────────────────────────────── Pexels: реалистичные коты ──────────────────────────────

CAT_PHOTO_QUERIES = {
    "зима": [
        "cat cozy blanket winter", "cat window snow", "kitten warm fireplace",
        "cat sleeping winter cozy", "fluffy cat sweater", "cat scarf winter",
    ],
    "весна": [
        "cat spring flowers", "kitten garden spring", "cat sunshine window",
        "cat grass spring", "kitten cherry blossom", "cat meadow flowers",
    ],
    "лето": [
        "cat summer garden", "cat sunbathing", "kitten playing grass summer",
        "cat hammock lazy", "cat sunny day outdoors", "kitten summer fun",
    ],
    "осень": [
        "cat autumn leaves", "kitten pumpkin fall", "cat cozy autumn",
        "cat rainy window", "kitten blanket fall", "cat sweater autumn",
    ],
}


async def search_pexels_cat(query: str) -> Optional[str]:
    """Ищет реалистичное фото кота на Pexels."""
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
        logger.warning("Pexels cat search error: %s", e)
    return None


async def get_cat_photo(month: int) -> Optional[str]:
    """Подбирает реалистичное фото кота по сезону."""
    season = _get_season(month)
    queries = CAT_PHOTO_QUERIES.get(season, ["cute cat portrait"])
    query = random.choice(queries)
    photo_url = await search_pexels_cat(query)
    if photo_url:
        return photo_url
    # Фоллбэк
    return await search_pexels_cat("cute cat portrait")


# ────────────────────────────── Системный промпт ──────────────────────────────

PULSDAYS_SYSTEM = """Ты — автор ежедневных постов для Telegram-канала @pulsdays («Пульс дня»).

Твоя задача — создать один полностью готовый к публикации пост на русском языке.
Пост форматирован в HTML для Telegram (используй <b>, <i> для выделения).

Тебе будут предоставлены ТОЧНЫЕ данные:
- Именины (НЕ придумывай другие, используй только те, что даны)
- Фаза Луны и знак зодиака Луны (НЕ придумывай, используй только данные)

Структура поста строго такая:

<b>☀️ Сегодняшний день</b>
Сегодня, <b>[дата]</b> — <b>[название праздника/памятного дня]</b>
Именины отмечают: <b>[ТОЛЬКО ДАННЫЕ ИМЕНИННИКИ]</b>

<b>📜 Событие дня</b>
Коротко опиши одно важное, интересное или вдохновляющее событие, которое произошло именно в этот день. 1–2 предложения.

<b>🌙 Луна и энергия дня</b>
[ИСПОЛЬЗУЙ ТОЛЬКО ПРЕДОСТАВЛЕННЫЕ ДАННЫЕ о фазе и знаке]
Кратко объясни, какая энергия у дня. 2–3 коротких предложения.

<b>✅ Как провести день</b>
• [совет 1]
• [совет 2]
• [совет 3]
• [совет 4]

<b>💬 Цитата дня</b>
<i>«[смешная цитата]»</i>
— [автор]

ТРЕБОВАНИЯ:
- Пиши только на русском.
- HTML-теги: <b> для заголовков и ключевых слов, <i> для цитаты.
- Не используй Markdown.
- Не используй слово «гороскоп».
- Не делай текст эзотерическим.
- Тон — дружелюбный, тёплый, с юмором.
- Не добавляй «Пульс дня напоминает».
- Заголовок цитаты — только «Цитата дня» (без слова «юмор»).
- Не ставь хэштеги.
- Эмодзи — только в заголовках блоков (☀️📜🌙✅💬), больше нигде.
- Не добавляй дисклеймеры.
- ИМЕНИНЫ: используй ТОЛЬКО предоставленный список, ничего не добавляй.
- ЛУНА: используй ТОЛЬКО предоставленные фазу и знак, ничего не меняй.
- Цитата ОБЯЗАТЕЛЬНО смешная, лёгкая, добрая. Без пошлости. Реальная цитата известного человека.
- После цитаты пост заканчивается. Ничего больше не добавляй.
""".strip()


# ────────────────────────────── Генерация текста ──────────────────────────────

async def generate_pulsdays_post(client: AsyncOpenAI, target_date: datetime.date) -> str:
    """Генерирует пост «Пульс дня» с проверенными данными."""
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

    # Достоверные данные
    nameday = get_nameday(target_date.month, target_date.day)
    dt_msk = datetime.datetime.combine(
        target_date, datetime.time(8, 0),
        tzinfo=datetime.timezone(datetime.timedelta(hours=3))
    )
    moon = get_moon_info(dt_msk)

    prompt = (
        f"Сегодня {weekday_str}, {date_str}.\n\n"
        f"ТОЧНЫЕ ДАННЫЕ (используй как есть, не придумывай свои):\n"
        f"- Именины: {nameday}\n"
        f"- Фаза Луны: {moon['phase']}\n"
        f"- Знак зодиака Луны: {moon['sign']}\n"
        f"- Освещённость Луны: {moon['illumination']}%\n"
        f"- Лунный день: {moon['age_days']}\n\n"
        f"Подбери праздник и историческое событие именно на "
        f"{target_date.day} {months_genitive[target_date.month]}.\n"
        f"Цитата дня — обязательно смешная.\n\n"
        f"Верни только готовый пост в HTML-формате."
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
            f"Сегодня, <b>{date_str}</b>\n"
            f"Именины: <b>{nameday}</b>\n\n"
            f"Пост будет позже — технические шоколадки. Хорошего дня!"
        )


# ────────────────────────────── Публикация ──────────────────────────────

async def publish_pulsdays_post(bot, client: AsyncOpenAI, force: bool = False) -> bool:
    """
    Ежедневная публикация в @pulsdays:
    1. Проверяет дубли (можно обойти через force=True)
    2. Генерирует текст поста с проверенными данными
    3. Ищет реалистичное фото кота через Pexels
    4. Публикует фото + текст в канал
    """
    if not force and was_published_today(PULSDAYS_CHANNEL, "daily"):
        logger.info("Pulsdays daily post already published today, skipping")
        return True

    now = _get_msk_now()
    today = now.date()

    logger.info("Publishing pulsdays post for %s", today)

    # Параллельно: текст + фото кота
    text_task = generate_pulsdays_post(client, today)
    photo_task = get_cat_photo(today.month)

    post_text, photo_url = await asyncio.gather(text_task, photo_task)

    # Публикуем
    try:
        if photo_url and len(post_text) <= TG_CAPTION_LIMIT:
            await bot.send_photo(
                chat_id=PULSDAYS_CHANNEL,
                photo=photo_url,
                caption=post_text,
                parse_mode="HTML",
            )
        elif photo_url:
            # Фото отдельно, текст отдельно
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
