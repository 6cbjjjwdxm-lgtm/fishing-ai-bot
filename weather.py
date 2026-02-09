import datetime
import json
import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(sys.stdout)], force=True)

OPENWEATHER_API_KEY = (os.getenv("OPENWEATHER_API_KEY") or "").strip()


def get_moon_phase() -> str:
    phases = ["🌑 Новолуние", "🌒 Растущая", "🌓 1-я четверть", "🌔 Растущая",
              "🌕 Полнолуние", "🌖 Убывающая", "🌗 Последняя четверть", "🌘 Старая"]
    days = (datetime.date.today() - datetime.date(2000, 1, 6)).days
    return phases[int(((days % 29.53) / 29.53) * 8) % 8]


def season_by_date(d: datetime.date) -> str:
    m = d.month
    if m in (12, 1, 2):
        return "winter"
    if m in (3, 4, 5):
        return "spring"
    if m in (6, 7, 8):
        return "summer"
    return "autumn"


def hpa_to_mm(hpa: float) -> int:
    return int(hpa * 0.75006)


def _clean_city_tokens(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"[?!.,;:()\[\]\"'«»]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _guess_nominative_ru(word: str) -> str:
    w = (word or "").strip()
    lw = w.lower()
    if len(w) <= 3:
        return w
    if lw.endswith("ве") and len(w) >= 5:
        return w[:-2] + "ва"   # Москве -> Москва
    if lw.endswith("ге") and len(w) >= 4:
        return w[:-1] + "а"    # Калуге -> Калуга
    if lw.endswith("ске") and len(w) >= 6:
        return w[:-2]          # Подольске -> Подольск
    if lw.endswith("е") and len(w) >= 5:
        return w[:-1]          # Туле -> Тул
    return w


def _city_variants(city: str) -> List[str]:
    city = _clean_city_tokens(city)
    if not city:
        return []
    parts = city.split()
    guessed = " ".join(_guess_nominative_ru(p) for p in parts)

    variants = []
    for v in [city, guessed]:
        v = v.strip()
        if v and v not in variants:
            variants.append(v)

    if len(parts) > 1:
        last = parts[-1]
        lastg = _guess_nominative_ru(last)
        for v in [last, lastg]:
            v = v.strip()
            if v and v not in variants:
                variants.append(v)

    return variants


async def _get_text(url: str, params: Dict[str, Any]) -> Tuple[int, str]:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params) as r:
            return r.status, await r.text()


async def geocode_any(name: str, country: str = "RU", limit: int = 5) -> Optional[Dict[str, Any]]:
    """
    Direct geocoding: name -> lat/lon. Можно limit до 5. [web:309]
    """
    if not OPENWEATHER_API_KEY:
        logging.warning("GEOCODE skipped: missing OPENWEATHER_API_KEY")
        return None

    url = "http://api.openweathermap.org/geo/1.0/direct"
    variants = _city_variants(name)
    if not variants:
        return None

    queries = []
    for v in variants:
        queries.append(f"{v},{country}")
        queries.append(v)

    for q in queries:
        status, body = await _get_text(url, {"q": q, "limit": max(1, min(int(limit), 5)), "appid": OPENWEATHER_API_KEY})
        logging.warning("GEOCODE q=%s status=%s body=%s", q, status, body[:200])
        if status != 200:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        if isinstance(data, list) and data:
            item = data[0]
            if "lat" in item and "lon" in item:
                item["_q_used"] = q
                return item
    return None


async def reverse_geocode(lat: float, lon: float, limit: int = 1) -> Optional[Dict[str, Any]]:
    """
    Reverse geocoding: lat/lon -> ближайшее место. [web:309]
    """
    if not OPENWEATHER_API_KEY:
        return None
    url = "http://api.openweathermap.org/geo/1.0/reverse"
    status, body = await _get_text(url, {"lat": lat, "lon": lon, "limit": max(1, min(int(limit), 5)), "appid": OPENWEATHER_API_KEY})
    logging.warning("REVERSE lat=%s lon=%s status=%s body=%s", lat, lon, status, body[:200])
    if status != 200:
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    if isinstance(data, list) and data:
        return data[0]
    return None


