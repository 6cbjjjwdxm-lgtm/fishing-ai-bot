import asyncio
import datetime
import json
import logging
import os
import sys
import time
from typing import Dict, List, Optional

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from openai import AsyncOpenAI
import scraper # модуль: load_cache(), get_rusfishing_context()

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
# callback_data store: short id -> payload
LOC_CB_STORE: Dict[str, Dict] = {}
LOC_CB_TTL = 60 * 60  # 1 час

# =========================
# UTILS
# =========================
def _make_loc_cb_id(user_id: int, river: str, place: str, day: int) -> str:
    base = f"{user_id}|{river}|{place}|{day}|{int(time.time())}"
    return str(abs(hash(base)) % 10**10)

def _store_loc_cb(cb_id: str, payload: Dict):
    payload = dict(payload)
    payload["_ts"] = time.time()
    LOC_CB_STORE[cb_id] = payload

def _get_loc_cb(cb_id: str) -> Optional[Dict]:
    item = LOC_CB_STORE.get(cb_id)
    if not item:
        return None
    if time.time() - item.get("_ts", 0) > LOC_CB_TTL:
        LOC_CB_STORE.pop(cb_id, None)
        return None
    return item

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
    if "послезавтра" in t:
        return 2
    if "завтра" in t:
        return 1
    return 0

def normalize_facts(f: Dict) -> Dict:
    if not isinstance(f, dict):
        return {"has_data": False, "ice_or_open_water": "unknown", "mentions": [], "key_notes": [], "check_links": []}
    f.setdefault("has_data", False)
    f.setdefault("ice_or_open_water", "unknown")

    if not isinstance(f.get("mentions"), list):
        f["mentions"] = []
    if not isinstance(f.get("key_notes"), list):
        f["key_notes"] = []
    if not isinstance(f.get("check_links"), list):
        f["check_links"] = []
    return f
# =========================
# WEATHER
# =========================
def get_moon_phase() -> str:
    phases = ["🌑 Новолуние", "🌒 Растущая", "🌓 1-я четверть", "🌔 Растущая",
              "🌕 Полнолуние", "🌖 Убывающая", "🌗 Последняя четверть", "🌘 Старая"]
    days = (datetime.date.today() - datetime.date(2000, 1, 6)).days
    return phases[int(((days % 29.53) / 29.53) * 8) % 8]

async def get_weather_forecast(city: str, day_offset: int) -> Optional[str]:
    if not OPENWEATHER_API_KEY or not city:
        return None
    if day_offset > 2:
        day_offset = 2

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

    if not data:
        return None

    target_date = datetime.date.today() + datetime.timedelta(days=day_offset)
    target_str = target_date.strftime("%Y-%m-%d")
    forecasts = data.get("list", [])

    day_data = [f for f in forecasts if target_str in f.get("dt_txt", "")]
    if not day_data:
        if not forecasts:
            return None
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

ДОКАЗАТЕЛЬНОСТЬ:
- Если в "СПРАВКА ПО ВОДОЕМУ" есть "ВЫЖИМКА С RUSFISHING" — делай выводы в первую очередь из нее.
- Не выдумывай деревни/ямы, которых нет в выжимке. Если данных мало — так и скажи и предложи 1–2 универсальных места (бровки/ямы/коряжник) без конкретных топонимов.
- Если "ССЫЛКИ ДЛЯ ПРОВЕРКИ" не пустые — добавь блок "Проверить на форуме:" и 3–5 ссылок. Если ссылок нет — блок не добавляй.
"""

async def analyze_user_query(text: str) -> dict:
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
        return {"intent": "general", "location_name": ""}
    
FACTS_EXTRACTOR_SYSTEM = """
Ты извлекаешь факты из текста "ВЫЖИМКА С RUSFISHING" и списка "ССЫЛКИ ДЛЯ ПРОВЕРКИ".
Ничего не выдумывай.

