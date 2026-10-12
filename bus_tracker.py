"""Unofficial real-time forecast client for Moscow Transport stop boards.

The upstream endpoint is undocumented and may change or become unavailable.
This module deliberately reports whether each forecast is telemetry-based or
schedule-based instead of presenting every ETA as live GPS data.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import aiohttp

from config import MOSCOW_TZ

API_BASE = "https://moscowtransport.app/api/stop_v2/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 MAI-LifeOS/1.0"
UUID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)


class BusTrackerError(Exception):
    """The real-time source did not provide usable forecast data."""


def normalize_route(value: str) -> str:
    """Normalize common Cyrillic/Latin lookalikes without fuzzy route matching."""
    route = "".join((value or "").lower().split())
    return route.translate(str.maketrans({"c": "с", "ё": "е"}))


def extract_stop_uuid(value: str) -> str | None:
    match = UUID_RE.search(value or "")
    return match.group(0) if match else None


async def resolve_stop_uuid(value: str) -> str | None:
    """Extract a stop UUID from a direct or shared Moscow Transport URL."""
    direct_id = extract_stop_uuid(value)
    if direct_id:
        return direct_id

    candidate = (value or "").strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if (parsed.hostname or "").lower() not in {"moscowapp.mos.ru", "moscowtransport.app"}:
        return None

    timeout = aiohttp.ClientTimeout(total=8, connect=4)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
            async with session.get(candidate, allow_redirects=True) as response:
                final_url = str(response.url)
                found = extract_stop_uuid(final_url)
                if found:
                    return found
                # Some share links resolve to an app page whose URL omits the UUID.
                body = await response.text()
                return extract_stop_uuid(body)
    except (aiohttp.ClientError, TimeoutError):
        return None


async def get_stop_forecast(stop_id: str, route_number: str) -> dict[str, Any]:
    """Fetch a stop board and return upcoming forecasts for one route."""
    timeout = aiohttp.ClientTimeout(total=8, connect=4)
    url = API_BASE + stop_id
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise BusTrackerError(f"Источник транспорта ответил HTTP {response.status}.")
                data = await response.json(content_type=None)
    except BusTrackerError:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise BusTrackerError("Не удалось подключиться к источнику прогнозов транспорта.") from exc

    if not isinstance(data, dict) or data.get("name") == "Exception":
        raise BusTrackerError("Источник не распознал эту остановку. Проверь ссылку на остановку.")

    wanted = normalize_route(route_number)
    matches: list[dict[str, Any]] = []
    for route in data.get("routePath", []) or []:
        if not isinstance(route, dict) or normalize_route(str(route.get("number", ""))) != wanted:
            continue
        for event in route.get("externalForecast", []) or []:
            if not isinstance(event, dict):
                continue
            try:
                seconds = int(event.get("time"))
            except (TypeError, ValueError):
                continue
            if seconds < 0 or seconds > 24 * 60 * 60:
                continue
            matches.append({
                "arrival": datetime.now(MOSCOW_TZ) + timedelta(seconds=seconds),
                "minutes": max(0, round(seconds / 60)),
                "realtime": event.get("byTelemetry", 0) not in (0, "0", False, None),
            })

    matches.sort(key=lambda item: item["arrival"])
    if not matches:
        raise BusTrackerError(
            f"В табло остановки нет ближайших прогнозов для маршрута «{route_number}». "
            "Возможно, маршрут сейчас не обслуживается или номер записан иначе."
        )

    stop_name = str(data.get("name") or "Остановка")
    return {"stop_name": stop_name, "forecasts": matches[:5], "source_url": url}
