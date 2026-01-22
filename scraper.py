import aiohttp
import asyncio
import logging
import json
import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# =========================
# CACHE
# =========================
CACHE_FILE = "places_cache.json"

RIVER_URLS = {
    "Ока": "https://www.rusfishing.ru/forum/forums/reka-oka.73/",
    "Река Ока": "https://www.rusfishing.ru/forum/forums/reka-oka.73/",
    "Москва-река": "https://www.rusfishing.ru/forum/forums/moskva-reka.147/",
    "Иваньковское вдхр": "https://www.rusfishing.ru/forum/forums/ivankovskoe-vdxr.53/",
    "Рузуское вдхр": "https://www.rusfishing.ru/forum/forums/ruzskoe-vdxr.54/",
    "Яузское вдхр": "https://www.rusfishing.ru/forum/forums/jauzskoe-vdxr.56/",
    "Можайское вдхр": "https://www.rusfishing.ru/forum/forums/mozhajskoe-vdxr.55/",
    "Истринское вдхр": "https://www.rusfishing.ru/forum/forums/istrinskoye-vodokhranilishche.69/",
}

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
}

async def fetch_forum_page(session, url: str) -> str | None:
    try:
        async with session.get(url, headers=headers, allow_redirects=True) as resp:
            txt = await resp.text(errors="ignore")
            logging.warning("RF HTTP %s %s", resp.status, url)
            logging.warning("RF BODY_HEAD %s", (txt or "")[:200].replace("\n", " "))
            if resp.status == 200 and txt:
                return txt
            return None
    except Exception:
        logging.exception("Ошибка парсинга %s", url)
        return None

def extract_locations_from_html(html: str, river_name: str):
    soup = BeautifulSoup(html, "lxml")
    locations = set()

    threads = soup.select(".structItem-title a")
    for thread in threads:
        text = thread.get_text(strip=True)
        clean_name = re.sub(r"\(.*?\)", "", text)
        clean_name = clean_name.replace(river_name, "").strip()

        if 2 < len(clean_name) < 40:
            locations.add(clean_name.strip(" .,-"))

    return sorted(list(locations))

async def update_rusfishing_cache():
    logging.info("🎣 Запуск обновления базы Русфишинга...")

    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except FileNotFoundError:
        cache = {}

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for river, url in RIVER_URLS.items():
            logging.info("Парсим: %s ...", river)
            html = await fetch_forum_page(session, url)
            if html:
                locs = extract_locations_from_html(html, river)
                if locs:
                    cache[river] = {"url": url, "locations": locs}
            await asyncio.sleep(1)

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    logging.info("✅ База мест обновлена! Всего рек: %s", len(cache))
    return cache

def load_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

# =========================
# RUSFISHING "RAG" LAYER
# =========================

STOP_TITLE_WORDS = {
    "отчёт", "отчет", "отчеты", "отчёты", "вопрос", "вопросы", "помогите",
    "лёд", "лед", "запрет", "нерест", "болталка", "флуд", "объявления"
}

FISH_ALIASES = {
    "судак": ["судак", "судач", "клыкаст"],
    "щука": ["щука", "щур", "пятнист"],
    "окунь": ["окун", "полосат"],
    "жерех": ["жерех"],
}

WATERBODY_ALIASES = {
    "можайка": "Можайское вдхр",
    "можайское": "Можайское вдхр",
    "истра": "Истринское вдхр",
    "истринское": "Истринское вдхр",
    "истринское вдхр": "Истринское вдхр",
    "руза": "Рузуское вдхр",
    "рузское": "Рузуское вдхр",
    "иванька": "Иваньковское вдхр",
    "иваньковское": "Иваньковское вдхр",
    "яуза": "Яузское вдхр",
    "ока": "Ока",
    "москва-река": "Москва-река",
    "москва река": "Москва-река",
}

def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()

def find_forum_url_for_waterbody(user_text: str, cache: dict) -> str | None:
    t = normalize_text(user_text)

    for k, canonical in WATERBODY_ALIASES.items():
        if k in t:
            if canonical in cache and cache[canonical].get("url"):
                return cache[canonical]["url"]
            if canonical in RIVER_URLS:
                return RIVER_URLS[canonical]

    for river in cache.keys():
        if normalize_text(river) in t and cache[river].get("url"):
            return cache[river]["url"]

    for river, url in RIVER_URLS.items():
        if normalize_text(river) in t:
            return url

    return None

def extract_thread_links_from_forum_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for a in soup.select(".structItem-title a"):
        title = a.get_text(" ", strip=True)
        href = a.get("href")
        if not href or not title:
            continue
        url = urljoin("https://www.rusfishing.ru", href)
        out.append({"title": title, "url": url})
    return out

