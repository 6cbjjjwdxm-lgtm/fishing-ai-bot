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
from aiogram.types import Message, ChatMemberUpdated, ChatMemberStatus
from aiogram.enums import ChatMemberStatus as Status
from openai import OpenAI

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
ZAJABRI_CHANNEL = "@zajabri"  # Основной канал для проверки

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    sys.exit("❌ Не найдены TELEGRAM_TOKEN или OPENAI_API_KEY")

dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)

# Базы данных в памяти (можно заменить на Redis/PostgreSQL)
user_histories: Dict[int, List[Dict]] = {}
subscribers: set[int] = set()  # Подписчики @zajabri
user_states: Dict[int, Dict] = defaultdict(dict)  # Состояния диалога

# --- Веб-сервер ---
async def handle(request):
    return web.Response(text="🎣 Smart Fishing Bot v2.0 is alive!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Web server: {port}")

# --- УМНЫЙ ПАРСЕР ЗАПРОСОВ ---
class SmartParser:
    @staticmethod
    def parse_fishing_query(text: str) -> Tuple[Optional[str], Optional[int], str]:
        """Парсит запросы типа: 'Клев москва сегодня', 'Как клюет на Пахре завтра?'"""
        text_lower = text.lower()
        
        # Локации (расширенный список Подмосковья)
        locations = {
            'москва', 'пахра', 'истринское', 'руза', 'можайка', 'десна', 'яуза', 
            'пехорка', 'рожайка', 'северка', 'нерская', 'ока', 'москва-река', 'нмр'
        }
        location = None
        
        # Время
        day_map = {'сегодня': 0, 'завтра': 1, 'послезавтра': 2, 'завтрашний': 1}
        day_offset = 0
        
        words = text_lower.split()
        for i, word in enumerate(words):
            # Проверяем локацию
            clean_word = re.sub(r'[^\w]', '', word)
            if clean_word in locations:
                location = clean_word.capitalize()
                break
            
            # Проверяем день
            if word in day_map:
                day_offset = day_map[word]
        
        # Если локацию не нашли, берем первую "непонятную" слово
        if not location:
            for word in words:
                clean = re.sub(r'[^\w]', '', word)
                if len(clean) > 3 and clean not in {'клев', 'прогноз', 'погода', 'будет', 'сегодня', 'завтра'}:
                    location = clean.capitalize()
                    break
        
        return location, day_offset, text

# --- ПРОВЕРКА ПОДПИСКИ ---
async def check_subscription(bot: Bot, user_id: int, chat_id: int) -> bool:
    """Проверяет подписку на @zajabri"""
    try:
        member = await bot.get_chat_member(ZAJABRI_CHANNEL, user_id)
        is_subscribed = member.status in [Status.MEMBER, Status.ADMINISTRATOR, Status.CREATOR]
        
        if is_subscribed:
            subscribers.add(user_id)
            return True
        else:
            # Удаляем из чата если не подписан
            try:
                await bot.ban_chat_member(chat_id, user_id)
                await bot.send_message(
                    chat_id, 
                    f"🚫 @{ZAJABRI_CHANNEL} отписался от канала!\n"
                    "Подпишись снова → получишь доступ к боту! 🎣"
                )
            except:
                pass
            return False
    except:
        return False

async def monitor_subscriptions(bot: Bot, event: ChatMemberUpdated):
    """Мониторит изменения в чате"""
    user_id = event.from_user.id
    chat_id = event.chat.id
    
    # Новый участник - проверяем подписку
    if event.new_chat_member.status == Status.MEMBER:
        if not await check_subscription(bot, user_id, chat_id):
            await asyncio.sleep(2)  # Даем время пользователю
            await check_subscription(bot, user_id, chat_id)
    
    # Ушел/заблокирован - убираем из подписчиков
    elif event.new_chat_member.status in [Status.LEFT, Status.KICKED]:
        subscribers.discard(user_id)

# --- УЛУЧШЕННЫЙ ПРОГНОЗ ПОГОДЫ ---
def get_weather_forecast(city: str, day_offset: int) -> str | None:
    if not OPENWEATHER_API_KEY: return None
    if day_offset > 2: return "LIMIT_EXCEEDED"
    
    try:
        url = f"http://api.openweathermap.org/data/2.5/forecast?q={city}&appid={OPENWEATHER_API_KEY}&units=metric&lang=ru"
        r = requests.get(url, timeout=5).json()
        if str(r.get("cod")) != "200": return None

        target_date = datetime.date.today() + datetime.timedelta(days=day_offset)
        target_date_str = target_date.strftime("%Y-%m-%d")
        forecasts = r.get("list", [])
        day_forecasts = [item for item in forecasts if target_date_str in item["dt_txt"]]
        
        best_forecast = day_forecasts[0] if day_forecasts else forecasts[0]
        temp = best_forecast["main"]["temp"]
        pressure_hpa = best_forecast["main"]["pressure"]
        pressure_mm = int(pressure_hpa * 0.75006)
        wind_speed = best_forecast["wind"]["speed"]
        wind_deg = best_forecast["wind"].get("deg", 0)
        desc = best_forecast["weather"][0]["description"]
        moon = get_moon_phase()
        
        dirs = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"]
        wind_dir = dirs[int((wind_deg / 45) + 0.5) % 8]
        
        day_names = ["Сегодня", "Завтра", "Послезавтра"]
        day_label = day_names[day_offset] if day_offset < 3 else target_date_str

        return f"📍 {city}, {day_label}: {desc.capitalize()}. 🌡 {temp}°C. 🔽 {pressure_mm} мм рт.ст. 💨 {wind_speed} м/с ({wind_dir}). {moon}"
    except:
        return None

