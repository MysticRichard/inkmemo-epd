from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from .models import Weather


_log = logging.getLogger("inkmemo.weather")
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open_json(request, timeout: float, attempts: int = 3) -> dict:
    # 直连会遇到偶发 TLS 中断（SSL: UNEXPECTED_EOF），单次失败不该让天气整块空白。
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with _DIRECT_OPENER.open(request, timeout=timeout) as response:
                return json.load(response)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.0)
    raise last if last else RuntimeError("weather request failed")


WEATHER_CODES = {
    0: ("晴", "sun"),
    1: ("大致晴", "partly"),
    2: ("局部多云", "partly"),
    3: ("阴", "cloud"),
    45: ("雾", "fog"),
    48: ("雾凇", "fog"),
    51: ("小毛毛雨", "rain"),
    53: ("毛毛雨", "rain"),
    55: ("强毛毛雨", "rain"),
    56: ("冻毛毛雨", "rain"),
    57: ("强冻毛毛雨", "rain"),
    61: ("小雨", "rain"),
    63: ("中雨", "rain"),
    65: ("大雨", "rain"),
    66: ("冻雨", "rain"),
    67: ("强冻雨", "rain"),
    71: ("小雪", "snow"),
    73: ("中雪", "snow"),
    75: ("大雪", "snow"),
    77: ("米雪", "snow"),
    80: ("阵雨", "rain"),
    81: ("强阵雨", "rain"),
    82: ("暴雨", "rain"),
    85: ("阵雪", "snow"),
    86: ("强阵雪", "snow"),
    95: ("雷暴", "thunder"),
    96: ("雷暴伴冰雹", "thunder"),
    99: ("强雷暴伴冰雹", "thunder"),
}


@dataclass(frozen=True)
class WeatherLocation:
    city: str
    latitude: float
    longitude: float


def _city_from_payload(payload: dict) -> str | None:
    for key in ("city", "locality", "principalSubdivision"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _reverse_city(latitude: float, longitude: float, timeout: float = 8.0) -> str | None:
    query = urllib.parse.urlencode(
        {
            "latitude": latitude,
            "longitude": longitude,
            "localityLanguage": "zh",
        }
    )
    request = urllib.request.Request(
        f"https://api.bigdatacloud.net/data/reverse-geocode-client?{query}",
        headers={"User-Agent": "InkMemo/0.1"},
    )
    return _city_from_payload(_open_json(request, timeout))


async def _windows_position() -> tuple[float, float]:
    from winrt.windows.devices.geolocation import Geolocator

    if int(await Geolocator.request_access_async()) != 1:
        raise RuntimeError("Windows location access denied")
    position = await Geolocator().get_geoposition_async()
    coordinate = position.coordinate
    return float(coordinate.latitude), float(coordinate.longitude)


def _geocode_city(city: str, timeout: float = 8.0) -> WeatherLocation | None:
    query = urllib.parse.urlencode(
        {"name": city, "count": 1, "language": "zh", "format": "json"}
    )
    request = urllib.request.Request(
        f"https://geocoding-api.open-meteo.com/v1/search?{query}",
        headers={"User-Agent": "InkMemo/0.1"},
    )
    payload = _open_json(request, timeout)
    results = payload.get("results") or []
    if not results:
        return None
    hit = results[0]
    return WeatherLocation(
        city=str(hit.get("name") or city),
        latitude=float(hit["latitude"]),
        longitude=float(hit["longitude"]),
    )


async def resolve_weather_location(
    fallback_city: str,
    fallback_latitude: float,
    fallback_longitude: float,
    timeout: float = 8.0,
) -> WeatherLocation:
    # 用户填了城市就按城市查；Windows 定位只在城市留空时兜底，避免系统定位
    # 与用户填写的地区不一致。
    configured = (fallback_city or "").strip()
    if configured:
        try:
            located = await asyncio.wait_for(
                asyncio.to_thread(_geocode_city, configured, timeout),
                timeout=timeout + 2,
            )
        except Exception as exc:
            _log.info("geocode failed: %s", type(exc).__name__)
            located = None
        if located is not None:
            return located
    try:
        latitude, longitude = await asyncio.wait_for(_windows_position(), timeout=timeout)
    except Exception as exc:
        _log.info("geolocation unavailable: %s", type(exc).__name__)
        return WeatherLocation(fallback_city, fallback_latitude, fallback_longitude)

    try:
        city = await asyncio.wait_for(
            asyncio.to_thread(_reverse_city, latitude, longitude),
            timeout=timeout,
        )
    except Exception as exc:
        _log.info("reverse geocode failed: %s", type(exc).__name__)
        city = None
    return WeatherLocation(city or "本地", latitude, longitude)


def fetch_weather(city: str, latitude: float, longitude: float, timeout: float = 8.0) -> Weather:
    query = urllib.parse.urlencode(
        {
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,weather_code",
            "daily": "temperature_2m_min,temperature_2m_max",
            "forecast_days": 1,
            "timezone": "Asia/Shanghai",
        }
    )
    request = urllib.request.Request(
        f"https://api.open-meteo.com/v1/forecast?{query}",
        headers={"User-Agent": "InkMemo/0.1"},
    )
    try:
        payload = _open_json(request, timeout)
        current = payload["current"]
        daily = payload["daily"]
        code = int(current["weather_code"])
        condition, icon = WEATHER_CODES.get(code, ("天气", "cloud"))
        return Weather(
            city=city,
            condition=condition,
            icon=icon,
            temperature=round(float(current["temperature_2m"])),
            low=round(float(daily["temperature_2m_min"][0])),
            high=round(float(daily["temperature_2m_max"][0])),
        )
    except Exception as exc:
        _log.warning("weather fetch failed: %s: %s", type(exc).__name__, exc)
        return Weather.unavailable(city)
