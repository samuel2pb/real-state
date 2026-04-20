from __future__ import annotations

import json
import time
from pathlib import Path

from geopy.geocoders import Nominatim
from haversine import haversine

from .config import settings

Coord = tuple[float, float]

_nominatim = Nominatim(user_agent=settings.geocoder_user_agent)


def _cache_file(name: str) -> Path:
    return settings.cache_path / name


def _load_json(p: Path):
    return json.loads(p.read_text("utf-8")) if p.exists() else None


def _save_json(p: Path, data) -> None:
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def geocode(query: str) -> Coord:
    candidates = [query]
    q2 = (query
          .replace("Av. Pres.", "Avenida Presidente")
          .replace("Av.", "Avenida")
          .replace("R.", "Rua"))
    if q2 != query:
        candidates.append(q2)
    # strip CEP tail and building number for broader match
    import re
    q3 = re.sub(r",?\s*\d{5}-?\d{3}.*$", "", q2).strip()
    if q3 and q3 not in candidates:
        candidates.append(q3)
    q4 = re.sub(r",\s*\d+\s*-", " -", q3)
    if q4 and q4 not in candidates:
        candidates.append(q4)
    last_err: Exception | None = None
    for q in candidates:
        time.sleep(settings.geocoder_rate_limit_sec)
        try:
            loc = _nominatim.geocode(q, country_codes="br", timeout=15)
        except Exception as e:
            last_err = e; continue
        if loc is not None:
            return (loc.latitude, loc.longitude)
    raise RuntimeError(f"geocode failed: {query!r} (last_err={last_err})")


def work_coords() -> Coord:
    if settings.work_lat is not None and settings.work_lng is not None:
        return (settings.work_lat, settings.work_lng)
    cf = _cache_file("work_coords.json")
    cached = _load_json(cf)
    if cached:
        return tuple(cached)  # type: ignore[return-value]
    coords = geocode(settings.work_address)
    _save_json(cf, list(coords))
    return coords


SP_CENTER = (-23.5505, -46.6333)

# Hand-verified centers for bairros Nominatim resolves wrong or collapses to points.
# Used as override when fetched coord falls outside SP city envelope.
MANUAL_CENTERS: dict[str, tuple[float, float]] = {
    "Jardim Paulistano": (-23.5825, -46.6845),
    "Vila Nova Conceição": (-23.5849, -46.6790),
    "Vila Olímpia": (-23.5956, -46.6880),
    "Brooklin": (-23.6120, -46.6936),
    "Campo Belo": (-23.6240, -46.6742),
    "Itaim Bibi": (-23.5844, -46.6784),
    "Moema": (-23.6014, -46.6628),
    "Pinheiros": (-23.5636, -46.6857),
    "Jardim Paulista": (-23.5652, -46.6602),
}


def neighborhood_bbox(name: str) -> dict:
    """Return {'north','south','east','west'} for a SP bairro. Cached."""
    cf = _cache_file("bboxes.json")
    cache = _load_json(cf) or {}
    if name in cache:
        c = cache[name]
        if haversine(SP_CENTER, (c["lat"], c["lng"])) <= 30:
            return c
    q = f"{name}, São Paulo, SP, Brazil"
    time.sleep(settings.geocoder_rate_limit_sec)
    results = _nominatim.geocode(
        q, country_codes="br", timeout=15, exactly_one=False, limit=10,
        viewbox=[(-23.35, -46.35), (-23.75, -46.95)], bounded=True,
    ) or []
    best = None
    best_d = 1e9
    manual = MANUAL_CENTERS.get(name)
    anchor = manual if manual else SP_CENTER
    max_d = 3 if manual else 30
    for loc in results:
        if not loc.raw.get("boundingbox"):
            continue
        d = haversine(anchor, (loc.latitude, loc.longitude))
        if d > max_d:
            continue
        if d < best_d:
            best, best_d = loc, d
    if best is None and manual:
        # Synthesize a bbox around the manual center.
        lat, lng = manual
        s, n = lat - 0.015, lat + 0.015
        w, e = lng - 0.015, lng + 0.015
        cache[name] = {"north": n, "south": s, "east": e, "west": w, "lat": lat, "lng": lng}
        _save_json(cf, cache)
        return cache[name]
    if best is None:
        raise RuntimeError(f"no SP-bounded bbox for {name!r}")
    s, n, w, e = (float(x) for x in best.raw["boundingbox"])
    # Inflate degenerate (point) bboxes so bbox-based search still returns results.
    MIN_HALF = 0.015  # ~1.6 km
    if (n - s) < 2 * MIN_HALF:
        n, s = best.latitude + MIN_HALF, best.latitude - MIN_HALF
    if (e - w) < 2 * MIN_HALF:
        e, w = best.longitude + MIN_HALF, best.longitude - MIN_HALF
    cache[name] = {"north": n, "south": s, "east": e, "west": w,
                   "lat": best.latitude, "lng": best.longitude}
    _save_json(cf, cache)
    return cache[name]


def distance_km(a: Coord, b: Coord) -> float:
    return haversine(a, b)


if __name__ == "__main__":
    print("work:", work_coords())
    for n in settings.neighborhoods_list:
        print(n, "->", neighborhood_bbox(n))
