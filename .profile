#!/bin/bash

# Skip Playwright install when explicitly requested (e.g., psql/query runs)
if [ "$SKIP_PLAYWRIGHT_INSTALL" = "1" ]; then
    echo "Skipping Playwright browsers install (SKIP_PLAYWRIGHT_INSTALL=1)"
    exit 0
fi

# If browsers are already cached, skip reinstall
if [ -d "/app/.cache/ms-playwright/chromium-1200" ] || [ -d "$HOME/.cache/ms-playwright/chromium-1200" ]; then
    echo "Playwright browsers already present; skipping install"
    exit 0
fi

echo "Installing Playwright browsers..."
python -m playwright install chromium


