"""Spotify monthly listeners / followers.

Pattern ported from omkarcloud/spotify-scraper (validated by that repo
2026-09-19): no browser, no login. An anonymous access token is read from a
public embed page's __NEXT_DATA__ (~1h TTL, cached here), then the
queryArtistOverview persisted GraphQL query hits api-partner.spotify.com.

curl_cffi with browser-impersonated TLS is REQUIRED: plain requests gets a
stripped page without the session (verified 2026-10-07).

Failure taxonomy (mirrors the reference):
  - 403/429, transport errors -> TransientError (503, keeper ages)
  - 401 -> force token refresh, retry once
  - GraphQL NotFound typename -> None (genuine NOT_FOUND)
"""
import asyncio
import json
import re
import threading
import time

from curl_cffi import requests as cr

from . import TransientError

SUPPORTED_METRICS = {"monthly_listeners", "followers"}

_EMBED_URL = "https://open.spotify.com/embed/track/4uLU6hMCjMI75M1A2tKUQm"
_PATHFINDER = "https://api-partner.spotify.com/pathfinder/v1/query"
# Persisted query hash for queryArtistOverview. If Spotify ships a new web
# player and queries start failing, refresh this from the player's JS bundle.
_OVERVIEW_HASH = "1ac33ddab5d39a3a9c27802774e6d78b9405cc188c6f75aed007df2a32737c72"
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_TOKEN_REFRESH_MARGIN = 60  # seconds before expiry

_token_lock = threading.Lock()
_token = {"value": None, "exp": 0.0}

# One pathfinder response carries every metric for the artist: cache the
# stats block so monthly_listeners + followers don't fetch twice.
_stats_lock = threading.Lock()
_stats_cache: dict[str, tuple[float, dict]] = {}
_STATS_TTL = 600


def _read_token() -> tuple[str, float]:
    try:
        r = cr.get(_EMBED_URL, impersonate="chrome120", timeout=30)
    except Exception as e:
        raise TransientError(f"spotify embed page failed: {type(e).__name__}: {e}")
    if r.status_code in (403, 429):
        raise TransientError(f"spotify embed page HTTP {r.status_code} (blocked)")
    m = _NEXT_DATA_RE.search(r.text or "")
    if not m:
        raise TransientError("spotify embed page has no __NEXT_DATA__ (layout changed?)")
    try:
        data = json.loads(m.group(1))
    except ValueError:
        raise TransientError("spotify embed page JSON unparseable")
    session = ((data.get("props") or {}).get("pageProps") or {}).get("state", {}).get("settings", {}).get("session", {})
    token = session.get("accessToken")
    exp_ms = session.get("accessTokenExpirationTimestampMs")
    if not token:
        raise TransientError("no anonymous session in spotify embed page")
    exp = float(exp_ms) / 1000 if exp_ms else time.time() + 1800
    return token, exp


def _access_token(force: bool = False) -> str:
    with _token_lock:
        if force or not _token["value"] or _token["exp"] - _TOKEN_REFRESH_MARGIN < time.time():
            token, exp = _read_token()
            _token["value"], _token["exp"] = token, exp
        return _token["value"]


def _query(artist_id: str, token: str) -> dict:
    headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "authorization": f"Bearer {token}",
        "app-platform": "WebPlayer",
        "origin": "https://open.spotify.com",
        "referer": "https://open.spotify.com/",
    }
    body = {
        "operationName": "queryArtistOverview",
        "variables": {"uri": f"spotify:artist:{artist_id}", "locale": "", "includePrerelease": True},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": _OVERVIEW_HASH}},
    }
    try:
        q = cr.post(_PATHFINDER, headers=headers, json=body, impersonate="chrome120", timeout=30)
    except Exception as e:
        raise TransientError(f"spotify query failed: {type(e).__name__}: {e}")
    if q.status_code == 401:
        return {"_retry": True}
    if q.status_code in (403, 429):
        raise TransientError(f"spotify pathfinder HTTP {q.status_code} (blocked)")
    if q.status_code != 200:
        raise TransientError(f"spotify pathfinder HTTP {q.status_code}")
    try:
        return q.json()
    except ValueError:
        raise TransientError("spotify pathfinder returned non-JSON")


def _stats_for(artist_id: str) -> dict | None:
    """Full stats block for the artist; None only on genuine NotFound."""
    with _stats_lock:
        entry = _stats_cache.get(artist_id)
        if entry and entry[0] > time.time():
            return entry[1]
    data = _query(artist_id, _access_token())
    if data.get("_retry"):
        data = _query(artist_id, _access_token(force=True))
        if data.get("_retry"):
            raise TransientError("spotify token refresh did not help")
    au = ((data.get("data") or {}).get("artistUnion")) or {}
    if au.get("__typename") == "NotFound":
        return None
    stats = au.get("stats") or {}
    with _stats_lock:
        _stats_cache[artist_id] = (time.time() + _STATS_TTL, stats)
    return stats


def _fetch_sync(artist_id: str, metric: str) -> int | None:
    stats = _stats_for(artist_id)
    if stats is None:
        return None
    raw = stats.get("monthlyListeners") if metric == "monthly_listeners" else stats.get("followers")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise TransientError(f"spotify {metric} missing from response")
    if value <= 0:
        raise TransientError(f"spotify {metric} missing from response")
    return value


async def fetch(handle: str, metric: str) -> int | None:
    if metric not in SUPPORTED_METRICS:
        raise ValueError(f"unsupported metric: {metric}")
    # handle is the raw artist ID; also accept a full open.spotify.com URL.
    m = re.search(r"spotify\.com/artist/([A-Za-z0-9]+)", handle or "")
    artist_id = m.group(1) if m else (handle or "").strip()
    if not artist_id:
        raise ValueError('handle is required')
    return await asyncio.to_thread(_fetch_sync, artist_id, metric)
