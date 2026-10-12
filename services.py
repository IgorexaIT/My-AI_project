from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp
import xml.etree.ElementTree as ET

from config import config
from data import RSS_FEEDS

WEATHER_CODES = {
    0: "ясно",
    1: "преимущественно ясно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "изморозевый туман",
    51: "слабая морось",
    53: "умеренная морось",
    55: "сильная морось",
    56: "слабый ледяной дождь",
    57: "сильный ледяной дождь",
    61: "небольшой дождь",
    63: "умеренный дождь",
    65: "сильный дождь",
    66: "слабый ледяной дождь",
    67: "сильный ледяной дождь",
    71: "небольшой снег",
    73: "умеренный снег",
    75: "сильный снег",
    77: "снежные зёрна",
    80: "небольшие ливни",
    81: "умеренные ливни",
    82: "сильные ливни",
    85: "снежные ливни",
    86: "сильные снежные ливни",
    95: "гроза",
    96: "гроза с небольшим градом",
    99: "гроза с сильным градом",
}


class LocationNotFoundError(ValueError):
    """Raised when the geocoding service cannot find the user-entered place."""


# User-triggered location searches are cached for this process. Nominatim's
# public service has a strict maximum of one request/second, so fallback calls
# are serialized and rate-limited as well.
_GEOCODE_CACHE: dict[str, dict[str, Any] | None] = {}
_NOMINATIM_LOCK = asyncio.Lock()
_LAST_NOMINATIM_REQUEST = 0.0


async def _nominatim_geocode(session: aiohttp.ClientSession, query: str) -> dict[str, Any] | None:
    global _LAST_NOMINATIM_REQUEST
    cache_key = query.casefold().strip()
    if cache_key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[cache_key]

    async with _NOMINATIM_LOCK:
        # Another concurrent request may have filled the cache while we waited.
        if cache_key in _GEOCODE_CACHE:
            return _GEOCODE_CACHE[cache_key]
        delay = 1.0 - (time.monotonic() - _LAST_NOMINATIM_REQUEST)
        if delay > 0:
            await asyncio.sleep(delay)
        _LAST_NOMINATIM_REQUEST = time.monotonic()
        params = {
            "q": query,
            "format": "jsonv2",
            "limit": 5,
            "addressdetails": 1,
            "accept-language": "ru",
        }
        async with session.get("https://nominatim.openstreetmap.org/search", params=params) as response:
            response.raise_for_status()
            places = await response.json()

        if not places:
            _GEOCODE_CACHE[cache_key] = None
            return None

        place = places[0]
        try:
            normalized = {
                "latitude": float(place["lat"]),
                "longitude": float(place["lon"]),
                "location": str(place.get("display_name") or query),
                "source": "OpenStreetMap Nominatim",
            }
        except (KeyError, TypeError, ValueError):
            _GEOCODE_CACHE[cache_key] = None
            return None
        _GEOCODE_CACHE[cache_key] = normalized
        return normalized


def _clean_place_name(place: dict[str, Any]) -> str:
    """Compose a readable place name from Open-Meteo geocoding fields."""
    pieces: list[str] = []
    for key in ("name", "admin2", "admin1", "country"):
        value = str(place.get(key) or "").strip()
        if value and value.casefold() not in {part.casefold() for part in pieces}:
            pieces.append(value)
    return ", ".join(pieces) or "Неизвестное место"


