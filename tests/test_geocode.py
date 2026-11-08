import json
from datetime import datetime, timedelta

from trip_resolver.exif import PhotoMeta
from trip_resolver.geocode import Geocoder, label_visits, place_from_nominatim
from trip_resolver.route import resolve_trip

RIALTO = {
    "name": "Ponte di Rialto", "category": "man_made", "type": "bridge",
    "address": {"man_made": "Ponte di Rialto", "road": "Ruga dei Oresi", "quarter": "Rialto",
                "suburb": "San Polo", "city": "Wenecja", "country": "Włochy"},
    "display_name": "Ponte di Rialto, Ruga dei Oresi, San Polo, Wenecja, Włochy",
}
STREET = {"name": "", "address": {"road": "Calle Larga", "suburb": "Castello", "city": "Wenecja"}}


def test_place_names():
    p = place_from_nominatim(RIALTO)
    assert p.name == "Ponte di Rialto" and p.detail == "Rialto, Wenecja"
    s = place_from_nominatim(STREET)
    assert s.name == "Calle Larga" and s.detail == "Castello, Wenecja"
    assert place_from_nominatim({"error": "Unable to geocode"}) is None


def test_lookup_caches_and_respects_rate_limit(tmp_path):
    calls, sleeps = [], []

    def fetch(url):
        calls.append(url)
        return RIALTO

    g = Geocoder(cache_path=tmp_path / "c.json", fetch=fetch, sleep=sleeps.append)
    assert g.lookup(45.43800, 12.33580).name == "Ponte di Rialto"
    assert g.lookup(45.43801, 12.33581).name == "Ponte di Rialto"  # ten sam klucz (~11 m) → z cache
    assert len(calls) == 1 and "accept-language=pl" in calls[0] and "zoom=18" in calls[0]
    g.lookup(45.4300, 12.3300)
    assert len(calls) == 2 and sleeps and 0 < sleeps[0] <= 1.0
    g.save()

    g2 = Geocoder(cache_path=tmp_path / "c.json", fetch=lambda u: (_ for _ in ()).throw(AssertionError("sieć")))
    assert g2.lookup(45.43800, 12.33580).name == "Ponte di Rialto"


def test_network_error_stops_further_queries(tmp_path):
    calls = []

    def fetch(url):
        calls.append(url)
        raise OSError("brak sieci")

    g = Geocoder(cache_path=tmp_path / "c.json", fetch=fetch, sleep=lambda s: None)
    assert g.lookup(45.0, 12.0) is None and g.lookup(46.0, 13.0) is None
    assert len(calls) == 1 and g.error
    assert json.loads((tmp_path / "c.json").read_text() if (tmp_path / "c.json").exists() else "{}") == {}


def test_label_visits(tmp_path):
    t0 = datetime(2024, 5, 1, 9)
    photos = [PhotoMeta(f"p{i}", local_time=t0 + timedelta(minutes=30 * i), utc_offset=timedelta(hours=2),
                        lat=45.43 + i * 0.01, lon=12.33) for i in range(3)]
    trip = resolve_trip(photos)
    answers = iter([RIALTO, {"error": "x"}, STREET])
    g = Geocoder(cache_path=tmp_path / "c.json", fetch=lambda u: next(answers), sleep=lambda s: None)
    assert label_visits(trip, g) == 2
    assert [v.name for v in trip.visits] == ["Ponte di Rialto", None, "Calle Larga"]
    assert not trip.warnings
