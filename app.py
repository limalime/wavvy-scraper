"""wavvy-scraper: single self-hosted scraper service for all Wavvy oracle markets.

Exposes the keeper's sidecar contract for every platform:
    GET /metric?platform=<platform>&handle=<handle>&metric=<metric>
    Authorization: Bearer <SIDECAR_TOKEN>

Response:
    {"value": 12345, "observedAt": 1760000000, "status": "OK", "version": "1.0.0"}
    {"status": "NOT_FOUND"}                      # entity genuinely missing
    HTTP 503                                      # transient: keeper posts nothing

Run:  uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}

Env:
    SIDECAR_TOKEN   shared secret with the keeper (same value both sides).
                    The service REFUSES to start without it; set
                    ALLOW_NO_AUTH=1 only for local dev, never in production.
    X_COOKIES       x.com cookies (auth_token + ct0), from: unjar x.com -f header
                    Extra accounts for pool rotation: X_COOKIES_2, X_COOKIES_3...
    X_DB_PATH       twscrape sqlite pool (default /data/accounts.db).
    IG_PROXY        optional http(s) proxy for Instagram (biggest lever against
                    datacenter-IP blocks, e.g. a residential proxy).
    IG_STORAGE_PATH persistent browser session file (default /data/ig_storage.json).
                    Mount a volume at /data so the session survives redeploys.
    CACHE_TTL       default cache seconds (default 600).
    CACHE_TTL_<PLATFORM>  per-platform override, e.g. CACHE_TTL_INSTAGRAM=900.
    PORT            default 8000

Anti rate-limit design:
  1. TTL cache: each (platform, handle, metric) hits the platform at most once
     per 10 min (15 for Instagram) no matter how often the keeper asks.
  2. Per-platform asyncio locks: requests to one platform run serially, never
     in bursts.
  3. Circuit breaker: 3 consecutive upstream failures short-circuits the
     platform for 120s — a blocked platform isn't hammered every keeper poll.
  4. Instagram: one persistent browser+context for the service lifetime, human
     delays, persisted cookies, realistic fingerprint, optional proxy.
  5. X: twscrape's pool rotates accounts on rate limits; add 2-3 accounts.
  6. Spotify: anonymous token cached ~1h, refreshed on 401.
At keeper volume (5 handles/platform/10min) this stays far under every limit.
"""
import asyncio
import hmac
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query

from platforms import TransientError, instagram, spotify, tiktok, x

log = logging.getLogger("wavvy-scraper")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SIDECAR_TOKEN = os.environ.get("SIDECAR_TOKEN", "")
if not SIDECAR_TOKEN and os.environ.get("ALLOW_NO_AUTH") != "1":
    sys.exit("FATAL: SIDECAR_TOKEN is not set. Set it, or ALLOW_NO_AUTH=1 for local dev only.")

CACHE_TTL_DEFAULT = int(os.environ.get("CACHE_TTL", "600"))


def _ttl_for(platform: str) -> int:
    return int(os.environ.get(f"CACHE_TTL_{platform.upper()}", CACHE_TTL_DEFAULT))


# Platform handle shapes. The host can never change (we build the URLs), so
# this is about rejecting garbage before it reaches a scraper, not SSRF.
_HANDLE_RE = {
    "tiktok": re.compile(r"^[A-Za-z0-9._]{1,30}$"),
    "x": re.compile(r"^[A-Za-z0-9_]{1,30}$"),
    "instagram": re.compile(r"^[A-Za-z0-9._]{1,30}$"),
    "spotify": re.compile(r"^[A-Za-z0-9]{22}$"),
}

PLATFORMS = {
    "tiktok": tiktok,
    "x": x,
    "instagram": instagram.scraper,
    "spotify": spotify,
}

_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
_locks = {name: asyncio.Lock() for name in PLATFORMS}
# A platform whose credentials/browser fail to initialize does not take down
# the other three: it is marked unavailable and answers 503 until configured.
_available: dict[str, bool] = {}
# Circuit breaker: {platform: (consecutive_failures, open_until)}
_breaker: dict[str, tuple[int, float]] = {}
_BREAKER_THRESHOLD = 3
_BREAKER_COOLDOWN = 120


