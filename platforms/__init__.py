"""Shared types for wavvy-scraper platform modules.

Each platform module exposes:
    SUPPORTED_METRICS: set[str]
    async def fetch(handle: str, metric: str) -> int | None

Contract:
  - returns int  -> the metric value (must be > 0)
  - returns None  -> the entity genuinely does not exist (NOT_FOUND)
  - raises TransientError -> rate-limited, blocked, browser crashed, upstream
    hiccup. The app maps this to HTTP 503 so the keeper posts nothing and the
    metric ages instead of being suspended.

Never return 0 for a missing metric. Never map a transient failure to None.
"""


class TransientError(Exception):
    """Retryable failure: block, rate-limit, crash, timeout, upstream 5xx."""
