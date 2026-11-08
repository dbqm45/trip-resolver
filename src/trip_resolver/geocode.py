"""Nazwy miejsc (reverse geocoding) przez Nominatim / OpenStreetMap.

Zasady publicznego serwera Nominatim: maks. 1 zapytanie na sekundę, rozpoznawalny
User-Agent i cache wyników — wszystko to jest tu respektowane. Można też wskazać
własną instancję (--geocoder-url).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__

DEFAULT_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = f"trip-resolver/{__version__} (+https://github.com/dbqm45/trip-resolver)"

_NAME_KEYS = ("tourism", "historic", "amenity", "building", "leisure", "shop", "man_made",
              "natural", "aeroway", "railway", "bridge", "attraction")
_AREA_KEYS = ("neighbourhood", "quarter", "suburb", "city_district", "hamlet", "village", "town",
              "city", "municipality")
_CITY_KEYS = ("city", "town", "village", "municipality", "county", "state")


@dataclass
class Place:
    name: str  # np. "Ponte di Rialto"
    detail: str = ""  # np. "San Polo, Wenecja"

    @property
    def label(self) -> str:
        return f"{self.name}, {self.detail}" if self.detail else self.name


def place_from_nominatim(r: dict) -> Place | None:
    if not r or "error" in r:
        return None
    addr = r.get("address") or {}
    first = lambda keys: next((addr[k] for k in keys if addr.get(k)), "")  # noqa: E731
    name = (r.get("name") or "").strip() or first(_NAME_KEYS)
    area, city = first(_AREA_KEYS), first(_CITY_KEYS)
    if not name:
        road = addr.get("road") or addr.get("pedestrian") or addr.get("footway") or ""
        name = road or area or city or (r.get("display_name") or "").split(",")[0].strip()
    if not name:
        return None
    detail_parts = []
    for part in (area, city):
        if part and part != name and part not in detail_parts:
            detail_parts.append(part)
    return Place(name, ", ".join(detail_parts))


def default_cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "trip-resolver" / "places.json"


class Geocoder:
    def __init__(
        self,
        url: str = DEFAULT_URL,
        lang: str = "pl",
        cache_path: str | os.PathLike | None = None,
        min_interval: float = 1.0,
        timeout: float = 10.0,
        fetch: Callable[[str], dict] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.url, self.lang, self.timeout = url, lang, timeout
        self.min_interval = min_interval
        self.cache_path = Path(cache_path) if cache_path else default_cache_path()
        self._fetch = fetch or self._http_get
        self._sleep = sleep
        self._last = 0.0
        self._lock = threading.Lock()
        self.error: str | None = None  # pierwszy błąd sieci — kolejne zapytania są pomijane
        self.cache: dict[str, dict | None] = {}
        try:
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}

    def _http_get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": self.lang})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _key(self, lat: float, lon: float) -> str:
        return f"{lat:.4f},{lon:.4f},{self.lang}"  # ~11 m — sąsiednie punkty dzielą wynik

    def lookup(self, lat: float, lon: float) -> Place | None:
        key = self._key(lat, lon)
        with self._lock:
            if key in self.cache:
                return place_from_nominatim(self.cache[key]) if self.cache[key] else None
            if self.error:
                return None
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            q = urllib.parse.urlencode({"format": "jsonv2", "lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
                                        "zoom": 18, "addressdetails": 1, "accept-language": self.lang})
            try:
                r = self._fetch(f"{self.url}?{q}")
            except Exception as e:  # brak internetu, blokada, timeout...
                self.error = f"{type(e).__name__}: {e}"
                return None
            finally:
                self._last = time.monotonic()
            self.cache[key] = r if r and "error" not in r else None
            return place_from_nominatim(r)

    def save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError:
            pass


def label_visits(trip, geocoder: Geocoder, progress: Callable[[int, int], None] | None = None) -> int:
    """Nadaje nazwy miejscom trasy. Zwraca liczbę nazwanych miejsc."""
    named = 0
    for i, v in enumerate(trip.visits):
        v.place = geocoder.lookup(v.lat, v.lon)
        named += v.place is not None
        if progress:
            progress(i + 1, len(trip.visits))
    geocoder.save()
    if geocoder.error:
        trip.warnings.append(f"Nie udało się pobrać nazw miejsc (OpenStreetMap): {geocoder.error}")
    return named