def get_moon_phase():
    lunar_cycle = 29.53058867
    date = datetime.date.today()
    known_new_moon = datetime.date(2000, 1, 6)
    days_since = (date - known_new_moon).days
    pos = days_since % lunar_cycle
    phases = ["🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘"]
    return phases[int(pos / 3.7)]

# --- GPT ЧАТ ---
SYSTEM_PROMPT = """
Ты — профи спиннингист Подмосковья. Общайся как "свой в доску": "братан", "микруха", "палка", "НХНЧ".

**НИКОГДА НЕ ГОВОРИ "УДАЧИ"** — только "Ни хвоста, ни чешуи!" (НХНЧ).

**Анализируй [ПОГОДУ] для прогноза клева.** Если локация/дата неясны — **ЗАДАВАЙ УТОЧНЕНИЯ**.

Примеры ответов:
- "На Пахре завтра? Давление норм, но ветерок — бери микдж 3г, голавль на мушек клюнет!"
- "Истринское послезавтра? Луна растет, судак на джиг 7-10г. Но уточни — верх или низ?"

Будь **КОНКРЕТНЫМ** по снастям и тактике.
"""

async def get_chat_response(user_id: int, user_text: str, weather_info: str = "", image_url: str = None) -> str:
    if user_id not in user_histories:
        user_histories[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    user_content = [{"type": "text", "text": user_text}]
    if weather_info:
        user_content[0]["text"] += f"\n\n[ПОГОДА: {weather_info}]"
    if image_url:
        user_content.append({"type": "image_url", "image_url": {"url": image_url}})
    
    user_histories[user_id].append({"role": "user", "content": user_content})
    
    if len(user_histories[user_id]) > 12:
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-10:]
    
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",  # Дешевле и быстрее
            messages=user_histories[user_id],
            temperature=0.8,
            max_tokens=500
        )
        answer = resp.choices[0].message.content
        user_histories[user_id].append({"role": "assistant", "content": answer})
        return answer
    except Exception as e:
        logging.error(f"GPT Error: {e}")
        return "⚠️ Сбой в блесне... Попробуй еще раз! 🎣"

# --- ХЕНДЛЕРЫ ---
@dp.message(CommandStart())
async def start(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📍 Проверить подписку", callback_data="check_sub")]
    ])
    await message.answer(
        "🎣 **Здарова, спиннингист!**\n\n"
        "Я твой AI-гид по клеву Подмосковья.\n"
        "• Спрашивай про клев на любой речке\n"
        "• Кидай фото рыбы — определю\n"
        "• Советую снасти под погоду\n\n"
        "**Сначала подпишись на @zajabri**\n"
        "Или жми кнопку ниже 👇",
        reply_markup=keyboard
    )

@dp.callback_query(F.data == "check_sub")
async def check_sub(callback):
    if await check_subscription(callback.bot, callback.from_user.id, callback.message.chat.id):
        await callback.message.edit_text("✅ Подписка ОК! Теперь спрашивай про клев! 🎣")
    else:
        await callback.answer("❌ Подпишись на @zajabri!", show_alert=True)

@dp.message()
async def smart_handler(message: Message):
    # Игнорируем если не подписан
    if message.chat.type != "private" and message.from_user.id not in subscribers:
        return
    
    text = message.caption if message.photo else message.text
    if not text or not text.startswith("*"):
        return

    user_id = message.from_user.id
    raw_text = text[1:].strip()

    await message.bot.send_chat_action(message.chat.id, "typing")
    
    # 🧠 УМНЫЙ ПАРСИНГ
    location, day_offset, clean_text = SmartParser.parse_fishing_query(raw_text)
    
    # Погода если нужно
    weather_context = ""
    if location and any(word in raw_text.lower() for word in ["клев", "прогноз", "погода"]):
        weather = get_weather_forecast(location, day_offset)
        if weather:
            weather_context = weather
        elif weather == "LIMIT_EXCEEDED":
            await message.reply("🗓️ Вангуй только на 3 дня вперед! 🎣")
            return

    # Фото
    image_url = None
    if message.photo:
        photo_id = message.photo[-1].file_id
        file_info = await message.bot.get_file(photo_id)
        image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"

    # GPT
    answer = await get_chat_response(user_id, clean_text, weather_context, image_url)
    await message.reply(answer)

# МОНИТОРИНГ ЧАТА
@dp.chat_join_request()
async def on_join_request(request):
    if await check_subscription(request.bot, request.from_user.id, request.chat.id):
        await request.bot.approve_chat_join_request(request.chat.id, request.from_user.id)

@dp.my_chat_member()
async def on_chat_member_update(event: ChatMemberUpdated):
    await monitor_subscriptions(event.bot, event)

async def main():
    bot = Bot(token=TELEGRAM_TOKEN)
    print("🚀 Smart Fishing Bot v2.0 starting...")
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(start_web_server())
    
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())






