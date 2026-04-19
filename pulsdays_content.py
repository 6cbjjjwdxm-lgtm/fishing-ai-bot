"""
pulsdays_content.py

Контент-завод для Telegram-канала @pulsdays — «Пульс дня».
Ежедневный пост: праздник, именины, событие, луна, рекомендации, цитата, кот дня.

Генерация текста — GPT-4o-mini.
Генерация картинки (кот дня) — DALL-E 3 через OpenAI Images API.
"""

import asyncio
import datetime
import io
import logging
import os
import re
from typing import Optional, Tuple

import aiohttp
from openai import AsyncOpenAI

from persistence import (
    record_published,
    was_published_today,
)

logger = logging.getLogger(__name__)

# ────────────────────────────── Константы ──────────────────────────────

PULSDAYS_CHANNEL = (os.getenv("PULSDAYS_CHANNEL") or "@pulsdays").strip()

# 07:00 МСК = 04:00 UTC
PULSDAYS_POST_HOUR_UTC = int(os.getenv("PULSDAYS_POST_HOUR_UTC", "4"))
PULSDAYS_POST_MINUTE_UTC = int(os.getenv("PULSDAYS_POST_MINUTE_UTC", "0"))

TG_CAPTION_LIMIT = 1024
TG_MESSAGE_LIMIT = 4096


# ────────────────────────────── Утилиты ──────────────────────────────

def _strip_markdown(text: str) -> str:
    """Убирает Markdown-символы для caption без parse_mode."""
    return text.replace('**', '').replace('*', '').replace('__', '').replace('_', '').replace('`', '')


def _get_msk_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)


# ────────────────────────────── Системный промпт ──────────────────────────────

