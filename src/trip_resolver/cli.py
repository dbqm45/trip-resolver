"""Wiersz poleceń: trip-resolver ZDJĘCIA... -o WYNIKI/"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import timedelta
from pathlib import Path

from . import __version__
from .exif import scan_paths
from .export import _fmt_dist, _fmt_dur, write_csv, write_geojson, write_html
from .route import Settings, resolve_trip

_SHIFT_RE = re.compile(r"^(?P<cam>.+)=(?P<sign>[+-])(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2}))?$")


def _parse_shift(value: str) -> tuple[str, timedelta]:
    m = _SHIFT_RE.match(value.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"oczekiwano APARAT=+HH:MM[:SS], dostałem: {value!r}")
    td = timedelta(hours=int(m["h"]), minutes=int(m["m"]), seconds=int(m["s"] or 0))
    return m["cam"].strip(), -td if m["sign"] == "-" else td


def _minutes(v: str) -> timedelta:
    return timedelta(minutes=float(v))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trip-resolver",
        description="Odtwarza przebytą trasę (chronologicznie) z metadanych zdjęć: CSV, GeoJSON i mapa HTML.",
    )
    p.add_argument("paths", nargs="+", help="pliki zdjęć lub katalogi (przeszukiwane rekurencyjnie)")
    p.add_argument("-o", "--out", default="trip-out", help="katalog wyników (domyślnie: trip-out)")
    p.add_argument("--title", default="Odtworzona trasa", help="tytuł raportu")
    p.add_argument("--tz", help="strefa czasowa dla zdjęć bez strefy, np. Europe/Rome")
    p.add_argument("--shift", action="append", type=_parse_shift, default=[], metavar="APARAT=+HH:MM[:SS]",
                   help="korekta zegara aparatu (dodawana do jego czasu), można podać wiele razy")
    p.add_argument("--stop-radius", type=float, default=150.0, metavar="M",
                   help="promień jednego miejsca w metrach (domyślnie 150)")
    p.add_argument("--stop-gap", type=_minutes, default=timedelta(hours=3), metavar="MIN",
                   help="maks. przerwa w obrębie jednego miejsca, w minutach (domyślnie 180)")
    p.add_argument("--interp-gap", type=_minutes, default=timedelta(hours=2), metavar="MIN",
                   help="maks. przerwa między zdjęciami z GPS, przy której szacujemy pozycję (domyślnie 120)")
    p.add_argument("--no-thumbnails", action="store_true", help="bez miniatur w raporcie HTML")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.tz:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(args.tz)
        except Exception:
            print(f"Nieznana strefa czasowa: {args.tz}", file=sys.stderr)
            return 2
    settings = Settings(
        tz=args.tz, camera_shift=dict(args.shift), stop_radius_m=args.stop_radius,
        stop_max_gap=args.stop_gap, max_interp_gap=args.interp_gap,
    )
    print("Skanuję zdjęcia...", file=sys.stderr)
    photos = scan_paths(args.paths)
    if not photos:
        print("Nie znaleziono zdjęć.", file=sys.stderr)
        return 1
    trip = resolve_trip(photos, settings)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(trip, out / "timeline.csv")
    write_geojson(trip, out / "route.geojson")
    write_html(trip, out / "trip.html", title=args.title, thumbnails=not args.no_thumbnails)

    print(f"\nZdjęć: {len(photos)}, na osi czasu: {len(trip.photos)}, miejsc: {len(trip.visits)}, "
          f"dni: {len(trip.days)}, dystans: {_fmt_dist(trip.distance_m)}")
    for day in trip.days:
        print(f"\n{day.date}  ({len(day.photos)} zdj., {_fmt_dist(day.distance_m)})")
        for v in day.visits:
            a, d = v.arrival.local_time, v.departure.local_time
            span = a.strftime("%H:%M") + (f"–{d.strftime('%H:%M')}" if d != a else "")
            print(f"  {v.index + 1:>3}. {span:<11} {v.lat:.5f}, {v.lon:.5f}  "
                  f"{len(v.photos)} zdj., {_fmt_dur(v.duration)}")
    for w in trip.warnings:
        print(f"\nUwaga: {w}", file=sys.stderr)
    print(f"\nWyniki: {out / 'trip.html'}, {out / 'timeline.csv'}, {out / 'route.geojson'}")
    return 0
