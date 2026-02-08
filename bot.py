import asyncio
import datetime
import io
import logging
import os
import sys
from typing import List

from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery

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
import reports
from reports import RepCB

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

REPORT_TARGET_CHANNEL = (os.getenv("REPORT_TARGET_CHANNEL") or "").strip()
PUBLIC_CHANNEL_URL = (os.getenv("PUBLIC_CHANNEL_URL") or "").strip()

REQUIRED_CHANNELS = [c.strip() for c in (os.getenv("REQUIRED_CHANNELS") or "").split(",") if c.strip()]
ADMIN_IDS = [int(x) for x in (os.getenv("ADMIN_IDS", "").split(",")) if x.strip().isdigit()]

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    sys.exit("❌ ОШИБКА: Не найдены токены TELEGRAM_TOKEN / OPENAI_API_KEY в .env")

dp = Dispatcher()
client = AsyncOpenAI(api_key=OPENAI_API_KEY)


async def safe_send_markdown(message: Message, text: str, reply_markup=None):
    try:
        await message.reply(text, parse_mode="Markdown", reply_markup=reply_markup)
    except TelegramBadRequest:
        await message.reply(text, reply_markup=reply_markup)


def _is_member_status(status: str) -> bool:
    return status in ("member", "administrator", "creator")


async def is_subscribed(bot: Bot, user_id: int, channels: List[str]) -> bool:
    if not channels:
        return True
    for ch in channels:
        try:
            m = await bot.get_chat_member(chat_id=ch, user_id=user_id)
            if not _is_member_status(getattr(m, "status", "")):
                return False
        except Exception:
            return False
    return True


async def subscription_gate(message: Message) -> bool:
    if not REQUIRED_CHANNELS:
        return True
    ok = await is_subscribed(message.bot, message.from_user.id, REQUIRED_CHANNELS)
    if not ok:
        await safe_send_markdown(message, "Доступ только для подписчиков канала(ов). Подпишись и попробуй снова.")
        return False
    return True


@dp.message(CommandStart())
async def cmd_start(message: Message):
    text = message.text or ""
    if "report" in text:
        if not await subscription_gate(message):
            return
        reports.start_report(message.from_user.id)
        prompt = await reports.next_prompt(reports.get_report(message.from_user.id))
        await safe_send_markdown(message, "🧾 Заполняем отчёт.\n" + prompt)
        return

    await message.answer(
        "👋 Привет! Я рыболовный AI‑ассистент.\n\n"
        "Я отвечаю только на сообщения со `*`.\n"
        "Чтобы добавить отчёт: нажми кнопку в закрепе канала (или /start report)."
    )


@dp.callback_query(RepCB.filter())
async def report_callbacks(callback: CallbackQuery, callback_data: RepCB):
    # оставь свою текущую версию callbacks (с редактированием)
    # здесь не меняем — чтобы не раздувать ответ
    await callback.answer("Ок")
    # ВАЖНО: оставь обработчик из предыдущей версии main.py с редактированием.


@dp.message(F.photo)
async def handle_photo(message: Message):
    uid = message.from_user.id
    caption = message.caption or ""

    if reports.has_active_report(uid):
        r = reports.get_report(uid)
        if r and r.get("step") == reports.STEP_MEDIA:
            r["media"].append({"type": "photo", "file_id": message.photo[-1].file_id})
            await safe_send_markdown(message, "Фото добавлено. Ещё фото/видео или напиши `готово`.")
            return

    if not caption.startswith("*"):
        return
    if not await subscription_gate(message):
        return

    query = caption[1:].strip() or "Определи, что на фото, и дай советы."
    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        bot = message.bot
        file = await bot.get_file(message.photo[-1].file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, destination=buf)
        image_bytes = buf.getvalue()

        ans = await assistant_with_photo(client, user_id=uid, query=query, image_bytes=image_bytes)
        await safe_send_markdown(message, ans)
    except Exception:
        logging.exception("photo handler error")
        await safe_send_markdown(message, "⚠️ Не получилось обработать фото. Попробуй другое (ближе/резче).")


@dp.message(F.text)
async def handle_text(message: Message):
    text = message.text or ""
    uid = message.from_user.id

    # мастер отчёта — оставь как у тебя (из версии с редактированием)
    if reports.has_active_report(uid):
        r = reports.get_report(uid)
        reply = await reports.handle_report_text_input(r, text)

        if r.get("step") == reports.STEP_CONFIRM:
            draft = reports.render_report_text(r, public_channel_url=PUBLIC_CHANNEL_URL)
            await safe_send_markdown(message, (reply or "Черновик:\n") + "\n\n" + draft, reply_markup=reports.keyboard_confirm_and_edit())
        else:
            if reply:
                await safe_send_markdown(message, reply)
        return

    # ассистент: только на *
    if not text.startswith("*"):
        return
    if not await subscription_gate(message):
        return

    query = text[1:].strip()
    if not query:
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    intent = classify_intent_ru(query)

    if intent == INTENT_FORECAST:
        city = extract_city_simple(query)
        if not city:
            await safe_send_markdown(message, "Укажи локацию: например `*клев завтра в Москве`.")
            return

        low = query.lower()

        if any(w in low for w in ["на 5", "5 дней", "пять дней", "на неделю", "неделю"]):
            days = await get_weather_5days(city)
            if not days:
                await safe_send_markdown(message, f"⚠️ Не нашёл погоду для: {city}. (Смотри логи GEOCODE/FORECAST)")
                return
            ctx = {"tool": "openweather_forecast_5d", "city": city, "days": days}
            ans = await assistant_text(client, user_id=uid, query=query, extra_context=ctx, temperature=0.45)
            await safe_send_markdown(message, ans)
            return

        day_offset = extract_day_offset_ru(query)
        iso = extract_date_iso(query)
        if iso:
            d = datetime.date.fromisoformat(iso)
            day_offset = (d - datetime.date.today()).days
        if day_offset is None:
            day_offset = 0
        if day_offset < 0 or day_offset > 4:
            await safe_send_markdown(message, "Погода доступна примерно на 5 дней. Спроси ближайшую дату/завтра/послезавтра.")
            return

        w = await get_weather_for_day(city, day_offset)
        if not w:
            await safe_send_markdown(message, f"⚠️ Не нашёл погоду для: {city}. (Смотри логи GEOCODE/FORECAST)")
            return

        ctx = {"tool": "openweather_forecast_day", "city": city, "weather": w}
        ans = await assistant_text(client, user_id=uid, query=query, extra_context=ctx, temperature=0.45)
        await safe_send_markdown(message, ans)
        return

    ans = await assistant_text(client, user_id=uid, query=query, extra_context=None, temperature=0.65)
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








