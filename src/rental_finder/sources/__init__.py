from .base import Source
from .quintoandar import QuintoAndarSource

__all__ = ["Source", "QuintoAndarSource"]


def enabled_sources(mode: str = "rent") -> list[Source]:
    """QuintoAndar is the only source, for both the rent and the buy module."""
    return [QuintoAndarSource()]