async def get_weather(location_query: str) -> dict[str, Any]:
    """Geocode a city/district name, then get current weather from Open-Meteo.

    Open-Meteo Geocoding searches named places globally. Some small informal
    neighbourhood names may not exist in its gazetteer; in that case the caller
    receives LocationNotFoundError and should ask the user to retry with a city.
    """
    query = " ".join((location_query or "").strip().split())
    if len(query) < 2:
        raise LocationNotFoundError("Название слишком короткое")

    timeout = aiohttp.ClientTimeout(total=12)
    headers = {"User-Agent": "MAI-LifeOS-TelegramBot/1.0 (contact: @residentazeroy)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        geocode_params = {"name": query, "count": 10, "language": "ru", "format": "json"}
        async with session.get(
            "https://geocoding-api.open-meteo.com/v1/search", params=geocode_params
        ) as response:
            response.raise_for_status()
            geocoded = await response.json()

        results = geocoded.get("results") or []
        normalized_query = query.casefold().strip()
        if results:
            query_parts = [part.strip().casefold() for part in query.split(",") if part.strip()]
            place_term = query_parts[0] if query_parts else normalized_query
            qualifiers = query_parts[1:]

            def score(place: dict[str, Any]) -> tuple[int, int, int, int]:
                place_name = str(place.get("name") or "").casefold().strip()
                admin_names = [str(place.get(key) or "").casefold().strip() for key in ("admin1", "admin2", "admin3", "admin4", "country")]
                exact = 0 if place_term == place_name else 1
                qualifiers_match = 0 if not qualifiers or all(any(q == admin or q in admin for admin in admin_names) for q in qualifiers) else 1
                is_russia = 0 if (not qualifiers and str(place.get("country_code", "")).upper() == "RU") else 1
                population = -int(place.get("population") or 0)
                return exact, qualifiers_match, is_russia, population

            results.sort(key=score)
            place = results[0]
            resolved = {
                "latitude": float(place["latitude"]),
                "longitude": float(place["longitude"]),
                "location": _clean_place_name(place),
                "source": "Open-Meteo Geocoding",
            }
        else:
            # Open-Meteo's geocoder mainly indexes towns and settlements. Use
            # Nominatim as a polite, rate-limited fallback for neighbourhoods.
            resolved = await _nominatim_geocode(session, query)
            if not resolved:
                raise LocationNotFoundError(query)

        params = {
            "latitude": resolved["latitude"],
            "longitude": resolved["longitude"],
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
            "timezone": "auto",
        }
        async with session.get("https://api.open-meteo.com/v1/forecast", params=params) as response:
            response.raise_for_status()
            data = await response.json()

    current = data.get("current", {})
    code = int(current.get("weather_code", -1))
    return {
        "time": str(current.get("time", "")),
        "timezone": str(data.get("timezone", "")),
        "location": str(resolved["location"]),
        "geocoding_source": str(resolved["source"]),
        "latitude": float(resolved["latitude"]),
        "longitude": float(resolved["longitude"]),
        "temperature": current.get("temperature_2m"),
        "feels_like": current.get("apparent_temperature"),
        "humidity": current.get("relative_humidity_2m"),
        "precipitation": current.get("precipitation"),
        "wind": current.get("wind_speed_10m"),
        "description": WEATHER_CODES.get(code, "условия не определены"),
    }


def _salary_text(salary: dict[str, Any] | None) -> str:
    if not salary:
        return "зарплата не указана"
    currency = salary.get("currency") or "RUR"
    curr_map = {"RUR": "₽", "RUB": "₽", "USD": "$", "EUR": "€"}
    symbol = curr_map.get(currency, currency)
    low, high = salary.get("from"), salary.get("to")
    if low is not None and high is not None:
        return f"{int(low):,}–{int(high):,} {symbol}".replace(",", " ")
    if low is not None:
        return f"от {int(low):,} {symbol}".replace(",", " ")
    if high is not None:
        return f"до {int(high):,} {symbol}".replace(",", " ")
    return "зарплата не указана"


async def search_hh_vacancies(query: str = "Python", internship: bool = False, limit: int = 5) -> list[dict[str, str]]:
    """Search HH's public API. A website link is the fallback if API access is limited."""
    params: dict[str, Any] = {
        "text": query,
        "area": 1,
        "experience": "noExperience",
        "per_page": limit,
        "page": 0,
    }
    if not internship:
        params["schedule"] = "remote"
    headers = {
        "User-Agent": config.hh_user_agent,
        "HH-User-Agent": config.hh_user_agent,
        "Accept": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get("https://api.hh.ru/vacancies", params=params) as response:
            response.raise_for_status()
            data = await response.json()
    items: list[dict[str, str]] = []
    for item in data.get("items", []):
        employer = item.get("employer") or {}
        items.append({
            "name": str(item.get("name") or "Вакансия"),
            "url": str(item.get("alternate_url") or "https://hh.ru/"),
            "salary": _salary_text(item.get("salary")),
            "employer": str(employer.get("name") or "Работодатель не указан"),
            "schedule": "удалённо / без опыта" if not internship else "стажировка / без опыта",
        })
    return items


def _xml_local_name(tag: str) -> str:
    """Return an XML tag name without its optional namespace."""
    return tag.rsplit("}", 1)[-1].lower()


def _xml_child_text(element: ET.Element, wanted_name: str) -> str:
    """Get text from the first direct child with a given local XML name."""
    for child in element:
        if _xml_local_name(child.tag) == wanted_name:
            return "".join(child.itertext()).strip()
    return ""


def _parse_feed_items(raw: bytes) -> list[tuple[str, str]]:
    """Parse RSS or Atom items with the standard library (Python 3.13+ safe)."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []

    results: list[tuple[str, str]] = []
    for element in root.iter():
        item_type = _xml_local_name(element.tag)
        if item_type not in {"item", "entry"}:
            continue

        title = _xml_child_text(element, "title")
        link = ""
        for child in element:
            if _xml_local_name(child.tag) != "link":
                continue
            # RSS usually stores the URL as link text; Atom stores it in href.
            href = (child.attrib.get("href") or "").strip()
            rel = (child.attrib.get("rel") or "alternate").strip().lower()
            if href and rel in {"alternate", ""}:
                link = href
                break
            text_link = "".join(child.itertext()).strip()
            if text_link:
                link = text_link
                break

        if title and link.startswith(("http://", "https://")):
            results.append((title, link))
    return results


async def get_backend_news(limit: int = 8) -> list[dict[str, str]]:
    """Read public RSS/Atom feeds and return short headline/link/source objects."""
    timeout = aiohttp.ClientTimeout(total=10)
    headers = {"User-Agent": "MAI-LifeOS-Bot/1.0 (RSS reader)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async def fetch(name: str, url: str) -> tuple[str, bytes] | None:
            try:
                async with session.get(url) as response:
                    response.raise_for_status()
                    return name, await response.read()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return None

        results = await asyncio.gather(*(fetch(name, url) for name, url in RSS_FEEDS))
        feeds = [x for x in results if x is not None]

    articles: list[dict[str, str]] = []
    seen: set[str] = set()
    for feed_name, raw in feeds:
        for title, link in _parse_feed_items(raw):
            if link in seen:
                continue
            seen.add(link)
            articles.append({"title": title, "url": link, "source": feed_name})
            if len(articles) >= limit:
                return articles
    return articles
