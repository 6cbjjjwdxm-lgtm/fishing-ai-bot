import asyncio
import datetime
import json
import logging
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from openai import OpenAI


# =========================
# CONFIG
# =========================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

# Overpass endpoint (OSM)
OVERPASS_URL = os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter")

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    sys.exit("❌ ОШИБКА: Не найдены TELEGRAM_TOKEN / OPENAI_API_KEY в .env")

dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)

user_histories: Dict[int, List[Dict]] = {}

# callback_data имеет лимит, поэтому большие данные держим в памяти по loc_id. [web:113]
# loc_id -> {"loc_name": str, "loc_type": str, "day": int, "places": [{"name": str, "place": str}]}
geo_ctx: Dict[str, Dict] = {}

# Кэш Overpass
settlements_cache: Dict[str, Tuple[float, List[Dict]]] = {}
CACHE_TTL_SEC = 12 * 3600

PLACE_FILTER = "^(city|town|village|hamlet)$"
PLACE_PRIORITY = {"city": 0, "town": 1, "village": 2, "hamlet": 3}


# =========================
# Safe send/edit (чтобы не падало из-за Markdown)
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


# =========================
# WEB SERVER (Render healthcheck)
# =========================
async def start_web_server():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="🎣 Expert Fishing Bot (OSM settlements v3) is Alive!"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


# =========================
# DAY OFFSET
# =========================
def extract_day_offset(text: str) -> int:
    t = (text or "").lower()
    if "послезавтра" in t:
        return 2
    if "завтра" in t:
        return 1
    if "сегодня" in t:
        return 0
    return 0


# =========================
# OVERPASS (OSM)
# =========================
async def overpass(query: str) -> dict:
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(OVERPASS_URL, data={"data": query}) as r:
            r.raise_for_status()
            return await r.json()


def normalize_water_name(name: str) -> str:
    if not name:
        return ""
    n = name.strip()
    low = n.lower()
    prefixes = ("река ", "р. ", "озеро ", "пруд ", "вдхр ", "водохранилище ")
    for p in prefixes:
        if low.startswith(p):
            n = n[len(p):].strip()
            break
    return n


def _gen_loc_id() -> str:
    return os.urandom(4).hex()


async def get_settlements_for_waterbody(location_name: str, location_type: str) -> List[Dict]:
    """
    Возвращает населённые пункты вдоль реки/водоёма из OSM через Overpass.
    around.<set>:radius — стандартный паттерн Overpass QL. [web:135]
    """
    water_name = normalize_water_name(location_name)
    if not water_name:
        return []

    cache_key = f"{location_type}:{water_name.lower()}"
    now = time.time()
    if cache_key in settlements_cache:
        ts, cached = settlements_cache[cache_key]
        if (now - ts) < CACHE_TTL_SEC:
            return cached

    if location_type == "river":
        water_selector = f'nwr["waterway"="river"]["name"="{water_name}"]'
    else:
        water_selector = f'nwr["natural"="water"]["name"="{water_name}"]'

    q = f"""
[out:json][timeout:25];
area["ISO3166-1"="RU"]->.ru;
(
  {water_selector}(area.ru);
)->.w;
node(around.w:2500)["place"~"{PLACE_FILTER}"]["name"];
out tags center;
"""

    try:
        data = await overpass(q)
    except Exception:
        return []

    places: List[Dict] = []
    seen = set()
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        nm = tags.get("name")
        pl = tags.get("place")
        if not nm or not pl:
            continue
        if nm in seen:
            continue
        seen.add(nm)
        places.append({"name": nm, "place": pl})

    places.sort(key=lambda x: (PLACE_PRIORITY.get(x.get("place", ""), 9), x.get("name", "")))
    places = places[:30]

    settlements_cache[cache_key] = (now, places)
    return places


