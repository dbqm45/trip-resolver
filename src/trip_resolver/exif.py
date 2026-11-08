"""Odczyt metadanych zdjęć (EXIF) potrzebnych do odtworzenia trasy."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

from PIL import Image

try:  # HEIC/HEIF (iPhone) — opcjonalnie
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:  # pragma: no cover - zależy od środowiska
    HEIF_SUPPORTED = False

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff", ".png", ".webp"}
HEIF_EXTENSIONS = {".heic", ".heif"}

# Tagi EXIF
_TAG_MAKE = 0x010F
_TAG_MODEL = 0x0110
_TAG_DATETIME = 0x0132
_IFD_EXIF = 0x8769
_IFD_GPS = 0x8825
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_DATETIME_DIGITIZED = 0x9004
_TAG_OFFSET_TIME_ORIGINAL = 0x9011
_TAG_OFFSET_TIME = 0x9010
_TAG_SUBSEC_ORIGINAL = 0x9291
_GPS_LAT_REF, _GPS_LAT = 1, 2
_GPS_LON_REF, _GPS_LON = 3, 4
_GPS_ALT_REF, _GPS_ALT = 5, 6
_GPS_TIMESTAMP = 7
_GPS_DATESTAMP = 29
_GPS_H_ERROR = 31  # GPSHPositioningError (m) — zapisuje m.in. iPhone

_OFFSET_RE = re.compile(r"^\s*([+-])(\d{2}):?(\d{2})\s*$")


@dataclass
class PhotoMeta:
    """Metadane jednego zdjęcia."""

    path: str
    local_time: datetime | None = None  # czas z aparatu (bez strefy)
    utc_offset: timedelta | None = None  # z OffsetTimeOriginal, jeśli jest
    gps_time: datetime | None = None  # czas UTC z odbiornika GPS
    lat: float | None = None
    lon: float | None = None
    altitude: float | None = None
    gps_error_m: float | None = None  # deklarowana dokładność pozycji
    make: str | None = None
    model: str | None = None
    width: int | None = None
    height: int | None = None
    error: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def camera(self) -> str:
        parts = [p for p in (self.make, self.model) if p]
        if len(parts) == 2 and parts[1].lower().startswith(parts[0].lower()):
            parts = [parts[1]]
        return " ".join(parts) or "nieznany aparat"

    @property
    def has_gps(self) -> bool:
        return self.lat is not None and self.lon is not None


def _clean_str(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "ignore")
    value = str(value).replace("\x00", "").strip()
    return value or None


def _parse_exif_datetime(value, subsec=None) -> datetime | None:
    value = _clean_str(value)
    if not value:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            dt = datetime.strptime(value[:19], fmt)
            break
        except ValueError:
            continue
    else:
        return None
    sub = _clean_str(subsec)
    if sub and sub.isdigit():
        dt = dt.replace(microsecond=int(sub[:6].ljust(6, "0")))
    return dt


def parse_offset(value) -> timedelta | None:
    value = _clean_str(value)
    if not value:
        return None
    m = _OFFSET_RE.match(value)
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    return sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))


def _rational(v) -> float:
    if isinstance(v, tuple) and len(v) == 2:
        return float(v[0]) / float(v[1]) if v[1] else 0.0
    return float(v)


def _dms_to_deg(dms, ref) -> float | None:
    try:
        if isinstance(dms, (int, float)) or not hasattr(dms, "__len__"):
            deg = _rational(dms)
        else:
            vals = [_rational(x) for x in dms]
            vals += [0.0] * (3 - len(vals))
            deg = vals[0] + vals[1] / 60 + vals[2] / 3600
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    ref = _clean_str(ref) or ""
    if ref.upper() in ("S", "W"):
        deg = -deg
    return deg


def _parse_gps(gps: dict) -> tuple[float | None, float | None, float | None, datetime | None]:
    lat = lon = alt = None
    if _GPS_LAT in gps and _GPS_LON in gps:
        lat = _dms_to_deg(gps[_GPS_LAT], gps.get(_GPS_LAT_REF, "N"))
        lon = _dms_to_deg(gps[_GPS_LON], gps.get(_GPS_LON_REF, "E"))
        # (0,0) to typowy "pusty" odczyt GPS
        if lat is not None and lon is not None:
            if abs(lat) < 1e-9 and abs(lon) < 1e-9:
                lat = lon = None
            elif not (-90 <= lat <= 90 and -180 <= lon <= 180):
                lat = lon = None
    if _GPS_ALT in gps:
        try:
            alt = _rational(gps[_GPS_ALT])
            ref = gps.get(_GPS_ALT_REF, 0)
            if isinstance(ref, bytes):
                ref = ref[0] if ref else 0
            if int(ref) == 1:
                alt = -alt
        except (TypeError, ValueError, ZeroDivisionError):
            alt = None
    gps_time = None
    date = _clean_str(gps.get(_GPS_DATESTAMP))
    ts = gps.get(_GPS_TIMESTAMP)
    if date and ts:
        try:
            h, m, s = (_rational(x) for x in ts)
            d = datetime.strptime(date.replace("-", ":"), "%Y:%m:%d")
            gps_time = (d + timedelta(hours=h, minutes=m, seconds=s)).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, ZeroDivisionError):
            gps_time = None
    return lat, lon, alt, gps_time


def read_photo(path: str | os.PathLike) -> PhotoMeta:
    """Czyta metadane z jednego pliku. Nigdy nie rzuca — błąd trafia do ``error``."""
    meta = PhotoMeta(path=str(path))
    try:
        with Image.open(path) as img:
            meta.width, meta.height = img.size
            exif = img.getexif()
            meta.make = _clean_str(exif.get(_TAG_MAKE))
            meta.model = _clean_str(exif.get(_TAG_MODEL))
            sub = exif.get_ifd(_IFD_EXIF)
            dt = _parse_exif_datetime(sub.get(_TAG_DATETIME_ORIGINAL), sub.get(_TAG_SUBSEC_ORIGINAL))
            if dt is None:
                dt = _parse_exif_datetime(sub.get(_TAG_DATETIME_DIGITIZED))
            if dt is None:
                dt = _parse_exif_datetime(exif.get(_TAG_DATETIME))
            meta.local_time = dt
            meta.utc_offset = parse_offset(sub.get(_TAG_OFFSET_TIME_ORIGINAL)) or parse_offset(
                sub.get(_TAG_OFFSET_TIME)
            )
            gps = exif.get_ifd(_IFD_GPS)
            if gps:
                meta.lat, meta.lon, meta.altitude, meta.gps_time = _parse_gps(gps)
                if _GPS_H_ERROR in gps:
                    try:
                        meta.gps_error_m = _rational(gps[_GPS_H_ERROR])
                    except (TypeError, ValueError, ZeroDivisionError):
                        pass
    except Exception as e:  # uszkodzony plik, nieobsługiwany format itp.
        meta.error = f"{type(e).__name__}: {e}"
    return meta


def iter_image_files(paths: Iterable[str | os.PathLike]) -> Iterator[Path]:
    exts = IMAGE_EXTENSIONS | HEIF_EXTENSIONS
    for p in paths:
        p = Path(p)
        if p.is_dir():
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for f in sorted(files):
                    if Path(f).suffix.lower() in exts and not f.startswith("."):
                        yield Path(root) / f
        elif p.is_file():
            yield p


def scan_paths(paths: Iterable[str | os.PathLike]) -> list[PhotoMeta]:
    """Skanuje pliki i katalogi (rekurencyjnie) i zwraca metadane wszystkich zdjęć."""
    out = []
    for f in iter_image_files(paths):
        if f.suffix.lower() in HEIF_EXTENSIONS and not HEIF_SUPPORTED:
            out.append(PhotoMeta(path=str(f), error="HEIC wymaga: pip install pillow-heif"))
            continue
        out.append(read_photo(f))
    return out