Верни JSON строго по схеме:
{
  "has_data": true/false,
  "ice_or_open_water": "ice"|"open"|"unknown",
  "mentions": [
    {"species":"", "method":"", "bait":"", "activity":"", "notes":""}
  ],
  "key_notes": ["..."],
  "check_links": ["https://..."]
}

Правила:
- check_links бери ТОЛЬКО из блока "ССЫЛКИ ДЛЯ ПРОВЕРКИ" (если ссылок нет — []).
- Если в выжимке нет явных фактов по клеву/уловам/снастям — has_data=false.
- Если про лед/открытую воду ничего нет — "unknown".
"""

async def extract_facts_from_rusfishing(user_query: str, forum_context: str) -> Dict:
    if not forum_context:
        return {"has_data": False, "ice_or_open_water": "unknown", "mentions": [], "key_notes": [], "check_links": []}

    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": FACTS_EXTRACTOR_SYSTEM},
                {"role": "user", "content": f"ЗАПРОС: {user_query}\n\nТЕКСТ:\n{forum_context}"},
            ],
            response_format={"type": "json_object"},
            temperature=0
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        logging.warning("Facts extraction failed: %s", e)
        return {"has_data": False, "ice_or_open_water": "unknown", "mentions": [], "key_notes": [], "check_links": []}
    
ANSWER_SYSTEM = """
Ты опытный рыбак (форумный стиль), но строго опираешься на факты из JSON.
Если фактов нет — честно скажи, что подтверждений по отчетам нет, и перейди в "безопасный режим" (универсальные советы без брендов и без конкретных топонимов).
Никогда не придумывай приманки/места/виды рыб, которых нет в facts.

