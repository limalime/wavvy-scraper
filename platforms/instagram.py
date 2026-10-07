"""Instagram follower counts via Playwright.

Extraction strategies ported from Shekhar0165/Instakand
(src/scraper/strategies/profile.strategy.ts), stripped to the single number
the keeper needs:
  1. GraphQL interception: capture /graphql responses during page load, read
     data.user.edge_followed_by.count — accepted ONLY when the payload's
     username matches the requested handle (page XHRs can carry other users).
  2. Page source: regex "edge_followed_by":{"count":N} in embedded JSON —
     accepted only when the page URL still belongs to the handle.
  3. Meta tags ("1.8M Followers") are ROUNDED: never posted to the oracle.
     Reaching this strategy means strategies 1-2 failed -> TransientError.

NOT_FOUND only on positive evidence: HTTP 404 + "isn't available" text.
Everything else unparseable -> TransientError (503, keeper ages the metric).

Anti rate-limit, layered (most block-happy platform):
  1. ONE persistent browser + context for the service lifetime. Fresh browser
     launches per request are the #1 bot signal — never do that. The browser
     is health-checked before every scrape and relaunched if dead.
  2. Persistent storage_state (cookies) on disk, saved after EVERY successful
     scrape (not just on shutdown — OOM-kills happen).
  3. Human-like delays (2-4.5s) after navigation; realistic fingerprint whose
     UA matches the actual Chromium build (130 for Playwright 1.48.0).
  4. Per-platform lock in app.py: Instagram scrapes run strictly serially.
  5. Long TTL cache in app.py (15 min): follower counts move slowly anyway.
  6. Optional IG_PROXY env: datacenter IP reputation is the main block vector;
     a residential/mobile proxy is the biggest single lever if blocks persist.
"""
import asyncio
import json
import os
import random
import re
from decimal import Decimal, InvalidOperation

from playwright.async_api import async_playwright

from . import TransientError

SUPPORTED_METRICS = {"followers"}

# Matches the Chromium build bundled with Playwright 1.48.0.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def _parse_compact(s: str) -> int:
    s = s.strip().upper().replace(",", "")
    mult = Decimal(1)
    if s.endswith("K"):
        mult, s = Decimal(1_000), s[:-1]
    elif s.endswith("M"):
        mult, s = Decimal(1_000_000), s[:-1]
    elif s.endswith("B"):
        mult, s = Decimal(1_000_000_000), s[:-1]
    try:
        return int(Decimal(s) * mult)
    except (InvalidOperation, ValueError):
        raise ValueError(f"unparseable count: {s}")


