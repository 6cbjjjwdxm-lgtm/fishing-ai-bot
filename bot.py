import asyncio
import logging
import sys
import datetime
import requests
import os
import json
from typing import Dict, List, Optional
from aiohttp import web 
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from openai import OpenAI

# --- КОНФИГУРАЦИЯ ---
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    sys.exit("❌ ОШИБКА: Не найдены ключи в .env")

dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)
user_histories: Dict[int, List[Dict]] = {}

# --- ВЕБ-СЕРВЕР ---
async def start_web_server():
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="🎣 Expert Fishing Bot (Smart Intent) is Alive!"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# --- 1. AI АНАЛИЗАТОР (INTENT + GEO) ---
async def analyze_user_query(text: str) -> dict:
    """
    Определяет:
    1. Тип вопроса (forecast - нужен прогноз клева, general - просто вопрос/фото).
    2. Географию (если нужен прогноз).
    """
    system_prompt = """
    Ты — Аналитик запросов для рыболовного бота.
    Твоя задача — классифицировать запрос пользователя и извлечь данные.

    ТИПЫ ЗАПРОСОВ (intent):
    1. "forecast" — вопросы про КЛЕВ, РЫБАЛКУ в конкретном месте ("Как клев на Оке?", "Куда поехать завтра?").
    2. "general" — вопросы про снасти, приманки, анализ фото, советы без привязки к месту ("Что это за воблер?", "Как вязать узел?", "Оцени улов").

    ЕСЛИ intent="forecast":
    - Найди название водоема (location_name).
    - Подбери 3-5 крупных населенных пунктов (cities) ПРЯМО НА ВОДОЕМЕ для проверки погоды.
    
    ЕСЛИ intent="general":
    - Поля location_name и cities оставь пустыми.

    ВЕРНИ JSON:
    {
      "intent": "forecast" или "general",
      "location_name": "Название места" (или null),
      "cities": ["Город1", "Город2"] (или []),
      "day_offset": 0
    }
    """
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Запрос: {text}"}
            ],
            temperature=0,
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logging.error(f"Analyzer Error: {e}")
        return {"intent": "general"} # Fallback

# --- 2. ПОГОДА ---
def get_moon_phase():
    phases = ["🌑 Новолуние", "🌒 Растущая", "🌓 1-я четверть", "🌔 Растущая", "🌕 Полнолуние", "🌖 Убывающая", "🌗 Последняя четверть", "🌘 Старая"]
    days = (datetime.date.today() - datetime.date(2000, 1, 6)).days
    return phases[int(((days % 29.53) / 29.53) * 8) % 8]

def get_weather_forecast(city: str, day_offset: int) -> str | None:
    if not OPENWEATHER_API_KEY or not city: return None
    if day_offset > 2: day_offset = 2
    try:
        url = f"http://api.openweathermap.org/data/2.5/forecast?q={city}&appid={OPENWEATHER_API_KEY}&units=metric&lang=ru"
        r = requests.get(url, timeout=4).json()
        if str(r.get("cod")) != "200": return None

        target_date = datetime.date.today() + datetime.timedelta(days=day_offset)
        target_str = target_date.strftime("%Y-%m-%d")
        
        forecasts = r.get("list", [])
        day_data = [f for f in forecasts if target_str in f["dt_txt"]]
        
        # Если прогноз на сегодня уже прошел (вечер), берем ближайший
        if not day_data and day_offset == 0:
             day_data = forecasts[:3]

        if not day_data: return None
        
        best = next((f for f in day_data if "12:00" in f["dt_txt"]), day_data[0])
        return f"📅 {['Сегодня','Завтра','Послезавтра'][day_offset]} ({target_str})\n📍 {city}\n🌡 {best['main']['temp']}°C, {best['weather'][0]['description']}\n🔽 {int(best['main']['pressure']*0.75006)} мм рт.ст., 💨 {best['wind']['speed']} м/с\n🌙 {get_moon_phase()}"
    except:
        return None

# --- 3. GPT РЫБОЛОВ ---
SYSTEM_PROMPT = """
Ты — ПРОФЕССИОНАЛЬНЫЙ РЫБОЛОВНЫЙ ГИД. 2026 год.

ТВОЯ ЗАДАЧА:
1. Если вопрос про КЛЕВ/ПРОГНОЗ: Используй переданные данные о погоде и месте. Дай детальный прогноз (активность рыбы, снасти).
2. Если вопрос про ФОТО/СНАСТИ (без погоды): Просто проанализируй фото или ответь на вопрос экспертно. Не выдумывай погоду, если её нет.

ФОРМАТ ОТВЕТА (Для прогноза):
📍 **[Место]** (Погода: [Город])
🌡 **ВЕРДИКТ:** ...
🐟 **ПРОГНОЗ:** ...

ФОРМАТ ОТВЕТА (Для общих вопросов):
📝 **ЭКСПЕРТНОЕ МНЕНИЕ:**
(Твой ответ на вопрос или анализ приманки с фото).

---
Ни хвоста, ни чешуи! 🎣
"""