# =========================
# LLM fallback: если OSM не нашёл (или вернул 0)
# =========================
async def llm_settlements_fallback(loc_name: str, loc_type: str) -> List[Dict]:
    """
    Запасной вариант: просим LLM назвать населённые пункты НА ЭТОМ водоёме.
    Это нужно, когда:
    - LLM дал не то имя,
    - OSM/Overpass не нашёл объект,
    - есть ограничения/таймауты.
    """
    system_prompt = """
Ты — справочник по географии России.

Задача: если пользователь спрашивает про клёв на реке/озере/водохранилище, нужно дать СПИСОК населённых пунктов,
которые реально стоят на этой реке/водоёме (не просто ближайший большой город).

Верни JSON:
{
  "places": [
    {"name": "Подольск", "place": "city"},
    {"name": "Дубровицы", "place": "village"}
  ]
}

Правила:
- place только из: city|town|village|hamlet
- названия: как обычно пишутся на русском
- 6..12 пунктов, если можешь, иначе сколько знаешь
"""
    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Водоём: {loc_name}\nТип: {loc_type}"},
            ],
            temperature=0,
            response_format={"type": "json_object"},
            max_tokens=300,
        )
        obj = json.loads(r.choices[0].message.content)
        places = obj.get("places", [])
        # нормализация + отсев мусора
        out = []
        for p in places:
            nm = (p.get("name") or "").strip()
            pl = (p.get("place") or "").strip()
            if nm and pl in {"city", "town", "village", "hamlet"}:
                out.append({"name": nm, "place": pl})
        out.sort(key=lambda x: (PLACE_PRIORITY.get(x.get("place", ""), 9), x.get("name", "")))
        return out[:25]
    except Exception:
        return []


# =========================
# 1) INTENT ANALYZER (LLM -> JSON)
# =========================
async def analyze_user_query(text: str) -> dict:
    # ВАЖНО: просим ИМЕНИТЕЛЬНЫЙ падеж, чтобы Overpass находил name="Пахра", а не "Пахре".
    system_prompt = """
Ты — Логический центр. Определи суть вопроса и верни JSON.

ТИПЫ (intent):
1) "forecast" — запрос ПРОГНОЗА ("Клев на Оке", "Клев на Пахре завтра", "Клев на Истринском вдхр").
2) "fish_search" — поиск места/тактики ("Где ловить форель?", "Куда за щукой?").
3) "general" — общие вопросы, снасти, фото.

ВАЖНО (для forecast):
- location_type: "river" | "lake" | "reservoir" | "pond" | "city"
- location_name: ОБЯЗАТЕЛЬНО в именительном падеже, без предлогов.
  Примеры: "Пахра" (не "Пахре"), "Ока" (не "Оке"), "Муром".

ФОРМАТ:
forecast:
{"intent":"forecast","location_type":"river","location_name":"Пахра"}
fish_search:
{"intent":"fish_search","target_fish":"Форель"}
general:
{"intent":"general"}
"""
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Запрос: {text}"},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        return json.loads(response.choices[0].message.content)
    except Exception:
        return {"intent": "general"}


# =========================
# 2) WEATHER (OpenWeather)
# =========================
def get_moon_phase() -> str:
    phases = [
        "🌑 Новолуние",
        "🌒 Растущая",
        "🌓 1-я четверть",
        "🌔 Растущая",
        "🌕 Полнолуние",
        "🌖 Убывающая",
        "🌗 Последняя четверть",
        "🌘 Старая",
    ]
    days = (datetime.date.today() - datetime.date(2000, 1, 6)).days
    return phases[int(((days % 29.53) / 29.53) * 8) % 8]


