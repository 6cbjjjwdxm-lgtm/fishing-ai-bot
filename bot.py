import asyncio
import datetime
import json
import logging
import os
import sys
import time
from typing import Dict, List, Optional

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ВАЖНО: Используем AsyncOpenAI
from openai import AsyncOpenAI
import scraper  # Наш новый модуль

# =========================
# CONFIG
# =========================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    sys.exit("❌ ОШИБКА: Не найдены токены в .env")

dp = Dispatcher()
# Асинхронный клиент OpenAI
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

user_histories: Dict[int, List[Dict]] = {}

# Глобальный кэш мест (загружаем при старте)
PLACES_CACHE = {}

# =========================
# UTILS
# =========================
async def safe_send_markdown(message: Message, text: str):
    try:
        await message.reply(text, parse_mode="Markdown")
    except TelegramBadRequest:
        await message.reply(text)

async def safe_edit_markdown(message: Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    except TelegramBadRequest:
        await message.edit_text(text, reply_markup=reply_markup)

def extract_day_offset(text: str) -> int:
    t = (text or "").lower()
    if "послезавтра" in t: return 2
    if "завтра" in t: return 1
    return 0

# =========================
# WEATHER
# =========================
def get_moon_phase() -> str:
    phases = ["🌑 Новолуние", "🌒 Растущая", "🌓 1-я четверть", "🌔 Растущая", "🌕 Полнолуние", "🌖 Убывающая", "🌗 Последняя четверть", "🌘 Старая"]
    days = (datetime.date.today() - datetime.date(2000, 1, 6)).days
    return phases[int(((days % 29.53) / 29.53) * 8) % 8]

async def get_weather_forecast(city: str, day_offset: int) -> Optional[str]:
    if not OPENWEATHER_API_KEY or not city: return None
    if day_offset > 2: day_offset = 2

    # ВАЖНО: Добавляем страну RU, чтобы не искать Oke в Нигерии
    # Если город уже содержит запятую (напр "Москва, RU"), не дублируем
    q_param = city if "," in city else f"{city},RU"

    url = "https://api.openweathermap.org/data/2.5/forecast"
    params = {"q": q_param, "appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "ru"}

    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as r:
                if r.status != 200:
                    # Попытка №2 без RU (для редких случаев)
                    if "RU" in q_param:
                        params["q"] = city
                        async with session.get(url, params=params) as r2:
                            if r2.status != 200: return None
                            data = await r2.json()
                    else:
                        return None
                else:
                    data = await r.json()
    except Exception:
        return None

    target_date = datetime.date.today() + datetime.timedelta(days=day_offset)
    target_str = target_date.strftime("%Y-%m-%d")
    forecasts = data.get("list", [])
    
    # Ищем прогноз на 12:00 нужного дня
    day_data = [f for f in forecasts if target_str in f.get("dt_txt", "")]
    if not day_data: 
        # Если нет конкретного дня, берем ближайший
        if not forecasts: return None
        best = forecasts[0]
    else:
        best = next((f for f in day_data if "12:00" in f.get("dt_txt", "")), day_data[0])

    temp = best["main"]["temp"]
    pressure = int(best["main"]["pressure"] * 0.75006)
    wind = best["wind"]["speed"]
    desc = best["weather"][0]["description"]
    moon = get_moon_phase()

    return (
        f"📍 {city} | {target_str}\n"
        f"🌡 Темп: {temp:.1f}°C ({desc})\n"
        f"🔽 Давление: {pressure} мм рт.ст.\n"
        f"💨 Ветер: {wind} м/с\n"
        f"🌙 Луна: {moon}"
    )

# =========================
# AI LOGIC
# =========================
SYSTEM_PROMPT = """
Ты — ЭЛИТНЫЙ РЫБОЛОВНЫЙ ГИД (Стаж 30 лет).
Дай прогноз, используя данные Русфишинга и погоду.

ШАБЛОН ОТВЕТА:
🌥 **АНАЛИЗ ПОГОДЫ:** ...
🐟 **КТО И КАК КЛЮЕТ:**
> **Щука:** ...
> **Судак:** ...
> **Окунь:** ...
⚙️ **СНАСТИ И ПРИМАНКИ:** ...
🎯 **ТАКТИКА ПОИСКА:** ...
---
Ни хвоста, ни чешуи! 🎣
"""

async def analyze_user_query(text: str) -> dict:
    """Определяем намерение + выделяем название реки/водоема"""
    system = "Извлеки intent (forecast/fish_search/general) и location_name (именительный падеж). JSON."
    try:
        # Асинхронный вызов!
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
            temperature=0
        )
        return json.loads(response.choices[0].message.content)
    except Exception:
        return {"intent": "general"}

async def get_chat_response(user_id: int, text: str, weather: str, loc_name: str, intent: str) -> str:
    if user_id not in user_histories:
        user_histories[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    user_prompt = f"ЗАПРОС: {text}\nПОГОДА: {weather}\n"
    
    # Добавляем контекст диалога
    history = user_histories[user_id]
    history.append({"role": "user", "content": user_prompt})
    
    # Ограничиваем историю
    if len(history) > 8:
        history = [history[0]] + history[-6:]

    try:
        # Асинхронный вызов!
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=history,
            temperature=0.7
        )
        answer = response.choices[0].message.content
        history.append({"role": "assistant", "content": answer})
        user_histories[user_id] = history
        return answer
    except Exception as e:
        logging.error(f"AI Error: {e}")
        return "⚠️ ИИ задумался и не ответил. Попробуй еще раз."

# =========================
# HANDLERS
# =========================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("👋 Привет! Я использую базу Русфишинга.\nНапиши: `*клев на Оке` или `*клев на Можайке`")

@dp.callback_query(F.data.startswith("loc:"))
async def cb_location_select(callback: CallbackQuery):
    # data: loc:RiverName:PlaceName:DayOffset
    try:
        await callback.answer()
        _, river, place, day_s = callback.data.split(":")
        day = int(day_s)
        
        await safe_edit_markdown(callback.message, f"✅ Выбрано: {place} ({river}). Анализирую...")

        # Параллельно запускаем погоду и подготовку промпта
        weather_task = asyncio.create_task(get_weather_forecast(place, day))
        # Здесь в будущем можно добавить asyncio.create_task(scraper.get_forum_context(...))
        
        weather = await weather_task or f"⚠️ Погода для {place} не найдена."
        
        # Генерируем ответ
        response = await get_chat_response(
            callback.from_user.id,
            f"Клев на {river} в районе {place}",
            weather,
            river,
            "forecast"
        )
        
        await safe_send_markdown(callback.message, response)
        
    except Exception as e:
        logging.exception("Error in callback")
        await safe_send_markdown(callback.message, "⚠️ Ошибка обработки.")

@dp.message((F.text & F.text.startswith("*")) | (F.caption & F.caption.startswith("*")))
async def main_handler(message: Message):
    text = message.caption or message.text
    query = text[1:].strip()
    
    # Визуально показываем, что бот "печатает", но не блокируем код
    await message.bot.send_chat_action(message.chat.id, "typing")

    day_offset = extract_day_offset(query)
    
    # 1. Анализ интента (LLM)
    analysis = await analyze_user_query(query)
    intent = analysis.get("intent", "general")
    loc_name = analysis.get("location_name", "").title() # Нормализуем регистр

    # 2. Если прогноз - ищем в кэше Русфишинга
    if intent == "forecast":
        # Пробуем найти реку в нашем кэше (нечеткий поиск можно добавить позже)
        # Сейчас ищем точное вхождение или "содержится в"
        found_river = None
        for key in PLACES_CACHE:
            if loc_name in key or key in loc_name:
                found_river = key
                break
        
        if found_river:
            locations = PLACES_CACHE[found_river].get("locations", [])
            
            # Строим клавиатуру с местами
            kb = InlineKeyboardBuilder()
            # Берем топ-14 мест, чтобы не перегружать
            for loc in locations[:14]:
                kb.button(text=loc, callback_data=f"loc:{found_river}:{loc}:{day_offset}")
            kb.adjust(2)
            
            await message.reply(
                f"📍 **{found_river}**. Выберите популярное место (данные Русфишинга):",
                reply_markup=kb.as_markup(),
                parse_mode="Markdown"
            )
            return
        
        # Если реки нет в кэше — fallback на город (как раньше) или общий ответ
        # Тут можно оставить логику "Если не нашел реку, считаем что это город"
        weather = await get_weather_forecast(loc_name, day_offset)
        if weather:
             resp = await get_chat_response(message.from_user.id, query, weather, loc_name, "forecast")
             await safe_send_markdown(message, resp)
             return

    # Общий ответ (болталка)
    resp = await get_chat_response(message.from_user.id, query, "", "", "general")
    await safe_send_markdown(message, resp)

# =========================
# BACKGROUND TASKS
# =========================
async def periodic_cache_update():
    """Обновляем базу каждые 24 часа"""
    global PLACES_CACHE
    while True:
        # Обновляем раз в сутки
        await asyncio.sleep(24 * 3600) 
        try:
            logging.info("⏳ Фоновое обновление базы...")
            new_cache = await scraper.update_rusfishing_cache()
            if new_cache:
                PLACES_CACHE = new_cache
        except Exception as e:
            logging.error(f"Background update failed: {e}")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is Alive"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# =========================
# MAIN
# =========================
async def main():
    global PLACES_CACHE
    
    # 1. Загружаем кэш сразу
    PLACES_CACHE = scraper.load_cache()
    logging.info(f"Loaded {len(PLACES_CACHE)} rivers from cache.")

    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)

    # 2. Запускаем фоновые задачи
    asyncio.create_task(start_web_server())
    asyncio.create_task(periodic_cache_update())

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass










