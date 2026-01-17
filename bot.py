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
    app.router.add_get('/', lambda r: web.Response(text="🎣 Expert Fishing Bot (Russia + Buttons) is Alive!"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# --- 1. AI ГЕО-КОДЕР С ВАРИАНТАМИ ---
async def extract_geo_variants(text: str) -> dict:
    """
    Возвращает список городов для уточнения.
    Пример: {"location_name": "Река Ахтуба", "cities": ["Харабали", "Ахтубинск"], "day": 0}
    """
    system_geo = """
    Ты — Geo-аналитик. Пользователь спрашивает про рыбалку.
    Твоя задача:
    1. Понять название места/водоема.
    2. Подобрать 2-3 КРУПНЫХ города рядом (где точно есть метеостанции) для уточнения погоды.
    3. Если пользователь уже указал конкретный город — верни только его в списке.
    4. Если это маленькая деревня — верни райцентры рядом.

    ВЕРНИ ТОЛЬКО JSON:
    {
      "location_name": "Название места (как в запросе)",
      "cities": ["Город1", "Город2"],
      "day_offset": 0 (0-сегодня, 1-завтра, 2-послезавтра)
    }
    """
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_geo},
                {"role": "user", "content": f"Запрос: {text}"}
            ],
            temperature=0,
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)
    except:
        return {}

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
        if not day_data: return None
        
        best = next((f for f in day_data if "12:00" in f["dt_txt"]), day_data[0])
        return f"📅 {['Сегодня','Завтра','Послезавтра'][day_offset]} ({target_str})\n📍 {city}\n🌡 {best['main']['temp']}°C, {best['weather'][0]['description']}\n🔽 {int(best['main']['pressure']*0.75006)} мм рт.ст., 💨 {best['wind']['speed']} м/с\n🌙 {get_moon_phase()}"
    except:
        return None

# --- 3. GPT РЫБОЛОВ ---
SYSTEM_PROMPT = """
Ты — ПРОФЕССИОНАЛЬНЫЙ РЫБОЛОВНЫЙ ГИД ПО РОССИИ. Сейчас 2026 год.
Твоя задача — дать глубокий прогноз клева.

🛑 ЛОГИКА:
1. АНАЛИЗ ПОГОДЫ: Обязательно используй цифры (давление, ветер) в анализе.
2. РЕГИОН: Учитывай специфику водоема (Волга, Карелия, Сибирь).
3. СЕЗОН: Если мороз — ЗИМА (лед, мормышки). Если тепло — ЛЕТО.

ФОРМАТ:
📍 **[Место] | [Дата]**

🌡 **ПОГОДНЫЙ ВЕРДИКТ:**
(Как погода влияет на рыбу).

🐟 **ПРОГНОЗ:**
> **[Рыба 1]:** Активность, тактика.
> **[Рыба 2]:** Активность, тактика.

⚙️ **СНАСТИ:**
(Подбери под сезон и место).

---
Ни хвоста, ни чешуи! 🎣
"""

async def get_chat_response(user_id: int, text: str, weather: str, loc_name: str, image_url: str = None) -> str:
    now = datetime.datetime.now()
    date_str = now.strftime("%d.%m.%Y")
    
    if user_id not in user_histories:
        user_histories[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    user_prompt = f"Вопрос: {text}\n📍 Место: {loc_name}\n📅 СЕГОДНЯ: {date_str}\n"
    if weather: user_prompt += f"\n📊 ПОГОДА:\n{weather}"
    else: user_prompt += "\n(Погода не найдена, дай общие советы)."

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
    await message.answer("👋 Привет! Спроси про любой водоем России: `*Клев на Ахтубе`")

# Обработка выбора города (кнопки)
@dp.callback_query(F.data.startswith("geo:"))
async def cb_geo_select(callback: CallbackQuery):
    # geo:Город:Локация:День
    try:
        _, city, loc, day = callback.data.split(":")
        day = int(day)
        
        weather = get_weather_forecast(city, day)
        if not weather: 
            weather = "⚠️ Погода не найдена, но вот прогноз по сезону."
        
        await callback.message.edit_text(f"✅ Выбрано: {city}. Готовлю прогноз...")
        
        prompt = f"Клев на {loc} (погода по г. {city})"
        response = await get_chat_response(callback.from_user.id, prompt, weather, loc)
        await callback.message.reply(response, parse_mode="Markdown")
    except:
        await callback.message.edit_text("⚠️ Ошибка обработки.")

@dp.message(F.text.startswith("*") | F.caption.startswith("*"))
async def expert_fishing_handler(message: Message):
    full_text = message.caption if message.photo else message.text
    if not full_text: return
    
    query = full_text[1:].strip()
    await message.bot.send_chat_action(message.chat.id, "typing")

    # 1. AI ищет города
    geo = await extract_geo_variants(query)
    cities = geo.get("cities", [])
    loc_name = geo.get("location_name", "Водоем")
    day_offset = geo.get("day_offset", 0)

    # 2. Логика кнопок
    if not cities:
        # AI не понял место -> Просто отвечаем общими фразами (Москва фоллбэк)
        weather = get_weather_forecast("Москва", day_offset)
        msg = f"⚠️ Не нашел такое место на карте. Даю общий прогноз (погода Мск):\n{weather}"
        resp = await get_chat_response(message.from_user.id, query, msg, loc_name)
        await message.reply(resp, parse_mode="Markdown")
        return

    if len(cities) > 1:
        # Несколько вариантов -> КНОПКИ
        kb = InlineKeyboardBuilder()
        for city in cities:
            # geo:City:Loc:Day (обрезаем Loc если длинный, Telegram limit 64 bytes)
            safe_loc = loc_name[:15]
            kb.button(text=city, callback_data=f"geo:{city}:{safe_loc}:{day_offset}")
        kb.adjust(2)
        await message.reply(f"📍 **{loc_name}**. Уточните ближайший город:", reply_markup=kb.as_markup())
        return

    # Один город -> Сразу ответ
    city = cities[0]
    weather = get_weather_forecast(city, day_offset)
    if not weather: weather = f"⚠️ Погода в {city} не найдена."
    
    img = None
    if message.photo:
        f = await message.bot.get_file(message.photo[-1].file_id)
        img = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{f.file_path}"

    resp = await get_chat_response(message.from_user.id, query, weather, loc_name, img)
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
















