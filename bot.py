import asyncio
import logging
import sys
import datetime
import requests
import os
from aiohttp import web 

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
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
    return web.Response(text="🎣 Fishing Bot is alive!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Web server started on port {port}")

# --- ЭКСПЕРТНЫЙ СИСТЕМНЫЙ ПРОМПТ ---
SYSTEM_PROMPT = """
Ты — профессиональный рыболовный гид и эксперт по водоемам Центральной России (особенно Подмосковье).
Твоя страсть — спиннинг на малых реках, но ты разбираешься и в фидере, и в зимней рыбалке.

**ГЛАВНОЕ ПРАВИЛО:** 
НИКОГДА НЕ ЖЕЛАЙ "УДАЧИ"! Это плохая примета для рыбака.
Вместо этого всегда используй фразу: **"Ни хвоста, ни чешуи!"** (или "НХНЧ").

**ТВОИ ЗНАНИЯ И ПРИНЦИПЫ:**

1. **ПРОГНОЗ КЛЕВА:**
   - Даешь прогноз ТОЛЬКО на основе переданных данных погоды (давление, ветер, температура, луна).
   - Если данных нет — честно скажи: "Не вижу погоды. Уточни город и дату (сегодня/завтра/послезавтра)".

2. **ЛИМИТ ПРОГНОЗА:** Точный прогноз возможен только на 3 дня (сегодня, завтра, послезавтра).

3. **ЭКСПЕРТИЗА ПО ВОДОЕМАМ (Подмосковье):**
   - *Малые реки (Пахра, Рожайка, Северка, Нерская):* Голавль, окунь, щука. Спиннинг (ультралайт), нахлыст.
   - *Десна, Моча, Пехорка:* Зимний спиннинг, щука в траве.
   - *НМР (Нижняя Москва-река):* Главный зимний полигон. Судак, щука, жерех.
   - *Ока:* Перекаты (голавль), ямы (судак, лещ).
   - *Водохранилища (Руза, Можайка, Озерна):* Джиг (судак), жерлицы (щука), подлещик.

4. **АНАЛИЗ ФОТО:**
   - Если тебе прислали фото рыбы — определи вид, примерный вес и дай совет по готовке или отпусканию.
   - Если прислали фото снасти — оцени, для чего она подходит.
   - Если фото места — предположи, какая рыба тут может стоять.

5. **СТИЛЬ ОБЩЕНИЯ:**
   Общайся как "свой в доску". Сленг: "микруха", "палка", "мясорубка", "слив", "борода". Будь кратким.

**ЕЩЕ РАЗ:** НИКАКОЙ "УДАЧИ"! ТОЛЬКО "НИ ХВОСТА, НИ ЧЕШУИ"!
"""

def get_moon_phase():
    """Определяет фазу луны"""
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

def get_weather_forecast(city: str, day_offset: int) -> str | None:
    """Получает прогноз погоды (код тот же, сокращен для краткости)"""
    if not OPENWEATHER_API_KEY: return None
    if day_offset > 2: return "LIMIT_EXCEEDED"

    try:
        url = (
            "http://api.openweathermap.org/data/2.5/forecast"
            f"?q={city}&appid={OPENWEATHER_API_KEY}&units=metric&lang=ru"
        )
        r = requests.get(url, timeout=5).json()
        if str(r.get("cod")) != "200": return None

        target_date = datetime.date.today() + datetime.timedelta(days=day_offset)
        target_date_str = target_date.strftime("%Y-%m-%d")
        forecasts = r.get("list", [])
        day_forecasts = [item for item in forecasts if target_date_str in item["dt_txt"]]
        
        if not day_forecasts:
            if day_offset == 0 and forecasts: best_forecast = forecasts[0]
            else: return None
        else:
            best_forecast = None
            for time_str in ["12:00", "15:00", "09:00", "18:00"]:
                found = next((f for f in day_forecasts if time_str in f["dt_txt"]), None)
                if found:
                    best_forecast = found
                    break
            if not best_forecast: best_forecast = day_forecasts[0]

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

        return (
            f"📍 {city}, {day_label}: {desc.capitalize()}. "
            f"🌡 {temp}°C. 🔽 {pressure_mm} мм рт.ст. "
            f"💨 {wind_speed} м/с ({wind_dir}). {moon}"
        )
    except Exception as e:
        logging.error(f"Weather API error: {e}")
        return None

