"""Zapis wyników: CSV, GeoJSON i raport HTML z mapą."""

from __future__ import annotations

import base64
import csv
import io
import json
import os
from datetime import timedelta
from pathlib import Path

from PIL import Image, ImageOps

from .route import Trip

DAY_COLORS = ["#d1495b", "#2e86ab", "#edae49", "#3b8b5a", "#8f5fbf", "#e07a3f", "#1f9e9e", "#b5446e"]


def _fmt_dur(td: timedelta) -> str:
    s = int(td.total_seconds())
    if s < 60:
        return f"{s} s"
    h, m = divmod(s // 60, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def _fmt_dist(m: float) -> str:
    return f"{m / 1000:.1f} km" if m >= 1000 else f"{m:.0f} m"


def _iso_local(p) -> str:
    return p.local_time.isoformat(timespec="seconds")


# ─────────────────────────── CSV ───────────────────────────

CSV_COLUMNS = [
    "order", "file", "local_time", "utc_time", "utc_offset", "time_source",
    "lat", "lon", "position_source", "gps_rejected", "altitude", "visit", "place", "day", "camera",
]


def write_csv(trip: Trip, path: str | os.PathLike) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for i, p in enumerate(trip.photos, 1):
            off = p.utc_offset
            w.writerow([
                i, p.meta.path, _iso_local(p), p.time_utc.isoformat(timespec="seconds"),
                "" if off is None else _offset_str(off), p.time_source,
                "" if p.lat is None else f"{p.lat:.6f}", "" if p.lon is None else f"{p.lon:.6f}",
                p.position_source or "", "1" if p.gps_rejected else "",
                "" if p.meta.altitude is None else f"{p.meta.altitude:.1f}",
                "" if p.visit_index is None else p.visit_index + 1,
                "" if p.visit_index is None or not trip.visits[p.visit_index].place
                else trip.visits[p.visit_index].place.label,
                p.local_date.isoformat(), p.meta.camera,
            ])


def _offset_str(off: timedelta) -> str:
    s = int(off.total_seconds())
    sign = "-" if s < 0 else "+"
    h, m = divmod(abs(s) // 60, 60)
    return f"{sign}{h:02d}:{m:02d}"


# ─────────────────────────── GeoJSON ───────────────────────────


def to_geojson(trip: Trip) -> dict:
    features = []
    for di, day in enumerate(trip.days):
        pts = [p for p in day.photos if p.has_position]
        if len(pts) >= 2:
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[p.lon, p.lat] for p in pts]},
                "properties": {"kind": "day-route", "date": day.date.isoformat(),
                               "distance_m": round(day.distance_m), "color": DAY_COLORS[di % len(DAY_COLORS)]},
            })
    for v in trip.visits:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [v.lon, v.lat]},
            "properties": {"kind": "visit", "index": v.index + 1,
                           "name": v.place.name if v.place else None,
                           "detail": v.place.detail if v.place else None,
                           "arrival": _iso_local(v.arrival),
                           "departure": _iso_local(v.departure), "photos": len(v.photos)},
        })
    for i, p in enumerate(trip.photos, 1):
        if p.has_position:
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [p.lon, p.lat]},
                "properties": {"kind": "photo", "order": i, "file": os.path.basename(p.meta.path),
                               "time": _iso_local(p), "position_source": p.position_source},
            })
    return {"type": "FeatureCollection", "features": features}


def write_geojson(trip: Trip, path: str | os.PathLike) -> None:
    Path(path).write_text(json.dumps(to_geojson(trip), ensure_ascii=False, indent=1), encoding="utf-8")


# ─────────────────────────── HTML ───────────────────────────


def _thumb(path: str, size: int) -> str | None:
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((size, size))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def report_data(trip: Trip, thumbnails: bool = True, thumb_size: int = 160) -> dict:
    photo_ids = {id(p): i for i, p in enumerate(trip.photos)}
    photos = []
    for i, p in enumerate(trip.photos):
        photos.append({
            "i": i, "file": os.path.basename(p.meta.path), "path": p.meta.path,
            "time": p.local_time.strftime("%H:%M:%S"), "date": p.local_date.isoformat(),
            "tz": "" if p.utc_offset is None else "UTC" + _offset_str(p.utc_offset),
            "timeSource": p.time_source, "lat": p.lat, "lon": p.lon, "pos": p.position_source,
            "rejected": p.gps_rejected, "visit": p.visit_index, "camera": p.meta.camera,
            "thumb": _thumb(p.meta.path, thumb_size) if thumbnails else None,
        })
    visits = [{
        "index": v.index, "lat": v.lat, "lon": v.lon,
        "name": v.place.name if v.place else None, "detail": v.place.detail if v.place else "",
        "arrival": v.arrival.local_time.strftime("%H:%M"), "departure": v.departure.local_time.strftime("%H:%M"),
        "date": v.arrival.local_date.isoformat(), "duration": _fmt_dur(v.duration),
        "photos": [photo_ids[id(p)] for p in v.photos],
    } for v in trip.visits]
    legs = [{
        "from": l.start.index, "to": l.end.index, "distance": _fmt_dist(l.distance_m),
        "duration": _fmt_dur(l.duration), "mode": l.mode,
        "speed": None if l.speed_kmh is None else round(l.speed_kmh, 1),
    } for l in trip.legs]
    days = [{
        "date": d.date.isoformat(), "color": DAY_COLORS[i % len(DAY_COLORS)],
        "distance": _fmt_dist(d.distance_m), "photos": [photo_ids[id(p)] for p in d.photos],
        "visits": [v.index for v in d.visits],
    } for i, d in enumerate(trip.days)]
    return {
        "summary": {
            "photos": len(trip.photos), "withGps": sum(1 for p in trip.photos if p.position_source == "exif"),
            "estimated": sum(1 for p in trip.photos if p.position_source in ("interpolated", "nearest")),
            "visits": len(trip.visits), "days": len(trip.days), "distance": _fmt_dist(trip.distance_m),
            "start": _iso_local(trip.photos[0]) if trip.photos else None,
            "end": _iso_local(trip.photos[-1]) if trip.photos else None,
            "unplaced": [os.path.basename(m.path) for m in trip.unplaced],
        },
        "warnings": trip.warnings, "photos": photos, "visits": visits, "legs": legs, "days": days,
    }


