# wavvy-scraper

Single self-hosted scraper service behind all Wavvy single-name oracle markets.
One codebase, one deploy, one log stream — instead of four heterogeneous
third-party scrapers.

Implements the keeper's sidecar contract (`wavvy-keeper/README.md`,
"Sidecar HTTP contract") for every platform:

```
GET /metric?platform=<platform>&handle=<handle>&metric=<metric>
Authorization: Bearer <SIDECAR_TOKEN>

→ {"value": 12345, "observedAt": 1760000000, "status": "OK", "version": "1.0.0"}
→ {"status": "NOT_FOUND"}   # entity genuinely missing
→ HTTP 503                  # transient: keeper posts nothing, metric ages
```

| platform  | metric(s)                       | how                                     | needs                       |
|-----------|---------------------------------|-----------------------------------------|-----------------------------|
| `tiktok`  | `followers`                     | profile HTML + `"followerCount"` regex  | nothing                     |
| `x`       | `followers`                     | twscrape (pip dependency)               | `X_COOKIES` (see below)     |
| `instagram`| `followers`                    | Playwright, 3-strategy port of Instakand| nothing (proxy optional)    |
| `spotify` | `monthly_listeners`, `followers`| anonymous token + pathfinder GraphQL   | nothing                     |

YouTube is **not** here — the keeper calls the YouTube Data API directly.

## Deploy (Render)

1. New Web Service → Docker → point at this directory.
2. Environment:
   - `SIDECAR_TOKEN` — generate with `openssl rand -hex 32`. **Same value**
     goes into the keeper's `.env`. The service **refuses to start** without
     it (`ALLOW_NO_AUTH=1` bypasses only for local dev).
   - `X_COOKIES` — from a logged-in x.com session: `unjar x.com -f header`
     (or DevTools → Application → Cookies, copy `auth_token` + `ct0` as a
     cookie header string). Read-only usage. Extra accounts for pool rotation:
     `X_COOKIES_2`, `X_COOKIES_3`, ...
   - `X_DB_PATH` — twscrape pool file (default `/data/accounts.db`).
   - `IG_PROXY` — optional `http://user:pass@host:port` residential proxy for
     Instagram. Biggest lever if Instagram starts blocking the datacenter IP.
   - `IG_STORAGE_PATH` — default `/data/ig_storage.json`.
   - `CACHE_TTL` / `CACHE_TTL_<PLATFORM>` — cache seconds, e.g.
     `CACHE_TTL_INSTAGRAM=900`.
3. Add a disk/volume mounted at `/data` so the Instagram browser session and
   twscrape's `accounts.db` survive redeploys.
4. In the keeper's `.env`: set `X_SIDECAR_URL`, `TIKTOK_SIDECAR_URL`,
   `INSTAGRAM_SIDECAR_URL`, `SPOTIFY_SIDECAR_URL` to this service's URL
   (one service serves all four platforms) and the same `SIDECAR_TOKEN`.

## Verify

```bash
curl "https://<service>/metric?platform=tiktok&handle=khaby.lame&metric=followers" \
  -H "Authorization: Bearer <SIDECAR_TOKEN>"
curl "https://<service>/metric?platform=spotify&handle=6qqNVTkY8uBg9cP3Jd7DAH&metric=monthly_listeners" \
  -H "Authorization: Bearer <SIDECAR_TOKEN>"
curl "https://<service>/health"
```

## Anti rate-limit design

- TTL cache per (platform, handle, metric): 10 min default, 15 min Instagram.
  At keeper volume (5 handles/platform/10 min) each platform is hit a handful
  of times per hour.
- Per-platform locks: requests to one platform run serially, never in bursts.
- Instagram: one persistent browser+context for the service lifetime (fresh
  launches are the #1 bot signal), human-like 2–4.5s delays, persisted
  cookies, realistic fingerprint, optional proxy.
- X: twscrape's pool rotates accounts on rate limits; add 2–3 accounts.
- Spotify: anonymous token cached ~1h, refreshed on 401.

## Failure contract (important)

- Scraper error / block / rate-limit → HTTP 503 → keeper posts **nothing**,
  metric ages, recovers next cycle.
- Only a genuinely missing entity → `{"status": "NOT_FOUND"}`.
- The service never returns 0 for a missing metric. Mapping a transient
  failure to NOT_FOUND would suspend the metric onchain — the code is
  written to make that mistake impossible by construction.

## Maintenance notes

- `requirements.txt` pins **exact** versions, no `^`. A floating Playwright
  minor once broke a deploy (1.48.0 image vs 1.57.0 package). When bumping
  `playwright`, the Dockerfile's `playwright install chromium` follows the
  pin automatically — keep them in lockstep. The base image is pinned to
  `-bookworm` for the same reason (the floating `-slim` tag moved to
  Debian 13, which Playwright 1.48.0 doesn't support).
- Spotify's `queryArtistOverview` persisted-query hash can rotate when Spotify
  ships a new web player. If Spotify queries start failing, refresh
  `_OVERVIEW_HASH` in `platforms/spotify.py` from the player's JS bundle.
- A platform that fails to initialize (no `X_COOKIES`, no browser) answers
  503 for its own routes but never takes down the other three. `/health`
  reports per-platform availability.
- The circuit breaker opens after 3 consecutive upstream failures per
  platform (120s cooldown) so a blocked platform isn't hammered every poll.
- Spotify's `queryArtistOverview` persisted-query hash can rotate when Spotify
  ships a new web player. If Spotify queries start failing, refresh
  `_OVERVIEW_HASH` in `platforms/spotify.py` from the player's JS bundle.
