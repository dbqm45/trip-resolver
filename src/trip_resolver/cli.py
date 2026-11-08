"""Wiersz poleceń.

  trip-resolver ZDJĘCIA... -o WYNIKI/     odtworzenie trasy z plików/katalogów
  trip-resolver serve                     strona do wgrywania zdjęć przeciąganiem
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import timedelta

from . import __version__
from .export import _fmt_dist, _fmt_dur
from .geocode import DEFAULT_URL, Geocoder
from .pipeline import NoPhotosError, run
from .route import Settings

_SHIFT_RE = re.compile(r"^(?P<cam>.+)=(?P<sign>[+-])(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2}))?$")


def _parse_shift(value: str) -> tuple[str, timedelta]:
    m = _SHIFT_RE.match(value.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"oczekiwano APARAT=+HH:MM[:SS], dostałem: {value!r}")
    td = timedelta(hours=int(m["h"]), minutes=int(m["m"]), seconds=int(m["s"] or 0))
    return m["cam"].strip(), -td if m["sign"] == "-" else td


def _minutes(v: str) -> timedelta:
    return timedelta(minutes=float(v))


def _add_geocode_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--geocoder-url", default=DEFAULT_URL, help="adres usługi Nominatim (domyślnie publiczny OSM)")
    p.add_argument("--geocode-cache", help="plik cache nazw miejsc (domyślnie ~/.cache/trip-resolver/places.json)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trip-resolver",
        description="Odtwarza przebytą trasę (chronologicznie) z metadanych zdjęć: CSV, GeoJSON i mapa HTML. "
                    "Strona do wgrywania zdjęć przeciąganiem: trip-resolver serve",
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
    p.add_argument("--no-geocode", action="store_true", help="bez pobierania nazw miejsc z OpenStreetMap")
    p.add_argument("--lang", default="pl", help="język nazw miejsc (domyślnie pl)")
    _add_geocode_args(p)
    p.add_argument("--no-thumbnails", action="store_true", help="bez miniatur w raporcie HTML")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def build_serve_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trip-resolver serve",
                                description="Lokalna strona do wgrywania zdjęć przeciąganiem.")
    p.add_argument("--port", type=int, default=8765, help="port (domyślnie 8765)")
    p.add_argument("--no-browser", action="store_true", help="nie otwieraj przeglądarki")
    _add_geocode_args(p)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "serve":
        from .server import serve

        a = build_serve_parser().parse_args(argv[1:])
        return serve(a.port, open_browser=not a.no_browser, geocoder_url=a.geocoder_url,
                     cache_path=a.geocode_cache)

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
    geocoder = None if args.no_geocode else Geocoder(url=args.geocoder_url, lang=args.lang,
                                                      cache_path=args.geocode_cache)
    try:
        trip = run(args.paths, args.out, settings, title=args.title, thumbnails=not args.no_thumbnails,
                   geocoder=geocoder, log=lambda s: print(s, file=sys.stderr))
    except NoPhotosError as e:
        print(e, file=sys.stderr)
        return 1

    print(f"\nNa osi czasu: {len(trip.photos)} zdjęć, miejsc: {len(trip.visits)}, "
          f"dni: {len(trip.days)}, dystans: {_fmt_dist(trip.distance_m)}")
    for day in trip.days:
        print(f"\n{day.date}  ({len(day.photos)} zdj., {_fmt_dist(day.distance_m)})")
        for v in day.visits:
            a, d = v.arrival.local_time, v.departure.local_time
            span = a.strftime("%H:%M") + (f"–{d.strftime('%H:%M')}" if d != a else "")
            where = v.place.label if v.place else f"{v.lat:.5f}, {v.lon:.5f}"
            print(f"  {v.index + 1:>3}. {span:<11} {where}  ({len(v.photos)} zdj., {_fmt_dur(v.duration)})")
    for w in trip.warnings:
        print(f"\nUwaga: {w}", file=sys.stderr)
    out = args.out.rstrip("/")
    print(f"\nWyniki: {out}/trip.html, {out}/timeline.csv, {out}/route.geojson")
    return 0