def write_html(trip: Trip, path: str | os.PathLike, title: str = "Odtworzona trasa",
               thumbnails: bool = True) -> None:
    payload = report_data(trip, thumbnails)
    payload["title"] = title
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = _TEMPLATE.replace("__TITLE__", _esc(title)).replace("__DATA__", data)
    Path(path).write_text(html, encoding="utf-8")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_TEMPLATE = r"""<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
:root{--bg:#f7f6f3;--panel:#fff;--ink:#1d1d1b;--muted:#6b6862;--line:#e4e1da;--accent:#2e86ab;--warn-bg:#fff4e0;--warn:#8a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--panel:#1f1f1d;--ink:#ecebe7;--muted:#a19d95;--line:#33322f;--accent:#6cb6d6;--warn-bg:#3a2e14;--warn:#f0c674}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.app{display:grid;grid-template-columns:400px 1fr;height:100vh}
aside{overflow-y:auto;border-right:1px solid var(--line);background:var(--panel);padding:16px}
#map{height:100vh}
h1{font-size:18px;margin:0 0 4px}
.sum{color:var(--muted);font-size:13px;margin-bottom:12px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:12px 0}
.stat{border:1px solid var(--line);border-radius:8px;padding:8px}
.stat b{display:block;font-size:17px}
.stat span{font-size:11px;color:var(--muted)}
.warn{background:var(--warn-bg);color:var(--warn);border-radius:8px;padding:8px 10px;font-size:12.5px;margin:6px 0}
.day{margin-top:18px}
.day h2{font-size:15px;margin:0 0 8px;display:flex;align-items:center;gap:8px}
.day h2 i{width:12px;height:12px;border-radius:50%;display:inline-block}
.day h2 small{color:var(--muted);font-weight:400}
.visit{border:1px solid var(--line);border-radius:8px;padding:8px 10px;cursor:pointer}
.visit:hover,.visit.on{border-color:var(--accent)}
.visit .hd{display:flex;gap:8px;align-items:baseline}
.num{font-weight:700;min-width:22px}
.visit .nm{font-weight:600}
.visit .meta{display:flex;gap:4px;color:var(--muted);font-size:12px;margin-top:2px;padding-left:30px}
.visit .t{font-variant-numeric:tabular-nums;color:var(--ink)}
.visit .d{margin-left:auto;white-space:nowrap}
.thumbs{display:flex;gap:4px;flex-wrap:wrap;margin-top:6px}
.thumbs img{width:56px;height:56px;object-fit:cover;border-radius:4px}
.leg{color:var(--muted);font-size:12px;padding:4px 0 4px 30px;border-left:2px dotted var(--line);margin-left:16px}
.nopos{font-size:12px;color:var(--muted);margin-top:6px}
.marker{background:var(--panel);color:var(--ink);border:2px solid;border-radius:50%;width:26px;height:26px;display:flex;align-items:center;justify-content:center;font:700 11px system-ui}
.leaflet-popup-content img{max-width:220px;border-radius:4px;display:block;margin-bottom:4px}
@media (max-width:800px){.app{grid-template-columns:1fr;height:auto}#map{height:55vh;order:-1}aside{border-right:0}}
</style>
</head>
<body>
<div class="app">
<aside id="side"></aside>
<div id="map"></div>
</div>
<script>
const D = __DATA__;
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function photoPopup(p) {
  const pos = {exif: "GPS ze zdjęcia", interpolated: "pozycja oszacowana (interpolacja)", nearest: "pozycja oszacowana (najbliższe zdjęcie)"}[p.pos] || "";
  return (p.thumb ? `<img src="${p.thumb}">` : "") + `<b>#${p.i + 1} ${esc(p.file)}</b><br>${esc(p.date)} ${esc(p.time)} ${esc(p.tz)}<br><small>${esc(pos)} · ${esc(p.camera)}</small>`;
}

const dayColor = {}; D.days.forEach(d => dayColor[d.date] = d.color);
const visitMarkers = {};
let map = null;
const S = D.summary;
let h = `<h1>${esc(D.title)}</h1><div class="sum">${S.start ? esc(S.start.replace("T", " ")) + " → " + esc(S.end.replace("T", " ")) : "Brak zdjęć z datą"}</div>
<div class="stats">
<div class="stat"><b>${S.distance}</b><span>dystans (w linii prostej między punktami)</span></div>
<div class="stat"><b>${S.visits}</b><span>miejsc w ${S.days} dni</span></div>
<div class="stat"><b>${S.photos}</b><span>zdjęć · ${S.withGps} z GPS, ${S.estimated} oszac.</span></div>
</div>`;
D.warnings.forEach(w => h += `<div class="warn">${esc(w)}</div>`);
if (S.unplaced.length) h += `<div class="warn">Pominięte: ${S.unplaced.map(esc).join(", ")}</div>`;

const legFrom = {}; D.legs.forEach(l => legFrom[l.from] = l);
D.days.forEach(day => {
  h += `<section class="day"><h2><i style="background:${day.color}"></i>${esc(day.date)} <small>${day.photos.length} zdj. · ${esc(day.distance)}</small></h2>`;
  day.visits.forEach(vi => {
    const v = D.visits[vi];
    const th = v.photos.slice(0, 8).map(i => D.photos[i]).filter(p => p.thumb).map(p => `<img src="${p.thumb}" alt="${esc(p.file)}" title="${esc(p.file)} ${esc(p.time)}">`).join("");
    const coords = `${v.lat.toFixed(5)}, ${v.lon.toFixed(5)}`;
    h += `<div class="visit" data-v="${vi}"><div class="hd"><span class="num">${vi + 1}.</span><span class="nm">${esc(v.name || coords)}</span></div>`
      + `<div class="meta"><span class="t">${esc(v.arrival)}${v.arrival !== v.departure ? "–" + esc(v.departure) : ""}</span>${v.detail ? " · " + esc(v.detail) : ""}<span class="d">${v.photos.length} zdj. · ${esc(v.duration)}</span></div>`
      + `${th ? `<div class="thumbs">${th}</div>` : ""}</div>`;
    const l = legFrom[vi];
    if (l && D.visits[l.to].date === day.date) h += `<div class="leg">↓ ${esc(l.distance)} · ${esc(l.duration)} · ${esc(l.mode)}</div>`;
  });
  const nopos = day.photos.map(i => D.photos[i]).filter(p => p.lat == null);
  if (nopos.length) h += `<div class="nopos">Bez położenia: ${nopos.map(p => esc(p.file) + " (" + esc(p.time) + ")").join(", ")}</div>`;
  h += `</section>`;
});
const side = document.getElementById("side");
side.innerHTML = h;
side.addEventListener("click", e => {
  const el = e.target.closest(".visit"); if (!el) return;
  side.querySelectorAll(".visit.on").forEach(x => x.classList.remove("on")); el.classList.add("on");
  const m = visitMarkers[+el.dataset.v];
  if (map && m) { map.flyTo(m.getLatLng(), Math.max(map.getZoom(), 16)); m.openPopup(); }
});

if (window.L) {
  map = L.map("map");
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'}).addTo(map);

  const bounds = [];

  D.days.forEach(day => {
    const pts = day.photos.map(i => D.photos[i]).filter(p => p.lat != null);
    if (pts.length > 1) L.polyline(pts.map(p => [p.lat, p.lon]), {color: day.color, weight: 4, opacity: .8}).addTo(map);
    pts.forEach(p => {
      bounds.push([p.lat, p.lon]);
      L.circleMarker([p.lat, p.lon], {radius: 3, color: day.color, weight: 1,
        fillOpacity: p.pos === "exif" ? .9 : .2, dashArray: p.pos === "exif" ? null : "2"})
        .bindPopup(photoPopup(p)).addTo(map);
    });
  });

  D.visits.forEach(v => {
    const color = dayColor[v.date];
    const icon = L.divIcon({className: "", html: `<div class="marker" style="border-color:${color}">${v.index + 1}</div>`, iconSize: [26, 26], iconAnchor: [13, 13]});
    visitMarkers[v.index] = L.marker([v.lat, v.lon], {icon, zIndexOffset: 1000})
      .bindPopup(`<b>${v.index + 1}. ${esc(v.name || "Miejsce " + (v.index + 1))}</b>${v.detail ? "<br>" + esc(v.detail) : ""}<br>${esc(v.date)} ${esc(v.arrival)}–${esc(v.departure)} · ${v.photos.length} zdj. · ${esc(v.duration)}`
        + (D.photos[v.photos[0]].thumb ? `<br><img src="${D.photos[v.photos[0]].thumb}">` : ""))
      .addTo(map);
  });
  if (bounds.length) map.fitBounds(bounds, {padding: [30, 30]}); else map.setView([50, 15], 4);
} else {
  document.getElementById("map").innerHTML = '<div class="warn" style="margin:16px">Nie udało się wczytać mapy (brak internetu?). Oś czasu po lewej działa bez niej.</div>';
}
</script>
</body>
</html>
"""