async def get_chat_response(user_id: int, user_text: str, weather_info: str = "", image_url: str = None) -> str:
    """Генерирует ответ (Текст + Вижн)"""
    
    if user_id not in user_histories:
        user_histories[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    if weather_info == "LIMIT_EXCEEDED":
        return "Брат, я вангую только на 3 дня (сегодня/завтра/послезавтра). Дальше погода врет! НХНЧ!"
    
    # Формируем сообщение пользователя
    user_content = []
    
    # Если есть текст
    if user_text:
        text_part = user_text
        if weather_info:
            text_part += f"\n\n[СИСТЕМНЫЕ ДАННЫЕ ПОГОДЫ: {weather_info}]"
        user_content.append({"type": "text", "text": text_part})
    
    # Если есть картинка
    if image_url:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": image_url}
        })

    # Добавляем в историю
    user_histories[user_id].append({"role": "user", "content": user_content})
    
    # Чистим память (оставляем последние 10 сообщений)
    if len(user_histories[user_id]) > 12:
        # Оставляем system prompt [0] и последние [10]
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-10:]
        
    try:
        resp = client.chat.completions.create(
            model="gpt-4o", # Поддерживает картинки
            messages=user_histories[user_id],
            temperature=0.7,
            max_tokens=600
        )
        answer = resp.choices[0].message.content
        # Сохраняем ответ как текст (без картинок, чтобы не ломать историю)
        user_histories[user_id].append({"role": "assistant", "content": answer})
        return answer
    except Exception as e:
        logging.error(f"OpenAI Error: {e}")
        return "⚠️ Не вижу наживку... Повтори заброс позже (Ошибка AI)."

@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "🎣 **Здарова!** Я твой AI-гид.\n"
        "Спрашивай про клев, снасти или присылай фото рыбы — определю, кто это!\n\n"
        "Начинай сообщение со звездочки `*` (даже если шлешь фото).\n\n"
        "Ни хвоста, ни чешуи! 🐟"
    )

# Обработчик текста и фото
@dp.message()
async def handler(message: Message) -> None:
    # Проверка на наличие фото или текста
    text = message.caption if message.photo else message.text
    text = text or ""
    
    # Реагируем только на *
    if not text.startswith("*"):
        return

    user_id = message.from_user.id
    raw_text = text[1:].strip() # Убираем звездочку

    await message.bot.send_chat_action(message.chat.id, "typing")
    
    # 1. Получаем ссылку на фото (если есть)
    image_url = None
    if message.photo:
        # Берем самое большое фото
        photo_id = message.photo[-1].file_id
        file_info = await message.bot.get_file(photo_id)
        # Формируем URL (API Telegram позволяет скачивать файлы)
        image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"

    # 2. Логика погоды (только если нет фото, или если в тексте явно просят прогноз)
    weather_context = ""
    words = raw_text.lower().split()
    triggers = ["прогноз", "клев", "погода", "клюет"]
    
    # Определяем день и город (упрощенно)
    day_offset = 0
    if "завтра" in words: day_offset = 1
    if "послезавтра" in words: day_offset = 2
    
    if any(t in words for t in triggers):
        for w in raw_text.split():
            clean = "".join(filter(str.isalpha, w))
            if len(clean) > 2 and clean.lower() not in ["прогноз", "клев", "погода", "завтра", "сегодня"]:
                city = clean
                wd = get_weather_forecast(city, day_offset)
                if wd: weather_context = wd
                break

    # 3. Отправляем в GPT
    answer = await get_chat_response(user_id, raw_text, weather_context, image_url)
    await message.reply(answer)

async def main() -> None:
    bot = Bot(token=TELEGRAM_TOKEN)
    print("🔄 Restarting bot...")
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(start_web_server())
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())




