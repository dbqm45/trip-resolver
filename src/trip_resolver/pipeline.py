"""Cały przebieg: skan → trasa → nazwy miejsc → pliki wynikowe. Wspólne dla CLI i serwera."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterable

from .exif import scan_paths
from .export import write_csv, write_geojson, write_html
from .geocode import Geocoder, label_visits
from .route import Settings, Trip, resolve_trip

OUTPUT_FILES = ("trip.html", "timeline.csv", "route.geojson")


class NoPhotosError(Exception):
    pass


def run(
    paths: Iterable[str | os.PathLike],
    out: str | os.PathLike,
    settings: Settings | None = None,
    title: str = "Odtworzona trasa",
    thumbnails: bool = True,
    geocoder: Geocoder | None = None,
    log: Callable[[str], None] = lambda s: None,
) -> Trip:
    log("Skanuję zdjęcia...")
    photos = scan_paths(paths)
    if not photos:
        raise NoPhotosError("Nie znaleziono zdjęć.")
    trip = resolve_trip(photos, settings or Settings())
    if geocoder and trip.visits:
        log(f"Pobieram nazwy {len(trip.visits)} miejsc z OpenStreetMap (ok. 1 s na miejsce, wyniki są zapamiętywane)...")
        label_visits(trip, geocoder)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(trip, out / "timeline.csv")
    write_geojson(trip, out / "route.geojson")
    write_html(trip, out / "trip.html", title=title, thumbnails=thumbnails)
    return trip
