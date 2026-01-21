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
    # ... (начало функции) ...

    # Список вариантов поиска (от точного к общему)
    # 1. "Ям, Moscow Oblast, RU" (Самый точный для МО)
    # 2. "Ям, RU" (По всей России)
    # 3. "Ям" (Как есть)
    
    queries = [
        f"{city}, Moscow Oblast, RU", 
        f"{city}, RU", 
        city
    ]
    
    url = "https://api.openweathermap.org/data/2.5/forecast"
    base_params = {"appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "ru"}

    timeout = aiohttp.ClientTimeout(total=5)
    
    async with aiohttp.ClientSession(timeout=timeout) as session:
        data = None
        # Пробуем варианты по очереди
        for q in queries:
            params = base_params.copy()
            params["q"] = q
            try:
                async with session.get(url, params=params) as r:
                    if r.status == 200:
                        data = await r.json()
                        # Если нашли - отлично, выходим из цикла
                        if data.get("cod") == "200":
                            break
            except Exception:
                continue
        
        if not data: return None

    # ... (дальше парсинг JSON как обычно) ...

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
    
    await message.bot.send_chat_action(message.chat.id, "typing")

    day_offset = extract_day_offset(query)
    
    # 1. Анализ интента
    analysis = await analyze_user_query(query)
    intent = analysis.get("intent", "general")
    # Принудительно чистим название от мусора
    raw_loc = analysis.get("location_name", "").strip()
    
    # 2. Логика поиска в кэше
    if intent == "forecast":
        found_river = None
        
        # Нормализация для поиска: "Оке" -> "ока", "Иваньковском" -> "иваньковск"
        # Простой способ: перебор ключей кэша и проверка вхождения В НИХ (или ИХ в запрос)
        
        # Создаем мапу нормализованных ключей для поиска
        # "ока": "Ока", "москва": "Москва-река", "можай": "Можайское вдхр"
        
        search_term = raw_loc.lower()
        
        for key in PLACES_CACHE:
            k_low = key.lower()
            # 1. Точное совпадение (ока == ока)
            if k_low == search_term:
                found_river = key
                break
            # 2. Ключ внутри запроса ("можайское" в "можайское вдхр")
            if k_low in search_term and len(k_low) > 3:
                found_river = key
                break
            # 3. Запрос внутри ключа ("оке" - плохой пример, но "можайка" -> "можайское")
            # Тут сложнее. Давайте надеяться на GPT, который должен вернуть именительный падеж.
            # Если GPT вернул "Оке", а у нас "Ока" -> slice it
            
            # Эвристика: сравниваем первые 3 буквы, если слово короткое
            if len(search_term) >= 3 and k_low.startswith(search_term[:3]):
                 # "Оке" starts "Ока" (нет), но "Иваньковск" starts "Ива"
                 # Для коротких слов (Ока, Дон) нужно точное совпадение или GPT должен дать "Ока"
                 pass
                 
        # Если "в лоб" не нашли, пробуем хак для падежей (удаляем последнюю букву у запроса)
        if not found_river and len(search_term) > 3:
             sub = search_term[:-1] # "Дубн" из "Дубне"
             for key in PLACES_CACHE:
                 if sub in key.lower():
                     found_river = key
                     break

        # Если все еще не нашли, но это Ока/Оке (GPT часто лажает с падежом для коротких слов)
        if not found_river:
             if "ок" in search_term and len(search_term) <= 4: found_river = "Ока"
             if "дон" in search_term: found_river = "Дон"

        if found_river:
            locations = PLACES_CACHE[found_river].get("locations", [])
            kb = InlineKeyboardBuilder()
            for loc in locations[:14]:
                kb.button(text=loc, callback_data=f"loc:{found_river}:{loc}:{day_offset}")
            kb.adjust(2)
            
            await message.reply(
                f"📍 **{found_river}**. Выберите место (база Русфишинга):",
                reply_markup=kb.as_markup(),
                parse_mode="Markdown"
            )
            return
        
        # FALLBACK: Если реки нет в кэше — считаем это городом.
        # ВАЖНО: Используем raw_loc (из GPT), но если он кривой ("на Оке"), OpenWeather ошибется.
        # Лучше честно сказать, что не нашли в базе рек.
        
        weather = await get_weather_forecast(raw_loc, day_offset)
        if weather:
             resp = await get_chat_response(message.from_user.id, query, weather, raw_loc, "forecast")
             await safe_send_markdown(message, resp)
             return
        else:
             # Если погода не нашлась (или город кривой), пишем юзеру
             await safe_send_markdown(message, f"⚠️ Не нашел водоем **{raw_loc}** в базе и погоду для него. Попробуйте уточнить название (например: *клев в Серпухове).")
             return

    # Общий ответ
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











