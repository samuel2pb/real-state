from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any

import structlog
from curl_cffi import requests as cffi

from .config import settings

log = structlog.get_logger(__name__)

DEFAULT_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
]


class CurlSession:
    def __init__(self, source_name: str, base_headers: dict[str, str] | None = None):
        self.source_name = source_name
        self.session = cffi.Session(impersonate=settings.http_impersonate)
        self.base_headers = base_headers or {}
        self._uas = [
            u.strip() for u in settings.http_user_agents.split("|") if u.strip()
        ] or DEFAULT_UAS
        self._cookie_file: Path = settings.cache_path / f"cookies_{source_name}.json"
        self._load_cookies()

    def _load_cookies(self) -> None:
        import json

        if self._cookie_file.exists():
            try:
                for k, v in json.loads(self._cookie_file.read_text("utf-8")).items():
                    self.session.cookies.set(k, v)
            except Exception:
                pass

    def _save_cookies(self) -> None:
        import json

        try:
            data = {c.name: c.value for c in self.session.cookies.jar}
            self._cookie_file.write_text(json.dumps(data), "utf-8")
        except Exception:
            pass

    def _pace(self) -> None:
        lo, hi = settings.http_min_delay_sec, settings.http_max_delay_sec
        time.sleep(random.uniform(lo, hi))

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {
            "User-Agent": random.choice(self._uas),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        }
        h.update(self.base_headers)
        if extra:
            h.update(extra)
        return h

    def request(self, method: str, url: str, **kw: Any):
        attempt = 0
        max_retries = settings.http_max_retries
        base = settings.http_backoff_base_sec
        while True:
            self._pace()
            kw["headers"] = self._headers(kw.pop("headers", None))
            try:
                r = self.session.request(method, url, timeout=30, **kw)
            except Exception as e:
                attempt += 1
                if attempt > max_retries:
                    raise
                log.warning(
                    "http_exc", source=self.source_name, err=str(e), attempt=attempt
                )
                time.sleep(base * (2 ** (attempt - 1)))
                continue
            if r.status_code == 400:
                log.error(
                    "http_400",
                    source=self.source_name,
                    url=url,
                    body=(r.text or "")[:400],
                )
                r.raise_for_status()
            if r.status_code in (429, 403, 503):
                attempt += 1
                if attempt > max_retries:
                    log.error(
                        "http_giveup",
                        source=self.source_name,
                        status=r.status_code,
                        url=url,
                    )
                    r.raise_for_status()
                retry_after = r.headers.get("Retry-After")
                wait = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else base * (2 ** (attempt - 1))
                )
                log.warning(
                    "http_backoff",
                    source=self.source_name,
                    status=r.status_code,
                    wait=wait,
                    attempt=attempt,
                )
                time.sleep(wait)
                continue
            r.raise_for_status()
            self._save_cookies()
            return r

    def get(self, url: str, **kw):
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw):
        return self.request("POST", url, **kw)
