import asyncio
import datetime
import json
import logging
import os
import sys
from typing import Dict, List, Optional

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Используем AsyncOpenAI
from openai import AsyncOpenAI
import scraper  # Наш модуль парсера

# =========================
# CONFIG
# =========================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    sys.exit("❌ ОШИБКА: Не найдены токены в .env")

dp = Dispatcher()
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

user_histories: Dict[int, List[Dict]] = {}
PLACES_CACHE = {}

# =========================
# UTILS
# =========================
async def safe_send_markdown(message: Message, text: str):
    try:
        await message.reply(text, parse_mode="Markdown")
    except TelegramBadRequest:
        await message.reply(text)

async def safe_edit_markdown(message: Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    except TelegramBadRequest:
        await message.edit_text(text, reply_markup=reply_markup)

def extract_day_offset(text: str) -> int:
    t = (text or "").lower()
    if "послезавтра" in t: return 2
    if "завтра" in t: return 1
    return 0

# =========================
# WEATHER
# =========================
def get_moon_phase() -> str:
    phases = ["🌑 Новолуние", "🌒 Растущая", "🌓 1-я четверть", "🌔 Растущая", "🌕 Полнолуние", "🌖 Убывающая", "🌗 Последняя четверть", "🌘 Старая"]
    days = (datetime.date.today() - datetime.date(2000, 1, 6)).days
    return phases[int(((days % 29.53) / 29.53) * 8) % 8]

async def get_weather_forecast(city: str, day_offset: int) -> Optional[str]:
    if not OPENWEATHER_API_KEY or not city: return None
    if day_offset > 2: day_offset = 2

    # Улучшенный поиск: пробуем с областью (для МО), потом с RU, потом просто название
    queries = [
        f"{city}, Moscow Oblast, RU", 
        f"{city}, RU", 
        city
    ]
    
    url = "https://api.openweathermap.org/data/2.5/forecast"
    base_params = {"appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "ru"}

    timeout = aiohttp.ClientTimeout(total=5)
    data = None
    
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for q in queries:
            params = base_params.copy()
            params["q"] = q
            try:
                async with session.get(url, params=params) as r:
                    if r.status == 200:
                        json_data = await r.json()
                        if json_data.get("cod") == "200":
                            data = json_data
                            break
            except Exception:
                continue
    
    if not data: return None

    target_date = datetime.date.today() + datetime.timedelta(days=day_offset)
    target_str = target_date.strftime("%Y-%m-%d")
    forecasts = data.get("list", [])
    
    day_data = [f for f in forecasts if target_str in f.get("dt_txt", "")]
    if not day_data: 
        if not forecasts: return None
        best = forecasts[0]
    else:
        best = next((f for f in day_data if "12:00" in f.get("dt_txt", "")), day_data[0])

    temp = best["main"]["temp"]
    pressure = int(best["main"]["pressure"] * 0.75006)
    wind = best["wind"]["speed"]
    desc = best["weather"][0]["description"]
    moon = get_moon_phase()

    return (
        f"📍 {city} | {target_str}\n"
        f"🌡 Темп: {temp:.1f}°C ({desc})\n"
        f"🔽 Давление: {pressure} мм рт.ст.\n"
        f"💨 Ветер: {wind} м/с\n"
        f"🌙 Луна: {moon}"
    )

# =========================
# AI LOGIC
# =========================
PROMPT_FORECAST = """
Ты — ЭЛИТНЫЙ РЫБОЛОВНЫЙ ГИД (Стаж 30 лет).
Дай прогноз, используя данные Русфишинга и погоду.

ШАБЛОН ОТВЕТА:
🌥 **АНАЛИЗ ПОГОДЫ С ДАННЫМИ:** ...
🐟 **КТО И КАК КЛЮЕТ:**
> **Щука:** ...
> **Судак:** ...
> **Жерех:** ...
> **Окунь:** ...
⚙️ **СНАСТИ И ПРИМАНКИ:** ...
🎯 **ТАКТИКА ПОИСКА:** ...
---
Ни хвоста, ни чешуи! 🎣
"""

PROMPT_ADVICE = """
Ты — бывалый рыбак с форума Русфишинг, эксперт с 30-летним стажем.
Твоя задача — дать дельный совет новичку или коллеге.

СТИЛЬ ОБЩЕНИЯ:
- Пиши живо, как в переписке на форуме. Без официоза.
- Используй сленг умеренно (твич, палка, плетня, жабовник, бровка).
- НЕ используй нумерованные списки (1, 2, 3) без нужды. Лучше разбивай на абзацы.
- Делись "секретами" (например: "Я обычно ставлю флюр потолще...", "Лучше всего работает на паузе...").
- Если спрашивают про воблеры/блесны — называй конкретные модели (ZipBaits, Mepps, Jackall), цвета (Mat Tiger, натуралка).

ВАЖНО:
- НИКОГДА не желай "удачи" или "успехов" (плохая примета!).
- В конце всегда пиши: "Ни хвоста, ни чешуи!" или "НХНЧ!".

ТВОЯ ЦЕЛЬ:
Объяснить суть, а не просто перечислить факты. Если спрашивают "как ловить на волкеры", объясни саму механику проводки "елочкой" (walking the dog) и почему это круто.

ДОКАЗАТЕЛЬНОСТЬ:
- Если в "СПРАВКА ПО ВОДОЕМУ" есть "ВЫЖИМКА С ФОРУМА" — делай выводы в первую очередь из нее.
- Не выдумывай деревни/ямы, которых нет в выжимке. Если данных мало — так и скажи и предложи 1–2 универсальных места (бровки/ямы/коряжник) без конкретных топонимов.
- В конце добавь блок "Проверить на форуме:" и перечисли 3–5 ссылок из "ССЫЛКИ ДЛЯ ПРОВЕРКИ".
"""

async def analyze_user_query(text: str) -> dict:
    """Определяем намерение + выделяем название реки/водоема"""
    system = """
Ты — классификатор запросов.
1. "forecast" — вопросы про КЛЕВ на конкретную дату/время ("клюет ли завтра", "прогноз на выходные", "клев на Оке").
2. "fish_search" — вопросы КАК/ГДЕ ловить, тактика, снасти ("как ловить жереха", "на что берет щука", "где искать судака").
3. "general" — остальное.

Верни JSON: {"intent": "...", "location_name": "..."}
"""
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
            temperature=0
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logging.error(f"Analysis Error: {e}")
        return {"intent": "general"}

async def get_chat_response(user_id: int, text: str, weather: str, loc_name: str, intent: str, extra_context: str = "") -> str:
    # Выбор промпта
    system_text = PROMPT_FORECAST if intent == "forecast" else PROMPT_ADVICE
    
    # Формируем сообщение пользователя
    user_content = f"ВОПРОС ПОЛЬЗОВАТЕЛЯ: {text}\n"
    
    if weather:
        user_content += f"\n📊 ПОГОДА:\n{weather}\n"
    
    if extra_context:
        user_content += f"\nℹ️ СПРАВКА ПО ВОДОЕМУ:\n{extra_context}\n"
        
    if intent == "forecast":
        user_content += "\n(Дай прогноз строго по шаблону)"
    else:
        user_content += "\n(Дай экспертный совет, погоду расписывать не нужно, если она не критична)"

    # Работа с историей сообщений
    if user_id not in user_histories:
        user_histories[user_id] = []
    
    # Всегда обновляем System Message на актуальный
    sys_msg_idx = -1
    for i, m in enumerate(user_histories[user_id]):
        if m["role"] == "system":
            sys_msg_idx = i
            break
            
    if sys_msg_idx >= 0:
        user_histories[user_id][sys_msg_idx] = {"role": "system", "content": system_text}
    else:
        user_histories[user_id].insert(0, {"role": "system", "content": system_text})

    user_histories[user_id].append({"role": "user", "content": user_content})
    
    if len(user_histories[user_id]) > 8:
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-6:]

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=user_histories[user_id],
            temperature=0.7
        )
        answer = response.choices[0].message.content
        user_histories[user_id].append({"role": "assistant", "content": answer})
        return answer
    except Exception as e:
        logging.error(f"AI Error: {e}")
        return "⚠️ ИИ задумался. Попробуй еще раз."


