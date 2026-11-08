"""Odtwarzanie trasy: czas → kolejność → pozycje → miejsca (postoje) → odcinki → dni."""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .exif import PhotoMeta
from .geo import haversine_m, interpolate

if TYPE_CHECKING:
    from .geocode import Place

# Źródła czasu, od najbardziej wiarygodnego
TIME_EXIF_OFFSET = "exif+offset"  # DateTimeOriginal + OffsetTimeOriginal
TIME_GPS_PAIR = "exif+gps"  # strefa wyliczona z różnicy czas aparatu vs czas GPS tego zdjęcia
TIME_USER_TZ = "exif+tz"  # strefa podana przez użytkownika (--tz)
TIME_CAMERA_OFFSET = "exif+camera"  # strefa wyliczona z innych zdjęć tego samego aparatu
TIME_GPS_ONLY = "gps"  # tylko czas GPS (UTC)
TIME_FLOATING = "exif-no-tz"  # czas aparatu bez strefy — traktowany jak UTC

POS_EXIF = "exif"
POS_INTERPOLATED = "interpolated"
POS_NEAREST = "nearest"


@dataclass
class Settings:
    tz: str | None = None  # np. "Europe/Rome" dla zdjęć bez strefy
    camera_shift: dict[str, timedelta] = field(default_factory=dict)  # korekta zegara aparatu
    stop_radius_m: float = 150.0  # zdjęcia bliżej niż tyle = to samo miejsce
    stop_max_gap: timedelta = timedelta(hours=3)  # ... o ile przerwa nie jest dłuższa
    max_interp_gap: timedelta = timedelta(hours=2)  # max przerwa między punktami GPS do interpolacji
    nearest_window: timedelta = timedelta(minutes=15)  # przypięcie do najbliższego punktu GPS
    max_speed_kmh: float = 1000.0  # szybciej = błędny odczyt GPS
    max_gps_error_m: float | None = 100.0  # punkty z gorszą deklarowaną dokładnością są pomijane
    excursion_max_photos: int = 2  # tyle zdjęć "obok" miejsca i powrót = szum GPS, nie osobne miejsce
    excursion_max_time: timedelta = timedelta(minutes=15)
    excursion_snap: timedelta = timedelta(minutes=3)  # 1–2 zdjęcia tuż obok dużego miejsca → do niego


@dataclass
class ResolvedPhoto:
    meta: PhotoMeta
    time_utc: datetime  # zawsze ze strefą (UTC)
    utc_offset: timedelta | None  # None = strefa nieznana
    time_source: str
    lat: float | None = None
    lon: float | None = None
    position_source: str | None = None
    gps_rejected: bool = False  # odczyt GPS odrzucony jako błędny
    visit_index: int | None = None

    @property
    def local_time(self) -> datetime:
        if self.utc_offset is None:
            return self.time_utc.replace(tzinfo=None)
        return self.time_utc.astimezone(timezone(self.utc_offset))

    @property
    def local_date(self) -> date:
        return self.local_time.date()

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass
class Visit:
    """Miejsce, w którym zrobiono jedno lub więcej zdjęć (postój)."""

    index: int
    lat: float
    lon: float
    photos: list[ResolvedPhoto]
    place: "Place | None" = None  # nazwa z OpenStreetMap (opcjonalnie)

    @property
    def name(self) -> str | None:
        return self.place.name if self.place else None

    @property
    def arrival(self) -> ResolvedPhoto:
        return self.photos[0]

    @property
    def departure(self) -> ResolvedPhoto:
        return self.photos[-1]

    @property
    def duration(self) -> timedelta:
        return self.departure.time_utc - self.arrival.time_utc

    @property
    def radius_m(self) -> float:
        return max((haversine_m(self.lat, self.lon, p.lat, p.lon) for p in self.photos), default=0.0)


@dataclass
class Leg:
    """Przejazd/przejście między dwoma kolejnymi miejscami."""

    start: Visit
    end: Visit
    distance_m: float
    duration: timedelta

    @property
    def speed_kmh(self) -> float | None:
        s = self.duration.total_seconds()
        return self.distance_m / s * 3.6 if s > 0 else None

    @property
    def mode(self) -> str:
        v = self.speed_kmh
        if v is None:
            return "?"
        if v <= 7:
            return "pieszo"
        if v <= 30:
            return "rower / łódź / komunikacja"
        if v <= 250:
            return "samochód / pociąg"
        return "samolot"


@dataclass
class Day:
    date: date
    visits: list[Visit]
    photos: list[ResolvedPhoto]
    distance_m: float


