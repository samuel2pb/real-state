from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    notion_token: str
    notion_parent_page_id: str
    notion_rent_db_id: str = ""

    modules_enabled: str = "rent"

    source_zapimoveis_enabled: bool = True
    source_quintoandar_enabled: bool = True

    rent_city: str = "São Paulo"
    rent_state: str = "SP"
    rent_neighborhoods: str
    rent_price_min: int
    rent_price_max: int
    rent_bedrooms_min: int = 2
    rent_bedrooms_max: int = 3
    rent_bathrooms_min: int = 2
    rent_suites_min: int = 1
    rent_sqm_min: int = 70
    rent_parking_min: int = 1
    rent_property_types: str = (
        ""  # comma-separated: "apartment,condo_house" or "" for any
    )
    rent_pets_required: bool = True
    rent_furnished_allowed: bool = True
    price_drop_min_pct: float = 1.0   # ignore drops smaller than this (noise)

    # Buy module
    notion_buy_db_id: str = ""
    source_zapimoveis_buy_enabled: bool = True
    source_quintoandar_buy_enabled: bool = False

    buy_city: str = "São Paulo"
    buy_state: str = "SP"
    buy_neighborhoods: str = ""   # fallback to rent_neighborhoods if empty
    buy_price_min: int = 500000
    buy_price_max: int = 1200000
    buy_condo_max: int = 0        # max monthly condo fee (0 = no cap)
    buy_bedrooms_min: int = 2
    buy_bedrooms_max: int = 3
    buy_bathrooms_min: int = 2
    buy_suites_min: int = 1
    buy_sqm_min: int = 70
    buy_parking_min: int = 1
    buy_property_types: str = ""
    buy_pets_required: bool = False
    buy_furnished_allowed: bool = True
    # Drop a listing on what the search hit already says, before paying a detail
    # fetch for it. Deliberately cannot cover distance (QuintoAndar search hits
    # carry no coordinates), suites (search sends 0) or the condo fee (search
    # sends condo+IPTU combined, the filter wants condo alone).
    buy_prefilter_enabled: bool = True

    # Condo enrichment (name + build year + age)
    condo_enrich_enabled: bool = True
    condo_apartment_types: str = "Apartamento,APARTMENT"
    condo_cache_file: str = "condos.json"
    condo_overrides_file: str = "condo_overrides.csv"
    condo_search_enabled: bool = True
    # "claude" runs `claude -p` with WebSearch/WebFetch and reads the ficha
    # técnica itself. The three SERP providers are the old snippet tier, kept
    # only as a fallback: they wrote a 1977 building down as 2023 off a
    # neighbourhood index page, and that row then topped the ranking.
    condo_search_provider: Literal["claude", "serper", "brave", "google_cse"] = "claude"
    condo_search_api_key: str = ""
    condo_search_cse_id: str = ""      # google_cse only
    condo_search_max_per_run: int = 50
    condo_search_min_confidence: Literal["estimated", "medium", "high"] = "medium"

    # `claude -p` tier
    condo_agent_cmd: str = "claude"           # resolved via PATH
    condo_agent_model: str = "sonnet"
    condo_agent_timeout_sec: int = 420
    # Per building, handed to `claude --max-budget-usd`. Measured ~$0.50 on a
    # building that needed two sources; a cap at 0.60 killed the run before it
    # could answer.
    condo_agent_max_usd: float = 1.20
    condo_agent_concurrency: int = 2          # parallel `claude -p` subprocesses
    # Agent lookups run on their own pool. They used to share the enrich pool,
    # where a couple of multi-minute subprocesses held workers that 1400 detail
    # fetches were queued behind. 0 -> mirror condo_agent_concurrency.
    condo_pool_workers: int = 0
    # A building the search already failed on is parked for good: the year is
    # simply not in any snippet, and re-querying it burns credits every run.
    # Flip this on (or run `condo-retry-misses`) after improving the extractor.
    condo_search_retry_misses: bool = False
    condo_search_retry_days: int = 90  # only applies when retry_misses is on

    # Age gate. 208 of the 286 buildings with a known year are over 40 years
    # old, so most of what the sources return is junk we never want a row for.
    # Enforced at discovery (never upserted) and by `condo-purge-old` on the
    # rows already in Notion.
    condo_max_age_years: int = 40      # 0 disables the gate
    condo_age_filter_enabled: bool = True
    # A year is only allowed to drop a listing at or above this confidence.
    # Dropping on an "estimated" year would throw away good buildings over a
    # number nobody corroborated.
    condo_age_min_confidence: Literal["estimated", "medium", "high"] = "medium"
    # A building whose year nobody could establish stays. The gate removes what
    # is provably old, not what is merely unknown.

    work_address: str
    work_max_distance_km: float = 5.5
    work_lat: float | None = None
    work_lng: float | None = None

    schedule_cron_times: str = "0 9 * * *,0 18 * * *"
    schedule_tz: str = "America/Sao_Paulo"

    # Enrich is the long pole of a buy cycle: every detail fetch sleeps
    # http_min/max_delay_sec first, so the phase costs roughly
    # (listings x mean_delay / pool_workers). Raising this scales the request
    # rate at the source in the same proportion — move it with the delays.
    pool_workers: int = 10

    http_impersonate: str = "chrome146"
    http_min_delay_sec: float = 3.0
    http_max_delay_sec: float = 10.0
    http_max_retries: int = 5
    http_backoff_base_sec: float = 1.0
    http_playwright_fallback: bool = True
    http_user_agents: str = ""

    # Notion's documented average is ~3 requests/second per integration. Writes
    # come off a 10-thread pool, so this is paced centrally rather than by
    # shrinking every pool.
    notion_max_rps: float = 2.5
    # Without this, every upsert spends one query just to learn the row's page
    # id, and all of them queue behind the pacer above. Priming an
    # (source, external_id) -> page snapshot costs one query per 100 rows.
    notion_page_index: bool = True
    notion_index_page_size: int = 100

    geocoder_user_agent: str = "real-estate-scraper/0.1"
    geocoder_rate_limit_sec: float = 1.0

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_dir: str = "logs"
    cache_dir: str = ".cache"

    @field_validator("work_lat", "work_lng", mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        if v == "" or v is None:
            return None
        return float(v)

    @property
    def neighborhoods_list(self) -> list[str]:
        return [n.strip() for n in self.rent_neighborhoods.split(",") if n.strip()]

    @property
    def buy_neighborhoods_list(self) -> list[str]:
        raw = self.buy_neighborhoods.strip()
        if raw:
            return [n.strip() for n in raw.split(",") if n.strip()]
        return self.neighborhoods_list  # fallback to rent list

    @property
    def condo_apartment_types_list(self) -> list[str]:
        return [
            t.strip().lower()
            for t in self.condo_apartment_types.split(",")
            if t.strip()
        ]

    @property
    def condo_pool_size(self) -> int:
        """Workers for the condo-lookup pool; 0 means follow the agent limit."""
        if self.condo_pool_workers > 0:
            return self.condo_pool_workers
        return max(1, self.condo_agent_concurrency)

    @property
    def modules_list(self) -> list[str]:
        return [m.strip() for m in self.modules_enabled.split(",") if m.strip()]

    @property
    def cron_list(self) -> list[str]:
        return [c.strip() for c in self.schedule_cron_times.split(",") if c.strip()]

    @property
    def cache_path(self) -> Path:
        p = ROOT / self.cache_dir
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def log_path(self) -> Path:
        p = ROOT / self.log_dir
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