PULSDAYS_SYSTEM = """Ты — автор ежедневных постов для Telegram-канала @pulsdays («Пульс дня»).

Твоя задача — создать один полностью готовый к публикации пост на русском языке.

Структура поста строго такая:

☀️ Сегодняшний день
Сегодня, [дата] — [название праздника/памятного дня]
Именины отмечают: [имена]

📜 Событие этого дня в истории
Коротко опиши одно важное, интересное или вдохновляющее событие, которое произошло именно в этот день в прошлом. 1–2 предложения, без перегруза датами и деталями.

🌙 Луна и энергия дня
Укажи актуальную фазу Луны, знак зодиака Луны и кратко объясни, какая энергия у дня. 2–3 коротких предложения.

✅ Рекомендации: как провести день
Сделай 3–4 коротких пункта списком. Советы должны быть практичные, спокойные, бытовые, позитивные.

💬 Цитата дня
Добавь одну короткую весёлую цитату известного человека. Цитата должна быть лёгкой, доброй, с юмором, без пошлости и без грубости. После цитаты укажи автора.

🐱 Кот дня
Напиши одно предложение-описание для генерации картинки с котиком. Описание должно соответствовать теме дня, настроению поста и цитате. Стиль — милый, выразительный, запоминающийся. Формат:
Картинка: [описание сцены с котом]

ТРЕБОВАНИЯ:
- Пиши только на русском.
- Не используй слово «гороскоп».
- Не делай текст слишком эзотерическим.
- Тон — дружелюбный, аккуратный, современный.
- Визуально удобно для Telegram (короткие абзацы, аккуратные переносы).
- Не добавляй фразу «Пульс дня напоминает».
- Не пиши «Цитата дня с юмором», только «Цитата дня».
- Не ставь хэштеги.
- Эмодзи используй умеренно (только в заголовках блоков).
- Не добавляй дисклеймеры вроде «по некоторым данным», «возможно», «считается».
- Если праздник малоизвестный, выбери наиболее интересный и понятный.
- Если именин несколько, выбери основные.
- Цитата должна быть общеизвестной, не слишком длинной.
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
        f"Верни только готовый пост без пояснений до и после."
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
            f"☀️ Сегодняшний день\n"
            f"Сегодня, {date_str}\n\n"
            f"Пост будет позже — технические шоколадки. Хорошего дня!"
        )


# ────────────────────────────── Извлечение prompt для картинки ──────────────────────────────

def extract_image_prompt(post_text: str) -> Optional[str]:
    """Извлекает описание картинки из блока «Кот дня»."""
    # Ищем строку: Картинка: ...
    match = re.search(r'[Кк]артинка:\s*(.+)', post_text)
    if match:
        return match.group(1).strip()
    return None


def remove_image_prompt_line(post_text: str) -> str:
    """Убирает строку 'Картинка: ...' из текста поста (она только для генерации)."""
    return re.sub(r'\n?[Кк]артинка:\s*.+', '', post_text).rstrip()


# ────────────────────────────── Генерация картинки (DALL-E) ──────────────────────────────

async def generate_cat_image(client: AsyncOpenAI, description: str) -> Optional[bytes]:
    """
    Генерирует картинку кота через DALL-E 3.
    Возвращает bytes изображения или None.
    """
    # Формируем английский промпт для DALL-E (лучше работает на EN)
    prompt = (
        f"A cute, expressive cartoon cat in a cozy scene: {description}. "
        f"Style: warm, colorful, digital illustration, Telegram sticker style, "
        f"soft lighting, no text, no watermarks."
    )

    try:
        resp = await client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            quality="standard",
            n=1,
        )
        image_url = resp.data[0].url
        if not image_url:
            return None

        # Скачиваем изображение
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(image_url) as img_resp:
                if img_resp.status == 200:
                    return await img_resp.read()
    except Exception as e:
        logger.error("DALL-E image generation failed: %s", e)
    return None


# ────────────────────────────── Публикация ──────────────────────────────

async def publish_pulsdays_post(bot, client: AsyncOpenAI) -> bool:
    """
    Ежедневная публикация в @pulsdays:
    1. Проверяет дубли
    2. Генерирует текст поста
    3. Извлекает описание для картинки кота
    4. Генерирует картинку через DALL-E
    5. Публикует пост + картинку в канал
    """
    # Защита от дублей
    if was_published_today(PULSDAYS_CHANNEL, "daily"):
        logger.info("Pulsdays daily post already published today, skipping")
        return True

    now = _get_msk_now()
    today = now.date()

    logger.info("Publishing pulsdays post for %s", today)

    # Генерируем текст
    post_text = await generate_pulsdays_post(client, today)

    # Извлекаем описание для картинки
    image_description = extract_image_prompt(post_text)

    # Убираем строку "Картинка: ..." из финального текста
    clean_text = remove_image_prompt_line(post_text)

    # Генерируем картинку кота
    image_bytes = None
    if image_description:
        image_bytes = await generate_cat_image(client, image_description)

    # Публикуем
    try:
        if image_bytes:
            # Пост с картинкой
            photo_input = io.BytesIO(image_bytes)
            photo_input.name = "cat_of_the_day.png"

            if len(clean_text) <= TG_CAPTION_LIMIT:
                # Текст влезает в caption
                await bot.send_photo(
                    chat_id=PULSDAYS_CHANNEL,
                    photo=photo_input,
                    caption=clean_text,
                )
            else:
                # Текст слишком длинный для caption — сначала фото, потом текст
                await bot.send_photo(
                    chat_id=PULSDAYS_CHANNEL,
                    photo=photo_input,
                )
                # Разбиваем на куски по 4096
                for i in range(0, len(clean_text), TG_MESSAGE_LIMIT):
                    await bot.send_message(
                        chat_id=PULSDAYS_CHANNEL,
                        text=clean_text[i:i + TG_MESSAGE_LIMIT],
                    )
        else:
            # Без картинки — просто текст
            for i in range(0, len(clean_text), TG_MESSAGE_LIMIT):
                await bot.send_message(
                    chat_id=PULSDAYS_CHANNEL,
                    text=clean_text[i:i + TG_MESSAGE_LIMIT],
                )

        record_published(PULSDAYS_CHANNEL, f"pulsdays_{today.isoformat()}", "daily")
        logger.info("Pulsdays post published for %s", today)
        return True
    except Exception as e:
        logger.error("Failed to publish pulsdays post: %s", e)
        return False