@dataclass
class Trip:
    photos: list[ResolvedPhoto]  # wszystkie z czasem, chronologicznie
    visits: list[Visit]
    legs: list[Leg]
    days: list[Day]
    unplaced: list[PhotoMeta]  # bez czasu (lub błąd odczytu) — nie da się ich ułożyć
    warnings: list[str]

    @property
    def distance_m(self) -> float:
        return sum(l.distance_m for l in self.legs)

    @property
    def path(self) -> list[ResolvedPhoto]:
        """Punkty trasy w kolejności (bez błędnych odczytów)."""
        return [p for p in self.photos if p.has_position]


# ─────────────────────────── czas ───────────────────────────


def _round_offset(td: timedelta) -> timedelta:
    q = 15 * 60
    return timedelta(seconds=round(td.total_seconds() / q) * q)


def _gps_pair_offset(m: PhotoMeta) -> timedelta | None:
    if m.local_time is None or m.gps_time is None:
        return None
    off = _round_offset(m.local_time.replace(tzinfo=timezone.utc) - m.gps_time)
    if abs(off) > timedelta(hours=14):
        return None
    # odczyt GPS bywa "stary" — akceptuj tylko gdy reszta różnicy jest mała
    resid = abs((m.local_time.replace(tzinfo=timezone.utc) - m.gps_time) - off)
    return off if resid <= timedelta(minutes=5) else None


def resolve_times(photos: list[PhotoMeta], settings: Settings, warnings: list[str]):
    def local(m: PhotoMeta) -> datetime | None:
        """Czas aparatu po korekcie zegara (bez modyfikowania metadanych)."""
        if m.local_time is None:
            return None
        shift = settings.camera_shift.get(m.camera) or settings.camera_shift.get(m.model or "")
        return m.local_time + shift if shift else m.local_time

    def pair_offset(m: PhotoMeta) -> timedelta | None:
        # różnicę z GPS liczymy z oryginalnego czasu aparatu — korekta dotyczy zegara, nie strefy
        return _gps_pair_offset(m)

    camera_offsets: dict[str, list[timedelta]] = defaultdict(list)
    for m in photos:
        off = m.utc_offset if m.utc_offset is not None else pair_offset(m)
        if off is not None and m.local_time is not None:
            camera_offsets[m.camera].append(off)
    camera_median = {
        cam: timedelta(seconds=statistics.median(o.total_seconds() for o in offs))
        for cam, offs in camera_offsets.items()
    }

    user_tz = ZoneInfo(settings.tz) if settings.tz else None
    resolved, unplaced = [], []
    for m in photos:
        if m.error:
            unplaced.append(m)
            continue
        lt = local(m)
        if lt is not None and m.utc_offset is not None:
            off, src = m.utc_offset, TIME_EXIF_OFFSET
        elif lt is not None and (pair := pair_offset(m)) is not None:
            off, src = pair, TIME_GPS_PAIR
        elif lt is not None and user_tz is not None:
            off, src = user_tz.utcoffset(lt), TIME_USER_TZ
        elif lt is not None and m.camera in camera_median:
            off, src = camera_median[m.camera], TIME_CAMERA_OFFSET
        elif lt is None and m.gps_time is not None:
            resolved.append(ResolvedPhoto(m, m.gps_time, None, TIME_GPS_ONLY))
            continue
        elif lt is not None:
            off, src = None, TIME_FLOATING
        else:
            unplaced.append(m)
            continue
        t = lt.replace(tzinfo=timezone.utc) - (off or timedelta(0))
        resolved.append(ResolvedPhoto(m, t, off, src))

    sources = {p.time_source for p in resolved}
    if TIME_FLOATING in sources and len(sources) > 1:
        warnings.append(
            "Część zdjęć nie ma strefy czasowej i zostały potraktowane jak UTC — "
            "kolejność względem pozostałych może być przesunięta. Podaj --tz."
        )
    elif sources == {TIME_FLOATING}:
        warnings.append("Brak informacji o strefie czasowej — czasy pokazane są tak, jak zapisał je aparat.")
    if unplaced:
        warnings.append(f"Pominięte w trasie (brak daty lub nieczytelny plik): {len(unplaced)}.")
    # stabilne sortowanie: czas, potem nazwa pliku (zdjęcia seryjne z tą samą sekundą)
    resolved.sort(key=lambda p: (p.time_utc, p.meta.path))
    return resolved, unplaced


# ─────────────────────────── pozycje ───────────────────────────