async def get_weather_forecast(city: str, day_offset: int) -> Optional[str]:
    if not OPENWEATHER_API_KEY or not city:
        return None
    if day_offset > 2:
        day_offset = 2

    url = "https://api.openweathermap.org/data/2.5/forecast"
    params = {"q": city, "appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "ru"}

    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as r:
                data = await r.json()
    except Exception:
        return None

    if str(data.get("cod")) != "200":
        return None

    target_date = datetime.date.today() + datetime.timedelta(days=day_offset)
    target_str = target_date.strftime("%Y-%m-%d")

    forecasts = data.get("list", [])
    day_data = [f for f in forecasts if target_str in f.get("dt_txt", "")]
    if not day_data and day_offset == 0:
        day_data = forecasts[:3]
    if not day_data:
        return None

    best = next((f for f in day_data if "12:00" in f.get("dt_txt", "")), day_data[0])

    temp = best["main"]["temp"]
    pressure = int(best["main"]["pressure"] * 0.75006)
    wind = best["wind"]["speed"]
    desc = best["weather"][0]["description"]
    moon = get_moon_phase()

    return (
        f"📍 {city} | {target_str}\n"
        f"🌡 Темп: {temp}°C ({desc})\n"
        f"🔽 Давление: {pressure} мм рт.ст.\n"
        f"💨 Ветер: {wind} м/с\n"
        f"🌙 Луна: {moon}"
    )


# =========================
# 3) EXPERT LLM (FORMAT НЕ МЕНЯЕМ)
# =========================
SYSTEM_PROMPT = """
Ты — ЭЛИТНЫЙ РЫБОЛОВНЫЙ ГИД (Стаж 30 лет).
Твоя задача — дать экспертный прогноз, строго соблюдая структуру.

🛑 ВАЖНО:
1. СЛОВО "УДАЧА" ЗАПРЕЩЕНО! Пиши "Ни хвоста, ни чешуи!".
2. Используй смайлики как в шаблоне.

ШАБЛОН ОТВЕТА (СТРОГО СОБЛЮДАЙ!):

🌥 **АНАЛИЗ ПОГОДЫ:**
(Здесь проанализируй переданные цифры: температуру, давление, ветер. Как это влияет на рыбу сегодня?)

🐟 **КТО И КАК КЛЮЕТ:**
> **Щука:** (Активность ?/10). Где стоит (ямы, трава), на что берет.
> **Судак:** (Активность ?/10). Глубины, тактика.
> **Окунь:** (Активность ?/10). Активность утром/вечером.

⚙️ **СНАСТИ И ПРИМАНКИ:**
• Снасти: (Рекомендуемая леска, тест удилища).
• Приманки: 
  - Щука: (конкретные цвета и модели).
  - Судак: (цвета резины/блесен).
  - Окунь: (размер и цвет мормышек).

🎯 **ТАКТИКА ПОИСКА:**
(Где бурить/кидать? Глубины? Бровки? Тактика перемещения).

---
Ни хвоста, ни чешуи! 🎣
"""


async def get_chat_response(user_id: int, text: str, weather: str, loc_name: str, intent: str) -> str:
    date_str = datetime.datetime.now().strftime("%d.%m.%Y")

    if user_id not in user_histories:
        user_histories[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    if intent == "forecast":
        user_prompt = (
            f"ЗАПРОС: {text}\n"
            f"📅 ДАТА: {date_str}\n\n"
            f"📊 ДАННЫЕ ПОГОДЫ:\n{weather}\n\n"
            f"(Обязательно используй эти данные в разделе 'АНАЛИЗ ПОГОДЫ'!)"
        )
    elif intent == "fish_search":
        user_prompt = f"ВОПРОС: {text}\n(Назови лучшие места для ловли этой рыбы. Погода не нужна)."
    else:
        user_prompt = f"ВОПРОС: {text}\n(Ответь как эксперт)."

    user_histories[user_id].append({"role": "user", "content": user_prompt})

    if len(user_histories[user_id]) > 8:
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-6:]

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=user_histories[user_id],
            temperature=0.6,
            max_tokens=1200,
        )
        answer = response.choices[0].message.content
        user_histories[user_id].append({"role": "assistant", "content": answer})
        return answer
    except Exception:
        return "⚠️ Ошибка AI."


# =========================
# HANDLERS
# =========================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("👋 Привет! Пиши со звёздочки:\n`*клев на Пахре завтра`\n`*клев на Оке`\n`*клев в Муроме`")


@dp.callback_query(F.data.startswith("geo_more:"))
async def cb_geo_more(callback: CallbackQuery):
    try:
        await callback.answer()  # подтверждение callback обязательно для нормального UX [web:45]
        _, loc_id = callback.data.split(":", 1)

        ctx = geo_ctx.get(loc_id)
        if not ctx:
            await safe_edit_markdown(callback.message, "⚠️ Контекст устарел. Повтори запрос.")
            return

        places = ctx.get("places", [])
        loc_name = ctx.get("loc_name", "Водоем")

        kb = InlineKeyboardBuilder()
        for i, p in enumerate(places[:25]):
            kb.button(text=p["name"], callback_data=f"geo:{loc_id}:{i}")
        kb.adjust(2)

        await safe_edit_markdown(
            callback.message,
            f"📍 **{loc_name}**. Выберите населённый пункт (включая деревни/посёлки):",
            reply_markup=kb.as_markup(),
        )
    except Exception:
        logging.exception("cb_geo_more failed")
        try:
            await callback.message.answer("⚠️ Сбой при показе списка.")
        except Exception:
            pass


@dp.callback_query(F.data.startswith("geo:"))
async def cb_geo_select(callback: CallbackQuery):
    try:
        await callback.answer()  # [web:45]
        _, loc_id, idx_s = callback.data.split(":")
        idx = int(idx_s)

        ctx = geo_ctx.get(loc_id)
        if not ctx:
            await safe_edit_markdown(callback.message, "⚠️ Контекст устарел. Повтори запрос.")
            return

        places = ctx.get("places", [])
        if idx < 0 or idx >= len(places):
            await safe_edit_markdown(callback.message, "⚠️ Некорректный выбор. Повтори запрос.")
            return

        city = places[idx]["name"]
        loc_name = ctx.get("loc_name", "Водоем")
        day = int(ctx.get("day", 0))

        await safe_edit_markdown(callback.message, f"✅ Точка: {city}. Анализирую...")

        weather = await get_weather_forecast(city, day) or f"⚠️ Погода в {city} не найдена."
        response = await get_chat_response(
            callback.from_user.id,
            f"Клев на {loc_name} (район {city})",
            weather,
            loc_name,
            "forecast",
        )
        await safe_send_markdown(callback.message, response)
    except Exception:
        logging.exception("cb_geo_select failed")
        try:
            await callback.message.answer("⚠️ Сбой.")
        except Exception:
            pass


