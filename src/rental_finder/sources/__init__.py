from .base import Source
from .zapimoveis import ZapSource
from .quintoandar import QuintoAndarSource

__all__ = ["Source", "ZapSource", "QuintoAndarSource"]


def enabled_sources() -> list[Source]:
    from ..config import settings
    out: list[Source] = []
    if settings.source_zapimoveis_enabled:
        out.append(ZapSource())
    if settings.source_quintoandar_enabled:
        out.append(QuintoAndarSource())
    return out
