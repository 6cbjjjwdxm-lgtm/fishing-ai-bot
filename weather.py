import datetime
import os
from typing import Any, Dict, List, Optional

import aiohttp

OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")


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


async def fetch_openweather_forecast(city: str) -> Optional[Dict[str, Any]]:
    if not OPENWEATHER_API_KEY or not city:
        return None

    url = "https://api.openweathermap.org/data/2.5/forecast"
    base_params = {"appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "ru"}

    queries = [
        f"{city}, Moscow Oblast, RU",
        f"{city}, RU",
        city,
    ]

    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for q in queries:
            params = dict(base_params)
            params["q"] = q
            try:
                async with session.get(url, params=params) as r:
                    if r.status != 200:
                        continue
                    data = await r.json()
                    if data.get("cod") == "200":
                        return data
            except Exception:
                continue
    return None


def group_by_day(forecast_list: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    by_day: Dict[str, List[Dict[str, Any]]] = {}
    for item in forecast_list or []:
        dt_txt = item.get("dt_txt") or ""
        day = dt_txt.split(" ")[0] if " " in dt_txt else ""
        if not day:
            continue
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

    def avg(xs):
        return (sum(xs) / len(xs)) if xs else None

    desc = max(set(descs), key=descs.count) if descs else ""
    precip = "none"
    if has_snow:
        precip = "snow"
    elif has_rain:
        precip = "rain"

    return {
        "temp_c": avg(temps),
        "pressure_mm": hpa_to_mm(avg(pressures)) if pressures else None,
        "wind_ms": avg(winds),
        "desc": desc,
        "precip": precip,
    }


async def get_weather_for_day(city: str, day_offset: int) -> Optional[Dict[str, Any]]:
    data = await fetch_openweather_forecast(city)
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

    return {
        "date": target_str,
        "temp_c": float(main.get("temp")) if main.get("temp") is not None else None,
        "pressure_mm": hpa_to_mm(float(main.get("pressure"))) if main.get("pressure") is not None else None,
        "wind_ms": float(wind.get("speed")) if wind.get("speed") is not None else None,
        "desc": (weather[0].get("description") or "").strip(),
        "moon": get_moon_phase(),
        "season": season_by_date(target_date),
    }


async def get_weather_5days(city: str) -> Optional[List[Dict[str, Any]]]:
    data = await fetch_openweather_forecast(city)
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
        })
        out.append(agg)

    return out
