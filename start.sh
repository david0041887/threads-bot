#!/bin/bash
set -e

BROWSER_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/app/.playwright-browsers}"

if ! find "$BROWSER_PATH" -name "chrome-headless-shell" 2>/dev/null | grep -q .; then
    echo "[startup] Chromium not found at $BROWSER_PATH, installing..."
    playwright install chromium
    echo "[startup] Chromium installed."
else
    echo "[startup] Chromium already present at $BROWSER_PATH, skipping install."
fi

exec uvicorn main:app --host 0.0.0.0 --port "$PORT"
