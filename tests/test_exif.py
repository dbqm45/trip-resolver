from datetime import datetime, timedelta, timezone

import pytest

from trip_resolver.exif import parse_offset, read_photo, scan_paths


def test_reads_time_offset_and_gps(photo_factory):
    p = photo_factory("a.jpg", taken=datetime(2024, 5, 1, 10, 30, 0), offset="+02:00",
                      lat=45.434, lon=12.338, gps_utc=datetime(2024, 5, 1, 8, 30, 0))
    m = read_photo(p)
    assert m.error is None
    assert m.local_time == datetime(2024, 5, 1, 10, 30, 0)
    assert m.utc_offset == timedelta(hours=2)
    assert m.lat == pytest.approx(45.434, abs=1e-5)
    assert m.lon == pytest.approx(12.338, abs=1e-5)
    assert m.gps_time == datetime(2024, 5, 1, 8, 30, tzinfo=timezone.utc)
    assert m.camera == "TestCam TC-1"


def test_southern_western_hemisphere(photo_factory):
    m = read_photo(photo_factory("b.jpg", taken=datetime(2024, 1, 1, 12), lat=-33.8568, lon=-70.6483))
    assert m.lat == pytest.approx(-33.8568, abs=1e-5)
    assert m.lon == pytest.approx(-70.6483, abs=1e-5)


def test_no_exif_and_broken_file(tmp_path, photo_factory):
    m = read_photo(photo_factory("c.jpg"))
    assert m.local_time is None and not m.has_gps and m.error is None
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    assert read_photo(bad).error


def test_scan_is_recursive_and_skips_other_files(tmp_path, photo_factory):
    photo_factory("x/1.jpg", taken=datetime(2024, 1, 1))
    photo_factory("x/y/2.jpg", taken=datetime(2024, 1, 2))
    (tmp_path / "x" / "notes.txt").write_text("hi")
    found = scan_paths([tmp_path / "x"])
    assert sorted(p.path.rsplit("/", 1)[1] for p in found) == ["1.jpg", "2.jpg"]


@pytest.mark.parametrize("raw,expected", [
    ("+02:00", timedelta(hours=2)), ("-05:30", -timedelta(hours=5, minutes=30)),
    ("+0100", timedelta(hours=1)), ("garbage", None), (None, None),
])
def test_parse_offset(raw, expected):
    assert parse_offset(raw) == expected
