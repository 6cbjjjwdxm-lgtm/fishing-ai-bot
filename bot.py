import asyncio
import logging
import sys
import datetime
import requests
import os
import re
from typing import Dict, List, Optional, Tuple
from aiohttp import web 
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, 
    ChatJoinRequest, 
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.enums import ChatMemberStatus as Status
from aiogram.utils.keyboard import InlineKeyboardBuilder
from openai import OpenAI

# --- КОНФИГУРАЦИЯ ---
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
ZAJABRI_CHANNEL = "@zajabri"

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    sys.exit("❌ ОШИБКА: Не найдены ключи в .env")

dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)
user_histories: Dict[int, List[Dict]] = {}
subscribers: set[int] = set()

# --- БАЗА ВОДОЕМОВ (Ключи - части корней для поиска) ---
WATER_BODY_MAP = {
    'пахр': ('Пахра', ['Подольск', 'Домодедово', 'Ям']),
    'ок': ('Ока', ['Серпухов', 'Кашира', 'Коломна', 'Пущино']),  # "на Оке"
    'москв': ('Москва-река', ['Москва', 'Звенигород', 'Жуковский', 'Бронницы']),
    'нмр': ('Нижняя МР', ['Жуковский', 'Бронницы', 'Чулково']),
    'волг': ('Волга', ['Дубна', 'Кимры', 'Калязин', 'Тверь']),
    'истр': ('Истринское вдхр', ['Истра', 'Соколово', 'Пятница']),
    'руз': ('Рузское вдхр', ['Руза', 'Осташево']),
    'можай': ('Можайское вдхр', ['Можайск', 'Горетово']),
    'озерн': ('Озернинское вдхр', ['Руза', 'Нововолково']),
    'клязьм': ('Клязьма', ['Щелково', 'Ногинск', 'Орехово-Зуево']),
    'пехор': ('Пехорка', ['Балашиха', 'Люберцы', 'Жуковский']),
    'сенеж': ('Сенеж', ['Солнечногорск']),
    'иваньк': ('Иваньковское вдхр', ['Дубна', 'Конаково']),
    'рыбин': ('Рыбинское вдхр', ['Рыбинск', 'Борок', 'Весьегонск']),
    'северк': ('Северка', ['Домодедово', 'Барыбино']),
    'нерск': ('Нерская', ['Воскресенск', 'Куровское']),
    'бисеров': ('Бисерово', ['Старая Купавна'])
}

# --- ВЕБ-СЕРВЕР ---
async def start_web_server():
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="🎣 Expert Fishing Bot v2.4 (Fix) is Alive!"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# --- ПАРСЕР ---
class SmartParser:
    @staticmethod
    def parse_fishing_query(text: str) -> Tuple[Optional[str], Optional[list], Optional[int], str]:
        text_lower = text.lower()
        
        found_name = None
        nearby_cities = None
        
        # 1. Умный поиск по корням (Пахр, Ок, Волг)
        for root, (full_name, cities) in WATER_BODY_MAP.items():
            if root in text_lower:
                found_name = full_name
                nearby_cities = cities
                break
        
        # 2. Определение дня
        day_offset = 0
        day_map = {'сегодня': 0, 'завтра': 1, 'послезавтра': 2, 'после': 2}
        words = text_lower.split()
        for word in words:
            clean = re.sub(r'[^\w]', '', word)
            if clean in day_map:
                day_offset = day_map[clean]

        # 3. Если водоем не найден, пробуем найти город напрямую
        if not found_name:
            popular_locs = {'москва', 'химки', 'дубна', 'серпухов', 'подольск', 'шатура'}
            for word in words:
                clean = re.sub(r'[^\w]', '', word)
                if clean in popular_locs:
                    found_name = clean.capitalize()
                    nearby_cities = [] # Это город, уточнять не надо

        return found_name, nearby_cities, day_offset, text

# --- ПОГОДА ---
def get_moon_phase():
    phases = ["🌑 Новолуние", "🌒 Растущая", "🌓 1-я четверть", "🌔 Растущая", "🌕 Полнолуние", "🌖 Убывающая", "🌗 Последняя четверть", "🌘 Старая"]
    lunar_cycle = 29.53
    days_since = (datetime.date.today() - datetime.date(2000, 1, 6)).days
    index = int(((days_since % lunar_cycle) / lunar_cycle) * 8) % 8
    return phases[index]

def get_weather_forecast(city: str, day_offset: int) -> str | None:
    if not OPENWEATHER_API_KEY: return None
    if day_offset > 2: return "LIMIT"
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
        
        temp = best["main"]["temp"]
        pressure = int(best["main"]["pressure"] * 0.75006)
        wind = best["wind"]["speed"]
        desc = best["weather"][0]["description"]
        moon = get_moon_phase()
        
        dates = ["Сегодня", "Завтра", "Послезавтра"]
        return f"📅 {dates[day_offset]} ({target_str})\n📍 {city}\n🌡 {temp}°C, {desc}\n🔽 {pressure} мм рт.ст., 💨 {wind} м/с\n🌙 Луна: {moon}"
    except:
        return None

