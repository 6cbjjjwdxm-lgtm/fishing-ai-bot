"""
persistence.py

Персистентное хранение состояния контент-завода.
Бот запоминает:
- Какие посты уже были опубликованы (дата, канал, тема, тип)
- Текущий контент-план на месяц (порядок тем)
- Счётчики: сколько постов опубликовано за месяц

Данные хранятся в JSON-файле на диске. При перезагрузке бот
продолжает с того места, где остановился, без повторений.

На Render нужен persistent disk (или volume), смонтированный в /data.
Если /data недоступен — используется ./data (текущая директория).
"""

import datetime
import json
import logging
import os
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ────────────────────────────── Конфигурация ──────────────────────────────

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
STATE_FILE = DATA_DIR / "content_state.json"

# Максимум записей в истории (чтобы файл не разрастался бесконечно)
MAX_HISTORY_PER_CHANNEL = 400  # ~13 месяцев


def _ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


# ────────────────────────────── Загрузка / Сохранение ──────────────────────────────

_state_cache: Optional[dict] = None


def _load_state() -> dict:
    """Загружает состояние из файла. Кэширует в памяти."""
    global _state_cache
    if _state_cache is not None:
        return _state_cache

    _ensure_data_dir()
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                _state_cache = json.load(f)
                return _state_cache
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Failed to load state: %s, starting fresh", e)

    _state_cache = {
        "published": {},   # {"@channel": [{"date": "...", "topic": "...", "type": "..."}, ...]}
        "plans": {},       # {"@channel": {"month": 4, "year": 2026, "order": [idx, idx, ...]}}
        "social_history": [],  # [{"date": "...", "topic": "...", "threads": bool, "ig": bool}]
    }
    return _state_cache


def _save_state():
    """Сохраняет состояние на диск."""
    global _state_cache
    if _state_cache is None:
        return
    _ensure_data_dir()
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(_state_cache, f, indent=2, ensure_ascii=False)
    except IOError as e:
        logger.error("Failed to save state: %s", e)


def _flush():
    """Принудительная запись на диск."""
    _save_state()


# ────────────────────────────── История публикаций ──────────────────────────────

def record_published(channel: str, topic: str, post_type: str = "daily"):
    """Записывает факт публикации поста."""
    state = _load_state()
    if channel not in state["published"]:
        state["published"][channel] = []

    entry = {
        "date": datetime.datetime.now(datetime.timezone.utc).isoformat()[:10],
        "topic": topic,
        "type": post_type,
    }
    state["published"][channel].append(entry)

    # Обрезаем историю если слишком длинная
    if len(state["published"][channel]) > MAX_HISTORY_PER_CHANNEL:
        state["published"][channel] = state["published"][channel][-MAX_HISTORY_PER_CHANNEL:]

    _save_state()
    logger.info("Recorded: %s → %s [%s]", channel, topic[:40], post_type)


def get_published_topics(channel: str, days: int = 60) -> Set[str]:
    """Возвращает множество тем, опубликованных за последние N дней."""
    state = _load_state()
    history = state.get("published", {}).get(channel, [])
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()[:10]

    return {
        entry["topic"]
        for entry in history
        if entry.get("date", "") >= cutoff
    }


def was_published_today(channel: str, post_type: str = "daily") -> bool:
    """Проверяет, был ли уже пост данного типа сегодня."""
    state = _load_state()
    history = state.get("published", {}).get(channel, [])
    today = datetime.datetime.now(datetime.timezone.utc).isoformat()[:10]

    return any(
        entry.get("date") == today and entry.get("type") == post_type
        for entry in history
    )


def get_today_date() -> str:
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)
    return now.date().isoformat()


# ────────────────────────────── Контент-план с перемешиванием ──────────────────────────────

