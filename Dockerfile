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
CMD ["sh", "-c", "gunicorn --preload --workers 1 --threads 8 -k gthread --bind 0.0.0.0:${PORT:-8080} app:app"]
