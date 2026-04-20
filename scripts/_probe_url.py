"""Probe full-filter QA URL."""

from curl_cffi import requests as cffi
import json, re

s = cffi.Session(impersonate="chrome146")
url = (
    "https://www.quintoandar.com.br/alugar/imovel/"
    "moema-sao-paulo-sp-brasil/apartamento/"
    "de-4500-a-6300-reais/2-quartos/1-2-3-vagas/2-3-4-banheiros/"
    "de-70-a-1000-m2/nao-mobiliado/aceita-pets/1-suites"
)
r = s.get(url, headers={"Accept": "text/html", "Accept-Language": "pt-BR"}, timeout=20)
print(f"Status: {r.status_code}")
m = re.search(r"__NEXT_DATA__[^>]*>(.*?)</script>", r.text, re.DOTALL)
if m:
    nd = json.loads(m.group(1))
    init = nd["props"]["pageProps"]["initialState"]
    houses = init.get("houses", {})
    ids = [k for k in houses if k.isdigit()]
    total = init.get("search", {}).get("visibleHouses", {}).get("total", 0)
    filters = init.get("search", {}).get("filters", {}).get("choices", {})
    print(f"Houses: {len(ids)}, Total: {total}")
    print(f"Filters: {json.dumps(filters, default=str)}")
    for hid in ids:
        h = houses[hid]
        pet_amenities = [a for a in (h.get("amenities") or []) if "pet" in a.lower()]
        print(
            f"  {hid}: rent={h.get('rentPrice')} total={h.get('totalCost')} "
            f"beds={h.get('bedrooms')} bath={h.get('bathrooms')} sqm={h.get('area')} "
            f"furn={h.get('isFurnished')} park={h.get('parkingSpots')} pets={pet_amenities}"
        )
