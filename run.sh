#!/usr/bin/env bash
# Start the Portfolio Tracker. Open http://127.0.0.1:5174 in your browser.
cd "$(dirname "$0")"
exec .venv/bin/python app.py
