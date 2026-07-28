import threading
from datetime import date

from rental_finder import pipeline
from rental_finder.models import Listing
from rental_finder.notion_store import NotionStore, _stored_price

DROP_KEYS = ("PrevPrice", "PriceDrop", "PriceDropPct", "PriceChangedAt")


def _mk(kind, price):
    base = dict(
        source="zap",
        external_id="1",
        url="http://x",
        neighborhood="Moema",
        bedrooms=2,
        bathrooms=2,
        suites=1,
        parking=1,
        sqm=80,
        pets=True,
        furnished=False,
        lat=None,
        lng=None,
    )
    if kind == "buy":
        return Listing(mode="buy", price_rent=0, price_total=price, price_sale=price, **base)
    return Listing(price_rent=price, price_total=price, **base)


class _FakeClient:
    def __init__(self, page):
        self.written = []

        def _create(_s, **kw):
            self.written.append(kw["properties"])
            return {"id": "new"}

        self.pages = type(
            "P",
            (),
            {
                "update": lambda _s, page_id, properties: self.written.append(properties),
                "create": _create,
            },
        )()
        self.data_sources = type(
            "D", (), {"query": lambda _s, **kw: {"results": [page] if page else []}}
        )()


def _upsert(kind, old, new):
    """Run upsert_listing against a stubbed Notion client; return (props, listing)."""
    col = "Price" if kind == "buy" else "PriceTotal"
    page = {"id": "p1", "properties": {col: {"number": old}}} if old else None
    st = NotionStore.__new__(NotionStore)
    st.client = _FakeClient(page)
    st._ds_cache = {kind: "ds"}
    st._missing_props = set()
    st._schema_ensured = set()
    st._lock = threading.Lock()
    st._data_source_id = lambda k: "ds"
    lst = _mk(kind, new)
    st.upsert_listing(lst, kind=kind)
    return st.client.written[0], lst


def test_stored_price_by_kind():
    assert _stored_price({"properties": {"Price": {"number": 900000}}}, "buy") == 900000
    assert _stored_price({"properties": {"PriceTotal": {"number": 5000}}}, "rent") == 5000
    assert _stored_price({"properties": {}}, "rent") is None
    assert _stored_price({"properties": {"PriceTotal": {"number": 0}}}, "rent") is None


def test_rent_drop_recorded():
    props, lst = _upsert("rent", 5000.0, 4700.0)
    assert props["PrevPrice"] == {"number": 5000.0}
    assert props["PriceDrop"] == {"number": 300.0}
    # Notion percent format expects a fraction
    assert abs(props["PriceDropPct"]["number"] - 0.06) < 1e-9
    assert props["PriceChangedAt"] == {"date": {"start": date.today().isoformat()}}
    assert (lst.prev_price, lst.price_drop) == (5000.0, 300.0)
    assert abs(lst.price_drop_pct - 6.0) < 1e-9


def test_buy_drop_uses_sale_price():
    props, lst = _upsert("buy", 900000.0, 850000.0)
    assert props["PriceDrop"] == {"number": 50000.0}
    assert lst.price_drop == 50000.0


def test_drop_below_threshold_ignored():
    props, lst = _upsert("rent", 5000.0, 4980.0)  # 0.4% < 1%
    assert not any(k in props for k in DROP_KEYS)
    assert lst.price_drop is None


def test_unchanged_price_preserves_recorded_drop():
    props, _ = _upsert("rent", 4700.0, 4700.0)
    assert not any(k in props for k in DROP_KEYS)


def test_price_rise_clears_drop():
    props, lst = _upsert("rent", 4700.0, 5100.0)
    assert props["PrevPrice"] == {"number": None}
    assert props["PriceDrop"] == {"number": None}
    assert props["PriceDropPct"] == {"number": None}
    assert props["PriceChangedAt"] == {"date": None}
    assert lst.price_drop is None


def test_create_path_writes_no_drop_keys():
    props, _ = _upsert("rent", None, 4700.0)
    assert "FirstSeen" in props  # confirm we captured the create payload
    assert not any(k in props for k in DROP_KEYS)


class _RecStore:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def list_alive(self, kind="rent"):
        return iter(self.rows)

    def touch_alive(self, pid):
        self.calls.append(("touch", pid))

    def mark_gone(self, pid):
        self.calls.append(("gone", pid))

    def mark_price_up(self, pid, kind="rent", price=None):
        self.calls.append(("up", pid, price))

    def mark_price_down(self, pid, kind="rent", price=None, prev_price=None):
        self.calls.append(("down", pid, price, prev_price))


class _RecSrc:
    name = "zap"

    def __init__(self, res):
        self.res = res

    def recheck(self, *, external_id, url, kind):
        return self.res


def _reconcile(recheck_result, stored_price=5000.0, kind="rent"):
    rows = [
        {
            "page_id": "p1",
            "external_id": "E1",
            "source": "zap",
            "url": "http://x",
            "neighborhood": "Moema",
            "price": stored_price,
        }
    ]
    store = _RecStore(rows)
    stats = {"gone": 0, "price_up": 0, "price_drop": 0}
    pipeline._reconcile_alive(
        store, {"zap": _RecSrc(recheck_result)}, {"zap": set()}, {"zap"}, kind, stats
    )
    return store.calls, stats


def test_reconcile_above_max_is_price_up():
    pmax = pipeline.settings.rent_price_max
    calls, stats = _reconcile((True, pmax + 500.0))
    assert calls[0][0] == "up" and stats["price_up"] == 1


def test_reconcile_below_min_is_price_down():
    pmin = pipeline.settings.rent_price_min
    calls, stats = _reconcile((True, pmin - 500.0))
    assert calls[0][0] == "down" and stats["price_drop"] == 1
    assert calls[0][3] == 5000.0  # prev_price carried from the stored price


def test_reconcile_buy_below_min_is_price_down():
    pmin = pipeline.settings.buy_price_min
    calls, stats = _reconcile((True, pmin - 1000.0), stored_price=900000.0, kind="buy")
    assert calls[0][0] == "down" and stats["price_drop"] == 1


def test_reconcile_dead_is_gone():
    calls, stats = _reconcile((False, None))
    assert calls[0][0] == "gone" and stats["gone"] == 1
