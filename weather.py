#!/usr/bin/env python3
"""Today's weather, for LLM/dashboard context only.

Open-Meteo (https://open-meteo.com): free, no API key, no signup --
geocoding + forecast in two plain HTTP calls. Never used to swap
session types (this project's treadmill/lower_body/upper_body/
calisthenics are all indoor by design) or adjust any number -- it's
context Claude can mention (e.g. "belle journee, une sortie dehors
serait bien"), same posture as body_battery/stress in metrics.py.

``fetch`` is an injectable ``requests.get``-shaped callable so the
self-check never makes a live HTTP call.
"""

from typing import Callable, Optional

import requests

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes (public spec, not Garmin-style
# guesswork) collapsed to a few broad French buckets.
_CONDITION_FR = {
    0: "ciel degage", 1: "plutot degage", 2: "partiellement nuageux",
    3: "couvert", 45: "brouillard", 48: "brouillard givrant",
    51: "bruine legere", 53: "bruine", 55: "bruine forte",
    61: "pluie legere", 63: "pluie", 65: "forte pluie",
    71: "neige legere", 73: "neige", 75: "forte neige",
    80: "averses", 81: "averses fortes", 82: "averses violentes",
    95: "orage", 96: "orage avec grele", 99: "orage violent",
}


def _condition_fr(code: Optional[int]) -> str:
    """Map a WMO weather code to a short French label."""
    return _CONDITION_FR.get(code, "conditions inconnues")


def geocode(
    city: str, fetch: Callable = requests.get,
) -> Optional[tuple[float, float]]:
    """Resolve a city name to (latitude, longitude).

    Parameters:
        city (str): Free-text city name (e.g. "Lyon", "Lyon,FR").
        fetch (Callable): ``requests.get``-shaped, injectable for tests.

    Returns:
        tuple[float, float] | None: ``(lat, lon)`` of the first
        match, or ``None`` if the city isn't found.
    """
    response = fetch(
        GEOCODE_URL, params={"name": city, "count": 1}, timeout=10,
    )
    response.raise_for_status()
    results = response.json().get("results") or []
    if not results:
        return None
    return results[0]["latitude"], results[0]["longitude"]


def today_weather(
    city: str, tz: str = "Europe/Paris", fetch: Callable = requests.get,
) -> Optional[dict]:
    """Today's forecast summary for a city, or ``None`` if unavailable.

    Parameters:
        city (str): Setting value (empty means the feature is off --
            callers should skip calling this at all in that case).
        tz (str): IANA timezone, so "today" matches the user's day.
        fetch (Callable): ``requests.get``-shaped, injectable for tests.

    Returns:
        dict | None: ``temp_max_c``, ``precip_mm``, ``condition_fr``,
        or ``None`` if the city can't be geocoded or the API fails.
    """
    coords = geocode(city, fetch)
    if coords is None:
        return None
    lat, lon = coords
    response = fetch(
        FORECAST_URL, params={
            "latitude": lat, "longitude": lon, "timezone": tz,
            "daily": "temperature_2m_max,precipitation_sum,weathercode",
            "forecast_days": 1,
        }, timeout=10,
    )
    response.raise_for_status()
    daily = response.json().get("daily") or {}
    temps = daily.get("temperature_2m_max") or []
    precip = daily.get("precipitation_sum") or []
    codes = daily.get("weathercode") or []
    if not temps:
        return None
    return {
        "temp_max_c": round(temps[0], 1),
        "precip_mm": round(precip[0], 1) if precip else 0.0,
        "condition_fr": _condition_fr(codes[0] if codes else None),
    }


if __name__ == "__main__":
    class _Response:
        def __init__(self, payload: dict):
            self._payload = payload

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return self._payload

    def _fake_fetch(url: str, params: dict, timeout: int) -> _Response:
        if url == GEOCODE_URL:
            if params["name"] == "Nowhereville":
                return _Response({"results": []})
            return _Response({
                "results": [{"latitude": 45.75, "longitude": 4.85}],
            })
        assert url == FORECAST_URL
        assert params["latitude"] == 45.75 and params["longitude"] == 4.85
        return _Response({
            "daily": {
                "temperature_2m_max": [24.3], "precipitation_sum": [0.0],
                "weathercode": [1],
            },
        })

    assert geocode("Lyon,FR", _fake_fetch) == (45.75, 4.85)
    assert geocode("Nowhereville", _fake_fetch) is None

    weather = today_weather("Lyon,FR", fetch=_fake_fetch)
    assert weather == {
        "temp_max_c": 24.3, "precip_mm": 0.0,
        "condition_fr": "plutot degage",
    }, weather

    def _fetch_no_city(url: str, params: dict, timeout: int) -> _Response:
        return _Response({"results": []})

    assert today_weather("Nowhereville", fetch=_fetch_no_city) is None
    assert _condition_fr(999) == "conditions inconnues"

    print("weather.py: all checks passed (no live HTTP call made)")
