import aiohttp
import asyncio
import logging
import json
import re
from bs4 import BeautifulSoup

# Файл, куда сохраняем базу
CACHE_FILE = "places_cache.json"

# Ссылка на карты разделов (можно расширять)
# ID форумов Русфишинга: Ока=146, Москва-река=147 (примерные ID, лучше брать полные ссылки)
# Для простоты начнем с маппинга, который будем пополнять
RIVER_URLS = {
    "Ока": "https://www.rusfishing.ru/forum/forums/reka-oka.73/",
    "Река Ока": "https://www.rusfishing.ru/forum/forums/reka-oka.73/",
    "на оке": "https://www.rusfishing.ru/forum/forums/reka-oka.73/",
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

async def fetch_forum_page(session, url):
    try:
        async with session.get(url, headers=headers, allow_redirects=True) as resp:
            txt = await resp.text(errors="ignore")
            logging.warning("RF HTTP %s %s", resp.status, url)
            # покажем первые символы — сразу увидишь "enable cookies / javascript" или капчу
            logging.warning("RF BODY_HEAD %s", (txt or "")[:200].replace("\n", " "))
            if resp.status == 200 and txt:
                return txt
            return None
    except Exception as e:
        logging.error(f"Ошибка парсинга {url}: {e}")
    return None

def extract_locations_from_html(html, river_name):
    soup = BeautifulSoup(html, "lxml")
    locations = set()
    
    # Ищем заголовки тем (обычно в div.structItem-title a)
    threads = soup.select(".structItem-title a")
    
    for thread in threads:
        text = thread.get_text(strip=True)
        # Убираем лишнее, оставляем суть.
        # Часто пишут "Ока в районе Каширы" -> "Кашира"
        # Или просто "Ступино"
        
        # Простая эвристика: если название темы короткое (< 4 слов), берем целиком
        # Если длинное, пытаемся найти слова с большой буквы, исключая предлоги
        
        # Очистка от слов-паразитов
        clean_name = re.sub(r'\(.*?\)', '', text) # убрать скобки
        clean_name = clean_name.replace(river_name, "").strip() # убрать название реки
        
        if len(clean_name) > 2 and len(clean_name) < 40:
             locations.add(clean_name.strip(" .,-"))

    return sorted(list(locations))

async def update_rusfishing_cache():
    """Главная функция обновления базы"""
    logging.info("🎣 Запуск обновления базы Русфишинга...")
    
    # 1. Загружаем текущий кэш
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except FileNotFoundError:
        cache = {}

    async with aiohttp.ClientSession() as session:
        for river, url in RIVER_URLS.items():
            logging.info(f"Парсим: {river}...")
            html = await fetch_forum_page(session, url)
            if html:
                locs = extract_locations_from_html(html, river)
                if locs:
                    # Сохраняем url и список мест
                    cache[river] = {
                        "url": url,
                        "locations": locs
                    }
            
            # Вежливая пауза
            await asyncio.sleep(2)

    # 2. Сохраняем обновленный кэш
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    
    logging.info(f"✅ База мест обновлена! Всего рек: {len(cache)}")
    return cache

def load_cache():
    """Синхронная загрузка для старта"""
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
# =========================
# RUSFISHING "RAG" LAYER (snippets + links)
# =========================

from urllib.parse import urljoin

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
    # можно расширять по мере запросов
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
    """
    Возвращает список {title, url}
    """
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
    # убираем мусор по стоп-словам
    for w in STOP_TITLE_WORDS:
        if w in t:
            return False
    return True

async def search_threads_in_forum(session, forum_url: str, query_words: list[str], pages: int = 2) -> list[dict]:
    """
    Ищем релевантные темы по заголовкам в пределах N страниц раздела.
    Возвращает top threads: [{title,url}, ...]
    """
    query_words = [normalize_text(w) for w in query_words if w]
    results = []

    for p in range(1, pages + 1):
        url = thread_url if p == 1 else thread_url.rstrip("/") + f"/page-{p}/"
        html = await fetch_forum_page(session, url)
        if not html:
            continue
        threads = extract_thread_links_from_forum_html(html)

        for th in threads:
            title = th["title"]
            if not looks_like_good_thread_title(title):
                continue
            title_norm = normalize_text(title)
            # простое ранжирование: сколько слов запроса встретилось в заголовке
            score = sum(1 for w in query_words if w and w in title_norm)
            if score > 0:
                results.append({**th, "score": score})

        await asyncio.sleep(1)

    results.sort(key=lambda x: x["score"], reverse=True)
    # уникализируем по url
    seen = set()
    uniq = []
    for r in results:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        uniq.append(r)
    return uniq[:6]

def extract_posts_from_thread_html(html: str, limit: int = 10) -> list[dict]:
    """
    XenForo: сообщения обычно в article.message (или div.message)
    Вытаскиваем: text snippet + permalink (на конкретный пост, если есть) + date (если есть).
    """
    soup = BeautifulSoup(html, "lxml")
    posts = []

    # наиболее типичные контейнеры XenForo
    items = soup.select("article.message")
    if not items:
        items = soup.select(".message")

    for it in items[:limit]:
        # permalink (обычно a.u-concealed или a[href*='#post-'])
        a = it.select_one("a[href*='#post-'], a.u-concealed[href]")
        permalink = None
        if a and a.get("href"):
            permalink = urljoin("https://www.rusfishing.ru", a.get("href"))

        # текст поста
        body = it.select_one(".message-body") or it.select_one(".bbWrapper") or it
        text = body.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()

        # режем до 240-320 символов, чтобы не тащить простыни
        if len(text) > 320:
            text = text[:320].rsplit(" ", 1)[0] + "…"

        if len(text) < 40:
            continue

        # дата (не всегда доступна без доп. селекторов)
        time_el = it.select_one("time")
        date_str = time_el.get("datetime") if time_el else ""

        posts.append({
            "snippet": text,
            "url": permalink,
            "date": date_str
        })

    # фильтруем посты без ссылки (если не нашли якорь — всё равно можно ссылать на тему)
    return posts

async def fetch_thread_post_snippets(session, thread_url: str, max_pages: int = 2) -> list[dict]:
    """
    Берём 1–2 страницы темы (обычно последние важнее, но тут упрощённо).
    Для улучшения можно потом искать last page, но это уже усложнение.
    """
    all_posts = []
    for p in range(1, max_pages + 1):
        url = thread_url if p == 1 else thread_url.rstrip("/") + f"page-{p}"
        html = await fetch_forum_page(session, url)
        if not html:
            continue
        all_posts.extend(extract_posts_from_thread_html(html, limit=12))
        await asyncio.sleep(1)

    # уникализируем по snippet/url
    seen = set()
    uniq = []
    for p in all_posts:
        key = (p.get("url") or "", p["snippet"][:80])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq[:12]

def extract_fish_keywords(user_text: str) -> list[str]:
    t = normalize_text(user_text)
    keys = []
    for fish, arr in FISH_ALIASES.items():
        if any(a in t for a in arr):
            keys.append(fish)
            keys.extend(arr[:2])
    return list(dict.fromkeys(keys))  # uniq preserve order

async def get_rusfishing_context(user_query: str, places_cache: dict) -> str:
    """
    Главная функция для bot.py:
    - находим форум-раздел водоема
    - находим 2–4 релевантных темы
    - вытаскиваем сниппеты + ссылки
    - возвращаем короткий контекст для LLM
    """
    forum_url = find_forum_url_for_waterbody(user_query, places_cache)
    if not forum_url:
        return ""

    fish_keys = extract_fish_keywords(user_query)
    # если рыбу не распознали — всё равно попробуем по общим словам
    query_words = fish_keys if fish_keys else ["где", "стоит", "точки", "ям", "бровк", "залив"]

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        threads = await search_threads_in_forum(session, forum_url, query_words, pages=2)
        if not threads:
            return ""

        picked = threads[:3]
        snippets = []
        source_links = []

        for th in picked:
            posts = await fetch_thread_post_snippets(session, th["url"], max_pages=1)
            # если нет пермалинков — хотя бы ссылка на тему
            if th["url"] not in source_links:
                source_links.append(th["url"])
            for p in posts[:4]:
                if p.get("url"):
                    source_links.append(p["url"])
                snippets.append(f"- {p['snippet']} ({p.get('url') or th['url']})")

        # ограничим, чтобы не “тащить подчистую”
        snippets = snippets[:10]
        source_links = list(dict.fromkeys(source_links))[:6]

        return (
            "ВЫЖИМКА С ФОРУМА (короткие фрагменты для фактов, проверяй по ссылкам):\n"
            + "\n".join(snippets)
            + "\n\nССЫЛКИ ДЛЯ ПРОВЕРКИ:\n"
            + "\n".join(f"- {u}" for u in source_links)
        )
