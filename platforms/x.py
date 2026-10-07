"""X follower counts via twscrape (used as a pip dependency, not a service).

CRITICAL correctness rule (verified against twscrape 0.20.1 source):
`QueueClient.req()` returns None when Cloudflare-challenged, when no account
is active, or on transport parse errors — and `parse_user()` swallows ALL
exceptions into None. So `user is None` NEVER implies "user doesn't exist".
This module reads the RAW GraphQL response instead:
  - rep is None                    -> TransientError (never NOT_FOUND)
  - errors mentioning not-found    -> None (positive evidence)
  - valid response, empty user      -> None (positive evidence)
  - anything else unparseable      -> TransientError

Anti-limit, layered:
  1. twscrape's own account pool: rotates accounts when an endpoint is
     rate-limited, persists sessions in SQLite at X_DB_PATH.
  2. wait_timeout=30 on the pool (None would wait FOREVER when every account
     is rate-limited) plus an outer asyncio.wait_for(60).
  3. Per-platform lock + TTL cache in app.py: no bursts, no duplicate hits.

Setup (once, on your machine with a logged-in x.com session):
    unjar x.com -f header
Paste the output into X_COOKIES. Extra accounts for pool rotation:
X_COOKIES_2, X_COOKIES_3, ... The accounts are read-only usage.
"""
import asyncio
import os

from twscrape import API
from twscrape.models import parse_user

from . import TransientError

SUPPORTED_METRICS = {"followers"}

_api: API | None = None
_started = False


def _db_path() -> str:
    path = os.environ.get("X_DB_PATH", "/data/accounts.db")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path


async def start() -> None:
    global _api, _started
    cookies = [v for k, v in sorted(os.environ.items()) if k.startswith("X_COOKIES") and v]
    if not cookies:
        raise RuntimeError(
            "X_COOKIES env is required: export auth_token+ct0 from a logged-in x.com "
            "session (unjar x.com -f header). Extra accounts: X_COOKIES_2, X_COOKIES_3..."
        )
    _api = API(_db_path(), wait_timeout=30)
    for i, c in enumerate(cookies):
        await _api.pool.add_account_cookies(f"keeper{i}", c)
    await _api.pool.login_all()
    _started = True


async def fetch(handle: str, metric: str) -> int | None:
    if metric not in SUPPORTED_METRICS:
        raise ValueError(f"unsupported metric: {metric}")
    handle = handle.strip().lstrip("@")
    if not handle:
        raise ValueError('handle is required')
    if not _started or _api is None:
        raise TransientError("x scraper not initialized")

    try:
        rep = await asyncio.wait_for(_api.user_by_login_raw(handle), timeout=60)
    except asyncio.TimeoutError:
        raise TransientError("x lookup timed out")
    except Exception as e:
        raise TransientError(f"x lookup failed: {type(e).__name__}")

    if rep is None:
        # Transport returned nothing: Cloudflare challenge, no active
        # account, session parse error. Transient by construction.
        raise TransientError("x returned no response")

    try:
        j = rep.json()
    except Exception:
        raise TransientError("x returned non-JSON response")

    errors = j.get("errors") or []
    if errors:
        msgs = " ".join(
            str(e.get("message", "")) for e in errors if isinstance(e, dict)
        ).lower()
        if any(k in msgs for k in ("not found", "could not find", "no user")):
            return None  # positive evidence the user doesn't exist
        raise TransientError("x api returned errors")

    result = ((j.get("data") or {}).get("user") or {}).get("result")
    if not result:
        return None  # valid response, empty user slot

    user = parse_user(rep)
    if user is None:
        raise TransientError("x profile unparseable")

    value = user.followersCount
    if not isinstance(value, int) or value <= 0:
        raise TransientError("follower count missing from profile")
    return value
