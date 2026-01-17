import asyncio
import logging
import sys
import datetime
import requests
import os
import re
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from aiohttp import web 
from dotenv import load_dotenv

# --- AIOGRAM 3.x IMPORTS ---
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

# Инициализация
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)

# Память
user_histories: Dict[int, List[Dict]] = {}
subscribers: set[int] = set()

# --- БАЗА ЗНАНИЙ: ВОДОЕМ -> ГОРОДА РЯДОМ ---
WATER_BODY_MAP = {
    'пахра': ['Подольск', 'Домодедово', 'Ленинская'],
    'ока': ['Серпухов', 'Кашира', 'Коломна', 'Пущино'],
    'москва-река': ['Москва', 'Звенигород', 'Жуковский', 'Бронницы'],
    'нмр': ['Жуковский', 'Бронницы', 'Чулково'], # Нижняя Москва Река
    'волга': ['Дубна', 'Кимры', 'Калязин', 'Тверь'],
    'истринское': ['Истра', 'Соколово', 'Пятница'],
    'рузское': ['Руза', 'Осташево'],
    'руза': ['Руза', 'Осташево'],
    'можайское': ['Можайск', 'Горетово'],
    'можайка': ['Можайск', 'Горетово'],
    'озернинское': ['Руза', 'Нововолково'],
    'озерна': ['Руза', 'Нововолково'],
    'клязьма': ['Щелково', 'Ногинск', 'Орехово-Зуево'],
    'пехорка': ['Балашиха', 'Люберцы', 'Жуковский'],
    'сенеж': ['Солнечногорск'],
    'иваньковское': ['Дубна', 'Конаково'],
    'иванька': ['Дубна', 'Конаково'],
    'рыбинка': ['Рыбинск', 'Борок', 'Весьегонск'],
    'рыбинское': ['Рыбинск', 'Борок', 'Весьегонск'],
    'северка': ['Домодедово', 'Барыбино', 'Коломна'],
    'нерская': ['Воскресенск', 'Куровское'],
    'бисерово': ['Старая Купавна']
}

# --- ВЕБ-СЕРВЕР ---
async def start_web_server():
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="🎣 Expert Fishing Bot v2.3 (Geo-Smart) is Alive!"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Web server started on port {port}")

# --- 1. ПАРСЕР ---
class SmartParser:
    @staticmethod
    def parse_fishing_query(text: str) -> Tuple[Optional[str], Optional[int], str]:
        text_lower = text.lower()
        
        location = None
        day_offset = 0 
        day_map = {'сегодня': 0, 'завтра': 1, 'послезавтра': 2, 'после': 2}
        
        words = text_lower.split()
        
        # 1. Сначала ищем в нашей базе водоемов
        for key in WATER_BODY_MAP.keys():
            if key in text_lower: # Ищем вхождение фразы (например "на пахре")
                location = key.capitalize() # Сохраняем "Пахра"
                break
        
        # 2. Если не нашли в базе, ищем обычные города или слова с большой буквы
        if not location:
            popular_locs = {'москва', 'химки', 'дубна', 'серпухов', 'подольск', 'шатура'}
            for word in words:
                clean = re.sub(r'[^\w]', '', word)
                if clean in popular_locs:
                    location = clean.capitalize()
                if clean in day_map:
                    day_offset = day_map[clean]

        # 3. Эвристика (если совсем ничего не нашли)
        if not location:
            ignore = {'клев', 'прогноз', 'погода', 'рыбалка', 'будет', 'скажи', 'как', 'где', 'когда', 'на', 'в'}
            for word in words:
                clean = re.sub(r'[^\w]', '', word)
                if len(clean) > 3 and clean not in ignore and clean not in day_map:
                    location = clean.capitalize()
                    break

        # Проверка дня еще раз (если не нашли в цикле)
        for word in words:
             clean = re.sub(r'[^\w]', '', word)
             if clean in day_map:
                day_offset = day_map[clean]

        return location, day_offset, text

# --- 2. ПОДПИСКА ---
async def check_subscription(bot: Bot, user_id: int, chat_id: int = None) -> bool:
    try:
        member = await bot.get_chat_member(ZAJABRI_CHANNEL, user_id)
        is_subscribed = member.status in [Status.MEMBER, Status.ADMINISTRATOR, Status.CREATOR]
        if is_subscribed: subscribers.add(user_id)
        return is_subscribed
    except Exception:
        return True 

# --- 3. ПОГОДА ---
def get_moon_phase():
    phases = ["🌑 Новолуние", "🌒 Растущая", "🌓 Первая четверть", "🌔 Растущая", "🌕 Полнолуние", "🌖 Убывающая", "🌗 Последняя четверть", "🌘 Старая"]
    lunar_cycle = 29.53
    date = datetime.date.today()
    known_new_moon = datetime.date(2000, 1, 6)
    days_since = (date - known_new_moon).days
    index = int(((days_since % lunar_cycle) / lunar_cycle) * 8) % 8
    return phases[index]

def get_weather_forecast(city: str, day_offset: int) -> str | None:
    if not OPENWEATHER_API_KEY: return None
    if day_offset > 2: return "LIMIT"
    try:
        url = f"http://api.openweathermap.org/data/2.5/forecast?q={city}&appid={OPENWEATHER_API_KEY}&units=metric&lang=ru"
        r = requests.get(url, timeout=5).json()
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
        return f"📅 {dates[day_offset]} ({target_str})\n📍 {city}\n🌡 {temp}°C, {desc}\n🔽 {pressure} мм рт.ст., 💨 {wind} м/с\n🌙 {moon}"
    except Exception as e:
        logging.error(f"Weather error: {e}")
        return None