@asynccontextmanager
async def lifespan(app: FastAPI):
    for name, starter in (("x", x.start), ("instagram", instagram.scraper.start)):
        try:
            await starter()
            _available[name] = True
        except Exception as e:
            _available[name] = False
            log.warning("platform '%s' unavailable at startup: %s", name, e)
    yield
    try:
        await instagram.scraper.stop()
    except Exception:
        pass


app = FastAPI(lifespan=lifespan)


def _auth(authorization: str | None) -> None:
    if not SIDECAR_TOKEN:
        return  # local dev with ALLOW_NO_AUTH=1
    if not hmac.compare_digest(
        (authorization or "").encode(), f"Bearer {SIDECAR_TOKEN}".encode()
    ):
        raise HTTPException(status_code=401, detail="invalid sidecar token")


def _breaker_open(platform: str) -> bool:
    fails, until = _breaker.get(platform, (0, 0.0))
    return fails >= _BREAKER_THRESHOLD and until > time.time()


def _breaker_record(platform: str, ok: bool) -> None:
    if ok:
        _breaker.pop(platform, None)
    else:
        fails, _ = _breaker.get(platform, (0, 0.0))
        fails += 1
        until = time.time() + _BREAKER_COOLDOWN if fails >= _BREAKER_THRESHOLD else 0.0
        _breaker[platform] = (fails, until)
        if fails == _BREAKER_THRESHOLD:
            log.warning("circuit breaker OPEN for '%s' (%ds cooldown)", platform, _BREAKER_COOLDOWN)


@app.get("/metric")
async def metric(
    platform: str = Query(...),
    handle: str = Query(...),
    metric: str = Query(...),
    authorization: str | None = Header(default=None),
):
    _auth(authorization)

    mod = PLATFORMS.get(platform)
    if mod is None:
        raise HTTPException(status_code=400, detail=f"unsupported platform: {platform}")
    if metric not in mod.SUPPORTED_METRICS:
        raise HTTPException(status_code=400, detail=f"unsupported metric: {metric} for {platform}")
    if _available.get(platform) is False:
        raise HTTPException(status_code=503, detail=f"platform '{platform}' not configured")

    clean = handle.strip().lstrip("@")
    if not _HANDLE_RE[platform].fullmatch(clean):
        raise HTTPException(status_code=400, detail="invalid handle format")
    # TikTok handles are case-insensitive; X/IG preserve case for the fetch
    # but share one cache entry.
    key = (platform, clean.lower(), metric)

    now = time.time()
    if key in _cache:
        expires_at, cached = _cache[key]
        if expires_at > now:
            return cached

    if _breaker_open(platform):
        raise HTTPException(status_code=503, detail=f"platform '{platform}' cooling down")

    async with _locks[platform]:
        # Re-check under the lock: a concurrent request may have filled it.
        if key in _cache and _cache[key][0] > time.time():
            return _cache[key][1]
        try:
            value = await mod.fetch(clean, metric)
        except TransientError as e:
            # 503 -> the keeper treats it as a failure: logs Error, posts
            # nothing, metric ages and recovers next cycle. Never suspends.
            _breaker_record(platform, False)
            log.warning("transient failure platform=%s handle=%s: %s", platform, clean, e)
            raise HTTPException(status_code=503, detail="upstream transient failure")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception:
            # Defense in depth: no platform module may ever 500 with an
            # unexpected error shape. A surprise is transient by definition.
            _breaker_record(platform, False)
            log.exception("unexpected failure platform=%s handle=%s", platform, clean)
            raise HTTPException(status_code=503, detail="upstream transient failure")

    _breaker_record(platform, True)
    if value is None:
        response = {"status": "NOT_FOUND"}
    else:
        response = {
            "value": value,
            "observedAt": int(time.time()),
            "status": "OK",
            "version": "1.0.0",
        }
    # TTL is anchored at store time, after the fetch completed.
    _cache[key] = (time.time() + _ttl_for(platform), response)
    return response


@app.get("/health")
async def health():
    return {"ok": True, "platforms": sorted(PLATFORMS), "available": dict(_available)}
