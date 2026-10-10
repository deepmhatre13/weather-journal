"""Weather data fetching module via OpenWeatherMap API."""

import json
import os
import sqlite3
import time
from pathlib import Path

import requests

API_BASE_URL = "https://api.openweathermap.org/data/2.5/weather"
CACHE_TTL_SECONDS = 600
DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent / ".cache" / "weather.db"

# Keep a small process-local first-level cache as an optimization. SQLite below
# is the source of truth that makes the cache survive separate CLI runs.
_cache = {}


def _cache_path(cache_path=None):
    configured_path = cache_path or os.environ.get("WEATHER_JOURNAL_CACHE")
    return Path(configured_path) if configured_path else DEFAULT_CACHE_PATH


def _memory_key(city_key, cache_path):
    """Build an in-memory cache key scoped to the resolved SQLite cache path.

    The process-local cache is shared across every configured cache path, so it
    must never be keyed by city alone or one cache configuration would serve
    another configuration's entry.
    """
    return str(_cache_path(cache_path)), city_key


def _connect_cache(cache_path):
    path = _cache_path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS weather_cache (
            city_key TEXT PRIMARY KEY,
            fetched_at REAL NOT NULL,
            payload TEXT NOT NULL
        )
    """)
    return conn


def _read_cache(city_key, cache_path, now):
    memory_key = _memory_key(city_key, cache_path)
    memory_entry = _cache.get(memory_key)
    if memory_entry and now - memory_entry[0] < CACHE_TTL_SECONDS:
        return memory_entry[1]

    conn = _connect_cache(cache_path)
    try:
        row = conn.execute(
            "SELECT fetched_at, payload FROM weather_cache WHERE city_key = ?",
            (city_key,),
        ).fetchone()
        if row is None:
            return None

        fetched_at, payload = row
        if now - fetched_at >= CACHE_TTL_SECONDS or fetched_at > now:
            conn.execute("DELETE FROM weather_cache WHERE city_key = ?", (city_key,))
            conn.commit()
            return None

        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            conn.execute("DELETE FROM weather_cache WHERE city_key = ?", (city_key,))
            conn.commit()
            return None
    finally:
        conn.close()

    _cache[memory_key] = (fetched_at, data)
    return data


def _write_cache(city_key, data, cache_path, fetched_at):
    payload = json.dumps(data, separators=(",", ":"), sort_keys=True)
    conn = _connect_cache(cache_path)
    try:
        conn.execute("""
            INSERT INTO weather_cache (city_key, fetched_at, payload)
            VALUES (?, ?, ?)
            ON CONFLICT(city_key) DO UPDATE SET
                fetched_at = excluded.fetched_at,
                payload = excluded.payload
        """, (city_key, fetched_at, payload))
        conn.commit()
    finally:
        conn.close()
    _cache[_memory_key(city_key, cache_path)] = (fetched_at, data)


def get_current(city, api_key=None, cache_path=None):
    """Fetch current weather, reusing results cached for ten minutes."""
    city_key = city.strip().casefold()
    now = time.time()
    cached_data = _read_cache(city_key, cache_path, now)
    if cached_data is not None:
        return cached_data

    key = api_key or os.environ.get("OWM_API_KEY")
    if not key:
        raise ValueError("Missing API key. Please set OWM_API_KEY environment variable or pass --key.")

    params = {
        "q": city,
        "appid": key,
        "units": "metric"
    }

    for attempt in range(3):
        try:
            resp = requests.get(API_BASE_URL, params=params, timeout=10)
            break
        except requests.RequestException as e:
            if attempt == 2:
                raise ValueError(f"Network error: {e}")
            time.sleep(2 ** attempt)

    if resp.status_code == 401:
        raise ValueError("Invalid API key.")
    elif resp.status_code == 404:
        raise ValueError(f"City '{city}' not found.")
    elif resp.status_code != 200:
        try:
            err_data = resp.json()
            err_msg = err_data.get("message", "Unknown error")
        except Exception:
            err_msg = resp.text or "Unknown error"
        raise ValueError(f"API Error: {err_msg}")

    try:
        data = resp.json()
    except Exception as e:
        raise ValueError(f"Invalid JSON in API response: {e}")

    result = {
        "city": data["name"],
        "temp": round(data["main"]["temp"], 1),
        "humidity": data["main"]["humidity"],
        "condition": data["weather"][0]["description"],
        "location": {
            "provider": "openweathermap",
            "provider_id": data.get("id"),
            "requested_name": city,
            "country": data.get("sys", {}).get("country"),
            "latitude": data.get("coord", {}).get("lat"),
            "longitude": data.get("coord", {}).get("lon"),
        },
    }

    _write_cache(city_key, result, cache_path, time.time())
    return result