class InstagramScraper:
    SUPPORTED_METRICS = SUPPORTED_METRICS

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._context = None
        self._storage_path = os.environ.get("IG_STORAGE_PATH", "/data/ig_storage.json")

    async def _launch(self) -> None:
        if self._pw is None:
            self._pw = await async_playwright().start()
        launch: dict = {"headless": True}
        proxy = os.environ.get("IG_PROXY", "")
        if proxy:
            launch["proxy"] = {"server": proxy}
        self._browser = await self._pw.chromium.launch(**launch)
        ctx: dict = {
            "viewport": {"width": 1366, "height": 768},
            "user_agent": _UA,
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }
        if os.path.exists(self._storage_path):
            ctx["storage_state"] = self._storage_path
        self._context = await self._browser.new_context(**ctx)
        # Optional logged-in session: IG_COOKIES as a JSON cookie array
        # (export via the Cookie-Editor browser extension while logged in
        # to instagram.com). A logged-in session is trusted far more than
        # an anonymous datacenter visit — the free alternative to IG_PROXY.
        # Cookie-Editor's export format differs from Playwright's: normalize
        # field names or add_cookies() silently rejects everything.
        raw = os.environ.get("IG_COOKIES", "")
        if raw:
            try:
                loaded = 0
                for c in json.loads(raw):
                    if not isinstance(c, dict):
                        continue
                    if "instagram.com" not in str(c.get("domain", "")):
                        continue
                    same_site = str(c.get("sameSite", "")).lower()
                    cookie = {
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c["domain"],
                        "path": c.get("path", "/"),
                    }
                    if c.get("expirationDate"):
                        cookie["expires"] = float(c["expirationDate"])
                    if c.get("httpOnly"):
                        cookie["httpOnly"] = True
                    if c.get("secure"):
                        cookie["secure"] = True
                    if same_site in ("no_restriction", "none"):
                        cookie["sameSite"] = "None"
                    elif same_site == "lax":
                        cookie["sameSite"] = "Lax"
                    elif same_site == "strict":
                        cookie["sameSite"] = "Strict"
                    await self._context.add_cookies([cookie])
                    loaded += 1
                print(f"[instagram] loaded {loaded} session cookies", flush=True)
            except Exception as e:
                print(f"[instagram] IG_COOKIES rejected: {type(e).__name__}: {e}", flush=True)

    async def start(self) -> None:
        await self._launch()

    async def _ensure_alive(self) -> None:
        try:
            alive = self._browser is not None and self._browser.is_connected()
        except Exception:
            alive = False
        if not alive:
            await self._launch()

    async def _save_session(self) -> None:
        try:
            if self._context:
                parent = os.path.dirname(self._storage_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                await self._context.storage_state(path=self._storage_path)
        except Exception:
            pass

    async def stop(self) -> None:
        await self._save_session()
        for obj, fn in ((self._context, "close"), (self._browser, "close")):
            try:
                if obj:
                    await getattr(obj, fn)()
            except Exception:
                pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._context = None

    async def fetch(self, handle: str, metric: str) -> int | None:
        if metric not in SUPPORTED_METRICS:
            raise ValueError(f"unsupported metric: {metric}")
        handle = handle.strip().lstrip("@").lower()
        if not handle:
            raise ValueError("handle is required")
        # Serialization is owned by app.py's per-platform lock.
        return await self._scrape(handle)

    async def _scrape(self, handle: str) -> int | None:
        await self._ensure_alive()
        if not self._context:
            raise TransientError("instagram browser not initialized")
        page = await self._context.new_page()
        captured: list = []

        def on_response(resp):
            url = resp.url
            if "/graphql" in url or "query_hash" in url:
                captured.append(resp)

        page.on("response", on_response)
        try:
            resp = await page.goto(
                f"https://www.instagram.com/{handle}/",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            # Human-like pause: let the page settle and XHRs land.
            await page.wait_for_timeout(random.randint(2500, 4500))

            title = await page.title()
            print(f"[instagram] @{handle}: url={page.url} title={title!r}", flush=True)

            # Strategy 1: intercepted GraphQL (exact count, username-verified).
            for r in captured:
                try:
                    if "application/json" not in r.headers.get("content-type", ""):
                        continue
                    j = await r.json()
                    user = (j.get("data") or {}).get("user") or (j.get("graphql") or {}).get("user")
                    if not user:
                        continue
                    if str(user.get("username", "")).lower() != handle:
                        continue  # another user's payload (suggested/viewer)
                    cnt = (user.get("edge_followed_by") or {}).get("count")
                    if cnt:
                        await self._save_session()
                        return int(cnt)
                except Exception:
                    continue

            html = await page.content()
            status = resp.status if resp else 0
            if status == 404 or "Sorry, this page isn't available" in html:
                return None

            # Strategy 2: embedded JSON (exact count, same-page verified).
            if handle not in page.url.lower():
                raise TransientError("instagram redirected away from the profile page")
            m = re.search(r'"edge_followed_by"\s*:\s*\{\s*"count"\s*:\s*(\d+)', html)
            if m:
                await self._save_session()
                return int(m.group(1))

            # Strategy 3 exists in the reference (og:description "1.8M") but its
            # numbers are ROUNDED: posting them would swing the oracle value by
            # up to ~5% between cycles. A missing exact count is transient.
            raise TransientError("instagram follower count not extractable (possible block/login wall)")
        except TransientError:
            raise
        except Exception as e:
            raise TransientError(f"instagram scrape failed: {type(e).__name__}")
        finally:
            try:
                await page.close()
            except Exception:
                pass


scraper = InstagramScraper()