def _speed_kmh(a: ResolvedPhoto, b: ResolvedPhoto) -> float:
    d = haversine_m(a.lat, a.lon, b.lat, b.lon)
    s = abs((b.time_utc - a.time_utc).total_seconds())
    return float("inf") if s == 0 and d > 50 else (d / s * 3.6 if s else 0.0)


def mark_outliers(photos: list[ResolvedPhoto], settings: Settings) -> int:
    """Oznacza pojedyncze "skoki" GPS (A → X → B, gdzie X jest nieosiągalne, a A → B tak)."""
    gps = [p for p in photos if p.has_position]
    count = 0
    for i in range(1, len(gps) - 1):
        a, x, b = gps[i - 1], gps[i], gps[i + 1]
        if a.gps_rejected:
            continue
        lim = settings.max_speed_kmh
        if _speed_kmh(a, x) > lim and _speed_kmh(x, b) > lim and _speed_kmh(a, b) <= lim:
            x.gps_rejected = True
            count += 1
    for p in gps:
        if p.gps_rejected:
            p.lat = p.lon = p.position_source = None
    return count


def fill_positions(photos: list[ResolvedPhoto], settings: Settings) -> int:
    """Nadaje położenie zdjęciom bez GPS na podstawie sąsiednich (w czasie) zdjęć z GPS."""
    anchors = [i for i, p in enumerate(photos) if p.has_position]
    filled = 0
    if not anchors:
        return 0
    ai = 0
    for i, p in enumerate(photos):
        if p.has_position:
            continue
        while ai + 1 < len(anchors) and anchors[ai + 1] < i:
            ai += 1
        prev = photos[anchors[ai]] if anchors[ai] < i else None
        nxt_idx = next((a for a in anchors[ai:] if a > i), None)
        nxt = photos[nxt_idx] if nxt_idx is not None else None
        lat = lon = src = None
        if prev and nxt and (nxt.time_utc - prev.time_utc) <= settings.max_interp_gap:
            span = (nxt.time_utc - prev.time_utc).total_seconds()
            f = (p.time_utc - prev.time_utc).total_seconds() / span if span else 0.0
            lat, lon = interpolate(prev.lat, prev.lon, nxt.lat, nxt.lon, f)
            src = POS_INTERPOLATED
        else:
            cands = [c for c in (prev, nxt) if c and abs(c.time_utc - p.time_utc) <= settings.nearest_window]
            if cands:
                c = min(cands, key=lambda c: abs(c.time_utc - p.time_utc))
                lat, lon, src = c.lat, c.lon, POS_NEAREST
        if src:
            p.lat, p.lon, p.position_source = lat, lon, src
            filled += 1
    return filled


# ─────────────────────────── miejsca, odcinki, dni ───────────────────────────


def _median_center(photos: list[ResolvedPhoto]) -> tuple[float, float]:
    """Środek miejsca odporny na pojedyncze skoki GPS."""
    return statistics.median(p.lat for p in photos), statistics.median(p.lon for p in photos)


