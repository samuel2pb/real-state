"""Probe QA house type URL slugs."""

from curl_cffi import requests as cffi
import json, re

s = cffi.Session(impersonate="chrome146")
# Test multi-type: /apartamento/casacondominio
url = (
    "https://www.quintoandar.com.br/alugar/imovel/"
    "moema-sao-paulo-sp-brasil/apartamento/casacondominio/"
    "de-4500-a-6300-reais/2-quartos/1-2-3-vagas/2-3-4-banheiros/"
    "de-70-a-1000-m2/nao-mobiliado/aceita-pets/1-suites"
)
r = s.get(url, headers={"Accept": "text/html", "Accept-Language": "pt-BR"}, timeout=20)
print(f"Status: {r.status_code}")
m = re.search(r"__NEXT_DATA__[^>]*>(.*?)</script>", r.text, re.DOTALL)
if m:
    nd = json.loads(m.group(1))
    init = nd.get("props", {}).get("pageProps", {}).get("initialState", {})
    filters = init.get("search", {}).get("filters", {}).get("choices", {})
    ht = filters.get("houseTypes", [])
    total = init.get("search", {}).get("visibleHouses", {}).get("total", 0)
    houses = init.get("houses", {})
    ids = [k for k in houses if k.isdigit()]
    print(f"houseTypes={ht} total={total} houses={len(ids)}")
    for hid in ids:
        h = houses[hid]
        print(
            f"  {hid}: type={h.get('houseType')} beds={h.get('bedrooms')} rent={h.get('rentPrice')}"
        )
else:
    print("NO_NEXT_DATA")
