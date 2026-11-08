from __future__ import annotations

from datetime import datetime
from fractions import Fraction
from pathlib import Path

import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational


def _dms(deg: float):
    deg = abs(deg)
    d = int(deg)
    m_full = (deg - d) * 60
    m = int(m_full)
    s = Fraction((m_full - m) * 60).limit_denominator(10000)
    return (IFDRational(d, 1), IFDRational(m, 1), IFDRational(s.numerator, s.denominator))


def make_photo(
    path: Path,
    taken: datetime | None = None,
    offset: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    gps_utc: datetime | None = None,
    gps_error: float | None = None,
    make: str = "TestCam",
    model: str = "TC-1",
    color=(120, 160, 200),
) -> Path:
    img = Image.new("RGB", (64, 48), color)
    exif = Image.Exif()
    exif[0x010F] = make
    exif[0x0110] = model
    sub = exif.get_ifd(0x8769)
    if taken:
        sub[0x9003] = taken.strftime("%Y:%m:%d %H:%M:%S")
    if offset:
        sub[0x9011] = offset
    if lat is not None and lon is not None:
        gps = exif.get_ifd(0x8825)
        gps[1] = "N" if lat >= 0 else "S"
        gps[2] = _dms(lat)
        gps[3] = "E" if lon >= 0 else "W"
        gps[4] = _dms(lon)
        if gps_error is not None:
            gps[31] = IFDRational(int(gps_error * 10), 10)
        if gps_utc:
            gps[7] = (IFDRational(gps_utc.hour, 1), IFDRational(gps_utc.minute, 1), IFDRational(gps_utc.second, 1))
            gps[29] = gps_utc.strftime("%Y:%m:%d")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", exif=exif.tobytes())
    return path


@pytest.fixture
def photo_factory(tmp_path):
    def factory(name, **kw):
        return make_photo(tmp_path / name, **kw)

    return factory
