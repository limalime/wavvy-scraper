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
  6. Optional IG_PROXY / IG_PROXY_2 / ... env: datacenter IP reputation is
     the main block vector; residential/mobile proxies rotate on every
     browser (re)launch for redundancy.
"""
import asyncio
import json
import os
import random
import re
from decimal import Decimal, InvalidOperation

from playwright.async_api import async_playwright
from playwright_stealth import stealth_async
from curl_cffi import requests as cr

from . import TransientError

SUPPORTED_METRICS = {"followers"}

# Direct API (no browser): web_profile_info with the logged-in session.
# No automation fingerprints at all — just an HTTP client with cookies.
# This runs BEFORE the browser strategies; browser stays as fallback.
_WEB_PROFILE_INFO = "https://www.instagram.com/api/v1/users/web_profile_info/?username={}"
_X_IG_APP_ID = "936619743392459"  # Instagram web app ID (stable for years)

# Apify fallback (paid): apify/instagram-profile-scraper via run-sync API.
# Used ONLY when free methods fail. At ~$0.0026/profile, 5 profiles hourly
# ≈ $9.50/month. Set APIFY_TOKEN env to enable; unset = skip (free-only).
_APIFY_ACTOR = "apify~instagram-profile-scraper"
_APIFY_URL = f"https://api.apify.com/v2/acts/{_APIFY_ACTOR}/run-sync-get-dataset-items"


def _apify_fetch(handle: str) -> int | None:
    """Paid fallback: Apify instagram-profile-scraper. Returns None when
    APIFY_TOKEN unset or the run fails (caller treats as transient)."""
    token = os.environ.get("APIFY_TOKEN", "")
    if not token:
        return None
    try:
        r = cr.post(
            _APIFY_URL,
            params={"token": token},
            json={"usernames": [handle]},
            timeout=120,
        )
    except Exception as e:
        print(f"[instagram] apify error: {type(e).__name__}", flush=True)
        return None
    if r.status_code != 200:
        print(f"[instagram] apify HTTP {r.status_code}", flush=True)
        return None
    try:
        items = json.loads(r.text or "[]")
    except ValueError:
        return None
    if not items:
        return None
    item = items[0] if isinstance(items, list) else {}
    if not isinstance(item, dict) or item.get("error"):
        return None
    if str(item.get("username", "")).lower() != handle.lower():
        return None
    try:
        cnt = int(item.get("followersCount") or 0)
    except (TypeError, ValueError):
        return None
    if cnt > 0:
        print(f"[instagram] @{handle}: apify hit ({cnt})", flush=True)
    return cnt if cnt > 0 else None

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


def _session_cookies() -> dict:
    """Parse IG_COOKIES env into a name->value dict (empty if unset)."""
    raw = os.environ.get("IG_COOKIES", "")
    if not raw:
        return {}
    try:
        return {c["name"]: c["value"] for c in json.loads(raw)
                if isinstance(c, dict) and "name" in c and "value" in c}
    except Exception:
        return {}


def _direct_fetch(handle: str) -> int | None:
    """Strategy 0: direct web_profile_info API with session cookies, no browser.
    No automation fingerprints at all — just an HTTP client with cookies.
    Returns None when unavailable (caller falls through to browser).
    """
    cookies = _session_cookies()
    if not cookies.get("sessionid"):
        return None  # no logged-in session: browser path only
    ig_names = {"sessionid", "ds_user_id", "csrftoken", "mid", "ig_did",
                "datr", "rur", "dpr", "shbid", "shbts", "wd"}
    headers = {
        "user-agent": _UA,
        "x-ig-app-id": _X_IG_APP_ID,
        "x-csrftoken": cookies.get("csrftoken", ""),
        "x-requested-with": "XMLHttpRequest",
        "referer": f"https://www.instagram.com/{handle}/",
        "cookie": "; ".join(f"{k}={v}" for k, v in cookies.items() if k in ig_names),
    }
    try:
        r = cr.get(_WEB_PROFILE_INFO.format(handle), headers=headers,
                   impersonate="chrome120", timeout=30)
    except Exception:
        return None
    if r.status_code != 200:
        return None  # 403/429/404: let the browser path decide NOT_FOUND vs transient
    try:
        data = json.loads(r.text or "{}")
    except ValueError:
        return None
    user = (data.get("data") or {}).get("user")
    if not user:
        return None
    if str(user.get("username", "")).lower() != handle.lower():
        return None  # wrong user payload
    try:
        cnt = int((user.get("edge_followed_by") or {}).get("count") or 0)
    except (TypeError, ValueError):
        return None
    return cnt if cnt > 0 else None


class InstagramScraper:
    SUPPORTED_METRICS = SUPPORTED_METRICS

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._context = None
        self._storage_path = os.environ.get("IG_STORAGE_PATH", "/data/ig_storage.json")
        self._proxy_index = 0

    @staticmethod
    def _proxies() -> list:
        # IG_PROXY, IG_PROXY_2, ... — multiple residential proxies rotate
        # on every browser (re)launch: redundancy + load spreading.
        # Format: http://user:pass@host:port (scheme + auth required).
        return [v for k, v in sorted(os.environ.items()) if k.startswith("IG_PROXY") and v]

    async def _check_proxy(self, proxy: str) -> bool:
        """Quick connectivity check: can we reach instagram.com via this proxy?"""
        def _check():
            try:
                r = cr.head("https://www.instagram.com/", proxy=proxy, timeout=15)
                ok = r.status_code < 500
                print(f"[instagram] proxy check: HTTP {r.status_code} -> {'OK' if ok else 'FAIL'}", flush=True)
                return ok
            except Exception as e:
                print(f"[instagram] proxy check failed: {type(e).__name__}: {e}", flush=True)
                return False
        return await asyncio.to_thread(_check)

    async def _launch(self) -> None:
        if self._pw is None:
            self._pw = await async_playwright().start()
        launch: dict = {"headless": True}
        proxies = self._proxies()
        if proxies:
            proxy = proxies[self._proxy_index % len(proxies)]
            self._proxy_index += 1
            print(f"[instagram] using proxy #{self._proxy_index} of {len(proxies)}", flush=True)
            if not await self._check_proxy(proxy):
                print(f"[instagram] proxy #{self._proxy_index} unreachable, launching without proxy", flush=True)
            else:
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
        # Strategy 0: direct API with session cookies, no browser at all.
        # No automation fingerprints — just an HTTP client. Falls through
        # when unavailable.
        direct = await asyncio.to_thread(_direct_fetch, handle)
        if direct:
            print(f"[instagram] @{handle}: direct API hit ({direct})", flush=True)
            return direct
        # Strategy 1 (paid): Apify when APIFY_TOKEN is set — skip the flaky
        # browser entirely. Otherwise fall through to the browser path.
        if os.environ.get("APIFY_TOKEN"):
            apify = await asyncio.to_thread(_apify_fetch, handle)
            if apify:
                return apify
            raise TransientError("instagram apify fallback failed")
        await self._ensure_alive()
        if not self._context:
            raise TransientError("instagram browser not initialized")
        page = await self._context.new_page()
        # Stealth evasions: Instagram serves empty shells to detected
        # automation even with a valid logged-in session.
        await stealth_async(page)
        # Bandwidth guard (proxy cost): follower counts come from XHR JSON and
        # embedded HTML only — images/video/fonts are pure bytes. Blocking them
        # cuts most of each page load: faster scrapes over slow proxies and far
        # fewer metered residential GB (5 markets, ~720 scrapes/day at 10-min TTL).
        async def _drop_heavy(route):
            if route.request.resource_type in ("image", "media", "font"):
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", _drop_heavy)
        captured: list = []

        def on_response(resp):
            url = resp.url
            if "/graphql" in url or "query_hash" in url or "/api/v1/users/" in url:
                captured.append(resp)

        page.on("response", on_response)
        try:
            resp = await page.goto(
                f"https://www.instagram.com/{handle}/",
                wait_until="domcontentloaded",
                # Free/shared proxies are slow: full page + JS needs room.
                timeout=int(os.environ.get("IG_GOTO_TIMEOUT", "90000")),
            )
            # Human-like pause: let the page settle and XHRs land.
            await page.wait_for_timeout(random.randint(2500, 4500))

            title = await page.title()
            html = await page.content()
            markers = {
                "has_login_form": "loginForm" in html or 'name="username"' in html,
                "has_profile_header": 'property="og:title"' in html,
                "has_shared_data": "edge_followed_by" in html,
                "len": len(html),
            }
            print(f"[instagram] @{handle}: url={page.url} title={title!r} {markers}", flush=True)

            # Strategy 1: intercepted API responses (exact count, username-verified).
            for r in captured:
                try:
                    if "application/json" not in r.headers.get("content-type", ""):
                        continue
                    j = await r.json()
                    user = (j.get("data") or {}).get("user") or (j.get("graphql") or {}).get("user") or j.get("user")
                    if not user:
                        continue
                    if str(user.get("username", "")).lower() != handle:
                        continue  # another user's payload (suggested/viewer)
                    cnt = (user.get("edge_followed_by") or {}).get("count") or user.get("follower_count")
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
