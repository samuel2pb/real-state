"""`drop-source` lists every row of one Source and trashes them only on apply."""

from __future__ import annotations

from rental_finder import pipeline
from rental_finder.notion_store import NotionStore


def _page(pid: str, source: str, status: str) -> dict:
    return {
        "id": pid,
        "properties": {
            "ExternalID": {"rich_text": [{"plain_text": pid}]},
            "Source": {"select": {"name": source}},
            "Status": {"select": {"name": status}},
            "URL": {"url": f"http://x/{pid}"},
        },
    }


class _FakeClient:
    """Two pages of results for the Source filter, and a trash recorder."""

    def __init__(self):
        self.queries: list[dict] = []
        self.trashed: list[str] = []
        pages = [
            {
                "results": [_page("a", "zap", "available"), _page("b", "zap", "gone")],
                "has_more": True,
                "next_cursor": "c1",
            },
            {"results": [_page("c", "zap", "gone")], "has_more": False},
        ]

        def _query(_s, **kw):
            self.queries.append(kw)
            return pages[len(self.queries) - 1]

        def _update(_s, page_id, in_trash=False, **_kw):
            if in_trash:
                self.trashed.append(page_id)

        self.data_sources = type("D", (), {"query": _query})()
        self.pages = type("P", (), {"update": _update})()


def _store(client) -> NotionStore:
    st = NotionStore.__new__(NotionStore)
    st.client = client
    st._ds_cache = {"rent": "ds"}
    st._missing_props = set()
    st._schema_ensured = set()
    st._index = {}
    import threading

    st._lock = threading.Lock()
    return st


def test_list_by_source_pages_through_every_status(monkeypatch):
    monkeypatch.setattr("rental_finder.notion_store._pace", lambda: None)
    client = _FakeClient()
    rows = list(_store(client).list_by_source("rent", "zap"))
    assert [r["page_id"] for r in rows] == ["a", "b", "c"]
    assert client.queries[0]["filter"] == {
        "property": "Source",
        "select": {"equals": "zap"},
    }
    assert client.queries[1]["start_cursor"] == "c1"


def _run(monkeypatch, apply: bool):
    monkeypatch.setattr("rental_finder.notion_store._pace", lambda: None)
    monkeypatch.setattr(type(pipeline.settings), "require_configured", lambda *a, **k: None)
    client = _FakeClient()
    monkeypatch.setattr(pipeline, "NotionStore", lambda: _store(client))
    return client, pipeline.drop_source("rent", "zap", apply=apply)


def test_drop_source_dry_run_trashes_nothing(monkeypatch):
    client, stats = _run(monkeypatch, apply=False)
    assert stats["matched"] == 3
    assert stats["by_status"] == {"available": 1, "gone": 2}
    assert stats["trashed"] == 0
    assert client.trashed == []


def test_drop_source_apply_trashes_every_row(monkeypatch):
    client, stats = _run(monkeypatch, apply=True)
    assert client.trashed == ["a", "b", "c"]
    assert stats["trashed"] == 3 and stats["failed"] == 0
