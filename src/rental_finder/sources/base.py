from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Iterator

from ..models import Listing


class Source(ABC):
    name: str

    @abstractmethod
    def search_rent(self, neighborhoods: list[str]) -> Iterator[Listing]: ...

    @abstractmethod
    def search_buy(self, neighborhoods: list[str]) -> Iterator[Listing]: ...

    @abstractmethod
    def check_alive(self, listing: Listing) -> bool: ...

    def enrich(self, lst: Listing) -> None:
        """Mutate listing with per-detail data (e.g. lat/lng, condo). Default no-op."""
        return None

    def search_tasks(
        self, neighborhoods: list[str], kind: str
    ) -> list[Callable[[], list[Listing]]]:
        """Build independent, thread-safe fetch units for parallel search fan-out.

        Each callable fetches one unit of work and returns its listings.
        Default: one task per neighborhood, delegating to search_buy/search_rent.
        """

        def _task(nb: str) -> list[Listing]:
            if kind == "buy":
                return list(self.search_buy([nb]))
            return list(self.search_rent([nb]))

        return [(lambda nb=nb: _task(nb)) for nb in neighborhoods]

    def recheck(self, *, external_id: str, url: str, kind: str) -> tuple[bool, float | None]:
        """Re-fetch a single listing that's missing from search results.

        Returns (alive, current_price). price = price_total for rent / price_sale
        for buy, or None if unknown. Default: aliveness via check_alive(), price unknown.
        """
        stub = Listing(
            source="",
            external_id=external_id,
            url=url,
            neighborhood="",
            price_rent=0.0,
            price_total=0.0,
            bedrooms=0,
            bathrooms=0,
            suites=0,
            parking=0,
            sqm=0.0,
            pets=False,
            furnished=None,
            lat=None,
            lng=None,
        )
        return (self.check_alive(stub), None)