async def get_chat_response(user_id: int, text: str, weather: str, loc_name: str, intent: str, image_url: str = None) -> str:
    now = datetime.datetime.now()
    date_str = now.strftime("%d.%m.%Y")
    
    if user_id not in user_histories:
        user_histories[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Формируем промпт в зависимости от интента
    if intent == "forecast":
        user_prompt = f"ЗАПРОС НА ПРОГНОЗ: {text}\n📍 Место: {loc_name}\n📅 Дата: {date_str}\n"
        if weather: user_prompt += f"\n📊 ПОГОДА:\n{weather}"
        else: user_prompt += "\n(Погода не найдена. Дай общие советы)."
    else:
        user_prompt = f"ВОПРОС ЭКСПЕРТУ: {text}\n(Это общий вопрос, погода не требуется)."

    content_payload = [{"type": "text", "text": user_prompt}]
    if image_url: content_payload.append({"type": "image_url", "image_url": {"url": image_url}})

    user_histories[user_id].append({"role": "user", "content": content_payload})
    if len(user_histories[user_id]) > 8: 
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-6:]

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini", messages=user_histories[user_id], temperature=0.7, max_tokens=1000
        )
        answer = response.choices[0].message.content
        user_histories[user_id].append({"role": "assistant", "content": answer})
        return answer
    except: return "⚠️ Ошибка AI."

# --- ХЕНДЛЕРЫ ---
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("👋 Привет! Я разбираюсь и в клеве, и в снастях.\n`*Клев на Волге`\n`*Что это за воблер?` (пришли фото)")

@dp.callback_query(F.data.startswith("geo:"))
async def cb_geo_select(callback: CallbackQuery):
    try:
        _, city, loc, day = callback.data.split(":")
        day = int(day)
        
        weather = get_weather_forecast(city, day)
        if not weather: weather = "⚠️ Погода не найдена."
        
        await callback.message.edit_text(f"✅ Выбрано: {city}. Готовлю прогноз...")
        
        # Для кнопок интент всегда forecast
        response = await get_chat_response(callback.from_user.id, f"Клев на {loc}", weather, loc, "forecast")
        await callback.message.reply(response, parse_mode="Markdown")
    except:
        await callback.message.edit_text("⚠️ Ошибка.")

@dp.message(F.text.startswith("*") | F.caption.startswith("*"))
async def expert_fishing_handler(message: Message):
    full_text = message.caption if message.photo else message.text
    if not full_text: return
    
    query = full_text[1:].strip()
    await message.bot.send_chat_action(message.chat.id, "typing")

    # 1. Анализируем запрос
    analysis = await analyze_user_query(query)
    intent = analysis.get("intent", "general")
    
    # Сразу проверяем фото
    image_url = None
    if message.photo:
        f = await message.bot.get_file(message.photo[-1].file_id)
        image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{f.file_path}"

    # === ЛОГИКА ДЛЯ ПРОГНОЗА ===
    if intent == "forecast":
        cities = analysis.get("cities", [])
        loc_name = analysis.get("location_name", "Водоем")
        day_offset = analysis.get("day_offset", 0)
        
        if not cities:
            # Fallback
            weather = get_weather_forecast("Москва", day_offset)
            msg = f"⚠️ Не нашел место. Прогноз по Москве:\n{weather}"
            resp = await get_chat_response(message.from_user.id, query, msg, loc_name, "forecast", image_url)
            await message.reply(resp, parse_mode="Markdown")
            return

        if len(cities) > 1:
            kb = InlineKeyboardBuilder()
            for city in cities[:6]: 
                safe_loc = loc_name[:15]
                kb.button(text=city, callback_data=f"geo:{city}:{safe_loc}:{day_offset}")
            kb.adjust(2)
            await message.reply(f"📍 **{loc_name}**. Уточните место:", reply_markup=kb.as_markup())
            return

        # Один город
        city = cities[0]
        weather = get_weather_forecast(city, day_offset)
        if not weather: weather = f"⚠️ Погода в {city} не найдена."
        
        resp = await get_chat_response(message.from_user.id, query, weather, loc_name, "forecast", image_url)
        await message.reply(resp, parse_mode="Markdown")
    
    # === ЛОГИКА ДЛЯ ОБЩИХ ВОПРОСОВ (ФОТО, СНАСТИ) ===
    else:
        # Просто отправляем в GPT без погоды
        resp = await get_chat_response(message.from_user.id, query, "", "", "general", image_url)
        await message.reply(resp, parse_mode="Markdown")

async def main():
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(start_web_server())
    try: await dp.start_polling(bot)
    finally: await bot.session.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try: asyncio.run(main())
    except: pass




















