from datetime import datetime, timedelta, timezone

import pytest

from trip_resolver.exif import PhotoMeta
from trip_resolver.route import (
    POS_EXIF, POS_INTERPOLATED, TIME_CAMERA_OFFSET, TIME_EXIF_OFFSET, TIME_FLOATING, TIME_GPS_PAIR,
    TIME_USER_TZ, Settings, resolve_trip,
)

T0 = datetime(2024, 5, 1, 9, 0, 0)
PLUS2 = timedelta(hours=2)


def pm(name, minutes, lat=None, lon=None, offset=PLUS2, camera=("Cam", "A"), gps_utc=None, local=True):
    return PhotoMeta(path=name, local_time=T0 + timedelta(minutes=minutes) if local else None,
                     utc_offset=offset, lat=lat, lon=lon, make=camera[0], model=camera[1], gps_time=gps_utc)


def test_chronological_order_regardless_of_input_order():
    photos = [pm("c", 60, 45.44, 12.34), pm("a", 0, 45.43, 12.33), pm("b", 30, 45.435, 12.335)]
    trip = resolve_trip(photos)
    assert [p.meta.path for p in trip.photos] == ["a", "b", "c"]
    assert all(p.time_source == TIME_EXIF_OFFSET for p in trip.photos)
    assert trip.photos[0].time_utc == datetime(2024, 5, 1, 7, 0, tzinfo=timezone.utc)


def test_mixed_cameras_ordered_by_real_time():
    # telefon zapisuje strefę (+02:00); aparat nie, ale ma tę samą strefę z --tz
    phone = pm("phone", 10, 45.43, 12.33)
    camera = pm("camera", 5, 45.43, 12.33, offset=None, camera=("Canon", "R6"))
    trip = resolve_trip([phone, camera], Settings(tz="Europe/Rome"))
    assert [p.meta.path for p in trip.photos] == ["camera", "phone"]
    assert trip.photos[0].time_source == TIME_USER_TZ


def test_offset_from_gps_time_and_camera_median():
    # zdjęcie z GPS: czas lokalny 09:00, GPS 07:00 UTC → +02:00; drugie z tego aparatu bez GPS dziedziczy
    a = pm("a", 0, 45.43, 12.33, offset=None, gps_utc=datetime(2024, 5, 1, 7, 0, tzinfo=timezone.utc))
    b = pm("b", 30, offset=None)
    trip = resolve_trip([a, b])
    by = {p.meta.path: p for p in trip.photos}
    assert by["a"].time_source == TIME_GPS_PAIR and by["a"].utc_offset == PLUS2
    assert by["b"].time_source == TIME_CAMERA_OFFSET and by["b"].utc_offset == PLUS2


def test_floating_time_warns():
    trip = resolve_trip([pm("a", 0, offset=None), pm("b", 1, offset=None)])
    assert {p.time_source for p in trip.photos} == {TIME_FLOATING}
    assert trip.warnings


def test_camera_clock_shift():
    a = pm("a", 0, offset=None, camera=("Canon", "R6"))
    b = pm("b", 5, offset=None, camera=("Nikon", "Z6"))
    # zegar Canona spóźnia się o 10 min → po korekcie (+10) a jest PO b
    settings = Settings(camera_shift={"Canon R6": timedelta(minutes=10)})
    trip = resolve_trip([a, b], settings)
    assert [p.meta.path for p in trip.photos] == ["b", "a"]
    # metadane nie są modyfikowane — drugie przeliczenie daje to samo
    again = resolve_trip([a, b], settings)
    assert [p.time_utc for p in again.photos] == [p.time_utc for p in trip.photos]


def test_interpolates_missing_gps():
    trip = resolve_trip([pm("a", 0, 45.0, 12.0), pm("mid", 30), pm("b", 60, 45.1, 12.1)])
    mid = next(p for p in trip.photos if p.meta.path == "mid")
    assert mid.position_source == POS_INTERPOLATED
    assert mid.lat == pytest.approx(45.05) and mid.lon == pytest.approx(12.05)


def test_does_not_interpolate_over_long_gap():
    trip = resolve_trip([pm("a", 0, 45.0, 12.0), pm("mid", 200), pm("b", 400, 45.1, 12.1)])
    mid = next(p for p in trip.photos if p.meta.path == "mid")
    assert mid.lat is None


def test_rejects_gps_jump():
    # Wenecja → nagle Nowy Jork na minutę → Wenecja
    photos = [pm("a", 0, 45.43, 12.33), pm("x", 1, 40.7, -74.0), pm("b", 2, 45.431, 12.331)]
    trip = resolve_trip(photos)
    x = next(p for p in trip.photos if p.meta.path == "x")
    assert x.gps_rejected
    assert x.position_source == POS_INTERPOLATED
    assert len(trip.visits) == 1


def test_real_flight_is_not_rejected():
    # Warszawa → Wenecja samolotem (~1h 50min) — zostaje
    photos = [pm("waw", 0, 52.165, 20.967), pm("vce", 110, 45.505, 12.352), pm("vce2", 120, 45.506, 12.353)]
    trip = resolve_trip(photos)
    assert not any(p.gps_rejected for p in trip.photos)
    assert len(trip.visits) == 2
    assert trip.legs[0].mode == "samolot"


def test_visits_legs_and_days():
    photos = [
        # dzień 1: dwa miejsca ~1 km od siebie
        pm("a1", 0, 45.4340, 12.3380), pm("a2", 10, 45.4342, 12.3381),
        pm("b1", 30, 45.4410, 12.3270), pm("b2", 50, 45.4411, 12.3272),
        # dzień 2: powrót w miejsce A — to nowa wizyta, nie ta sama
        pm("c1", 24 * 60, 45.4340, 12.3380),
    ]
    trip = resolve_trip(photos)
    assert len(trip.visits) == 3
    assert [len(v.photos) for v in trip.visits] == [2, 2, 1]
    assert len(trip.days) == 2
    assert [len(d.visits) for d in trip.days] == [2, 1]
    leg = trip.legs[0]
    assert 900 < leg.distance_m < 1400
    assert leg.duration == timedelta(minutes=20)
    assert leg.mode == "pieszo"
    assert trip.distance_m > 0


def test_photo_without_time_is_unplaced():
    trip = resolve_trip([pm("a", 0, 45.0, 12.0), pm("nodate", 0, local=False, offset=None)])
    assert [m.path for m in trip.unplaced] == ["nodate"]
    assert all(p.meta.path != "nodate" for p in trip.photos)


def test_gps_only_time():
    m = PhotoMeta(path="g", lat=45.0, lon=12.0, gps_time=datetime(2024, 5, 1, 7, 0, tzinfo=timezone.utc))
    trip = resolve_trip([m])
    assert trip.photos[0].time_utc.hour == 7 and trip.photos[0].position_source == POS_EXIF
