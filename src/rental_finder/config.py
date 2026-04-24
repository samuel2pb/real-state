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

    # Buy module
    notion_buy_db_id: str = ""
    source_zapimoveis_buy_enabled: bool = True
    source_quintoandar_buy_enabled: bool = False

    buy_city: str = "São Paulo"
    buy_state: str = "SP"
    buy_neighborhoods: str = ""   # fallback to rent_neighborhoods if empty
    buy_price_min: int = 500000
    buy_price_max: int = 1200000
    buy_bedrooms_min: int = 2
    buy_bedrooms_max: int = 3
    buy_bathrooms_min: int = 2
    buy_suites_min: int = 1
    buy_sqm_min: int = 70
    buy_parking_min: int = 1
    buy_property_types: str = ""
    buy_pets_required: bool = False
    buy_furnished_allowed: bool = True

    work_address: str
    work_max_distance_km: float = 5.5
    work_lat: float | None = None
    work_lng: float | None = None

    schedule_cron_times: str = "0 9 * * *,0 18 * * *"
    schedule_tz: str = "America/Sao_Paulo"

    http_impersonate: str = "chrome146"
    http_min_delay_sec: float = 3.0
    http_max_delay_sec: float = 10.0
    http_max_retries: int = 5
    http_backoff_base_sec: float = 1.0
    http_playwright_fallback: bool = True
    http_user_agents: str = ""

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
