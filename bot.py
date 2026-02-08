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

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

REPORT_TARGET_CHANNEL = (os.getenv("REPORT_TARGET_CHANNEL") or "").strip()
REQUIRED_CHANNELS = [c.strip() for c in (os.getenv("REQUIRED_CHANNELS") or "").split(",") if c.strip()]

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
    # True = можно, False = нельзя
    if not REQUIRED_CHANNELS:
        return True
    ok = await is_subscribed(message.bot, message.from_user.id, REQUIRED_CHANNELS)
    if not ok:
        await safe_send_markdown(
            message,
            "Доступ только для подписчиков канала(ов).\n"
            "Подпишись и попробуй снова."
        )
        return False
    return True


@dp.message(CommandStart())
async def cmd_start(message: Message):
    # /start report
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
        "Я отвечаю только на сообщения со звёздочкой `*`.\n"
        "Примеры:\n"
        "`*клев завтра в Звенигороде`\n"
        "`*прогноз клева на 5 дней в Подольске`\n"
        "`*подбери комплект спиннинга на щуку бюджет 20к`\n\n"
        "Чтобы добавить отчёт: нажми кнопку в закрепе канала (или /start report)."
    )


@dp.callback_query(F.data.startswith("rep:"))
async def report_callbacks(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    r = reports.get_report(uid)
    if not r:
        await safe_send_markdown(callback.message, "Сессия отчёта не активна. Нажми кнопку ещё раз.")
        return

    if callback.data == "rep:cancel":
        reports.cancel_report(uid)
        await safe_send_markdown(callback.message, "Отчёт отменён.")
        return

    if callback.data == "rep:restart":
        reports.start_report(uid)
        prompt = await reports.next_prompt(reports.get_report(uid))
        await safe_send_markdown(callback.message, "Ок, начнём заново.\n" + prompt)
        return

    if callback.data == "rep:send":
        if not REPORT_TARGET_CHANNEL:
            await safe_send_markdown(callback.message, "⚠️ REPORT_TARGET_CHANNEL не настроен в .env")
            return

        draft = reports.render_report_text(r)
        ok, reason = await reports.moderate_text_openai(client, draft)
        if not ok:
            await safe_send_markdown(callback.message, f"⚠️ Отчёт не прошёл модерацию: {reason}\nПерепиши текст без нарушений и попробуй снова.")
            # возвращаем на шаг NOTES, чтобы пользователь переписал
            r["step"] = reports.STEP_NOTES
            return

        # Публикация: сначала медиа (если есть), потом текст
        bot = callback.message.bot
        media = r.get("media") or []

        try:
            if media:
                # отправляем по одному (проще). Можно заменить на sendMediaGroup.
                for item in media[:10]:
                    if item["type"] == "photo":
                        await bot.send_photo(chat_id=REPORT_TARGET_CHANNEL, photo=item["file_id"])
                    elif item["type"] == "video":
                        await bot.send_video(chat_id=REPORT_TARGET_CHANNEL, video=item["file_id"])

            await bot.send_message(chat_id=REPORT_TARGET_CHANNEL, text=draft, parse_mode="Markdown")
            reports.cancel_report(uid)
            await safe_send_markdown(callback.message, "✅ Отчёт отправлен в канал.")
        except Exception:
            logging.exception("send to channel failed")
            await safe_send_markdown(callback.message, "⚠️ Не удалось отправить в канал. Проверь: бот админ канала и есть право постинга.")


@dp.message(F.photo)
async def handle_photo(message: Message):
    caption = message.caption or ""
    uid = message.from_user.id

    # 1) Если активен отчёт — фото добавляем в отчёт (без звездочки)
    if reports.has_active_report(uid):
        r = reports.get_report(uid)
        if r and r.get("step") == reports.STEP_MEDIA:
            r["media"].append({"type": "photo", "file_id": message.photo[-1].file_id})
            await safe_send_markdown(message, "Фото добавлено. Можешь прислать ещё или напиши `готово`.")
            return

    # 2) В режиме ассистента: обрабатываем фото только если подпись начинается с *
    if not caption.startswith("*"):
        return
    if not await subscription_gate(message):
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

        ans = await assistant_with_photo(client, user_id=uid, query=query, image_bytes=image_bytes)
        await safe_send_markdown(message, ans)
    except Exception:
        logging.exception("photo handler error")
        await safe_send_markdown(message, "⚠️ Не получилось обработать фото. Попробуй другое (ближе/резче).")


@dp.message(F.video)
async def handle_video(message: Message):
    uid = message.from_user.id
    if reports.has_active_report(uid):
        r = reports.get_report(uid)
        if r and r.get("step") == reports.STEP_MEDIA:
            r["media"].append({"type": "video", "file_id": message.video.file_id})
            await safe_send_markdown(message, "Видео добавлено. Можешь прислать ещё или напиши `готово`.")
            return


@dp.message(F.location)
async def handle_location(message: Message):
    uid = message.from_user.id
    if not reports.has_active_report(uid):
        return
    r = reports.get_report(uid)
    if not r:
        return
    if r.get("step") != reports.STEP_GEO:
        return

    txt = reports.handle_report_location(r, message.location.latitude, message.location.longitude)
    await safe_send_markdown(message, txt)


@dp.message(F.text)
async def handle_text(message: Message):
    text = message.text or ""
    uid = message.from_user.id

    # 1) Если активен отчёт — обрабатываем как ввод мастера
    if reports.has_active_report(uid):
        r = reports.get_report(uid)
        if not r:
            return
        reply = await reports.handle_report_text_input(r, text)

        if r.get("step") == reports.STEP_CONFIRM:
            draft = reports.render_report_text(r)
            await safe_send_markdown(message, reply or "Черновик готов.", reply_markup=reports.report_keyboard_confirm())
        else:
            if reply:
                await safe_send_markdown(message, reply)
        return

    # 2) Ассистент отвечает только на *
    if not text.startswith("*"):
        return
    if not await subscription_gate(message):
        return

    query = text[1:].strip()
    if not query:
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    intent = classify_intent_ru(query)
    extra_context = None

    if intent == INTENT_FORECAST:
        city = extract_city_simple(query)
        if not city:
            await safe_send_markdown(message, "Укажи локацию: например `*клев завтра в Подольске`.")
            return

        low = query.lower()
        if any(w in low for w in ["на 5", "5 дней", "пять дней", "на неделю", "неделю"]):
            days = await get_weather_5days(city)
            if not days:
                await safe_send_markdown(message, f"⚠️ Не нашёл погоду для: {city}. Попробуй другое название.")
                return
            extra_context = {"tool": "openweather_forecast_5d", "city": city, "days": days}
            ans = await assistant_text(client, user_id=uid, query=query, extra_context=extra_context, temperature=0.45)
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
            await safe_send_markdown(message, f"⚠️ Не нашёл погоду для: {city}. Попробуй другое название.")
            return

        extra_context = {"tool": "openweather_forecast_day", "city": city, "weather": w}
        ans = await assistant_text(client, user_id=uid, query=query, extra_context=extra_context, temperature=0.45)
        await safe_send_markdown(message, ans)
        return

    # Остальное: полный ассистент
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
































