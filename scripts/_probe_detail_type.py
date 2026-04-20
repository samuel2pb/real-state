"""Probe QA detail for property type field."""

from curl_cffi import requests as cffi
import json, re

s = cffi.Session(impersonate="chrome146")
url = "https://www.quintoandar.com.br/imovel/892794149"
r = s.get(url, headers={"Accept": "text/html", "Accept-Language": "pt-BR"}, timeout=20)
m = re.search(r"__NEXT_DATA__[^>]*>(.*?)</script>", r.text, re.DOTALL)
if m:
    nd = json.loads(m.group(1))
    house = (
        nd.get("props", {})
        .get("pageProps", {})
        .get("initialState", {})
        .get("house", {})
    )
    hi = house.get("houseInfo") or {}
    # Look for type-related fields
    for key in sorted(hi.keys()):
        val = hi[key]
        if isinstance(val, str) and any(
            w in val.lower() for w in ["apart", "casa", "condo", "tipo", "type"]
        ):
            print(f"  houseInfo.{key} = {val!r}")
        elif isinstance(val, str) and len(val) < 50:
            continue
        elif key.lower() in ["type", "housetype", "propertytype", "kind", "tipoimovel"]:
            print(f"  houseInfo.{key} = {val!r}")
    # Also check direct keys
    print(f"\nhouseType = {hi.get('houseType')!r}")
    print(f"type = {hi.get('type')!r}")
    print(f"propertyType = {hi.get('propertyType')!r}")
    print(f"category = {hi.get('category')!r}")
    print(f"kind = {hi.get('kind')!r}")
    # Dump all keys
    print(f"\nAll houseInfo keys: {sorted(hi.keys())}")
