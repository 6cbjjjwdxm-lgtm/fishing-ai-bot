import datetime
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from openai import AsyncOpenAI

REPORT_SESSIONS: Dict[int, Dict[str, Any]] = {}

STEP_MEDIA = "media"
STEP_PLACE = "place"
STEP_GEO = "geo"
STEP_METHOD = "method"
STEP_BAIT = "bait"
STEP_RESULTS = "results"
STEP_NOTES = "notes"
STEP_CONFIRM = "confirm"


def start_report(user_id: int) -> Dict[str, Any]:
    REPORT_SESSIONS[user_id] = {
        "step": STEP_MEDIA,
        "media": [],  # list of {"type":"photo|video", "file_id": "..."}
        "place_text": "",
        "geo": None,  # {"lat":..,"lon":..}
        "method": "",
        "bait": "",
        "results": "",
        "notes": "",
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    return REPORT_SESSIONS[user_id]


def has_active_report(user_id: int) -> bool:
    return user_id in REPORT_SESSIONS


def get_report(user_id: int) -> Optional[Dict[str, Any]]:
    return REPORT_SESSIONS.get(user_id)


def cancel_report(user_id: int):
    REPORT_SESSIONS.pop(user_id, None)


def report_keyboard_confirm() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить в канал", callback_data="rep:send")
    kb.button(text="✏️ Начать заново", callback_data="rep:restart")
    kb.button(text="❌ Отмена", callback_data="rep:cancel")
    kb.adjust(1)
    return kb.as_markup()


def render_report_text(r: Dict[str, Any]) -> str:
    place = r.get("place_text") or "—"
    method = r.get("method") or "—"
    bait = r.get("bait") or "—"
    results = r.get("results") or "—"
    notes = r.get("notes") or "—"
    geo = r.get("geo")

    geo_line = "—"
    if isinstance(geo, dict) and "lat" in geo and "lon" in geo:
        geo_line = f"{geo['lat']:.5f}, {geo['lon']:.5f}"

    text = (
        "🎣 **Рыболовный отчёт**\n"
        f"📍 **Место:** {place}\n"
        f"🧭 **Координаты:** {geo_line}\n"
        f"🧊/🚣 **Способ:** {method}\n"
        f"🪱 **На что ловил:** {bait}\n"
        f"🐟 **Результат:** {results}\n"
        f"📝 **Комментарий:** {notes}\n"
        "\n"
        "_Отчёт отправлен анонимно._"
    )
    return text


async def moderate_text_openai(client: AsyncOpenAI, text: str) -> Tuple[bool, str]:
    """
    Простая модерация через LLM-классификатор: мат/политика/призывы/экстремизм.
    Если не ок — вернуть ok=False и причину.
    """
    system = """
Ты модератор контента.
Запрещено: мат, оскорбления, политическая агитация, призывы к насилию/ненависти, запрещенные призывы.
Верни JSON: {"ok": true/false, "reason": "..."}.
""".strip()

    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    data = json.loads(resp.choices[0].message.content)
    ok = bool(data.get("ok"))
    reason = (data.get("reason") or "").strip()
    return ok, reason


async def next_prompt(r: Dict[str, Any]) -> str:
    step = r.get("step")
    if step == STEP_MEDIA:
        return (
            "Пришли фото/видео с рыбалки (можно несколько). Когда закончишь — напиши `готово`.\n"
            "Если медиа нет — напиши `без фото`."
        )
    if step == STEP_PLACE:
        return "Напиши место (водоём/район/населённый пункт)."
    if step == STEP_GEO:
        return "Отправь геолокацию (скрепка → Геопозиция) или напиши `пропустить`."
    if step == STEP_METHOD:
        return "Как ловил? (со льда / с берега / с лодки, снасть: мормышка/фидер/спиннинг и т.д.)"
    if step == STEP_BAIT:
        return "На что ловил? (насадка/приманки/прикормка — кратко)"
    if step == STEP_RESULTS:
        return "Результат: что поймал и сколько (можно примерно)."
    if step == STEP_NOTES:
        return "Комментарий: условия, лёд/глубина/ветер, что сработало/нет."
    return "Ок."


def _advance_step(r: Dict[str, Any], new_step: str):
    r["step"] = new_step


async def handle_report_text_input(r: Dict[str, Any], text: str) -> Optional[str]:
    t = (text or "").strip()
    step = r.get("step")

    if step == STEP_MEDIA:
        if t.lower() in ("готово", "готов", "done", "ок"):
            _advance_step(r, STEP_PLACE)
            return await next_prompt(r)
        if t.lower() in ("без фото", "без видео", "нет", "пропустить"):
            _advance_step(r, STEP_PLACE)
            return await next_prompt(r)
        return "Сейчас жду фото/видео. Когда закончишь — напиши `готово`."

    if step == STEP_PLACE:
        r["place_text"] = t
        _advance_step(r, STEP_GEO)
        return await next_prompt(r)

    if step == STEP_GEO:
        if t.lower() in ("пропустить", "skip"):
            _advance_step(r, STEP_METHOD)
            return await next_prompt(r)
        return "Локацию лучше отправить кнопкой (скрепка → Геопозиция) или напиши `пропустить`."

    if step == STEP_METHOD:
        r["method"] = t
        _advance_step(r, STEP_BAIT)
        return await next_prompt(r)

    if step == STEP_BAIT:
        r["bait"] = t
        _advance_step(r, STEP_RESULTS)
        return await next_prompt(r)

    if step == STEP_RESULTS:
        r["results"] = t
        _advance_step(r, STEP_NOTES)
        return await next_prompt(r)

    if step == STEP_NOTES:
        r["notes"] = t
        _advance_step(r, STEP_CONFIRM)
        draft = render_report_text(r)
        return "Черновик отчёта:\n\n" + draft

    return None


def handle_report_location(r: Dict[str, Any], lat: float, lon: float) -> str:
    r["geo"] = {"lat": float(lat), "lon": float(lon)}
    _advance_step(r, STEP_METHOD)
    return "Локация принята.\n" + "Теперь: " + "Как ловил? (со льда / с берега / с лодки, снасть...)"