@dp.message((F.text & F.text.startswith("*")) | (F.caption & F.caption.startswith("*")))
async def expert_fishing_handler(message: Message):
    full_text = message.caption if message.caption else message.text
    if not full_text:
        return

    query = full_text[1:].strip()
    await message.bot.send_chat_action(message.chat.id, "typing")

    day_offset = extract_day_offset(query)
    analysis = await analyze_user_query(query)
    intent = analysis.get("intent", "general")

    # ---------- FORECAST ----------
    if intent == "forecast":
        loc_name = analysis.get("location_name", "Водоем")
        loc_type = analysis.get("location_type", "city")

        # Река/водоём -> ВСЕГДА показываем выбор населённого пункта (OSM -> fallback LLM)
        if loc_type in {"river", "lake", "reservoir", "pond"}:
            places = await get_settlements_for_waterbody(loc_name, loc_type)
            if not places:
                places = await llm_settlements_fallback(loc_name, loc_type)

            if places:
                loc_id = _gen_loc_id()
                geo_ctx[loc_id] = {"loc_name": loc_name, "loc_type": loc_type, "day": day_offset, "places": places}

                primary_idx = [i for i, p in enumerate(places) if p.get("place") in {"city", "town"}]
                secondary_idx = [i for i, p in enumerate(places) if p.get("place") in {"village", "hamlet"}]

                kb = InlineKeyboardBuilder()

                if primary_idx:
                    for i in primary_idx[:6]:
                        kb.button(text=places[i]["name"], callback_data=f"geo:{loc_id}:{i}")
                    if secondary_idx:
                        kb.button(text="Показать деревни/посёлки", callback_data=f"geo_more:{loc_id}")
                    kb.adjust(2)

                    await message.reply(
                        f"📍 **{loc_name}**. Выберите город/крупный посёлок на этом водоёме:",
                        reply_markup=kb.as_markup(),
                        parse_mode="Markdown",
                    )
                    return

                # если городов нет — сразу деревни/посёлки
                for i in secondary_idx[:10]:
                    kb.button(text=places[i]["name"], callback_data=f"geo:{loc_id}:{i}")
                kb.adjust(2)

                await message.reply(
                    f"📍 **{loc_name}**. Выберите населённый пункт на этом водоёме:",
                    reply_markup=kb.as_markup(),
                    parse_mode="Markdown",
                )
                return

            # если совсем ничего не нашли — честный fallback
            weather = await get_weather_forecast("Москва", day_offset) or "⚠️ Погода не найдена."
            msg = f"⚠️ Не смог найти населённые пункты для '{loc_name}'. Дам прогноз по Москве:\n{weather}"
            resp = await get_chat_response(message.from_user.id, query, msg, loc_name, "forecast")
            await safe_send_markdown(message, resp)
            return

        # Город -> сразу прогноз
        if loc_type == "city":
            city = loc_name
            weather = await get_weather_forecast(city, day_offset) or f"⚠️ Погода в {city} не найдена."
            resp = await get_chat_response(message.from_user.id, query, weather, loc_name, "forecast")
            await safe_send_markdown(message, resp)
            return

        # Общий fallback
        weather = await get_weather_forecast("Москва", day_offset) or "⚠️ Погода не найдена."
        resp = await get_chat_response(message.from_user.id, query, weather, loc_name, "forecast")
        await safe_send_markdown(message, resp)
        return

    # ---------- FISH SEARCH ----------
    if intent == "fish_search":
        resp = await get_chat_response(message.from_user.id, query, "", "", "fish_search")
        await safe_send_markdown(message, resp)
        return

    # ---------- GENERAL ----------
    resp = await get_chat_response(message.from_user.id, query, "", "", "general")
    await safe_send_markdown(message, resp)


# =========================
# RUN
# =========================
async def main():
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)

    asyncio.create_task(start_web_server())

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    try:
        asyncio.run(main())
    except Exception:
        pass