def build_visits(photos: list[ResolvedPhoto], settings: Settings) -> list[Visit]:
    # 1) kolejne zdjęcia blisko siebie → jedna grupa
    groups: list[list[ResolvedPhoto]] = []
    cur: list[ResolvedPhoto] = []
    clat = clon = 0.0
    for p in photos:
        if not p.has_position:
            continue
        if cur and (
            haversine_m(clat, clon, p.lat, p.lon) <= settings.stop_radius_m
            and p.time_utc - cur[-1].time_utc <= settings.stop_max_gap
        ):
            cur.append(p)
            n = len(cur)
            clat += (p.lat - clat) / n
            clon += (p.lon - clon) / n
        else:
            if cur:
                groups.append(cur)
            cur, clat, clon = [p], p.lat, p.lon
    if cur:
        groups.append(cur)

    # 2) sprzątanie szumu GPS
    radius = settings.stop_radius_m
    centers = [_median_center(g) for g in groups]

    def dist(i, j):
        return haversine_m(*centers[i], *centers[j])

    changed = True
    while changed:
        changed = False
        # a) sąsiednie grupy w tym samym miejscu (np. po przesunięciu środka) → jedna
        for i in range(len(groups) - 1):
            if dist(i, i + 1) <= radius and groups[i + 1][0].time_utc - groups[i][-1].time_utc <= settings.stop_max_gap:
                groups[i:i + 2] = [groups[i] + groups[i + 1]]
                centers[i:i + 2] = [_median_center(groups[i])]
                changed = True
                break
        if changed:
            continue
        # b) A → krótki "wyskok" X → A: X to szum, całość to jedno miejsce
        for i in range(1, len(groups) - 1):
            x = groups[i]
            if (
                len(x) <= settings.excursion_max_photos
                and groups[i + 1][0].time_utc - groups[i - 1][-1].time_utc <= settings.excursion_max_time
                and dist(i - 1, i + 1) <= radius
                and dist(i - 1, i) <= 5 * radius
            ):
                groups[i - 1:i + 2] = [groups[i - 1] + x + groups[i + 1]]
                centers[i - 1:i + 2] = [_median_center(groups[i - 1])]
                changed = True
                break
        if changed:
            continue
        # c) 1–2 zdjęcia zrobione chwilę przed/po dużym miejscu, niedaleko → skok GPS, dołącz do niego
        for i, x in enumerate(groups):
            if len(x) > settings.excursion_max_photos:
                continue
            best = None
            for j in (i - 1, i + 1):
                if not 0 <= j < len(groups) or len(groups[j]) <= settings.excursion_max_photos:
                    continue
                gap = x[0].time_utc - groups[j][-1].time_utc if j < i else groups[j][0].time_utc - x[-1].time_utc
                if gap <= settings.excursion_snap and dist(i, j) <= 5 * radius and (best is None or gap < best[0]):
                    best = (gap, j)
            if best:
                j = best[1]
                lo, hi = min(i, j), max(i, j)
                groups[lo:hi + 1] = [groups[lo] + groups[hi]]
                centers[lo:hi + 1] = [_median_center(groups[lo])]
                changed = True
                break

    visits = []
    for g, (lat, lon) in zip(groups, centers):
        v = Visit(len(visits), lat, lon, g)
        for p in g:
            p.visit_index = v.index
        visits.append(v)
    return visits


def build_legs(visits: list[Visit]) -> list[Leg]:
    return [
        Leg(a, b, haversine_m(a.lat, a.lon, b.lat, b.lon), b.arrival.time_utc - a.departure.time_utc)
        for a, b in zip(visits, visits[1:])
    ]


def build_days(photos: list[ResolvedPhoto], visits: list[Visit], legs: list[Leg]) -> list[Day]:
    by_day: dict[date, list[ResolvedPhoto]] = defaultdict(list)
    for p in photos:
        by_day[p.local_date].append(p)
    dist: dict[date, float] = defaultdict(float)
    for l in legs:  # odcinek liczony do dnia, w którym dotarto do celu
        dist[l.end.arrival.local_date] += l.distance_m
    days = []
    for d in sorted(by_day):
        ps = by_day[d]
        idx = sorted({p.visit_index for p in ps if p.visit_index is not None})
        days.append(Day(d, [visits[i] for i in idx], ps, dist[d]))
    return days


def resolve_trip(photos: list[PhotoMeta], settings: Settings | None = None) -> Trip:
    settings = settings or Settings()
    warnings: list[str] = []
    resolved, unplaced = resolve_times(photos, settings, warnings)
    inaccurate = 0
    for p in resolved:
        if p.meta.has_gps:
            err = p.meta.gps_error_m
            if settings.max_gps_error_m is not None and err is not None and err > settings.max_gps_error_m:
                p.gps_rejected = True  # zbyt niedokładny — pozycja zostanie oszacowana jak bez GPS
                inaccurate += 1
                continue
            p.lat, p.lon, p.position_source = p.meta.lat, p.meta.lon, POS_EXIF
    if inaccurate:
        warnings.append(f"Pominięte niedokładne odczyty GPS (błąd > {settings.max_gps_error_m:.0f} m): {inaccurate}.")
    n_out = mark_outliers(resolved, settings)
    if n_out:
        warnings.append(f"Odczyty GPS odrzucone jako błędne (nierealna prędkość): {n_out}.")
    fill_positions(resolved, settings)
    no_pos = sum(1 for p in resolved if not p.has_position)
    if resolved and no_pos == len(resolved):
        warnings.append("Żadne zdjęcie nie ma współrzędnych GPS — można odtworzyć tylko kolejność, nie trasę.")
    elif no_pos:
        warnings.append(f"Zdjęcia bez położenia (brak GPS i brak bliskich w czasie zdjęć z GPS): {no_pos}.")
    visits = build_visits(resolved, settings)
    legs = build_legs(visits)
    days = build_days(resolved, visits, legs)
    return Trip(resolved, visits, legs, days, unplaced, warnings)