async def fetch_forecast_by_latlon(lat: float, lon: float) -> Optional[Dict[str, Any]]:
    url = "https://api.openweathermap.org/data/2.5/forecast"
    if not OPENWEATHER_API_KEY:
        return None
    status, body = await _get_text(url, {
        "lat": lat, "lon": lon,
        "appid": OPENWEATHER_API_KEY,
        "units": "metric",
        "lang": "ru",
    })
    logging.warning("FORECAST lat=%s lon=%s status=%s body=%s", lat, lon, status, body[:200])
    if status != 200:
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    return data if data.get("cod") == "200" else None


def group_by_day(forecast_list: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    by_day: Dict[str, List[Dict[str, Any]]] = {}
    for item in forecast_list or []:
        dt_txt = item.get("dt_txt") or ""
        day = dt_txt.split(" ")[0] if " " in dt_txt else ""
        if day:
            by_day.setdefault(day, []).append(item)
    return by_day


def day_aggregate(items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not items:
        return None
    temps, pressures, winds, descs = [], [], [], []
    has_rain, has_snow = False, False
    for it in items:
        main = it.get("main") or {}
        wind = it.get("wind") or {}
        weather = (it.get("weather") or [{}])
        desc = (weather[0].get("description") or "").strip()
        if main.get("temp") is not None:
            temps.append(float(main["temp"]))
        if main.get("pressure") is not None:
            pressures.append(float(main["pressure"]))
        if wind.get("speed") is not None:
            winds.append(float(wind["speed"]))
        if desc:
            descs.append(desc)
        if isinstance(it.get("rain"), dict):
            has_rain = True
        if isinstance(it.get("snow"), dict):
            has_snow = True

    def avg(xs): return (sum(xs) / len(xs)) if xs else None

    desc = max(set(descs), key=descs.count) if descs else ""
    precip = "snow" if has_snow else ("rain" if has_rain else "none")
    return {
        "temp_c": avg(temps),
        "pressure_mm": hpa_to_mm(avg(pressures)) if pressures else None,
        "wind_ms": avg(winds),
        "desc": desc,
        "precip": precip,
    }


async def forecast_by_coords(lat: float, lon: float, day_offset: int) -> Optional[Dict[str, Any]]:
    data = await fetch_forecast_by_latlon(lat, lon)
    if not data:
        return None

    forecasts = data.get("list") or []
    target_date = datetime.date.today() + datetime.timedelta(days=max(0, int(day_offset)))
    target_str = target_date.strftime("%Y-%m-%d")

    day_items = [f for f in forecasts if target_str in (f.get("dt_txt") or "")]
    if not day_items:
        return None
    best = next((f for f in day_items if "12:00:00" in (f.get("dt_txt") or "")), day_items[0])

    main = best.get("main") or {}
    wind = best.get("wind") or {}
    weather = (best.get("weather") or [{}])

    nearest = await reverse_geocode(lat, lon, limit=1)

    return {
        "date": target_str,
        "temp_c": float(main.get("temp")) if main.get("temp") is not None else None,
        "pressure_mm": hpa_to_mm(float(main.get("pressure"))) if main.get("pressure") is not None else None,
        "wind_ms": float(wind.get("speed")) if wind.get("speed") is not None else None,
        "desc": (weather[0].get("description") or "").strip(),
        "moon": get_moon_phase(),
        "season": season_by_date(target_date),
        "resolved": {
            "lat": lat, "lon": lon,
            "nearest_name": (nearest or {}).get("name"),
            "nearest_country": (nearest or {}).get("country"),
        }
    }


async def get_weather_for_day(place: str, day_offset: int) -> Optional[Dict[str, Any]]:
    geo = await geocode_any(place)
    if not geo:
        return None
    return await forecast_by_coords(float(geo["lat"]), float(geo["lon"]), day_offset)


async def get_weather_5days(place: str) -> Optional[List[Dict[str, Any]]]:
    geo = await geocode_any(place)
    if not geo:
        return None
    data = await fetch_forecast_by_latlon(float(geo["lat"]), float(geo["lon"]))
    if not data:
        return None

    forecasts = data.get("list") or []
    by_day = group_by_day(forecasts)
    days = sorted(by_day.keys())[:5]
    out: List[Dict[str, Any]] = []

    for day in days:
        agg = day_aggregate(by_day[day])
        if not agg:
            continue
        d = datetime.date.fromisoformat(day)
        agg.update({
            "date": day,
            "moon": get_moon_phase(),
            "season": season_by_date(d),
            "resolved": {"name": geo.get("name"), "lat": geo.get("lat"), "lon": geo.get("lon"), "q_used": geo.get("_q_used")},
        })
        out.append(agg)

    return out


