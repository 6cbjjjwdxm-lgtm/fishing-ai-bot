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
    app.router.add_get('/', lambda r: web.Response(text="🎣 Expert Fishing Bot (Pro Mode) is Alive!"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# --- 1. AI АНАЛИЗАТОР (УМНЫЙ ГЕОГРАФ) ---
async def analyze_user_query(text: str) -> dict:
    """
    Классифицирует запрос и ищет ТОЧНЫЕ города на реке.
    """
    system_prompt = """
    Ты — Рыболовный Аналитик. Твоя задача — понять суть вопроса.
    
    1. INTENT (Тип): "forecast" (прогноз клева) или "general" (общий вопрос/фото).
    
    2. ЕСЛИ forecast, найди:
       - location_name: Название водоема.
       - cities: Список из 3-4 населенных пунктов, стоящих ПРЯМО НА БЕРЕГУ этого водоема.
         ВАЖНО: Не предлагай города, которые далеко от воды!
         Пример: Для Пахры -> ["Подольск", "с. Ям", "п. Володарского", "Зеленая Слобода"]. (Люберцы НЕЛЬЗЯ, они далеко).
         Пример: Для Оки -> ["Серпухов", "Кашира", "Коломна"].
       - day_offset: 0 (сегодня), 1 (завтра), 2 (послезавтра).

    ВЕРНИ JSON:
    {
      "intent": "forecast" / "general",
      "location_name": "...",
      "cities": ["..."],
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
            temperature=0.2,
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)
    except:
        return {"intent": "general"}

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
        if not day_data and day_offset == 0: day_data = forecasts[:3]
        if not day_data: return None
        
        best = next((f for f in day_data if "12:00" in f["dt_txt"]), day_data[0])
        return f"📅 {['Сегодня','Завтра','Послезавтра'][day_offset]} ({target_str})\n📍 {city}\n🌡 {best['main']['temp']}°C, {best['weather'][0]['description']}\n🔽 {int(best['main']['pressure']*0.75006)} мм рт.ст., 💨 {best['wind']['speed']} м/с\n🌙 {get_moon_phase()}"
    except: return None

# --- 3. GPT РЫБОЛОВ (ЭКСПЕРТ) ---
SYSTEM_PROMPT = """
Ты — ПРОФЕССИОНАЛЬНЫЙ РЫБОЛОВНЫЙ ЭКСПЕРТ. Стаж 30 лет.
Твой стиль: Уверенный, конкретный, без воды. Ты говоришь как бывалый мужик, но культурно.

🛑 ЗАПРЕТЫ:
1. НИКОГДА не пиши "Удачи". Это оскорбление! Пиши только "Ни хвоста, ни чешуи!" или "НХНЧ!".
2. Не пиши общие фразы типа "рыба может клевать". Пиши конкретно: "Щука стоит в ямах", "Окунь активен".

ТВОЯ ЗАДАЧА (ПРОГНОЗ):
1. Проанализируй погоду (давление, ветер). Если давление скачет — скажи, что клев будет трудовой.
2. Дай расклад по 3 рыбам: Щука, Судак, Окунь (или Белая рыба, если уместно).
3. Снасти: Указывай конкретные веса, цвета, толщину лески. (Зимой — мормышки, жерлицы. Летом — воблеры).

ФОРМАТ ОТВЕТА:
📍 **[Место]** (Погода: [Город])
🌡 **ВЕРДИКТ:** (Короткий и честный вывод: жор или глухозимье?)

🐟 **ЧТО ПО РЫБЕ:**
> **Щука:** (Активность ?/10). Где стоит, на что брать.
> **Судак:** (Активность ?/10). Тактика поиска.
> **Окунь:** (Активность ?/10).

⚙️ **АРСЕНАЛ:**
• Снасти: ...
• Приманки: (Назови 2-3 уловистых цвета/модели).

🎯 **СОВЕТ БЫВАЛОГО:**
(Хитрость или тактика для этого дня).

---
НХНЧ! 🎣
"""

async def get_chat_response(user_id: int, text: str, weather: str, loc_name: str, intent: str, image_url: str = None) -> str:
    now = datetime.datetime.now()
    date_str = now.strftime("%d.%m.%Y")
    
    if user_id not in user_histories:
        user_histories[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    if intent == "forecast":
        user_prompt = f"ЗАПРОС ПРОГНОЗА: {text}\n📍 Водоем: {loc_name}\n📅 Дата: {date_str}\n"
        if weather: user_prompt += f"\n📊 ДАННЫЕ ПОГОДЫ:\n{weather}\n(Опирайся на эти цифры!)"
        else: user_prompt += "\n(Погода не найдена. Дай прогноз по сезону)."
    else:
        user_prompt = f"ВОПРОС: {text}\n(Погода не нужна. Ответь как эксперт)."

    content_payload = [{"type": "text", "text": user_prompt}]
    if image_url: content_payload.append({"type": "image_url", "image_url": {"url": image_url}})

    user_histories[user_id].append({"role": "user", "content": content_payload})
    if len(user_histories[user_id]) > 8: 
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-6:]

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini", messages=user_histories[user_id], temperature=0.7, max_tokens=1100
        )
        answer = response.choices[0].message.content
        user_histories[user_id].append({"role": "assistant", "content": answer})
        return answer
    except: return "⚠️ Ошибка AI."

# --- ХЕНДЛЕРЫ ---
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("👋 Привет, рыбак! Спрашивай честно — отвечу честно.\n`*Клев на Пахре`\n`*Оцени воблер` (с фото)")

@dp.callback_query(F.data.startswith("geo:"))
async def cb_geo_select(callback: CallbackQuery):
    try:
        _, city, loc, day = callback.data.split(":")
        day = int(day)
        
        weather = get_weather_forecast(city, day)
        if not weather: weather = "⚠️ Погода не найдена."
        
        await callback.message.edit_text(f"✅ Точка: {city}. Анализирую...")
        
        response = await get_chat_response(callback.from_user.id, f"Клев на {loc}", weather, loc, "forecast")
        await callback.message.reply(response, parse_mode="Markdown")
    except:
        await callback.message.edit_text("⚠️ Сбой.")

@dp.message(F.text.startswith("*") | F.caption.startswith("*"))
async def expert_fishing_handler(message: Message):
    full_text = message.caption if message.photo else message.text
    if not full_text: return
    
    query = full_text[1:].strip()
    await message.bot.send_chat_action(message.chat.id, "typing")

    # 1. Анализ
    analysis = await analyze_user_query(query)
    intent = analysis.get("intent", "general")
    
    image_url = None
    if message.photo:
        f = await message.bot.get_file(message.photo[-1].file_id)
        image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{f.file_path}"

    # 2. Прогноз
    if intent == "forecast":
        cities = analysis.get("cities", [])
        loc_name = analysis.get("location_name", "Водоем")
        day_offset = analysis.get("day_offset", 0)
        
        if not cities:
            weather = get_weather_forecast("Москва", day_offset)
            msg = f"⚠️ Не нашел точное место. Вот прогноз по Москве:\n{weather}"
            resp = await get_chat_response(message.from_user.id, query, msg, loc_name, "forecast", image_url)
            await message.reply(resp, parse_mode="Markdown")
            return

        if len(cities) > 1:
            kb = InlineKeyboardBuilder()
            for city in cities[:6]: 
                safe_loc = loc_name[:15]
                kb.button(text=city, callback_data=f"geo:{city}:{safe_loc}:{day_offset}")
            kb.adjust(2)
            await message.reply(f"📍 **{loc_name}**. Уточните точку ловли:", reply_markup=kb.as_markup())
            return

        city = cities[0]
        weather = get_weather_forecast(city, day_offset)
        if not weather: weather = f"⚠️ Погода в {city} не найдена."
        
        resp = await get_chat_response(message.from_user.id, query, weather, loc_name, "forecast", image_url)
        await message.reply(resp, parse_mode="Markdown")
    
    # 3. Общий вопрос
    else:
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






















