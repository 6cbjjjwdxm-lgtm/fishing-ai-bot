"""
token_manager.py

Автоматическое обновление Meta-токенов (Threads + Instagram).
- Хранит токены и даты выдачи в JSON-файле
- Каждые 50 дней обновляет long-lived токены через API
- При старте бота проверяет и обновляет если нужно
- Уведомляет админа в ТГ при успехе/ошибке

Эндпоинты:
- Threads:   GET https://graph.threads.net/refresh_access_token?grant_type=th_refresh_token&access_token=TOKEN
- Instagram: GET https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token=TOKEN

Токены живут 60 дней, обновляем каждые 50 — с запасом 10 дней.
"""

import datetime
import json
import logging
import os
from pathlib import Path
from typing import Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)

# ────────────────────────────── Хранилище ──────────────────────────────

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
TOKEN_FILE = DATA_DIR / "meta_tokens.json"

# Через сколько дней обновлять (токен живёт 60 дней, обновляем за 10 дней до)
REFRESH_AFTER_DAYS = 50


def _ensure_data_dir():
    """Создаёт директорию для данных если её нет."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_token_data() -> dict:
    """Загружает данные токенов из файла."""
    _ensure_data_dir()
    if TOKEN_FILE.exists():
        try:
            with open(TOKEN_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Failed to load token data: %s", e)
    return {}


def _save_token_data(data: dict):
    """Сохраняет данные токенов в файл."""
    _ensure_data_dir()
    try:
        with open(TOKEN_FILE, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except IOError as e:
        logger.error("Failed to save token data: %s", e)


# ────────────────────────────── Логика обновления ──────────────────────────────

def _needs_refresh(token_data: dict, key: str) -> bool:
    """Проверяет, нужно ли обновить токен."""
    issued = token_data.get(f"{key}_issued")
    if not issued:
        return False  # нет данных о дате — токен ещё не был сохранён через наш менеджер

    issued_dt = datetime.datetime.fromisoformat(issued)
    age_days = (datetime.datetime.now(datetime.timezone.utc) - issued_dt).days
    return age_days >= REFRESH_AFTER_DAYS


async def _refresh_threads_token(current_token: str) -> Tuple[bool, str, Optional[str]]:
    """
    Обновляет Threads long-lived токен.
    Возвращает: (success, message, new_token_or_None)
    """
    url = "https://graph.threads.net/refresh_access_token"
    params = {
        "grant_type": "th_refresh_token",
        "access_token": current_token,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as resp:
                body = await resp.json()
                if resp.status == 200 and "access_token" in body:
                    new_token = body["access_token"]
                    expires_in = body.get("expires_in", 5184000)
                    days = expires_in // 86400
                    return True, f"Threads токен обновлён, действует {days} дней", new_token
                else:
                    error = body.get("error", {}).get("message", str(body))
                    return False, f"Threads refresh error: {error}", None
    except Exception as e:
        return False, f"Threads refresh exception: {e}", None


async def _refresh_instagram_token(current_token: str) -> Tuple[bool, str, Optional[str]]:
    """
    Обновляет Instagram long-lived токен.
    Возвращает: (success, message, new_token_or_None)
    """
    url = "https://graph.instagram.com/refresh_access_token"
    params = {
        "grant_type": "ig_refresh_token",
        "access_token": current_token,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as resp:
                body = await resp.json()
                if resp.status == 200 and "access_token" in body:
                    new_token = body["access_token"]
                    expires_in = body.get("expires_in", 5184000)
                    days = expires_in // 86400
                    return True, f"Instagram токен обновлён, действует {days} дней", new_token
                else:
                    error = body.get("error", {}).get("message", str(body))
                    return False, f"Instagram refresh error: {error}", None
    except Exception as e:
        return False, f"Instagram refresh exception: {e}", None


# ────────────────────────────── Публичный API ──────────────────────────────

def save_initial_tokens(
    threads_token: Optional[str] = None,
    ig_token: Optional[str] = None,
):
    """
    Сохраняет токены при первой настройке.
    Вызывать после получения long-lived токенов вручную.
    """
    data = _load_token_data()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if threads_token:
        data["threads_access_token"] = threads_token
        data["threads_issued"] = now
        logger.info("Threads token saved, issued at %s", now)

    if ig_token:
        data["ig_access_token"] = ig_token
        data["ig_issued"] = now
        logger.info("Instagram token saved, issued at %s", now)

    _save_token_data(data)


def get_active_threads_token() -> str:
    """Возвращает актуальный Threads-токен (из файла или env)."""
    data = _load_token_data()
    stored = data.get("threads_access_token", "")
    env_token = (os.getenv("THREADS_ACCESS_TOKEN") or "").strip()
    # Приоритет: сохранённый (обновлённый) > env
    return stored or env_token


def get_active_ig_token() -> str:
    """Возвращает актуальный Instagram-токен (из файла или env)."""
    data = _load_token_data()
    stored = data.get("ig_access_token", "")
    env_token = (os.getenv("IG_ACCESS_TOKEN") or "").strip()
    return stored or env_token


async def check_and_refresh_tokens(bot=None, admin_ids=None) -> dict:
    """
    Главная функция: проверяет возраст токенов и обновляет если нужно.
    Вызывается:
    - При старте бота
    - По расписанию каждый день
    
    Возвращает: {"threads": "ok|refreshed|error|skipped", "instagram": "ok|refreshed|error|skipped"}
    """
    data = _load_token_data()
    results = {}
    messages = []

    # ── Threads ──
    threads_token = data.get("threads_access_token") or (os.getenv("THREADS_ACCESS_TOKEN") or "").strip()
    if not threads_token:
        results["threads"] = "skipped"
    elif not data.get("threads_issued"):
        # Токен есть в env, но не сохранён — сохраняем и начинаем отслеживать
        save_initial_tokens(threads_token=threads_token)
        results["threads"] = "registered"
        messages.append("🔑 Threads токен зарегистрирован для автообновления")
    elif _needs_refresh(data, "threads"):
        ok, msg, new_token = await _refresh_threads_token(threads_token)
        if ok and new_token:
            data["threads_access_token"] = new_token
            data["threads_issued"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            data["threads_last_refresh"] = data["threads_issued"]
            _save_token_data(data)
            results["threads"] = "refreshed"
            messages.append(f"✅ {msg}")
        else:
            results["threads"] = "error"
            messages.append(f"⚠️ {msg}")
    else:
        issued = datetime.datetime.fromisoformat(data["threads_issued"])
        age = (datetime.datetime.now(datetime.timezone.utc) - issued).days
        results["threads"] = "ok"
        logger.info("Threads token OK, age: %d days (refresh at %d)", age, REFRESH_AFTER_DAYS)

    # ── Instagram ──
    ig_token = data.get("ig_access_token") or (os.getenv("IG_ACCESS_TOKEN") or "").strip()
    if not ig_token:
        results["instagram"] = "skipped"
    elif not data.get("ig_issued"):
        save_initial_tokens(ig_token=ig_token)
        results["instagram"] = "registered"
        messages.append("🔑 Instagram токен зарегистрирован для автообновления")
    elif _needs_refresh(data, "ig"):
        ok, msg, new_token = await _refresh_instagram_token(ig_token)
        if ok and new_token:
            data["ig_access_token"] = new_token
            data["ig_issued"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            data["ig_last_refresh"] = data["ig_issued"]
            _save_token_data(data)
            results["instagram"] = "refreshed"
            messages.append(f"✅ {msg}")
        else:
            results["instagram"] = "error"
            messages.append(f"⚠️ {msg}")
    else:
        issued = datetime.datetime.fromisoformat(data["ig_issued"])
        age = (datetime.datetime.now(datetime.timezone.utc) - issued).days
        results["instagram"] = "ok"
        logger.info("Instagram token OK, age: %d days (refresh at %d)", age, REFRESH_AFTER_DAYS)

    # Уведомляем админа если были обновления или ошибки
    if messages and bot and admin_ids:
        text = "🔐 Token Manager:\n\n" + "\n".join(messages)
        for aid in admin_ids:
            try:
                await bot.send_message(chat_id=aid, text=text)
            except Exception as e:
                logger.warning("Failed to notify admin %s: %s", aid, e)

    return results


def get_token_status() -> str:
    """Возвращает читаемый статус токенов для админ-команды."""
    data = _load_token_data()
    now = datetime.datetime.now(datetime.timezone.utc)
    lines = ["🔐 Статус Meta-токенов:\n"]

    for name, key in [("Threads", "threads"), ("Instagram", "ig")]:
        token = data.get(f"{key}_access_token", "")
        issued = data.get(f"{key}_issued", "")
        last_refresh = data.get(f"{key}_last_refresh", "")

        if not token and not (os.getenv(f"{key.upper()}_ACCESS_TOKEN") or os.getenv("THREADS_ACCESS_TOKEN" if key == "threads" else "IG_ACCESS_TOKEN") or "").strip():
            lines.append(f"  {name}: ❌ не настроен")
            continue

        if not issued:
            env_var = "THREADS_ACCESS_TOKEN" if key == "threads" else "IG_ACCESS_TOKEN"
            if (os.getenv(env_var) or "").strip():
                lines.append(f"  {name}: 🟡 есть в env, не зарегистрирован для автообновления")
            else:
                lines.append(f"  {name}: ❌ не настроен")
            continue

        issued_dt = datetime.datetime.fromisoformat(issued)
        age_days = (now - issued_dt).days
        expires_in = 60 - age_days
        status_emoji = "✅" if expires_in > 10 else "🟡" if expires_in > 0 else "🔴"

        line = f"  {name}: {status_emoji} возраст {age_days}д, истекает через {max(0, expires_in)}д"
        if last_refresh:
            line += f" (последний рефреш: {last_refresh[:10]})"
        lines.append(line)

    lines.append(f"\n  Автообновление: каждые {REFRESH_AFTER_DAYS} дней")
    return "\n".join(lines)