Правило ссылок:
- Блок "Проверить на форуме:" добавляй ТОЛЬКО если facts.check_links не пустой (3–5 ссылок).
В конце: "НХНЧ!".
"""

async def render_answer_from_facts(user_query: str, facts: Dict, weather: str, intent: str) -> str:
    # weather можно добавлять только для forecast; для fish_search обычно пусто
    payload = {"query": user_query, "intent": intent, "weather": weather or "", "facts": facts}

    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": ANSWER_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        temperature=0.2 if intent in ("forecast", "fish_search") else 0.7
    )
    return resp.choices[0].message.content

async def get_chat_response(user_id: int, text: str, weather: str, loc_name: str,
                            intent: str, extra_context: str = "") -> str:
    system_text = PROMPT_FORECAST if intent == "forecast" else PROMPT_ADVICE

    user_content = f"ВОПРОС ПОЛЬЗОВАТЕЛЯ: {text}\n"

    if weather:
        user_content += f"\n📊 ПОГОДА:\n{weather}\n"

    if extra_context:
        user_content += f"\nℹ️ СПРАВКА ПО ВОДОЕМУ:\n{extra_context}\n"

    if intent == "forecast":
        user_content += "\n(Дай прогноз строго по шаблону)"
    else:
        user_content += "\n(Дай экспертный совет, погоду расписывать не нужно, если она не критична)"

    if user_id not in user_histories:
        user_histories[user_id] = []

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
        temp = 0.2 if intent in ("forecast", "fish_search") else 0.7
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=user_histories[user_id],
            temperature=temp
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
    try:
        await callback.answer()
        parts = callback.data.split(":")
        if len(parts) != 2:
            return

        cb_id = parts[1]
        payload = _get_loc_cb(cb_id)
        if not payload:
            await safe_send_markdown(callback.message, "⚠️ Кнопка устарела. Нажми запрос ещё раз.")
            return

        river = payload["river"]
        place = payload["place"]
        day = int(payload["day"])


        await safe_edit_markdown(callback.message, f"✅ Выбрано: {place} ({river}). Анализирую...")

        weather_task = asyncio.create_task(get_weather_forecast(place, day))
        weather = await weather_task or f"⚠️ Погода для {place} не найдена."

        forum_context = ""
        try:
            forum_context = await scraper.get_rusfishing_context(f"{river} {place} клев")
        except Exception as e:
            logging.warning("Vertex forum context failed: %s", e)

        facts = await extract_facts_from_rusfishing(f"Клев на {river} в районе {place}", forum_context)
        answer = await render_answer_from_facts(
            user_query=f"Клев на {river} в районе {place}",
            facts=facts,
            weather=weather or "",
            intent="forecast"
        )
        await safe_send_markdown(callback.message, answer)

    except Exception:
        logging.exception("Error in callback")
        await safe_send_markdown(callback.message, "⚠️ Ошибка обработки.")

@dp.message((F.text & F.text.startswith("*")) | (F.caption & F.caption.startswith("*")))

async def main_handler(message: Message):
    text = message.caption or message.text
    query = text[1:].strip()
    await message.bot.send_chat_action(message.chat.id, "typing")

    day_offset = extract_day_offset(query)

    # 1) анализ интента
    analysis = await analyze_user_query(query)
    intent = (analysis.get("intent") or "general").strip()
    loc_name = (analysis.get("location_name") or "").strip()
    logging.warning("INTENT=%s loc=%s query=%s", intent, loc_name, query)

    # 2) контекст по кэшу мест
    river_context = ""
    found_river_key = None

    for key in PLACES_CACHE:
        if key.lower() in query.lower() or (loc_name and key.lower() in loc_name.lower()):
            found_river_key = key
            break

    if found_river_key:
        top_places = ", ".join(PLACES_CACHE[found_river_key].get("locations", [])[:5])
        river_context = f"ВОДОЕМ: {found_river_key}. Популярные точки: {top_places}."

    # ===== forecast =====
    if intent == "forecast":
        if found_river_key:
            locations = PLACES_CACHE[found_river_key].get("locations", [])
            kb = InlineKeyboardBuilder()
            for loc in locations[:14]:
                cb_id = _make_loc_cb_id(message.from_user.id, found_river_key, loc, day_offset)
                _store_loc_cb(cb_id, {"river": found_river_key, "place": loc, "day": day_offset})
                kb.button(text=loc, callback_data=f"loc:{cb_id}")
            kb.adjust(2)

            await message.reply(
                f"📍 **{found_river_key}**. Выберите место (база Русфишинга):",
                reply_markup=kb.as_markup(),
                parse_mode="Markdown"
            )
            return

        weather = await get_weather_forecast(loc_name, day_offset)

        forum_context = ""
        try:
            forum_context = await scraper.get_rusfishing_context(query)
        except Exception as e:
            logging.warning("Vertex forum context failed: %s", e)

        facts = await extract_facts_from_rusfishing(query, forum_context)
        answer = await render_answer_from_facts(query, facts, weather=weather or "", intent="forecast")
        await safe_send_markdown(message, answer)
        return


    # ===== fish_search =====
    elif intent == "fish_search":
        forum_context = ""
        try:
            forum_context = await scraper.get_rusfishing_context(query)
        except Exception as e:
            logging.warning("Vertex forum context failed: %s", e)

        facts = await extract_facts_from_rusfishing(query, forum_context)
        facts = normalize_facts(facts)

        if river_context:
            facts["key_notes"].insert(0, river_context)

        month = datetime.date.today().month
        is_winter = month in (12, 1, 2)
        if is_winter and not facts.get("has_data"):
            facts["key_notes"].append("Сезон: зима. Если по спиннингу нет подтверждений в отчетах — лучше не гадать.")

        answer = await render_answer_from_facts(
            user_query=query,
            facts=facts,
            weather="",
            intent="fish_search"
        )
        await safe_send_markdown(message, answer)
        return
        # ===== general =====
    else:
        resp = await get_chat_response(message.from_user.id, query, "", "", "general")
        await safe_send_markdown(message, resp)
        return


# =========================
# BACKGROUND TASKS
# =========================
async def periodic_cache_update():
    # Кэш мест обновляем отдельно (например через GitHub Actions), а не парсим форум с Render.
    while True:
        await asyncio.sleep(24 * 3600)
        logging.info("Cache update: skipped (offline).")

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
    logging.info("Loaded %s rivers from cache.", len(PLACES_CACHE))

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


























