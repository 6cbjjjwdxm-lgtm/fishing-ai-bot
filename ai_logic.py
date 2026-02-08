import base64
import datetime
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from openai import AsyncOpenAI

# intents
INTENT_FORECAST = "forecast"
INTENT_GEAR = "gear"
INTENT_TACTICS = "tactics"
INTENT_GENERAL = "general"

# Память на пользователя: user_id -> messages[]
# ВАЖНО: храним только короткую историю
USER_HISTORIES: Dict[int, List[Dict[str, Any]]] = {}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def classify_intent_ru(text: str) -> str:
    t = (text or "").lower()

    if any(w in t for w in ["клев", "клёв", "прогноз клева", "прогноз клёва", "клюет", "клюёт"]):
        return INTENT_FORECAST
    if any(w in t for w in ["на 5", "5 дней", "пять дней", "на неделю", "неделю"]) and any(w in t for w in ["клев", "клёв", "прогноз"]):
        return INTENT_FORECAST

    if any(w in t for w in ["катушка", "удилище", "спиннинг", "фидер", "поплав", "шнур", "леска",
                            "приманк", "комплект", "набор", "мотор", "лодк", "эхолот"]):
        return INTENT_GEAR

    if any(w in t for w in ["как ловить", "где ловить", "где искать", "тактика", "проводк", "прикормк",
                            "точк", "места", "ям", "бровк", "перекат", "коряж"]):
        return INTENT_TACTICS

    return INTENT_GENERAL


def extract_day_offset_ru(text: str) -> Optional[int]:
    t = (text or "").lower()
    if "послезавтра" in t:
        return 2
    if "завтра" in t:
        return 1
    if "сегодня" in t:
        return 0
    return None


def extract_date_iso(text: str) -> Optional[str]:
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\b", text or "")
    if not m:
        return None
    d = int(m.group(1))
    mo = int(m.group(2))
    y = int(m.group(3)) if m.group(3) else datetime.date.today().year
    try:
        return datetime.date(y, mo, d).isoformat()
    except Exception:
        return None


def extract_city_simple(query: str) -> str:
    """
    Очень простой экстрактор локации.
    Формат: "... в <город/место>"
    """
    q = _norm(query)
    low = q.lower()
    if " в " not in f" {low} ":
        return ""
    return q.split(" в ", 1)[-1].strip()


ASSISTANT_SYSTEM = """
Ты — рыболовный AI‑ассистент и гид, стиль — как опытный рыбак‑практик.

ТЫ УМЕЕШЬ:
- Давать прогноз клева по погоде (температура, давление, ветер, осадки, фаза луны) на сегодня/завтра/послезавтра и на 5 дней вперёд (если данные есть).
- Разделять прогноз: Белая рыба (как группа) и Хищник (как группа).
- Подбирать снасти/комплекты/моторы/тактику.
- По фото: определить вероятный вид рыбы или тип снасти, объяснить признаки и дать советы.

ПРАВИЛА ЧЕСТНОСТИ:
- Не ссылайся на форумы/отзывы/интернет: у тебя нет доступа к ним.
- Если в данных нет точности — говори прямо (например: "по фото могу ошибиться", "погода доступна на 5 дней").
- Не обещай 100% клёв.

СЕЗОН:
- Если season = winter (декабрь/январь/февраль) и пользователь не сказал "открытая вода",
  то давай рекомендации для ловли СО ЛЬДА (мормышка/балансир/блесна/жерлицы) и коротко напомни про безопасность льда.
- Если пользователь явно говорит про открытую воду зимой — разрешено обсуждать фидер/спиннинг, но осторожно.

ФОРМАТ:
- Отвечай на русском.
- Когда речь о прогнозе: обязательно отдельные блоки "Белая рыба" и "Хищник".
- Пиши структурно, но живо.
В конце, если тема рыбалки: "НХНЧ!"
""".strip()


def _b64_image(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _ensure_history(user_id: int):
    if user_id not in USER_HISTORIES:
        USER_HISTORIES[user_id] = [{"role": "system", "content": ASSISTANT_SYSTEM}]
    else:
        # обновляем system на всякий случай
        if USER_HISTORIES[user_id] and USER_HISTORIES[user_id][0].get("role") == "system":
            USER_HISTORIES[user_id][0] = {"role": "system", "content": ASSISTANT_SYSTEM}
        else:
            USER_HISTORIES[user_id].insert(0, {"role": "system", "content": ASSISTANT_SYSTEM})


def _trim_history(user_id: int, keep_last: int = 10):
    msgs = USER_HISTORIES.get(user_id) or []
    if len(msgs) <= keep_last + 1:
        return
    # сохраняем system + последние keep_last сообщений
    USER_HISTORIES[user_id] = [msgs[0]] + msgs[-keep_last:]


async def assistant_text(
    client: AsyncOpenAI,
    user_id: int,
    query: str,
    extra_context: Optional[Dict[str, Any]] = None,
    temperature: float = 0.6,
) -> str:
    _ensure_history(user_id)

    if extra_context:
        USER_HISTORIES[user_id].append({
            "role": "user",
            "content": "КОНТЕКСТ (данные от инструментов):\n" + json.dumps(extra_context, ensure_ascii=False)
        })

    USER_HISTORIES[user_id].append({"role": "user", "content": query})
    _trim_history(user_id, keep_last=12)

    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=USER_HISTORIES[user_id],
        temperature=temperature,
    )
    ans = resp.choices[0].message.content
    USER_HISTORIES[user_id].append({"role": "assistant", "content": ans})
    _trim_history(user_id, keep_last=12)
    return ans


async def assistant_with_photo(
    client: AsyncOpenAI,
    user_id: int,
    query: str,
    image_bytes: bytes,
    extra_context: Optional[Dict[str, Any]] = None,
) -> str:
    _ensure_history(user_id)

    if extra_context:
        USER_HISTORIES[user_id].append({
            "role": "user",
            "content": "КОНТЕКСТ (данные от инструментов):\n" + json.dumps(extra_context, ensure_ascii=False)
        })

    data_url = "data:image/jpeg;base64," + _b64_image(image_bytes)

    USER_HISTORIES[user_id].append({
        "role": "user",
        "content": [
            {"type": "text", "text": query},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
    })
    _trim_history(user_id, keep_last=10)

    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=USER_HISTORIES[user_id],
        temperature=0.5,
    )
    ans = resp.choices[0].message.content
    USER_HISTORIES[user_id].append({"role": "assistant", "content": ans})
    _trim_history(user_id, keep_last=12)
    return ans
