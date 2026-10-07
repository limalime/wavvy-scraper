"""TikTok follower counts. Ports the Clawk (haithamaouati/Clawk) extraction:
plain HTTP GET of the public profile page, "followerCount" from embedded JSON.

No auth, no browser. Anti-limit: TTL cache + per-platform lock in app.py, rotating UA here, plus a
modest timeout. Verified live 2026-10-07.
"""
import asyncio
import random
import re

import requests

from . import TransientError

SUPPORTED_METRICS = {"followers"}

_UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


def _fetch_sync(handle: str) -> int | None:
    handle = handle.lstrip("@")
    try:
        r = requests.get(
            f"https://www.tiktok.com/@{handle}",
            headers={"User-Agent": random.choice(_UA), "Accept-Language": "en-US,en;q=0.9"},
            timeout=30,
        )
    except Exception as e:
        raise TransientError(f"tiktok fetch failed: {type(e).__name__}: {e}")
    if r.status_code in (429, 403):
        raise TransientError(f"tiktok HTTP {r.status_code} (rate-limit/block)")
    if r.status_code != 200:
        raise TransientError(f"tiktok HTTP {r.status_code}")

    html = r.text
    # clawk.sh logic: no "id":"<digits>" means the fetch failed.
    if not re.search(r'"id":"\d+"', html):
        if "couldn't find this account" in html.lower():
            return None
        raise TransientError("tiktok page unparseable (possible block)")

    # The followerCount must belong to the requested handle: verify the
    # page's own uniqueId instead of trusting the first regex match.
    uids = {u.lower() for u in re.findall(r'"uniqueId":"([^"]+)"', html)}
    if handle.lower() not in uids:
        raise TransientError("tiktok page identity mismatch")

    m = re.search(r'"followerCount":(\d+)', html)
    if not m:
        raise TransientError("followerCount not present in page")
    value = int(m.group(1))
    if value <= 0:
        raise TransientError("follower count missing")
    return value


async def fetch(handle: str, metric: str) -> int | None:
    if metric not in SUPPORTED_METRICS:
        raise ValueError(f"unsupported metric: {metric}")
    if not handle:
        raise ValueError('handle is required')
    return await asyncio.to_thread(_fetch_sync, handle)
