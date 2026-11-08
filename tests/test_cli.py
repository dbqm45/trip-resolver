import csv
import json
from datetime import datetime

from trip_resolver.cli import main


def test_end_to_end(tmp_path, photo_factory):
    photo_factory("in/3.jpg", taken=datetime(2024, 5, 1, 12, 0), offset="+02:00", lat=45.4410, lon=12.3270)
    photo_factory("in/1.jpg", taken=datetime(2024, 5, 1, 10, 0), offset="+02:00", lat=45.4340, lon=12.3380)
    photo_factory("in/2.jpg", taken=datetime(2024, 5, 1, 11, 0), offset="+02:00")  # bez GPS
    out = tmp_path / "out"
    assert main([str(tmp_path / "in"), "-o", str(out), "--title", "Wenecja <test>"]) == 0

    rows = list(csv.DictReader(open(out / "timeline.csv", encoding="utf-8")))
    assert [r["file"].rsplit("/", 1)[1] for r in rows] == ["1.jpg", "2.jpg", "3.jpg"]
    assert rows[1]["position_source"] == "interpolated"
    assert rows[0]["utc_time"] == "2024-05-01T08:00:00+00:00"

    gj = json.loads((out / "route.geojson").read_text(encoding="utf-8"))
    kinds = [f["properties"]["kind"] for f in gj["features"]]
    assert kinds.count("day-route") == 1 and kinds.count("photo") == 3

    html = (out / "trip.html").read_text(encoding="utf-8")
    assert "Wenecja &lt;test&gt;" in html and "data:image/jpeg;base64," in html
    assert "<script" not in html.split("const D = ")[1].split(";\nconst esc")[0]


def test_bad_tz(tmp_path, photo_factory):
    photo_factory("in/1.jpg", taken=datetime(2024, 5, 1, 10, 0))
    assert main([str(tmp_path / "in"), "-o", str(tmp_path / "o"), "--tz", "Mars/Olympus"]) == 2
