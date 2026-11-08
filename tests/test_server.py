import http.client
import json
import threading
from datetime import datetime

import pytest

from trip_resolver.server import create_server, safe_filename


@pytest.fixture
def server(tmp_path):
    httpd, app = create_server(0, root=tmp_path / "srv")
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1], app
    httpd.shutdown()
    httpd.server_close()


def req(port, method, path, body=None, headers=None, host="127.0.0.1"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": f"{host}:{port}", **(headers or {})}
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def test_upload_and_resolve(server, photo_factory):
    port, app = server
    status, page = req(port, "GET", "/")
    assert status == 200 and b"Przeci" in page

    sid = json.loads(req(port, "POST", "/api/session")[1])["id"]
    p1 = photo_factory("a/IMG_1.jpg", taken=datetime(2024, 5, 1, 12), offset="+02:00", lat=45.441, lon=12.327)
    p2 = photo_factory("b/IMG_1.jpg", taken=datetime(2024, 5, 1, 10), offset="+02:00", lat=45.434, lon=12.338)
    for p in (p1, p2):  # ta sama nazwa pliku z dwóch folderów — obie mają zostać
        s, _ = req(port, "POST", f"/api/upload/{sid}?name=IMG_1.jpg", p.read_bytes())
        assert s == 200
    assert len(list((app.root / sid / "photos").iterdir())) == 2

    s, body = req(port, "POST", f"/api/resolve/{sid}", json.dumps({"title": "Test", "geocode": False}),
                  {"Content-Type": "application/json"})
    res = json.loads(body)
    assert s == 200, res
    assert res["summary"]["photos"] == 2 and res["summary"]["visits"] == 2
    s, html = req(port, "GET", res["report"])
    assert s == 200 and b"Test" in html
    s, csv_ = req(port, "GET", res["csv"])
    assert s == 200 and csv_.startswith(b"order,")


def test_rejects_bad_input(server):
    port, app = server
    sid = json.loads(req(port, "POST", "/api/session")[1])["id"]
    assert req(port, "POST", f"/api/upload/{sid}?name=evil.sh", b"rm -rf")[0] == 415
    assert req(port, "POST", "/api/upload/nope?name=a.jpg", b"x")[0] == 404
    assert req(port, "POST", f"/api/resolve/{sid}", b"{}", {"Content-Type": "application/json"})[0] == 400
    assert req(port, "GET", f"/r/{sid}/../../etc/passwd")[0] == 404
    assert req(port, "GET", "/", host="evil.example")[0] == 403  # ochrona przed DNS rebinding


@pytest.mark.parametrize("raw,expected", [
    ("IMG_1.JPG", "IMG_1.JPG"), ("../../x.jpg", "x.jpg"), ("C:\\a\\b.heic", "b.heic"),
    ("notes.txt", None), ("..jpg", None), ("zdjęcie 1.jpeg", "zdjęcie 1.jpeg"),
])
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected
