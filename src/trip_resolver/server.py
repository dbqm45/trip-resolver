"""Lokalna strona do wgrywania zdjęć przeciąganiem: trip-resolver serve

Serwer słucha tylko na 127.0.0.1. Zdjęcia trafiają do katalogu tymczasowego,
który jest usuwany po zatrzymaniu serwera.
"""

from __future__ import annotations

import http.server
import json
import re
import secrets
import shutil
import socketserver
import tempfile
import threading
import webbrowser
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

from .exif import HEIF_EXTENSIONS, IMAGE_EXTENSIONS
from .export import _fmt_dist
from .geocode import DEFAULT_URL, Geocoder
from .pipeline import OUTPUT_FILES, NoPhotosError, run
from .route import Settings

MAX_FILE_BYTES = 500 * 1024 * 1024
ALLOWED_EXT = IMAGE_EXTENSIONS | HEIF_EXTENSIONS
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_UNSAFE = re.compile(r"[^\w.\- ]+", re.UNICODE)

CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".csv": "text/csv; charset=utf-8",
                 ".geojson": "application/geo+json; charset=utf-8"}


class App:
    def __init__(self, root: Path, geocoder_url: str = DEFAULT_URL, cache_path=None):
        self.root = root
        self.geocoder_url = geocoder_url
        self.cache_path = cache_path
        self.lock = threading.Lock()
        self.sessions: dict[str, Path] = {}

    def new_session(self) -> str:
        sid = secrets.token_urlsafe(18)
        d = self.root / sid
        (d / "photos").mkdir(parents=True)
        with self.lock:
            self.sessions[sid] = d
        return sid

    def session(self, sid: str) -> Path | None:
        if not _SID_RE.match(sid or ""):
            return None
        with self.lock:
            return self.sessions.get(sid)


def safe_filename(name: str) -> str | None:
    name = Path(unquote(name).replace("\\", "/")).name
    name = _UNSAFE.sub("_", name).strip(" .")
    if not name or Path(name).suffix.lower() not in ALLOWED_EXT:
        return None
    return name[:150]


def make_handler(app: App):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "trip-resolver"

        def log_message(self, fmt, *args):  # cisza w konsoli
            pass

        # ── odpowiedzi ──
        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _local_only(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                self._json(403, {"error": "Dostęp tylko z tego komputera."})
                return False
            return True

        # ── GET ──
        def do_GET(self):
            if not self._local_only():
                return
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._send(200, UPLOAD_PAGE.encode(), "text/html; charset=utf-8")
            m = re.match(r"^/r/([^/]+)/([^/]+)$", path)
            if m:
                d = app.session(m.group(1))
                fname = m.group(2)
                f = d / "out" / fname if d and fname in OUTPUT_FILES else None
                if f and f.is_file():
                    return self._send(200, f.read_bytes(), CONTENT_TYPES[f.suffix])
            self._json(404, {"error": "Nie ma takiej strony."})

        # ── POST ──
        def do_POST(self):
            if not self._local_only():
                return
            url = urlparse(self.path)
            try:
                if url.path == "/api/session":
                    return self._json(200, {"id": app.new_session()})
                m = re.match(r"^/api/upload/([^/]+)$", url.path)
                if m:
                    return self._upload(m.group(1), parse_qs(url.query).get("name", [""])[0])
                m = re.match(r"^/api/resolve/([^/]+)$", url.path)
                if m:
                    return self._resolve(m.group(1))
                self._json(404, {"error": "Nie ma takiego adresu."})
            except Exception as e:
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def _read_json(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                raise ValueError("za duże zapytanie")
            return json.loads(self.rfile.read(n) or b"{}")

        def _upload(self, sid: str, name: str):
            d = app.session(sid)
            n = int(self.headers.get("Content-Length") or 0)
            fname = safe_filename(name)
            if not d:
                return self._json(404, {"error": "Nieznana sesja."})
            if not fname:
                self.rfile.read(min(n, MAX_FILE_BYTES))
                return self._json(415, {"error": f"Pominięto (to nie zdjęcie): {name}"})
            if n <= 0 or n > MAX_FILE_BYTES:
                return self._json(413, {"error": f"Nieprawidłowy rozmiar pliku: {name}"})
            target = d / "photos" / fname
            i = 1
            while target.exists():  # ta sama nazwa z różnych folderów
                target = d / "photos" / f"{Path(fname).stem}~{i}{Path(fname).suffix}"
                i += 1
            remaining = n
            with open(target, "wb") as f:
                while remaining:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            if remaining:
                target.unlink(missing_ok=True)
                return self._json(400, {"error": f"Przerwane wysyłanie: {name}"})
            self._json(200, {"ok": True, "name": target.name})

        def _resolve(self, sid: str):
            d = app.session(sid)
            if not d:
                return self._json(404, {"error": "Nieznana sesja."})
            opts = self._read_json()
            tz = (opts.get("tz") or "").strip() or None
            if tz:
                try:
                    ZoneInfo(tz)
                except Exception:
                    return self._json(400, {"error": f"Nieznana strefa czasowa: {tz}"})
            settings = Settings(tz=tz, stop_radius_m=float(opts.get("stop_radius") or 150))
            if opts.get("interp_gap"):
                settings.max_interp_gap = timedelta(minutes=float(opts["interp_gap"]))
            geocoder = (Geocoder(url=app.geocoder_url, lang=opts.get("lang") or "pl", cache_path=app.cache_path)
                        if opts.get("geocode", True) else None)
            title = (opts.get("title") or "").strip() or "Odtworzona trasa"
            try:
                trip = run([d / "photos"], d / "out", settings, title=title, geocoder=geocoder)
            except NoPhotosError:
                return self._json(400, {"error": "Nie wgrano żadnych zdjęć."})
            base = f"/r/{sid}/"
            self._json(200, {
                "report": base + "trip.html", "csv": base + "timeline.csv", "geojson": base + "route.geojson",
                "summary": {"photos": len(trip.photos), "visits": len(trip.visits), "days": len(trip.days),
                            "distance": _fmt_dist(trip.distance_m)},
                "warnings": trip.warnings,
            })

    return Handler


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(port: int = 8765, root: Path | None = None, **app_kw) -> tuple[_Server, App]:
    app = App(root or Path(tempfile.mkdtemp(prefix="trip-resolver-")), **app_kw)
    httpd = _Server(("127.0.0.1", port), make_handler(app))
    return httpd, app


def serve(port: int = 8765, open_browser: bool = True, **app_kw) -> int:
    httpd, app = create_server(port, **app_kw)
    url = f"http://localhost:{httpd.server_address[1]}/"
    print(f"trip-resolver działa: {url}\nPrzeciągnij zdjęcia na stronę. Zatrzymanie: Ctrl+C")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nZatrzymano.")
    finally:
        httpd.server_close()
        shutil.rmtree(app.root, ignore_errors=True)
    return 0


UPLOAD_PAGE = r"""<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trip Resolver</title>
<style>
:root{--bg:#f7f6f3;--panel:#fff;--ink:#1d1d1b;--muted:#6b6862;--line:#d9d5cc;--accent:#2e7da0;--accent-ink:#fff;--warn-bg:#fff4e0;--warn:#7a4f00;--err:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--panel:#1f1f1d;--ink:#ecebe7;--muted:#a19d95;--line:#3a3935;--accent:#6cb6d6;--accent-ink:#0d1b22;--warn-bg:#3a2e14;--warn:#f0c674;--err:#ff8a80}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:760px;margin:0 auto;padding:32px 16px 48px}
h1{font-size:24px;margin:0 0 4px}
.lead{color:var(--muted);margin:0 0 24px}
.drop{display:block;border:2px dashed var(--line);border-radius:12px;background:var(--panel);padding:40px 16px;text-align:center;cursor:pointer;transition:border-color .15s,background .15s}
.drop:hover,.drop.over{border-color:var(--accent)}
.drop strong{display:block;font-size:17px;margin-bottom:4px}
.drop span{color:var(--muted);font-size:13px}
.drop input{position:absolute;width:1px;height:1px;opacity:0}
.picks{display:flex;gap:8px;justify-content:center;margin-top:14px;flex-wrap:wrap}
.btn{font:inherit;border-radius:8px;padding:10px 16px;min-height:44px;cursor:pointer;border:1px solid var(--line);background:var(--panel);color:var(--ink)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
.btn:disabled{opacity:.5;cursor:not-allowed}
.status{margin:16px 0 0;color:var(--muted);font-size:14px;min-height:21px}
.bar{height:6px;border-radius:3px;background:var(--line);overflow:hidden;margin-top:8px}
.bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .2s}
fieldset{border:1px solid var(--line);border-radius:12px;background:var(--panel);margin:20px 0 0;padding:14px 16px}
legend{padding:0 6px;font-weight:600}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
label.f{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--muted)}
label.f input{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;min-height:44px}
label.c{display:flex;gap:8px;align-items:center;margin-top:12px;font-size:14px}
label.c input{width:20px;height:20px}
.actions{margin-top:20px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.msg{border-radius:8px;padding:8px 12px;margin-top:10px;font-size:14px;background:var(--warn-bg);color:var(--warn)}
.msg.err{color:var(--err)}
#result{margin-top:28px}
#result h2{font-size:18px;margin:0 0 6px}
.links{display:flex;gap:16px;flex-wrap:wrap;margin:8px 0 14px}
.links a{color:var(--accent)}
iframe{width:100%;height:80vh;border:1px solid var(--line);border-radius:12px;background:var(--panel)}
.hint{font-size:12px;color:var(--muted);margin-top:6px}
@media (max-width:560px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<main>
<h1>Trip Resolver</h1>
<p class="lead">Wrzuć zdjęcia z wyjazdu — trasa zostanie odtworzona z ich metadanych (czas, GPS) w kolejności, w jakiej była przebyta. Zdjęcia nie opuszczają tego komputera.</p>

<label class="drop" id="drop">
  <strong>Przeciągnij tu zdjęcia lub foldery</strong>
  <span>JPG, TIFF, HEIC, PNG, WebP</span>
  <input type="file" id="files" multiple accept="image/*,.heic,.heif">
</label>
<div class="picks">
  <button class="btn" type="button" id="pickFiles">Wybierz pliki</button>
  <button class="btn" type="button" id="pickDir">Wybierz folder</button>
  <input type="file" id="dir" webkitdirectory multiple hidden>
</div>
<div class="status" id="status" aria-live="polite">Nie wybrano zdjęć.</div>
<div class="bar" aria-hidden="true"><i id="bar"></i></div>

<fieldset>
  <legend>Ustawienia</legend>
  <div class="grid">
    <label class="f">Tytuł raportu<input id="title" value="Odtworzona trasa"></label>
    <label class="f">Strefa czasowa zdjęć bez strefy (opcjonalnie)<input id="tz" placeholder="np. Europe/Rome" list="tzs"></label>
    <label class="f">Promień jednego miejsca (m)<input id="radius" type="number" min="20" max="5000" value="150"></label>
    <label class="f">Szacuj pozycję przy przerwie do (min)<input id="gap" type="number" min="5" max="1440" value="120"></label>
  </div>
  <label class="c"><input type="checkbox" id="geocode" checked> Pobierz nazwy miejsc z OpenStreetMap</label>
  <div class="hint">Wysyłane są tylko współrzędne miejsc (ok. 1 zapytanie na sekundę), nie zdjęcia.</div>
  <datalist id="tzs"></datalist>
</fieldset>

<div class="actions">
  <button class="btn primary" type="button" id="go" disabled>Odtwórz trasę</button>
  <button class="btn" type="button" id="clear" disabled>Wyczyść</button>
</div>
<div id="msgs"></div>

<section id="result" hidden>
  <h2>Gotowe</h2>
  <div id="summary"></div>
  <div class="links">
    <a id="lReport" target="_blank" rel="noopener">Otwórz raport w nowej karcie</a>
    <a id="lCsv" download="timeline.csv">Pobierz CSV</a>
    <a id="lGeo" download="route.geojson">Pobierz GeoJSON</a>
  </div>
  <iframe id="frame" title="Raport trasy"></iframe>
</section>
</main>
<script>
const EXT = /\.(jpe?g|tiff?|png|webp|heic|heif)$/i;
let queue = [];
const $ = id => document.getElementById(id);
const status = t => $("status").textContent = t;
const bar = f => $("bar").style.width = (f * 100).toFixed(1) + "%";
function msg(text, err) { const d = document.createElement("div"); d.className = "msg" + (err ? " err" : ""); d.textContent = text; $("msgs").appendChild(d); }

try { $("tzs").innerHTML = Intl.supportedValuesOf("timeZone").map(z => `<option value="${z}">`).join(""); } catch (e) {}

function add(files) {
  const seen = new Set(queue.map(f => f.name + f.size + f.lastModified));
  let skipped = 0;
  for (const f of files) {
    if (!EXT.test(f.name)) { skipped++; continue; }
    const k = f.name + f.size + f.lastModified;
    if (!seen.has(k)) { seen.add(k); queue.push(f); }
  }
  const mb = queue.reduce((s, f) => s + f.size, 0) / 1048576;
  status(queue.length ? `Wybrano ${queue.length} zdjęć (${mb.toFixed(1)} MB)` + (skipped ? ` · pominięto ${skipped} innych plików` : "") : "Nie wybrano zdjęć.");
  $("go").disabled = $("clear").disabled = !queue.length;
}

async function entryFiles(entry) {
  if (entry.isFile) return new Promise(res => entry.file(f => res([f]), () => res([])));
  if (!entry.isDirectory) return [];
  const reader = entry.createReader(); const out = [];
  while (true) {
    const batch = await new Promise(res => reader.readEntries(res, () => res([])));
    if (!batch.length) break;
    for (const e of batch) out.push(...await entryFiles(e));
  }
  return out;
}

const drop = $("drop");
["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", async e => {
  const items = [...(e.dataTransfer.items || [])].map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  if (items.length) { status("Czytam foldery..."); const all = []; for (const it of items) all.push(...await entryFiles(it)); add(all); }
  else add(e.dataTransfer.files);
});
$("files").addEventListener("change", e => { add(e.target.files); e.target.value = ""; });
$("dir").addEventListener("change", e => { add(e.target.files); e.target.value = ""; });
$("pickFiles").addEventListener("click", () => $("files").click());
$("pickDir").addEventListener("click", () => $("dir").click());
$("clear").addEventListener("click", () => { queue = []; add([]); bar(0); $("msgs").innerHTML = ""; });

async function api(path, opts) {
  const r = await fetch(path, opts); let j = {};
  try { j = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

$("go").addEventListener("click", async () => {
  $("go").disabled = $("clear").disabled = true; $("msgs").innerHTML = ""; $("result").hidden = true;
  try {
    const { id } = await api("/api/session", { method: "POST" });
    const total = queue.reduce((s, f) => s + f.size, 0) || 1; let sent = 0, done = 0, i = 0;
    status(`Wysyłanie 0/${queue.length}...`);
    async function worker() {
      while (i < queue.length) {
        const f = queue[i++];
        try { await api(`/api/upload/${id}?name=${encodeURIComponent(f.name)}`, { method: "POST", body: f }); }
        catch (e) { msg(e.message, true); }
        sent += f.size; done++; bar(sent / total * .9); status(`Wysyłanie ${done}/${queue.length}...`);
      }
    }
    await Promise.all([worker(), worker(), worker(), worker()]);
    const geocode = $("geocode").checked;
    status(geocode ? "Odtwarzam trasę i pobieram nazwy miejsc (ok. 1 s na miejsce)..." : "Odtwarzam trasę...");
    const res = await api(`/api/resolve/${id}`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: $("title").value, tz: $("tz").value, stop_radius: +$("radius").value,
                             interp_gap: +$("gap").value, geocode, lang: (navigator.language || "pl").slice(0, 2) }) });
    bar(1);
    const s = res.summary;
    status(`Gotowe: ${s.photos} zdjęć, ${s.visits} miejsc, ${s.days} dni, ${s.distance}.`);
    (res.warnings || []).forEach(w => msg(w));
    $("summary").textContent = `${s.visits} miejsc w ${s.days} dni · ${s.distance}`;
    $("lReport").href = res.report; $("lCsv").href = res.csv; $("lGeo").href = res.geojson;
    $("frame").src = res.report; $("result").hidden = false;
    $("result").scrollIntoView({ behavior: "smooth" });
  } catch (e) { msg(e.message, true); status("Nie udało się."); }
  finally { $("go").disabled = $("clear").disabled = !queue.length; }
});
</script>
</body>
</html>
"""
