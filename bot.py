import asyncio
import datetime
import io
import logging
import os
import sys
from typing import List, Optional

from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery

from openai import AsyncOpenAI

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import reports
from reports import RepCB

from weather import get_weather_for_day, get_weather_5days, forecast_by_coords
from ai_logic import (
    classify_intent_ru,
    extract_day_offset_ru,
    extract_date_iso,
    extract_city_simple,
    looks_like_waterbody_query,
    assistant_text,
    assistant_with_photo,
    INTENT_FORECAST,
)
from content_factory import (
    publish_daily_post,
    publish_monthly_plan_preview,
    POST_HOUR_UTC,
    POST_MINUTE_UTC,
    CONTENT_CHANNEL,
)

load_dotenv()

TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()

REPORT_TARGET_CHANNEL = (os.getenv("REPORT_TARGET_CHANNEL") or "").strip()
PUBLIC_CHANNEL_URL = (os.getenv("PUBLIC_CHANNEL_URL") or "").strip()

REQUIRED_CHANNELS = [c.strip() for c in (os.getenv("REQUIRED_CHANNELS") or "").split(",") if c.strip()]
ADMIN_IDS = [int(x) for x in (os.getenv("ADMIN_IDS") or "").split(",") if x.strip().isdigit()]

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    sys.exit("❌ ОШИБКА: Не найдены TELEGRAM_TOKEN / OPENAI_API_KEY в .env")

dp = Dispatcher()
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ожидание уточнения локации для прогноза:
# user_id -> {"query": "...", "day_offset": int, "mode": "coords|place"}
PENDING_FORECAST: dict[int, dict] = {}


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
        except Exception as e:
            logging.warning("getChatMember failed chat=%s user=%s err=%s", ch, user_id, repr(e))
            return False
    return True


async def subscription_gate(message: Message) -> bool:
    # если сообщение пришло из канала (type=channel), не блокируем
    try:
        if getattr(message.chat, "type", "") == "channel":
            return True
    except Exception:
        pass

    if not REQUIRED_CHANNELS:
        return True

    try:
        ok = await is_subscribed(message.bot, message.from_user.id, REQUIRED_CHANNELS)
    except Exception as e:
        logging.warning("SUB CHECK ERROR: %s", repr(e))
        ok = False

    if not ok:
        logging.warning("SUB CHECK FAILED user_id=%s required=%s", message.from_user.id, REQUIRED_CHANNELS)
        await safe_send_markdown(message, "Доступ только для подписчиков канала(ов). Подпишись и попробуй снова.")
        return False

    return True


def _pending_set(uid: int, query: str, day_offset: int):
    PENDING_FORECAST[uid] = {"query": query, "day_offset": int(day_offset)}


def _pending_pop(uid: int):
    PENDING_FORECAST.pop(uid, None)


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
        "Я отвечаю только на сообщения со звёздочкой `*`.\n"
        "Пример: `*клев завтра в Москве`.\n\n"
        "Отчёт: кнопка в закрепе канала или /start report."
    )


@dp.callback_query(RepCB.filter())
async def report_callbacks(callback: CallbackQuery, callback_data: RepCB):
    await callback.answer()
    uid = callback.from_user.id
    r = reports.get_report(uid)
    if not r:
        await safe_send_markdown(callback.message, "Сессия отчёта не активна. Нажми кнопку ещё раз.")
        return

    act = callback_data.action

    if act == "cancel":
        reports.cancel_report(uid)
        await safe_send_markdown(callback.message, "Отчёт отменён.")
        return

    if act == "restart":
        reports.start_report(uid)
        prompt = await reports.next_prompt(reports.get_report(uid))
        await safe_send_markdown(callback.message, "Ок, начнём заново.\n" + prompt)
        return

    if act == "edit_menu":
        draft = reports.render_report_text(r, public_channel_url=PUBLIC_CHANNEL_URL)
        await safe_send_markdown(callback.message, "Выбери, что исправить:\n\n" + draft, reply_markup=reports.keyboard_edit_menu())
        return

    if act == "back":
        draft = reports.render_report_text(r, public_channel_url=PUBLIC_CHANNEL_URL)
        await safe_send_markdown(callback.message, "Черновик:\n\n" + draft, reply_markup=reports.keyboard_confirm_and_edit())
        return

    if act == "edit_geo":
        r["step"] = reports.STEP_EDIT
        r["edit_field"] = "geo"
        await safe_send_markdown(callback.message, "Ок. Отправь новую геолокацию (скрепка → Геопозиция).")
        return

    if act == "edit":
        field_map = {
            "place": "place_text",
            "method": "method",
            "bait": "bait",
            "results": "results",
            "notes": "notes",
        }
        field_key = field_map.get(callback_data.field)
        if not field_key:
            await safe_send_markdown(callback.message, "Не понял, что редактировать.")
            return
        r["step"] = reports.STEP_EDIT
        r["edit_field"] = field_key
        await safe_send_markdown(callback.message, reports.edit_prompt(field_key))
        return

    if act == "send":
        if not REPORT_TARGET_CHANNEL:
            await safe_send_markdown(callback.message, "⚠️ REPORT_TARGET_CHANNEL не настроен в .env")
            return

        ok_req, why = reports.validate_required(r)
        if not ok_req:
            await safe_send_markdown(callback.message, f"⚠️ {why}\nНажми «Править поля» и дополни.")
            return

        draft = reports.render_report_text(r, public_channel_url=PUBLIC_CHANNEL_URL)

        ok_mod, reason = await reports.moderate_text_openai(client, draft)
        if not ok_mod:
            await safe_send_markdown(callback.message, f"⚠️ Отчёт не прошёл модерацию: {reason}\nНажми «Править поля» и исправь текст.")
            return

        bot = callback.message.bot
        media = r.get("media") or []

        try:
            for item in media[:10]:
                if item["type"] == "photo":
                    await bot.send_photo(chat_id=REPORT_TARGET_CHANNEL, photo=item["file_id"])
                elif item["type"] == "video":
                    await bot.send_video(chat_id=REPORT_TARGET_CHANNEL, video=item["file_id"])

            await bot.send_message(chat_id=REPORT_TARGET_CHANNEL, text=draft, parse_mode="Markdown")

            reports.cancel_report(uid)
            await safe_send_markdown(callback.message, "✅ Отчёт опубликован в канале.")
        except Exception:
            logging.exception("send to channel failed")
            await safe_send_markdown(callback.message, "⚠️ Не удалось отправить в канал. Проверь права бота в канале.")
        return


@dp.message(F.location)
async def handle_location(message: Message):
    uid = message.from_user.id

    # 1) отчёты: гео
    if reports.has_active_report(uid):
        r = reports.get_report(uid)
        if r and r.get("step") in (reports.STEP_GEO, reports.STEP_EDIT):
            txt = reports.handle_report_location(r, message.location.latitude, message.location.longitude)
            if r.get("step") == reports.STEP_CONFIRM:
                draft = reports.render_report_text(r, public_channel_url=PUBLIC_CHANNEL_URL)
                await safe_send_markdown(message, "Черновик:\n\n" + draft, reply_markup=reports.keyboard_confirm_and_edit())
                return
            await safe_send_markdown(message, txt)
            return

    # 2) прогноз: гео (если ждём)
    pending = PENDING_FORECAST.get(uid)
    if pending:
        if not await subscription_gate(message):
            return

        day_offset = pending.get("day_offset", 0)
        query = pending.get("query", "")

        lat = message.location.latitude
        lon = message.location.longitude

        w = await forecast_by_coords(lat, lon, day_offset)
        if not w:
            await safe_send_markdown(message, "⚠️ Не смог получить прогноз по этой точке. Попробуй ещё раз или укажи ближайший населённый пункт.")
            return

        ctx = {"tool": "openweather_forecast_day_coords", "weather": w}
        ans = await assistant_text(client, user_id=uid, query=query, extra_context=ctx, temperature=0.45)
        await safe_send_markdown(message, ans)
        _pending_pop(uid)
        return


@dp.message(F.photo)
async def handle_photo(message: Message):
    uid = message.from_user.id
    caption = message.caption or ""

    # отчёт: фото без звездочки
    if reports.has_active_report(uid):
        r = reports.get_report(uid)
        if r and r.get("step") == reports.STEP_MEDIA:
            r["media"].append({"type": "photo", "file_id": message.photo[-1].file_id})
            await safe_send_markdown(message, "Фото добавлено. Ещё фото/видео или напиши `готово`.")
            return

    # ассистент: только если подпись начинается с *
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


