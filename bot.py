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
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ChatJoinRequest, ChatMemberUpdated, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ChatMemberStatus as Status
from openai import OpenAI

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
ZAJABRI_CHANNEL = "@zajabri"

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    sys.exit("❌ Не найдены ключи!")

dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)

user_histories: Dict[int, List[Dict]] = {}
subscribers: set[int] = set()
user_states: Dict[int, Dict] = defaultdict(dict)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', lambda req: web.Response(text="🎣 v2.0 OK!"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Port {port}")

class SmartParser:
    @staticmethod
    def parse_fishing_query(text: str) -> Tuple[Optional[str], Optional[int], str]:
        text_lower = text.lower()
        locations = {
            'москва', 'пахра', 'истринское', 'руза', 'можайка', 'десна', 'яуза', 
            'пехорка', 'рожайка', 'северка', 'нерская', 'ока', 'москва-река', 'нмр'
        }
        location = None
        day_offset = 0
        day_map = {'сегодня': 0, 'завтра': 1, 'послезавтра': 2}
        
        words = text_lower.split()
        for word in words:
            clean = re.sub(r'[^\w]', '', word)
            if clean in locations:
                location = clean.capitalize()
            if word in day_map:
                day_offset = day_map[word]
        
        if not location:
            for word in words:
                clean = re.sub(r'[^\w]', '', word)
                if len(clean) > 3 and clean not in {'клев', 'прогноз', 'погода'}:
                    location = clean.capitalize()
                    break
        
        return location, day_offset, text

async def check_subscription(bot: Bot, user_id: int, chat_id: int = None) -> bool:
    try:
        member = await bot.get_chat_member(ZAJABRI_CHANNEL, user_id)
        is_sub = member.status in [Status.MEMBER, Status.ADMINISTRATOR, Status.CREATOR]
        if is_sub:
            subscribers.add(user_id)
            return True
        elif chat_id:
            try:
                await bot.ban_chat_member(chat_id, user_id)
            except: pass
        return False
    except:
        return False

def get_moon_phase():
    lunar_cycle = 29.53058867
    date = datetime.date.today()
    known_new_moon = datetime.date(2000, 1, 6)
    days_since = (date - known_new_moon).days
    pos = days_since % lunar_cycle
    phases = ["🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘"]
    return phases[int(pos / 3.7)]

def get_weather_forecast(city: str, day_offset: int) -> str | None:
    if not OPENWEATHER_API_KEY or day_offset > 2: 
        return "LIMIT" if day_offset > 2 else None
    
    try:
        url = f"http://api.openweathermap.org/data/2.5/forecast?q={city}&appid={OPENWEATHER_API_KEY}&units=metric&lang=ru"
        r = requests.get(url, timeout=5).json()
        if str(r.get("cod")) != "200": return None

        target_date = datetime.date.today() + datetime.timedelta(days=day_offset)
        forecasts = r.get("list", [])
        day_forecasts = [f for f in forecasts if target_date.strftime("%Y-%m-%d") in f["dt_txt"]]
        forecast = day_forecasts[0] if day_forecasts else forecasts[0]
        
        temp = forecast["main"]["temp"]
        pressure_mm = int(forecast["main"]["pressure"] * 0.75006)
        wind_speed = forecast["wind"]["speed"]
        desc = forecast["weather"][0]["description"]
        moon = get_moon_phase()
        
        day_names = ["Сегодня", "Завтра", "Послезавтра"]
        return f"📍 {city}, {day_names[day_offset]}: {desc}. 🌡{temp}°C. 🔽{pressure_mm}мм. 💨{wind_speed}м/с. {moon}"
    except:
        return None

SYSTEM_PROMPT = """
Профи-спиннингист Подмосковья. Говоришь как "свой": "брат", "микруха", "НХНЧ". 

НИ "УДАЧИ"! Только "Ни хвоста, ни чешуи!"

Анализируй [ПОГОДА] для прогноза. Задавай уточнения если неясно.

Примеры:
"Пахра завтра? Давление ок, микдж 3г на голавля!"
"""

async def get_chat_response(user_id: int, text: str, weather: str = "", image_url: str = None) -> str:
    if user_id not in user_histories:
        user_histories[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    content = [{"type": "text", "text": text}]
    if weather and weather != "LIMIT":
        content[0]["text"] += f"\n\n[ПОГОДА: {weather}]"
    if image_url:
        content.append({"type": "image_url", "image_url": {"url": image_url}})
    
    user_histories[user_id].append({"role": "user", "content": content})
    if len(user_histories[user_id]) > 10:
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-8:]
    
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=user_histories[user_id],
            temperature=0.8,
            max_tokens=400
        )
        answer = resp.choices[0].message.content
        user_histories[user_id].append({"role": "assistant", "content": answer})
        return answer
    except:
        return "⚠️ Сбой... Попробуй еще! 🎣"

@dp.message(CommandStart())
async def start(message: Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="📍 Проверить @zajabri", callback_data="check_sub")
    await message.answer(
        "🎣 **Здарова!**\n\n"
        "AI-гид по клеву Подмосковья.\n"
        "• `*Клев Пахра завтра?*`\n"
        "• Фото рыбы = определение\n\n"
        "**Подпишись @zajabri** 👇",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "check_sub")
async def check_sub(cb):
    bot = cb.bot
    if await check_subscription(bot, cb.from_user.id):
        await cb.message.edit_text("✅ Готов к забросу! `*Клев Москва?*` 🎣")
    else:
        await cb.answer("❌ Подпишись @zajabri!", show_alert=True)

@dp.message(F.text.startswith("*") | F.photo)
async def handle_query(message: Message):
    text = message.caption if message.photo else message.text
    if not text or not text.startswith("*"):
        return

    user_id = message.from_user.id
    
    # Проверка подписки в группах
    if message.chat.type != "private":
        if not await check_subscription(message.bot, user_id, message.chat.id):
            return

    raw_text = text[1:].strip()
    await message.bot.send_chat_action(message.chat.id, "typing")

    # Парсинг
    location, day_offset, clean_text = SmartParser.parse_fishing_query(raw_text)
    weather = ""
    if location:
        w = get_weather_forecast(location, day_offset)
        weather = w if w else ""

    # Фото
    image_url = None
    if message.photo:
        file_info = await message.bot.get_file(message.photo[-1].file_id)
        image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"

    answer = await get_chat_response(user_id, clean_text, weather, image_url)
    await message.reply(answer)

@dp.chat_join_request()
async def join_request(request: ChatJoinRequest):
    if await check_subscription(request.bot, request.from_user.id):
        await request.bot.approve_chat_join_request(request.chat.id, request.from_user.id)

async def main():
    bot = Bot(token=TELEGRAM_TOKEN)
    print("🚀 v2.0...")
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(start_web_server())
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())








