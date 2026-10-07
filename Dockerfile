# Pinned to bookworm: the -slim floating tag moved to Debian 13, which the
# pinned Playwright 1.48.0 does not support (it knows debian11/12 only).
# Keep the base image and the playwright pin in lockstep on every bump.
FROM python:3.12-slim-bookworm

# Playwright Chromium system dependencies (Debian bookworm).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Downloads the Chromium build matching the pinned playwright version.
RUN python -m playwright install chromium

COPY app.py ./
COPY platforms ./platforms

# /data holds the Instagram persistent browser session (ig_storage.json) and
# twscrape's accounts.db. Mount a volume here so sessions survive redeploys.
VOLUME ["/data"]

# exec form via sh: SIGTERM reaches uvicorn (no intermediate shell stays
# parent), so lifespan shutdown — including the Instagram session save — runs.
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
