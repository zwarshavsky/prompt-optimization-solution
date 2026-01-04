#!/bin/bash
# Install Playwright browsers on dyno startup (ephemeral filesystem)
if [ ! -d "/app/.cache/ms-playwright/chromium-1200" ]; then
    echo "Installing Playwright browsers..."
    python -m playwright install chromium
fi