def get_monthly_plan(channel: str, month: int, year: int, topics_list: List[str]) -> List[str]:
    """
    Возвращает контент-план на месяц (порядок тем).
    
    При первом вызове для данного месяца — создаёт перемешанный план.
    При повторных вызовах (в т.ч. после рестарта) — возвращает тот же план.
    
    Это гарантирует что:
    1. Темы идут в разном порядке каждый месяц
    2. Порядок не меняется после рестарта
    3. Использованные темы не повторяются
    """
    state = _load_state()
    plans = state.get("plans", {})
    plan_key = channel

    existing = plans.get(plan_key)

    # Проверяем актуальность плана
    if existing and existing.get("month") == month and existing.get("year") == year:
        # План актуален — возвращаем сохранённый порядок
        order = existing["order"]
        return [topics_list[i % len(topics_list)] for i in order]

    # Нужен новый план — генерируем перемешанный порядок
    import calendar
    days_in_month = calendar.monthrange(year, month)[1]

    # Получаем темы, которые были недавно опубликованы (не повторяем из предыдущих 2 месяцев)
    recent_topics = get_published_topics(channel, days=65)

    # Создаём список индексов, приоритизируя неиспользованные темы
    indices = list(range(len(topics_list)))
    unused = [i for i in indices if topics_list[i] not in recent_topics]
    used = [i for i in indices if topics_list[i] in recent_topics]

    # Перемешиваем оба списка
    random.shuffle(unused)
    random.shuffle(used)

    # Сначала неиспользованные, потом использованные (если тем не хватает)
    ordered = unused + used

    # Обеспечиваем нужное количество (дней в месяце)
    while len(ordered) < days_in_month:
        ordered.extend(ordered[:days_in_month - len(ordered)])
    ordered = ordered[:days_in_month]

    # Сохраняем план
    plans[plan_key] = {
        "month": month,
        "year": year,
        "order": ordered,
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    state["plans"] = plans
    _save_state()

    logger.info(
        "Generated new plan for %s: %d/%d, %d topics (%d unused, %d reused)",
        channel, month, year, days_in_month, len(unused), max(0, days_in_month - len(unused))
    )

    return [topics_list[i % len(topics_list)] for i in ordered]


def get_topic_for_today(channel: str, month: int, year: int, day: int, topics_list: List[str]) -> str:
    """Возвращает тему на конкретный день месяца."""
    plan = get_monthly_plan(channel, month, year, topics_list)
    idx = (day - 1) % len(plan)
    return plan[idx]


# ────────────────────────────── Социальные сети ──────────────────────────────

def record_social_post(topic: str, threads_ok: bool, ig_ok: bool):
    """Записывает публикацию в соцсети."""
    state = _load_state()
    if "social_history" not in state:
        state["social_history"] = []

    state["social_history"].append({
        "date": datetime.datetime.now(datetime.timezone.utc).isoformat()[:10],
        "topic": topic,
        "threads": threads_ok,
        "ig": ig_ok,
    })

    # Обрезаем
    if len(state["social_history"]) > MAX_HISTORY_PER_CHANNEL:
        state["social_history"] = state["social_history"][-MAX_HISTORY_PER_CHANNEL:]

    _save_state()


def was_social_posted_today() -> bool:
    """Проверяет, была ли уже соцсеть-связка сегодня."""
    state = _load_state()
    today = datetime.datetime.now(datetime.timezone.utc).isoformat()[:10]
    return any(
        entry.get("date") == today
        for entry in state.get("social_history", [])
    )


def get_social_topics_recent(days: int = 30) -> Set[str]:
    """Темы, использованные в соцсетях за последние N дней."""
    state = _load_state()
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()[:10]
    return {
        entry["topic"]
        for entry in state.get("social_history", [])
        if entry.get("date", "") >= cutoff
    }


# ────────────────────────────── Статистика ──────────────────────────────

def get_stats() -> str:
    """Возвращает читаемую статистику для админ-команды."""
    state = _load_state()
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)
    today = now.date().isoformat()
    this_month = f"{now.year}-{now.month:02d}"

    lines = ["📊 Статистика контент-завода:\n"]

    for channel in ["@dnevnikrib", "@zajabri"]:
        history = state.get("published", {}).get(channel, [])
        total = len(history)
        today_count = sum(1 for e in history if e.get("date") == today)
        month_count = sum(1 for e in history if (e.get("date") or "")[:7] == this_month)

        plan = state.get("plans", {}).get(channel, {})
        plan_status = f"{plan.get('month')}/{plan.get('year')}" if plan else "нет"

        lines.append(f"  {channel}:")
        lines.append(f"    Всего постов: {total}")
        lines.append(f"    Сегодня: {today_count}")
        lines.append(f"    За месяц: {month_count}")
        lines.append(f"    Текущий план: {plan_status}")

    social = state.get("social_history", [])
    social_today = sum(1 for e in social if e.get("date") == today)
    social_month = sum(1 for e in social if (e.get("date") or "")[:7] == this_month)
    lines.append(f"\n  Соцсети (Threads+IG):")
    lines.append(f"    Сегодня: {social_today}")
    lines.append(f"    За месяц: {social_month}")

    return "\n".join(lines)
