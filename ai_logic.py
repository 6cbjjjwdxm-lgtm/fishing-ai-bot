import base64
import datetime
import json
import re
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

USER_HISTORIES: Dict[int, List[Dict[str, Any]]] = {}

INTENT_FORECAST = "forecast"
INTENT_OTHER = "other"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def classify_intent_ru(text: str) -> str:
    t = (text or "").lower()
    if any(w in t for w in ["клев", "клёв", "прогноз", "клюет", "клюёт"]):
        return INTENT_FORECAST
    return INTENT_OTHER


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
    q = _norm(query)
    low = q.lower()
    if " в " not in f" {low} ":
        return ""
    return q.split(" в ", 1)[-1].strip()


ASSISTANT_SYSTEM = """
Ты — рыболовный AI‑ассистент и гид.

Функции:
- Прогноз клева по погоде, отдельно: Белая рыба и Хищник.
- Подбор снастей/комплектов/моторов, тактика ловли.
- Анализ фото рыбы/снасти.

Правила:
- Никаких ссылок/форумов/отзывов: доступа к интернету нет.
- Не обещай 100%.
- Зимой (декабрь/январь/февраль) по умолчанию советы со льда, если не сказано "открытая вода".
- Пиши по‑товарищески, но структурно.
В конце по теме рыбалки: "НХНЧ!"
""".strip()


def _ensure_history(user_id: int):
    if user_id not in USER_HISTORIES:
        USER_HISTORIES[user_id] = [{"role": "system", "content": ASSISTANT_SYSTEM}]
    else:
        if USER_HISTORIES[user_id] and USER_HISTORIES[user_id][0].get("role") == "system":
            USER_HISTORIES[user_id][0] = {"role": "system", "content": ASSISTANT_SYSTEM}
        else:
            USER_HISTORIES[user_id].insert(0, {"role": "system", "content": ASSISTANT_SYSTEM})


def _trim_history(user_id: int, keep_last: int = 12):
    msgs = USER_HISTORIES.get(user_id) or []
    if len(msgs) <= keep_last + 1:
        return
    USER_HISTORIES[user_id] = [msgs[0]] + msgs[-keep_last:]


def _b64_image(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


async def assistant_text(
    client: AsyncOpenAI,
    user_id: int,
    query: str,
    extra_context: Optional[Dict[str, Any]] = None,
    temperature: float = 0.65,
) -> str:
    _ensure_history(user_id)

    if extra_context:
        USER_HISTORIES[user_id].append({
            "role": "user",
            "content": "КОНТЕКСТ:\n" + json.dumps(extra_context, ensure_ascii=False)
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
            "content": "КОНТЕКСТ:\n" + json.dumps(extra_context, ensure_ascii=False)
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