# --- GPT ---
SYSTEM_PROMPT = """
Ты — ПРОФЕССИОНАЛЬНЫЙ РЫБОЛОВНЫЙ ГИД. Сейчас 2026 год.
Твоя задача — дать РАЗВЕРНУТЫЙ и ПОЛЕЗНЫЙ прогноз клева. Не пиши общие фразы.

🛑 ТВОЯ ЛОГИКА:
1. ПОГОДА: Обязательно проанализируй переданные цифры! (Пример: "Давление 760 — высокое, щука будет пассивна, ищи судака").
2. СЕЗОН: Если температура ниже 0 — это ЗИМА (лед). Пиши про мормышки, жерлицы, толщину лески, глубины.
3. КОНКРЕТИКА: Называй конкретные цвета приманок (например, "кислотный зелёный", "машинное масло"), веса джиг-головок.

ФОРМАТ ОТВЕТА:
📍 **[Место] | [Дата] | [Температура]**

🌥 **АНАЛИЗ ПОГОДЫ:**
(Как погода влияет на рыбу сегодня. Давление, ветер, луна).

🐟 **КТО И КАК КЛЮЕТ:**
> **Щука:** (Активность ?/10). Где стоит.
> **Судак:** (Активность ?/10).
> **Окунь:** (Активность ?/10).

⚙️ **СНАСТИ И ПРИМАНКИ:**
• Снасти: (Удочка, леска)
• Приманки: (Конкретные виды и цвета)

🎯 **ТАКТИКА ПОИСКА:**
Где бурить/кидать? (Бровки, поливы, коряжник).

---
Ни хвоста, ни чешуи! 🎣
"""

async def get_chat_response(user_id: int, text: str, weather: str = "", image_url: str = None) -> str:
    now = datetime.datetime.now()
    date_str = now.strftime("%d.%m.%Y")
    
    if user_id not in user_histories:
        user_histories[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    user_prompt = f"Вопрос: {text}\n📅 СЕГОДНЯ: {date_str}\n"
    
    if weather:
        user_prompt += f"\n📊 ТОЧНАЯ ПОГОДА:\n{weather}\n\nВАЖНО: Построй прогноз, опираясь на эти цифры!"
    else:
        user_prompt += "\n(Погода неизвестна. Ориентируйся на сезон! Сейчас ЗИМА!)"

    content_payload = [{"type": "text", "text": user_prompt}]
    if image_url: content_payload.append({"type": "image_url", "image_url": {"url": image_url}})

    user_histories[user_id].append({"role": "user", "content": content_payload})
    # Ротация
    if len(user_histories[user_id]) > 8: 
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-6:]

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini", 
            messages=user_histories[user_id], 
            temperature=0.7, # Чуть больше креатива
            max_tokens=1000
        )
        answer = response.choices[0].message.content
        user_histories[user_id].append({"role": "assistant", "content": answer})
        return answer
    except Exception:
        return "⚠️ Рыба сорвалась (Ошибка нейросети)."

# --- ХЕНДЛЕРЫ ---
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("👋 Привет! Я твой рыболовный гид.\nСпроси меня: `*Клев на Пахре`")

@dp.callback_query(F.data.startswith("loc:"))
async def cb_city_select(callback: CallbackQuery):
    # data: loc:Подольск:Пахра:0
    try:
        _, city, river, day_off = callback.data.split(":")
        day_offset = int(day_off)
        
        # Получаем погоду
        weather = get_weather_forecast(city, day_offset)
        if not weather: weather = get_weather_forecast("Москва", day_offset)
        
        await callback.message.edit_text(f"✅ Выбрано: {city}. Забрасываю удочки...")
        
        prompt = f"Прогноз клева на реке {river} (район г. {city})"
        response = await get_chat_response(callback.from_user.id, prompt, weather)
        await callback.message.reply(response, parse_mode="Markdown")
    except:
        await callback.message.edit_text("⚠️ Ошибка.")

@dp.message(F.text.startswith("*") | F.caption.startswith("*"))
async def expert_fishing_handler(message: Message):
    full_text = message.caption if message.photo else message.text
    if not full_text: return
    
    # 1. Парсинг
    query = full_text[1:].strip()
    loc_name, nearby_cities, day_offset, _ = SmartParser.parse_fishing_query(query)
    
    print(f"DEBUG: Query='{query}' -> Loc='{loc_name}', Cities={nearby_cities}")

    weather_info = ""
    
    # 2. Если это РЕКА и есть список городов -> КНОПКИ
    if loc_name and nearby_cities:
        kb = InlineKeyboardBuilder()
        for city in nearby_cities:
            kb.button(text=city, callback_data=f"loc:{city}:{loc_name}:{day_offset}")
        kb.adjust(2)
        await message.reply(f"📍 **{loc_name}**. Уточните место для точной погоды:", reply_markup=kb.as_markup())
        return

    # 3. Если это ГОРОД или просто неизвестное место
    if loc_name:
        # Пытаемся найти погоду для этого места
        weather_info = get_weather_forecast(loc_name, day_offset)
    
    # ФОЛЛБЭК: Если погоды нет, берем Москву
    if not weather_info:
        w_msk = get_weather_forecast("Москва", day_offset)
        if w_msk:
            weather_info = f"⚠️ (Погода по месту не найдена, даю Москву):\n{w_msk}"

    # 4. Ответ GPT
    image_url = None
    if message.photo:
        file = await message.bot.get_file(message.photo[-1].file_id)
        image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}"
        
    await message.bot.send_chat_action(message.chat.id, "typing")
    response = await get_chat_response(message.from_user.id, query, weather_info, image_url)
    await message.reply(response, parse_mode="Markdown")

# --- ЗАПУСК ---
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