# =========================
# HANDLERS
# =========================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("👋 Привет! Я использую базу Русфишинга.\nНапиши: `*клев на Оке` или `*как ловить щуку на Пахре`")

@dp.callback_query(F.data.startswith("loc:"))
async def cb_location_select(callback: CallbackQuery):
    # data: loc:RiverName:PlaceName:DayOffset
    try:
        await callback.answer()
        parts = callback.data.split(":")
        # Защита от старых коллбеков разной длины
        if len(parts) < 4: return
        
        _, river, place, day_s = parts
        day = int(day_s)
        
        await safe_edit_markdown(callback.message, f"✅ Выбрано: {place} ({river}). Анализирую...")

        weather_task = asyncio.create_task(get_weather_forecast(place, day))
        weather = await weather_task or f"⚠️ Погода для {place} не найдена."
        
        response = await get_chat_response(
            callback.from_user.id,
            f"Клев на {river} в районе {place}",
            weather,
            river,
            "forecast"
        )
        await safe_send_markdown(callback.message, response)
        
    except Exception as e:
        logging.exception("Error in callback")
        await safe_send_markdown(callback.message, "⚠️ Ошибка обработки.")

@dp.message((F.text & F.text.startswith("*")) | (F.caption & F.caption.startswith("*")))
async def main_handler(message: Message):
    text = message.caption or message.text
    query = text[1:].strip()
    await message.bot.send_chat_action(message.chat.id, "typing")

    day_offset = extract_day_offset(query)
    
    # 1. Анализ интента
    analysis = await analyze_user_query(query)
    intent = analysis.get("intent", "general")
    loc_name = analysis.get("location_name", "").strip()
    logging.warning("INTENT=%s loc=%s query=%s", intent, loc_name, query)
    
    # 2. Поиск контекста реки
    river_context = ""
    found_river_key = None
    
    # Улучшенный поиск: ищем вхождение ключа (Ока) в запрос (Оке) или наоборот
    for key in PLACES_CACHE:
        # Если "ока" в "на оке" ИЛИ "можай" в "можайка"
        if key.lower() in query.lower() or (loc_name and key.lower() in loc_name.lower()):
            found_river_key = key
            break
            
    if found_river_key:
        top_places = ", ".join(PLACES_CACHE[found_river_key].get("locations", [])[:5])
        river_context = f"ВОДОЕМ: {found_river_key}. Популярные точки: {top_places}."

    # ВЕТКА: ПРОГНОЗ
    if intent == "forecast":
        # Если нашли реку -> даем кнопки
        if found_river_key:
            locations = PLACES_CACHE[found_river_key].get("locations", [])
            kb = InlineKeyboardBuilder()
            for loc in locations[:14]:
                kb.button(text=loc, callback_data=f"loc:{found_river_key}:{loc}:{day_offset}")
            kb.adjust(2)

            await message.reply(
                f"📍 **{found_river_key}**. Выберите место (база Русфишинга):",
                reply_markup=kb.as_markup(),
                parse_mode="Markdown"
            )
            return

        # Если реки нет в базе - просто прогноз по городу
        weather = await get_weather_forecast(loc_name, day_offset)
        resp = await get_chat_response(message.from_user.id, query, weather, loc_name, "forecast")
        await safe_send_markdown(message, resp)
        return

    # ВЕТКА: ВОПРОС КАК ЛОВИТЬ

    elif intent == "fish_search":
        # 1) Форумный контекст (сниппеты + ссылки) — только для "как/где ловить"
        forum_context = ""
        try:
            forum_context = await scraper.get_rusfishing_context(query, PLACES_CACHE)
        except Exception as e:
            logging.exception("Rusfishing context error")  # покажет stacktrace
            forum_context = ""

        # Логи ДОЛЖНЫ быть тут, а не в except
        logging.warning("FORUM_CONTEXT_LEN=%s", len(forum_context or ""))
        logging.warning("FORUM_CONTEXT_HEAD=%s", (forum_context or "")[:400])

        if not forum_context:
            logging.warning("FORUM_CONTEXT_EMPTY for query=%s loc=%s found_river=%s",
                            query, loc_name, found_river_key)

        # 2) Склеиваем контекст: быстрый справочник + фактура из веток
        extra = river_context
        if forum_context:
            extra = (extra + "\n\n" if extra else "") + forum_context

        response = await get_chat_response(
            message.from_user.id,
            query,
            weather="",
            loc_name=loc_name,
            intent="fish_search",
            extra_context=extra
        )
        await safe_send_markdown(message, response)
        return

    # ОБЩИЙ ВОПРОС
    else:
        resp = await get_chat_response(message.from_user.id, query, "", "", "general")
        await safe_send_markdown(message, resp)
        return



# =========================
# BACKGROUND TASKS
# =========================
async def periodic_cache_update():
    global PLACES_CACHE
    while True:
        await asyncio.sleep(24 * 3600) 
        try:
            logging.info("⏳ Фоновое обновление базы...")
            new_cache = await scraper.update_rusfishing_cache()
            if new_cache:
                PLACES_CACHE = new_cache
        except Exception as e:
            logging.error(f"Background update failed: {e}")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is Alive"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# =========================
# MAIN
# =========================
async def main():
    global PLACES_CACHE
    PLACES_CACHE = scraper.load_cache()
    logging.info(f"Loaded {len(PLACES_CACHE)} rivers from cache.")

    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)

    asyncio.create_task(start_web_server())
    asyncio.create_task(periodic_cache_update())

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, force=True)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass



















