# Pin Python 3.12 so pandas/numpy wheels install cleanly.
FROM python:3.12-slim

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Production: HTTPS-only cookies + trust the platform's reverse proxy.
ENV PORTFOLIO_ENV=production
# Persist DB + secret on a mounted volume (set DATA_DIR=/data on Railway).
ENV DATA_DIR=/data

EXPOSE 8080

# Railway/most platforms inject $PORT; default to 8080 locally.
# --timeout 120: price refresh can take a while; don't let gunicorn kill it at
# the default 30s (which left the UI stuck on "刷新中…"). 2 workers so one slow
# request can't make the whole app unresponsive.
CMD ["sh", "-c", "gunicorn --preload --workers 2 --threads 8 --timeout 120 -k gthread --bind 0.0.0.0:${PORT:-8080} app:app"]