@dp.message(F.video)
async def handle_video(message: Message):
    uid = message.from_user.id
    if reports.has_active_report(uid):
        r = reports.get_report(uid)
        if r and r.get("step") == reports.STEP_MEDIA:
            r["media"].append({"type": "video", "file_id": message.video.file_id})
            await safe_send_markdown(message, "Видео добавлено. Ещё или напиши `готово`.")
            return


@dp.message(F.text)
async def handle_text(message: Message):
    text = message.text or ""
    uid = message.from_user.id

    # 0) админ-команда для закрепа (если используешь)
    if ADMIN_IDS and uid in ADMIN_IDS and text.strip() == "*pin":
        await safe_send_markdown(message, "Команда *pin включена, но функция pin не вставлена в этот main.py.")
        return

    # 0b) Admin: принудительная публикация поста (для теста)
    if ADMIN_IDS and uid in ADMIN_IDS and text.strip() == "*post_now":
        bot = message.bot
        await safe_send_markdown(message, "⏳ Публикую тестовый пост в канал...")
        ok = await publish_daily_post(bot, client)
        if ok:
            await safe_send_markdown(message, f"✅ Пост опубликован в {CONTENT_CHANNEL}")
        else:
            await safe_send_markdown(message, "❌ Не удалось опубликовать. Проверь логи.")
        return

    # 0c) Admin: публикация анонса плана на месяц
    if ADMIN_IDS and uid in ADMIN_IDS and text.strip() == "*plan_now":
        bot = message.bot
        await safe_send_markdown(message, "⏳ Публикую анонс контент-плана...")
        ok = await publish_monthly_plan_preview(bot, client)
        if ok:
            await safe_send_markdown(message, f"✅ Анонс плана опубликован в {CONTENT_CHANNEL}")
        else:
            await safe_send_markdown(message, "❌ Не удалось опубликовать. Проверь логи.")
        return

    # 1) мастер отчёта
    if reports.has_active_report(uid):
        r = reports.get_report(uid)
        reply = await reports.handle_report_text_input(r, text)

        if r.get("step") == reports.STEP_CONFIRM:
            draft = reports.render_report_text(r, public_channel_url=PUBLIC_CHANNEL_URL)
            await safe_send_markdown(
                message,
                (reply or "Черновик:\n") + "\n\n" + draft,
                reply_markup=reports.keyboard_confirm_and_edit()
            )
        else:
            if reply:
                await safe_send_markdown(message, reply)
        return

    # 2) если мы ждём уточнение места для прогноза, и человек прислал просто текст без *
    pending = PENDING_FORECAST.get(uid)
    if pending and not text.startswith("*"):
        if not await subscription_gate(message):
            return

        place = text.strip()
        if len(place) < 2:
            await safe_send_markdown(message, "Напиши ближайший населённый пункт текстом или отправь геолокацию.")
            return

        query = pending.get("query", "")
        day_offset = pending.get("day_offset", 0)

        w = await get_weather_for_day(place, day_offset)
        if not w:
            await safe_send_markdown(message, "⚠️ Не нашёл погоду для этого пункта. Попробуй другой ближайший город или отправь геолокацию.")
            return

        ctx = {"tool": "openweather_forecast_day", "place": place, "weather": w}
        ans = await assistant_text(client, user_id=uid, query=query, extra_context=ctx, temperature=0.45)
        await safe_send_markdown(message, ans)
        _pending_pop(uid)
        return

    # 3) ассистент: только на *
    if not text.startswith("*"):
        return
    if not await subscription_gate(message):
        return

    query = text[1:].strip()
    if not query:
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    intent = classify_intent_ru(query)

    # === ПРОГНОЗ ===
    if intent == INTENT_FORECAST:
        # если похоже на водоём/река и нет явной "в <город>" — просим уточнение
        if looks_like_waterbody_query(query) and not extract_city_simple(query):
            day_offset = extract_day_offset_ru(query) or 0
            _pending_set(uid, query=query, day_offset=day_offset)
            await safe_send_markdown(
                message,
                "Понял, речь про водоём/реку.\n"
                "📍 Уточни место:\n"
                "- Напиши ближайший населённый пункт (следующим сообщением, без `*`), или\n"
                "- Отправь геолокацию (скрепка → Геопозиция).\n"
            )
            return

        place = extract_city_simple(query)
        if not place:
            day_offset = extract_day_offset_ru(query) or 0
            _pending_set(uid, query=query, day_offset=day_offset)
            await safe_send_markdown(
                message,
                "Укажи локацию (город/посёлок) или отправь геолокацию.\n"
                "Например: `*клев завтра в Москве`.\n"
                "Либо отправь точку на карте — я возьму прогноз по ближайшей местности."
            )
            return

        low = query.lower()
        if any(w in low for w in ["на 5", "5 дней", "пять дней", "на неделю", "неделю"]):
            days = await get_weather_5days(place)
            if not days:
                await safe_send_markdown(message, f"⚠️ Не нашёл погоду для: {place}. Попробуй ближайший город или отправь геолокацию.")
                return
            ctx = {"tool": "openweather_forecast_5d", "place": place, "days": days}
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

        w = await get_weather_for_day(place, day_offset)
        if not w:
            await safe_send_markdown(message, f"⚠️ Не нашёл погоду для: {place}. Попробуй ближайший город или отправь геолокацию.")
            return

        ctx = {"tool": "openweather_forecast_day", "place": place, "weather": w}
        ans = await assistant_text(client, user_id=uid, query=query, extra_context=ctx, temperature=0.45)
        await safe_send_markdown(message, ans)
        return

    # === ОСТАЛЬНОЕ ===
    ans = await assistant_text(client, user_id=uid, query=query, extra_context=None, temperature=0.65)
    await safe_send_markdown(message, ans)


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    """
    Планировщик контент-завода:
    - ежедневно в POST_HOUR_UTC:POST_MINUTE_UTC UTC публикует пост
    - 1-го числа каждого месяца публикует анонс контент-плана
    """
    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        publish_daily_post,
        trigger=CronTrigger(hour=POST_HOUR_UTC, minute=POST_MINUTE_UTC, timezone="UTC"),
        args=[bot, client],
        id="daily_post",
        name="Daily fishing post to channel",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    plan_hour = max(0, POST_HOUR_UTC - 1)
    scheduler.add_job(
        publish_monthly_plan_preview,
        trigger=CronTrigger(day=1, hour=plan_hour, minute=POST_MINUTE_UTC, timezone="UTC"),
        args=[bot, client],
        id="monthly_plan",
        name="Monthly content plan preview",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    logging.info(
        "Scheduler configured: daily post at %02d:%02d UTC, monthly plan on 1st at %02d:%02d UTC",
        POST_HOUR_UTC, POST_MINUTE_UTC, plan_hour, POST_MINUTE_UTC
    )
    return scheduler



async def on_startup(bot: Bot):
    """Webhook регистрируется при старте."""
    webhook_url = (os.getenv("WEBHOOK_URL") or "").strip()
    if not webhook_url:
        logging.error("WEBHOOK_URL не задан в окружении!")
        return
    await bot.set_webhook(
        url=webhook_url,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types(),
    )
    logging.info("Webhook set: %s", webhook_url)


async def on_shutdown(bot: Bot):
    await bot.delete_webhook()
    logging.info("Webhook deleted")


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, force=True)

    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    bot = Bot(token=TELEGRAM_TOKEN)
    scheduler = setup_scheduler(bot)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is Alive"))

    # aiogram обрабатывает входящие апдейты на /webhook
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    async def _start_scheduler(app):
        scheduler.start()
        logging.info(
            "Content factory started. Channel: %s, Post time: %02d:%02d UTC (%02d:%02d MSK)",
            CONTENT_CHANNEL, POST_HOUR_UTC, POST_MINUTE_UTC,
            (POST_HOUR_UTC + 3) % 24, POST_MINUTE_UTC,
        )

    async def _stop_scheduler(app):
        scheduler.shutdown()

    app.on_startup.append(_start_scheduler)
    app.on_cleanup.append(_stop_scheduler)

    port = int(os.getenv("PORT", 10000))
    logging.info("Starting on port %d (webhook mode)", port)
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
