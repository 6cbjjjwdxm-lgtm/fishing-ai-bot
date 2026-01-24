import json
import logging
import os
import re
import time
from typing import Dict, List

import aiohttp
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest

# =========================
# CACHE (для кнопок мест)
# =========================
CACHE_FILE = "places_cache.json"

def load_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

# =========================
# VERTEX AI SEARCH CONFIG
# =========================
VERTEX_PROJECT_ID = (os.getenv("VERTEX_PROJECT_ID") or "").strip()
VERTEX_LOCATION = (os.getenv("VERTEX_LOCATION") or "global").strip()
VERTEX_ENGINE_ID = (os.getenv("VERTEX_ENGINE_ID") or "").strip()
GOOGLE_SA_JSON = (os.getenv("GOOGLE_SA_JSON") or "").strip()

# access token cache
_token_cache = {"token": None, "exp": 0}

def _get_access_token() -> str:
    now = int(time.time())
    if _token_cache["token"] and now < _token_cache["exp"]:
        return _token_cache["token"]

    if not GOOGLE_SA_JSON:
        raise RuntimeError("GOOGLE_SA_JSON is not set")

    info = json.loads(GOOGLE_SA_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    creds.refresh(GoogleAuthRequest())

    token = creds.token
    _token_cache["token"] = token
    _token_cache["exp"] = now + 3000  # ~50 минут
    return token

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def _pick_first(*vals: str) -> str:
    for v in vals:
        v = _norm(v)
        if v:
            return v
    return ""

async def vertex_search(query: str, page_size: int = 7) -> List[Dict]:
    if not (VERTEX_PROJECT_ID and VERTEX_LOCATION and VERTEX_ENGINE_ID):
        raise RuntimeError("VERTEX_PROJECT_ID / VERTEX_LOCATION / VERTEX_ENGINE_ID not set")

    token = _get_access_token()

    # В документации встречаются servingConfigs:
    # .../engines/*/servingConfigs/default_serving_config (и варианты default_search в UI)
    # Мы пробуем оба варианта по очереди.
    serving_configs = [
        f"projects/{VERTEX_PROJECT_ID}/locations/{VERTEX_LOCATION}/collections/default_collection"
        f"/engines/{VERTEX_ENGINE_ID}/servingConfigs/default_search",
        f"projects/{VERTEX_PROJECT_ID}/locations/{VERTEX_LOCATION}/collections/default_collection"
        f"/engines/{VERTEX_ENGINE_ID}/servingConfigs/default_serving_config",
    ]

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
    "query": query,
    "pageSize": max(1, min(int(page_size), 10)),
    "safeSearch": True,
    "contentSearchSpec": {"snippetSpec": {"returnSnippet": True}},
    }

    timeout = aiohttp.ClientTimeout(total=20)

    last_error = None
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for sc in serving_configs:
            url = f"https://discoveryengine.googleapis.com/v1beta/{sc}:search"
            try:
                async with session.post(url, headers=headers, json=payload) as r:
                    data = await r.json()
                    if r.status != 200:
                        last_error = f"HTTP {r.status}: {data}"
                        continue
                    return _parse_vertex_results(data)
            except Exception as e:
                last_error = str(e)

    raise RuntimeError(f"Vertex search failed: {last_error}")

def _parse_vertex_results(data: dict) -> List[Dict]:
    out = []
    for item in data.get("results", []) or []:
        doc = item.get("document") or {}
        derived = doc.get("derivedStructData") or {}

        title = _pick_first(
            derived.get("title"),
            doc.get("title"),
            derived.get("htmlTitle"),
        )
        link = _pick_first(
            derived.get("link"),
            derived.get("url"),
            doc.get("id"),
        )
        snippet = _pick_first(
            derived.get("snippet"),
            derived.get("description"),
            derived.get("htmlSnippet"),
        )

        # если ссылок нет — всё равно добавим, но ссылка будет пустая
        out.append({"title": title, "link": link, "snippet": snippet})

    return out

async def get_rusfishing_context(user_query: str) -> str:
    # тут можно добавить легкую нормализацию запроса
    query = _norm(user_query)
    if not query:
        return ""

    results = await vertex_search(query, page_size=7)
    if not results:
        return ""

    lines = []
    links = []
    for i, r in enumerate(results[:7], 1):
        t = r.get("title") or "Без названия"
        s = r.get("snippet") or ""
        u = r.get("link") or ""
        lines.append(f"{i}. {t} — {s}".strip())
        if u:
            links.append(u)

    # дедуп ссылок
    uniq_links = []
    seen = set()
    for u in links:
        if u in seen:
            continue
        seen.add(u)
        uniq_links.append(u)

    return (
        "ВЫЖИМКА С RUSFISHING (Vertex AI Search):\n"
        + "\n".join(lines)
        + "\n\nССЫЛКИ ДЛЯ ПРОВЕРКИ:\n"
        + "\n".join(f"- {u}" for u in uniq_links[:7])
    )


