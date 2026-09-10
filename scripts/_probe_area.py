"""Probe: which area fields does a QuintoAndar buy detail actually serve?

`Listing.sqm` is read from the search hit's `area`, and at least one row
(125652767, Rua Jose Antonio Coelho 603) stored 141 m2 when the private area is
~119 m2 — the 141 is the total with both parking spaces. Every R$/m2 in the
analysis rides on this field, so find out whether the detail payload separates
useful from total area.

    uv run python scripts/_probe_area.py 125652767 [more ids...]
"""

from __future__ import annotations

import json
import sys

from rental_finder.sources.quintoandar import QuintoAndarSource

AREA_HINTS = ("area", "m2", "metragem", "size", "usable", "total", "private")


def walk(node, path="", out=None):
    out = {} if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{path}.{k}" if path else k
            if any(h in k.lower() for h in AREA_HINTS) and isinstance(
                v, (int, float, str)
            ):
                out[here] = v
            walk(v, here, out)
    elif isinstance(node, list):
        for i, v in enumerate(node[:3]):
            walk(v, f"{path}[{i}]", out)
    return out


def main() -> None:
    ids = sys.argv[1:] or ["125652767"]
    src = QuintoAndarSource()
    for hid in ids:
        for route in ("classificado", "imovel"):
            url = f"https://www.quintoandar.com.br/{route}/{hid}/comprar"
            detail = src._fetch_buy_detail(hid, url)
            if not detail:
                print(f"{hid} via {route}: no payload")
                continue
            hi = detail.get("houseInfo") or {}
            print(f"\n=== {hid} via {route} ===")
            print("houseInfo keys:", sorted(hi)[:80])
            print(json.dumps(walk(detail), ensure_ascii=False, indent=1)[:4000])
            break


if __name__ == "__main__":
    main()
