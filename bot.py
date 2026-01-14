import asyncio
import logging
import sys
import datetime
import requests
import os
from aiohttp import web 

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message
from openai import OpenAI

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    sys.exit("Ошибка: не найдены TELEGRAM_TOKEN или OPENAI_API_KEY")

dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)
user_histories = {}

# --- Веб-сервер для Render ---
async def handle(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Web server started on port {port}")

# --- Логика бота ---
SYSTEM_PROMPT = """
Ты — элитный эксперт по спиннинговой рыбалке.
Твоя задача: давать прогноз клева на основе переданных данных погоды.
Анализируй давление (важна стабильность), ветер, температуру и фазу луны.
Отвечай кратко и по делу. Если данных о погоде нет — попроси пользователя уточнить город.
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

def get_weather_forecast(city: str, day_offset: int = 0) -> str | None:
    """
    Получает прогноз погоды на конкретный день.
    day_offset: 0 - сегодня, 1 - завтра, 2 - послезавтра.
    """
    if not OPENWEATHER_API_KEY:
        return None
    
    try:
        # Используем endpoint 'forecast' (прогноз на 5 дней)
        url = (
            "http://api.openweathermap.org/data/2.5/forecast"
            f"?q={city}&appid={OPENWEATHER_API_KEY}&units=metric&lang=ru"
        )
        r = requests.get(url).json()
        
        if str(r.get("cod")) != "200":
            return None

        # Определяем целевую дату
        target_date = datetime.date.today() + datetime.timedelta(days=day_offset)
        target_date_str = target_date.strftime("%Y-%m-%d")

        # Ищем прогноз на 12:00 или 15:00 этого дня (середина рыбалки)
        forecasts = r.get("list", [])
        best_forecast = None
        
        for item in forecasts:
            # item["dt_txt"] имеет формат "2023-10-05 12:00:00"
            if target_date_str in item["dt_txt"]:
                # Берем первый попавшийся прогноз на этот день (обычно утро/день)
                # Или стараемся найти день (12:00 / 15:00)
                if "12:00" in item["dt_txt"] or "15:00" in item["dt_txt"]:
                    best_forecast = item
                    break
                if best_forecast is None: # Если нет идеального времени, берем хоть какое-то
                    best_forecast = item

        if not best_forecast:
            return f"Нет данных прогноза для {city} на {target_date_str}."

        temp = best_forecast["main"]["temp"]
        pressure_hpa = best_forecast["main"]["pressure"]
        pressure_mm = int(pressure_hpa * 0.75006)
        wind_speed = best_forecast["wind"]["speed"]
        deg = best_forecast["wind"].get("deg", 0)
        directions = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"]
        wind_dir = directions[int((deg / 45) + 0.5) % 8]
        desc = best_forecast["weather"][0]["description"]
        moon = get_moon_phase()

        day_name = ["Сегодня", "Завтра", "Послезавтра"][day_offset] if day_offset < 3 else target_date_str

        return (
            f"ПРОГНОЗ ({city}, {day_name}): {desc.capitalize()}. "
            f"Температура {temp}°C. Давление {pressure_mm} мм рт.ст. "
            f"Ветер {wind_speed} м/с ({wind_dir}). Луна: {moon}."
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
            {"role": "system", "content": f"Данные погоды: {weather_info}"}
        )
        content_to_send = f"Дай прогноз клева, учитывая эти данные: {user_text}"
    
    user_histories[user_id].append({"role": "user", "content": content_to_send})
    
    if len(user_histories[user_id]) > 10:
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-8:]
        
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
        return "⚠ Ошибка ИИ. Попробуйте позже."

@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "🎣 Привет! Я ИИ-эксперт по рыбалке.\n"
        "Напиши `*` перед сообщением, чтобы спросить меня.\n"
        "Примеры:\n"
        "`*Клев Дубна завтра`\n"
        "`*Погода Москва послезавтра`"
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
    words = raw_text.lower().split()
    
    # Простая логика определения даты
    day_offset = 0
    if "завтра" in words:
        day_offset = 1
    elif "послезавтра" in words:
        day_offset = 2
    
    # Поиск города (очень простой: ищем слово с большой буквы в оригинале или второе слово)
    city_candidate = ""
    original_words = raw_text.split()
    if len(original_words) > 1:
        # Пытаемся найти город (обычно второе слово или то, что не "клев"/"прогноз")
        for w in original_words:
            clean_w = "".join(filter(str.isalpha, w))
            if len(clean_w) > 2 and clean_w.lower() not in ["клев", "прогноз", "погода", "завтра", "послезавтра", "сегодня"]:
                city_candidate = clean_w
                break
    
    if city_candidate:
        wd = get_weather_forecast(city_candidate, day_offset)
        if wd:
            weather_context = wd

    # Убираем Markdown форматирование для надежности
    answer = get_chat_response(user_id, raw_text, weather_context)
    await message.reply(answer)

async def main() -> None:
    bot = Bot(token=TELEGRAM_TOKEN)
    
    print("Удаляю старый вебхук...")
    await bot.delete_webhook(drop_pending_updates=True)
    
    print("Запускаю веб-сервер...")
    asyncio.create_task(start_web_server())

    print("Запускаю поллинг...")
    try:
        await dp.start_polling(bot)
    except Exception as e:
        print(f"Ошибка: {e}")
    finally:
        await bot.session.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())



