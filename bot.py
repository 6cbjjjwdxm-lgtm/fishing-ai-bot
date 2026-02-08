import asyncio
import datetime
import io
import logging
import os
import sys
from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import Message

from openai import AsyncOpenAI

from weather import get_weather_for_day, get_weather_5days
from ai_logic import (
    classify_intent_ru,
    extract_day_offset_ru,
    extract_date_iso,
    extract_city_simple,
    assistant_text,
    assistant_with_photo,
    INTENT_FORECAST,
)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    sys.exit("❌ ОШИБКА: Не найдены токены TELEGRAM_TOKEN / OPENAI_API_KEY в .env")

dp = Dispatcher()
client = AsyncOpenAI(api_key=OPENAI_API_KEY)


async def safe_send_markdown(message: Message, text: str):
    try:
        await message.reply(text, parse_mode="Markdown")
    except TelegramBadRequest:
        await message.reply(text)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я рыболовный AI‑ассистент.\n\n"
        "Пиши запросы так (только со звёздочкой *):\n"
        "`*клев завтра в Звенигороде`\n"
        "`*прогноз клева на 5 дней в Подольске`\n"
        "`*подбери комплект спиннинга на щуку бюджет 20к`\n"
        "`*как ловить леща зимой со льда`\n\n"
        "Фото тоже можно, но подпись должна начинаться с `*`."
    )


@dp.message(F.photo)
async def handle_photo(message: Message):
    # Фото обрабатываем ТОЛЬКО если есть подпись и она начинается с *
    caption = message.caption or ""
    if not caption.startswith("*"):
        return

    query = caption[1:].strip() or "Определи, что на фото, и дай советы."
    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        bot = message.bot
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)

        buf = io.BytesIO()
        await bot.download_file(file.file_path, destination=buf)
        image_bytes = buf.getvalue()

        user_id = message.from_user.id
        ans = await assistant_with_photo(client, user_id=user_id, query=query, image_bytes=image_bytes)
        await safe_send_markdown(message, ans)

    except Exception:
        logging.exception("photo handler error")
        await safe_send_markdown(message, "⚠️ Не получилось обработать фото. Попробуй другое (ближе/резче).")


@dp.message(F.text)
async def handle_text(message: Message):
    text = message.text or ""
    if not text.startswith("*"):
        return

    query = text[1:].strip()
    if not query:
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    user_id = message.from_user.id
    intent = classify_intent_ru(query)

    extra_context = None

    if intent == INTENT_FORECAST:
        city = extract_city_simple(query)
        if not city:
            await safe_send_markdown(message, "Укажи локацию: например `*клев завтра в Подольске`.")
            return

        low = query.lower()

        # 5 дней, если попросили
        if any(w in low for w in ["на 5", "5 дней", "пять дней", "на неделю", "неделю"]):
            days = await get_weather_5days(city)
            if not days:
                await safe_send_markdown(message, f"⚠️ Не нашёл погоду для: {city}. Попробуй другое название.")
                return
            extra_context = {"tool": "openweather_forecast_5d", "city": city, "days": days}
            ans = await assistant_text(client, user_id=user_id, query=query, extra_context=extra_context, temperature=0.45)
            await safe_send_markdown(message, ans)
            return

        # день: сегодня/завтра/послезавтра или дата
        day_offset = extract_day_offset_ru(query)
        iso = extract_date_iso(query)
        if iso:
            d = datetime.date.fromisoformat(iso)
            day_offset = (d - datetime.date.today()).days

        if day_offset is None:
            day_offset = 0

        if day_offset < 0 or day_offset > 4:
            await safe_send_markdown(
                message,
                "По погоде у меня данные примерно на 5 дней вперёд. "
                "Спроси `сегодня/завтра/послезавтра` или ближайшую дату."
            )
            return

        w = await get_weather_for_day(city, day_offset)
        if not w:
            await safe_send_markdown(message, f"⚠️ Не нашёл погоду для: {city}. Попробуй другое название.")
            return

        extra_context = {"tool": "openweather_forecast_day", "city": city, "weather": w}
        ans = await assistant_text(client, user_id=user_id, query=query, extra_context=extra_context, temperature=0.45)
        await safe_send_markdown(message, ans)
        return

    # не forecast — просто “полный ассистент” с памятью
    ans = await assistant_text(client, user_id=user_id, query=query, extra_context=None, temperature=0.65)
    await safe_send_markdown(message, ans)


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is Alive"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def main():
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)

    asyncio.create_task(start_web_server())
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, force=True)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass






























