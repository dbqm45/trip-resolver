"""Proste funkcje geograficzne."""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_008.8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Odległość po kole wielkim w metrach."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def interpolate(lat1: float, lon1: float, lat2: float, lon2: float, f: float) -> tuple[float, float]:
    """Liniowa interpolacja położenia (wystarczająca na krótkich odcinkach)."""
    dlon = lon2 - lon1
    if dlon > 180:
        dlon -= 360
    elif dlon < -180:
        dlon += 360
    lon = lon1 + dlon * f
    if lon > 180:
        lon -= 360
    elif lon < -180:
        lon += 360
    return lat1 + (lat2 - lat1) * f, lon
