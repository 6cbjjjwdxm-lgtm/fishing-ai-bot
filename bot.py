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

**ТВОИ ЗНАНИЯ И ПРИНЦИПЫ:**

1. **ПРОГНОЗ КЛЕВА:**
   - Ты даешь прогноз ТОЛЬКО на основе данных погоды (давление, ветер, температура, луна), которые тебе передаст система в формате "АКТУАЛЬНАЯ ПОГОДА ДЛЯ АНАЛИЗА: ...".
   - Если ты видишь такие данные — обязательно анализируй их (стабильность давления, направление ветра, фаза луны).
   - Если данных нет — честно скажи: "Не вижу погоды. Уточни город и дату (сегодня/завтра/послезавтра)".

2. **ЛИМИТ ПРОГНОЗА:** Точный прогноз возможен только на 3 дня (сегодня, завтра, послезавтра). Дальше погода непредсказуема.

3. **ЭКСПЕРТИЗА ПО ВОДОЕМАМ (Подмосковье):**

   **МАЛЫЕ РЕКИ (МР) — твоя коронная тема:**
   - *Пахра:* Сложная река с прессингом. Крупный лещ, жерех, голавль. Зимой нижнее течение не замерзает (теплые сбросы). Лучшие точки: Красный Строитель, Подольск.
   - *Рожайка, Северка, Нерская:* Идеальны для ультралайта (голавль 200-500г, окунь, щучка-травянка). Лучшее время — май (майский жук, вертушки №00-0) и осень (джиг 1-2").
   - *Десна, Моча:* Классические "жабовники" — щука в траве, окунь на микроджиг (0.8-2г).
   - *Пехорка:* Теплая, никогда не замерзает (химия). Много разной рыбы, но качество воды сомнительное. Популярна зимой у спиннингистов.
   - *Истра, Яуза:* Живописные, малолюдные. Окунь, щука, голавль. Рыбалка сложная (завалы, узкие участки).
   
   **КРУПНЫЕ РЕКИ:**
   - *НМР (Нижняя Москва-река):* Чулково, Бронницы, Фаустово. Главный зимний полигон (не замерзает). Судак на джиг 10-14см, щука, жерех.
   - *Ока:* Серпухов, Кашира, Озёры. Перекаты — голавль и жерех (кастмастеры, воблеры). Ямы — судак (джиг 12-15см), крупный лещ (фидер).
   
   **ВОДОХРАНИЛИЩА:**
   - *Можайское, Рузское, Озернинское:* Судак (джиг на бровках 5-12м), щука вдоль травы. Зимой — подлещик, окунь.
   - *Истринское:* Окунь, щука. Популярное место для семейного отдыха.

4. **СЕЗОННОСТЬ:**
   - *Зима (дек-фев):* НМР/Пехорка (спиннинг в оттепель), водохранилища (лёд: жерлицы, балансир).
   - *Весна (март-май):* Малые реки перед нерестом (плотва, уклейка). Май — жор голавля на "майского".
   - *Лето (июнь-авг):* Ока (утро/вечер), заросшие малые реки (воблеры-минноу, топвотеры). Жара — клев слабый.
   - *Осень (сен-ноя):* Жор щуки везде! Джиг на ямах, воблеры на мели.

5. **ИСТОЧНИКИ ИНФОРМАЦИИ:**
   Используй обороты: "Мужики на Русфишинге пишут...", "По последним отчетам с водоемов...", "Как говорят местные...".

6. **СТИЛЬ ОБЩЕНИЯ:**
   Общайся как "свой в доску". Используй сленг: "микруха" (микроджиг), "палка" (спиннинг), "мясорубка" (катушка), "слив" (сход рыбы), "борода" (запутанная леска), "борщ" (мутная вода). Будь позитивным, кратким и конкретным.

**ВАЖНО:** НЕ выдумывай погоду. Если данных нет — скажи прямо. Не пиши длинных текстов — 3-5 предложений.
"""

def get_moon_phase():
    """Определяет фазу луны на сегодня"""
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
    """
    Получает прогноз погоды через OpenWeatherMap API.
    day_offset: 0 (сегодня), 1 (завтра), 2 (послезавтра).
    Возвращает строку с данными или "LIMIT_EXCEEDED", если запрос > 2 дней.
    """
    if not OPENWEATHER_API_KEY:
        return None
    
    # Ограничение 3 дня
    if day_offset > 2:
        return "LIMIT_EXCEEDED"

    try:
        url = (
            "http://api.openweathermap.org/data/2.5/forecast"
            f"?q={city}&appid={OPENWEATHER_API_KEY}&units=metric&lang=ru"
        )
        r = requests.get(url, timeout=5).json()
        
        if str(r.get("cod")) != "200":
            return None

        target_date = datetime.date.today() + datetime.timedelta(days=day_offset)
        target_date_str = target_date.strftime("%Y-%m-%d")

        # Ищем прогноз на нужный день (приоритет: полдень -> день -> любое время)
        forecasts = r.get("list", [])
        day_forecasts = [item for item in forecasts if target_date_str in item["dt_txt"]]
        
        if not day_forecasts:
            # Если на сегодня уже нет (поздний вечер), берем ближайший
            if day_offset == 0 and forecasts:
                best_forecast = forecasts[0]
            else:
                return None
        else:
            # Ищем оптимальное время (полдень / день)
            best_forecast = None
            for time_str in ["12:00", "15:00", "09:00", "18:00"]:
                found = next((f for f in day_forecasts if time_str in f["dt_txt"]), None)
                if found:
                    best_forecast = found
                    break
            if not best_forecast:
                best_forecast = day_forecasts[0]

        # Парсинг данных
        temp = best_forecast["main"]["temp"]
        pressure_hpa = best_forecast["main"]["pressure"]
        pressure_mm = int(pressure_hpa * 0.75006)
        wind_speed = best_forecast["wind"]["speed"]
        deg = best_forecast["wind"].get("deg", 0)
        directions = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"]
        wind_dir = directions[int((deg / 45) + 0.5) % 8]
        desc = best_forecast["weather"][0]["description"]
        moon = get_moon_phase()

        day_names = ["Сегодня", "Завтра", "Послезавтра"]
        day_label = day_names[day_offset] if day_offset < 3 else target_date_str

        return (
            f"📍 {city}, {day_label}: {desc.capitalize()}. "
            f"🌡 t={temp}°C. 🔽 Давление {pressure_mm} мм рт.ст. "
            f"💨 Ветер {wind_speed} м/с ({wind_dir}). {moon}"
        )

    except Exception as e:
        logging.error(f"Weather API error: {e}")
        return None

def get_chat_response(user_id: int, user_text: str, weather_info: str = "") -> str:
    """Генерирует ответ от OpenAI с учетом истории и погоды"""
    
    # Инициализация истории пользователя
    if user_id not in user_histories:
        user_histories[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    # Обработка лимита дней
    if weather_info == "LIMIT_EXCEEDED":
        return "Извини, друг! Точный прогноз я могу дать только на 3 дня (сегодня/завтра/послезавтра). Дальше погода — лотерея. 🎲"
    
    # КЛЮЧЕВОЙ МОМЕНТ: Добавление погоды в контекст
    if weather_info:
        # Добавляем погоду как системное сообщение (бот её "увидит")
        user_histories[user_id].append(
            {"role": "system", "content": f"АКТУАЛЬНАЯ ПОГОДА ДЛЯ АНАЛИЗА: {weather_info}"}
        )
    
    # Добавляем вопрос пользователя
    user_histories[user_id].append({"role": "user", "content": user_text})
    
    # Ограничиваем историю (чтобы не тратить токены)
    if len(user_histories[user_id]) > 16:
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-14:]
        
    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=user_histories[user_id],
            temperature=0.7,
            max_tokens=500  # Ограничение для краткости
        )
        answer = resp.choices[0].message.content
        user_histories[user_id].append({"role": "assistant", "content": answer})
        return answer
    except Exception as e:
        logging.error(f"OpenAI Error: {e}")
        return "⚠️ Облом с нейросетью. Попробуй через минутку."

@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "🎣 **Привет, рыбак!** Я твой AI-гид по водоемам.\n\n"
        "💬 Пиши вопрос, начиная со звездочки `*`.\n\n"
        "**Примеры:**\n"
        "🔹 `*Клев Дубна завтра`\n"
        "🔹 `*Куда поехать на выходные?`\n"
        "🔹 `*На что ловить щуку сейчас?`\n\n"
        "Погнали! 🚀"
    )

@dp.message()
async def handler(message: Message) -> None:
    text = message.text or ""
    
    # Реагируем только на сообщения со звездочкой
    if not text.startswith("*"):
        return

    user_id = message.from_user.id
    raw_text = text[1:].strip()
    
    if not raw_text:
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    # --- ЛОГИКА ОПРЕДЕЛЕНИЯ ПАРАМЕТРОВ ---
    words_lower = raw_text.lower().split()
    
    # 1. Определяем день (сегодня/завтра/послезавтра)
    day_offset = 0
    if "завтра" in words_lower:
        day_offset = 1
    if "послезавтра" in words_lower or ("после" in words_lower and "завтра" in words_lower):
        day_offset = 2
    
    # Если просят "через неделю" или подобное — ставим заведомо > 2
    if any(word in words_lower for word in ["неделю", "недели", "месяц", "выходные"]):
        # "выходные" может быть и ближайшие (завтра), но для подстраховки проверим контекст
        if day_offset == 0 and not ("этих" in words_lower or "эти" in words_lower):
            day_offset = 3  # Триггер отказа
    
    # 2. Ищем город (только если запрос про погоду/клев)
    weather_context = ""
    triggers = ["прогноз", "клев", "погода", "клюет", "завтра", "сегодня", "послезавтра"]
    
    if any(t in words_lower for t in triggers):
        # Ищем город в исходном тексте (слово с большой буквы или подходящее по длине)
        original_words = raw_text.split()
        stop_words = {"прогноз", "клев", "погода", "завтра", "послезавтра", "сегодня", 
                     "клюет", "как", "на", "в", "для", "где", "куда", "ли"}
        
        for word in original_words:
            clean = "".join(filter(str.isalpha, word))
            if len(clean) > 2 and clean.lower() not in stop_words:
                city_candidate = clean
                # Пытаемся получить погоду
                weather_data = get_weather_forecast(city_candidate, day_offset)
                if weather_data:
                    weather_context = weather_data
                    break
    
    # 3. Отправляем в GPT
    answer = get_chat_response(user_id, raw_text, weather_context)
    await message.reply(answer)

async def main() -> None:
    bot = Bot(token=TELEGRAM_TOKEN)
    
    print("🔄 Удаляю старый вебхук...")
    await bot.delete_webhook(drop_pending_updates=True)
    
    print("🌐 Запускаю веб-сервер...")
    asyncio.create_task(start_web_server())

    print("📡 Запускаю поллинг...")
    try:
        await dp.start_polling(bot)
    except Exception as e:
        print(f"❌ Ошибка: {e}")
    finally:
        await bot.session.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())




