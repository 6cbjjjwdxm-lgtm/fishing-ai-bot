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
    "Ока": "https://www.rusfishing.ru/forum/forums/oka.146/",
    "Москва-река": "https://www.rusfishing.ru/forum/forums/moskva-reka.147/",
    "Иваньковское вдхр": "https://www.rusfishing.ru/forum/forums/ivankovskoe-vdxr.53/",
    "Рузуское вдхр": "https://www.rusfishing.ru/forum/forums/ruzskoe-vdxr.54/",
    "Яузское вдхр": "https://www.rusfishing.ru/forum/forums/jauzskoe-vdxr.56/",
    "Можайское вдхр": "https://www.rusfishing.ru/forum/forums/mozhajskoe-vdxr.55/"
}

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
}

async def fetch_forum_page(session, url):
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.text()
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
