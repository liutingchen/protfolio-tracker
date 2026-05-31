#!/usr/bin/env bash
# Production launch — run behind an HTTPS reverse proxy (see README "部署上线").
# --preload runs db.init_db() once in the master before forking workers.
cd "$(dirname "$0")"
export PORTFOLIO_ENV=production
exec .venv/bin/gunicorn --preload \
  --workers "${WEB_WORKERS:-2}" --threads "${WEB_THREADS:-4}" -k gthread \
  --bind "127.0.0.1:${PORT:-5174}" \
  --access-logfile - --error-logfile - \
  app:app
