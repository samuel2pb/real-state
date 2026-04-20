from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator

from ..models import Listing


class Source(ABC):
    name: str

    @abstractmethod
    def search_rent(self, neighborhoods: list[str]) -> Iterator[Listing]: ...

    @abstractmethod
    def check_alive(self, listing: Listing) -> bool: ...