# --- 4. GPT ЛОГИКА ---
SYSTEM_PROMPT = """
Ты — ЭЛИТНЫЙ РЫБОЛОВНЫЙ ГИД. Сейчас 2026 год.
Твоя задача — дать прогноз клева, основываясь НА ДАТЕ и ПОГОДЕ.

🛑 ПРАВИЛА:
1. ЕСЛИ ЗИМА (январь-март, декабрь): Пиши про лед, жерлицы, мотыля, мормышки.
2. ЕСЛИ ЛЕТО: Пиши про спиннинг, фидер.
3. Не желай "удачи", только "Ни хвоста, ни чешуи!".

ФОРМАТ:
📍 **[Место] | [Дата]**
🎣 **АКТИВНОСТЬ РЫБЫ:** (Кого ловим)
⚙️ **СНАСТИ:** (Актуальные для сезона)
🎯 **ГДЕ ИСКАТЬ:** (Ямы, бровки)
"""

async def get_chat_response(user_id: int, text: str, weather: str = "", image_url: str = None) -> str:
    now = datetime.datetime.now()
    date_str = now.strftime("%d.%m.%Y")
    
    if user_id not in user_histories:
        user_histories[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    user_prompt = f"Вопрос: {text}\n📅 ДАТА: {date_str}\n"
    if weather: user_prompt += f"\n📊 ПОГОДА:\n{weather}\n"
    else: user_prompt += "\n(Погода неизвестна, ориентируйся на сезон!)"

    content_payload = [{"type": "text", "text": user_prompt}]
    if image_url: content_payload.append({"type": "image_url", "image_url": {"url": image_url}})

    user_histories[user_id].append({"role": "user", "content": content_payload})
    if len(user_histories[user_id]) > 10: user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-8:]

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini", messages=user_histories[user_id], temperature=0.5, max_tokens=900
        )
        answer = response.choices[0].message.content
        user_histories[user_id].append({"role": "assistant", "content": answer})
        return answer
    except Exception:
        return "⚠️ Ошибка связи с нейросетью."

# --- 5. ОБРАБОТЧИКИ ---

@dp.message(CommandStart())
async def cmd_start(message: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Проверить подписку", callback_data="check_subscription")
    await message.answer("👋 Привет! Спроси меня про клев через `*`.\nПример: `*Клев на Пахре`", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "check_subscription")
async def cb_check_sub(callback: CallbackQuery):
    if await check_subscription(callback.bot, callback.from_user.id):
        await callback.message.edit_text("✅ Доступ открыт! Спрашивай.")
    else:
        await callback.answer("❌ Сначала подписка!", show_alert=True)

# Хендлер для уточнения города (нажатие на кнопку)
@dp.callback_query(F.data.startswith("loc:"))
async def cb_city_select(callback: CallbackQuery):
    # data формата: "loc:Подольск:Пахра:0" (город:река:день)
    try:
        _, city, river_name, day_off_str = callback.data.split(":")
        day_offset = int(day_off_str)
        
        # Получаем погоду для выбранного города
        weather = get_weather_forecast(city, day_offset)
        if not weather: weather = get_weather_forecast("Москва", day_offset) # Fallback
        
        await callback.message.edit_text(f"✅ Выбрано: {city}. Анализирую...")
        
        # Запрос к GPT с контекстом реки и города
        prompt = f"Клев на водоеме {river_name} (район г. {city})"
        response = await get_chat_response(callback.from_user.id, prompt, weather)
        await callback.message.reply(response, parse_mode="Markdown")
        
    except Exception as e:
        logging.error(f"Error in city select: {e}")
        await callback.message.edit_text("⚠️ Ошибка выбора города.")

@dp.message(F.text.startswith("*") | F.caption.startswith("*"))
async def expert_fishing_handler(message: Message):
    full_text = message.caption if message.photo else message.text
    if not full_text: return
    
    if message.chat.type != "private":
        if not await check_subscription(message.bot, message.from_user.id, message.chat.id): return

    await message.bot.send_chat_action(message.chat.id, "typing")
    query_text = full_text[1:].strip()
    
    # 1. Парсим
    location, day_offset, clean_text = SmartParser.parse_fishing_query(query_text)
    
    weather_info = ""
    buttons_needed = False
    
    if location:
        # 2. Пробуем получить погоду напрямую (вдруг это город, например "Дубна")
        weather_info = get_weather_forecast(location, day_offset)
        
        # 3. Если погоды нет (значит это река типа "Пахра"), ищем в базе городов
        if not weather_info:
            loc_lower = location.lower()
            if loc_lower in WATER_BODY_MAP:
                nearby_cities = WATER_BODY_MAP[loc_lower]
                
                # Создаем кнопки
                kb = InlineKeyboardBuilder()
                for city in nearby_cities:
                    # loc:Город:Река:День
                    kb.button(text=city, callback_data=f"loc:{city}:{location}:{day_offset}")
                kb.adjust(2)
                
                await message.reply(
                    f"📍 **{location}** — водоем большой. \nГде именно смотрим погоду?", 
                    reply_markup=kb.as_markup()
                )
                return # ПРЕРЫВАЕМ выполнение, ждем нажатия кнопки

    # 4. Если локация неизвестна или это город без кнопок -> даем ответ сразу
    # Если погоды все еще нет, берем Москву
    if not weather_info and weather_info != "LIMIT":
        default_w = get_weather_forecast("Москва", day_offset)
        if default_w:
             weather_info = f"⚠️ (Локация точная не найдена, погода по Москве):\n{default_w}"

    image_url = None
    if message.photo:
        file = await message.bot.get_file(message.photo[-1].file_id)
        image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}"

    response = await get_chat_response(message.from_user.id, clean_text, weather_info, image_url)
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












