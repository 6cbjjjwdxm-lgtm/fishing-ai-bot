import asyncio
import logging
import sys
import datetime
import requests
import os
from aiohttp import web # Добавили для веб-сервера

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message
from openai import OpenAI

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

# --- НАЧАЛО: Веб-сервер для Render ---
async def handle(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    # Render передает порт через переменную окружения PORT, по дефолту 10000
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
# --- КОНЕЦ: Веб-сервер для Render ---

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    sys.exit("Ошибка: не найдены TELEGRAM_TOKEN или OPENAI_API_KEY")

dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)
user_histories = {}

SYSTEM_PROMPT = """
Ты — элитный эксперт по спиннинговой рыбалке и администратор рыболовного чата.
Отвечаешь кратко, по делу, только когда пользователь обращается к тебе (сообщение начинается со *). 
Используешь данные погоды (температура, давление, ветер, фаза луны) для прогноза клева.
"""

def get_moon_phase():
    lunar_cycle = 29.53058867
    date = datetime.date.today()
    known_new_moon = datetime.date(2000, 1, 6)
    days_since = (date - known_new_moon).days
    pos = days_since % lunar_cycle
    if 0 <= pos < 1: return "Новолуние 🌑"
    if 1 <= pos < 7: return "Растущая луна 🌒"
    if 7 <= pos < 8: return "Первая четверть 🌓"
    if 8 <= pos < 14: return "Растущая 🌔"
    if 14 <= pos < 16: return "Полнолуние 🌕"
    if 16 <= pos < 22: return "Убывающая 🌖"
    if 22 <= pos < 23: return "Последняя четверть 🌗"
    return "Старая луна 🌘"

def get_weather_data(city: str) -> str | None:
    if not OPENWEATHER_API_KEY:
        return None
    try:
        url = (
            "http://api.openweathermap.org/data/2.5/weather"
            f"?q={city}&appid={OPENWEATHER_API_KEY}&units=metric&lang=ru"
        )
        r = requests.get(url).json()
        if str(r.get("cod")) != "200":
            return None
        temp = r["main"]["temp"]
        pressure_hpa = r["main"]["pressure"]
        pressure_mm = int(pressure_hpa * 0.75006)
        wind_speed = r["wind"]["speed"]
        deg = r["wind"].get("deg", 0)
        directions = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"]
        wind_dir = directions[int((deg / 45) + 0.5) % 8]
        desc = r["weather"][0]["description"]
        moon = get_moon_phase()
        return (
            f"ТЕХДАННЫЕ (г.{city}): Температура {temp}°C, "
            f"давление {pressure_mm} мм рт.ст., ветер {wind_speed} м/с ({wind_dir}), "
            f"погода: {desc}, луна: {moon}."
        )
    except Exception as e:
        logging.error(f"Weather API error: {e}")
        return None

def get_chat_response(user_id: int, user_text: str, weather_info: str = "") -> str:
    if user_id not in user_histories:
        user_histories[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    content_to_send = user_text
    if weather_info:
        user_histories[user_id].append(
            {"role": "system", "content": f"Актуальная сводка погоды: {weather_info}"}
        )
        content_to_send = (
            f"Проанализируй эти данные и дай прогноз клева для запроса: {user_text}"
        )
    user_histories[user_id].append({"role": "user", "content": content_to_send})
    if len(user_histories[user_id]) > 12:
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-10:]
    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=user_histories[user_id],
            temperature=0.7,
        )
        answer = resp.choices[0].message.content
        user_histories[user_id].append({"role": "assistant", "content": answer})
        return answer
    except Exception as e:
        return f"⚠ Ошибка нейросети: {e}"

@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Привет! Я ИИ по спиннингу.\n"
        "Чтобы обратиться ко мне, начни сообщение со `*`.\n"
        "Например:\n"
        "*Прогноз Дубна\n"
        "*Подбери спиннинг до 7000р"
    )

@dp.message()
async def handler(message: Message) -> None:
    text = message.text or ""
    if not text.startswith("*"):
        return

    user_id = message.from_user.id
    raw_text = text[1:].strip()
    if not raw_text:
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    weather_context = ""
    words = raw_text.split()
    triggers = ["прогноз", "клев", "погода"]
    if words and any(t in words[0].lower() for t in triggers):
        if len(words) > 1:
            city_candidate = "".join(filter(str.isalpha, words[1]))
            if len(city_candidate) > 2:
                wd = get_weather_data(city_candidate)
                if wd:
                    weather_context = wd

    answer = get_chat_response(user_id, raw_text, weather_context)
    await message.reply(answer, parse_mode="Markdown")

async def main() -> None:
    bot = Bot(token=TELEGRAM_TOKEN)
    print("Бот запускается...")
    # Запускаем веб-сервер и поллинг параллельно
    await start_web_server() 
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