def looks_like_good_thread_title(title: str) -> bool:
    t = normalize_text(title)
    if len(t) < 3:
        return False
    for w in STOP_TITLE_WORDS:
        if w in t:
            return False
    return True

async def get_top_threads_in_forum(session, forum_url: str, limit: int = 6) -> list[dict]:
    html = await fetch_forum_page(session, forum_url)
    if not html:
        return []
    threads = extract_thread_links_from_forum_html(html)
    out = []
    for th in threads:
        if looks_like_good_thread_title(th["title"]):
            out.append(th)
        if len(out) >= limit:
            break
    return out

async def search_threads_in_forum(session, forum_url: str, query_words: list[str], pages: int = 2) -> list[dict]:
    query_words = [normalize_text(w) for w in query_words if w]
    results = []

    for p in range(1, pages + 1):
        # FIX: тут должен быть forum_url, а не thread_url
        url = forum_url if p == 1 else forum_url.rstrip("/") + f"/page-{p}/"
        html = await fetch_forum_page(session, url)
        if not html:
            continue

        threads = extract_thread_links_from_forum_html(html)
        for th in threads:
            title = th["title"]
            if not looks_like_good_thread_title(title):
                continue
            title_norm = normalize_text(title)
            score = sum(1 for w in query_words if w and w in title_norm)
            if score > 0:
                results.append({**th, "score": score})

        await asyncio.sleep(0.6)

    results.sort(key=lambda x: x["score"], reverse=True)

    seen = set()
    uniq = []
    for r in results:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        uniq.append(r)
    return uniq[:6]

def extract_posts_from_thread_html(html: str, limit: int = 10) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    posts = []

    items = soup.select("article.message")
    if not items:
        items = soup.select(".message")

    for it in items[:limit]:
        a = it.select_one("a[href*='#post-'], a.u-concealed[href]")
        permalink = urljoin("https://www.rusfishing.ru", a.get("href")) if a and a.get("href") else None

        body = it.select_one(".message-body") or it.select_one(".bbWrapper") or it
        text = body.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) > 320:
            text = text[:320].rsplit(" ", 1)[0] + "…"
        if len(text) < 40:
            continue

        time_el = it.select_one("time")
        date_str = time_el.get("datetime") if time_el else ""

        posts.append({"snippet": text, "url": permalink, "date": date_str})

    return posts

async def fetch_thread_post_snippets(session, thread_url: str, max_pages: int = 2) -> list[dict]:
    all_posts = []
    for p in range(1, max_pages + 1):
        # FIX: нужна форма /page-2/
        url = thread_url if p == 1 else thread_url.rstrip("/") + f"/page-{p}/"
        html = await fetch_forum_page(session, url)
        if not html:
            continue
        all_posts.extend(extract_posts_from_thread_html(html, limit=12))
        await asyncio.sleep(0.6)

    seen = set()
    uniq = []
    for it in all_posts:
        key = (it.get("url") or "", it["snippet"][:80])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    return uniq[:12]

def extract_fish_keywords(user_text: str) -> list[str]:
    t = normalize_text(user_text)
    keys = []
    for fish, arr in FISH_ALIASES.items():
        if any(a in t for a in arr):
            keys.append(fish)
            keys.extend(arr[:2])
    return list(dict.fromkeys(keys))

async def get_rusfishing_context(user_query: str, places_cache: dict) -> str:
    forum_url = find_forum_url_for_waterbody(user_query, places_cache)
    if not forum_url:
        return ""

    fish_keys = extract_fish_keywords(user_query)
    query_words = fish_keys if fish_keys else ["где", "стоит", "точки", "ям", "бровк", "залив"]

    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        threads = await search_threads_in_forum(session, forum_url, query_words, pages=2)

        # FIX: fallback если по заголовкам ничего не нашли
        if not threads:
            threads = await get_top_threads_in_forum(session, forum_url, limit=5)

        if not threads:
            return ""

        picked = threads[:3]
        snippets = []
        source_links = []

        for th in picked:
            posts = await fetch_thread_post_snippets(session, th["url"], max_pages=1)

            if th["url"] not in source_links:
                source_links.append(th["url"])

            for p in posts[:4]:
                if p.get("url"):
                    source_links.append(p["url"])
                snippets.append(f"- {p['snippet']} ({p.get('url') or th['url']})")

        snippets = snippets[:10]
        source_links = list(dict.fromkeys(source_links))[:6]

        return (
            "ВЫЖИМКА С ФОРУМА (короткие фрагменты для фактов, проверяй по ссылкам):\n"
            + "\n".join(snippets)
            + "\n\nССЫЛКИ ДЛЯ ПРОВЕРКИ:\n"
            + "\n".join(f"- {u}" for u in source_links)
        )

