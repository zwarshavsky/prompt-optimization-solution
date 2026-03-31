#!/usr/bin/env python3
"""
Activate API-created Search Index via UI.

API POST creates the index metadata but doesn't trigger the build.
This script opens the index in the UI and clicks "Build" to start processing.

Usage:
    python3 activate_index_ui.py --index-id 18lKc000000oN3K \
        --username user@sf.com --password pass --instance-url https://...
"""

import argparse
import asyncio
from playwright.async_api import async_playwright


async def activate_index(index_id: str, username: str, password: str, instance_url: str, headless: bool = True):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()

        # Login
        print(f"🔐 Logging in to {instance_url}...")
        await page.goto(f"{instance_url}/", wait_until='domcontentloaded', timeout=60000)
        await page.fill('input[id="username"]', username)
        await page.fill('input[id="password"]', password)
        await page.click('input[id="Login"]')
        await asyncio.sleep(5)

        # Navigate to index builder
        builder_url = f"{instance_url}/runtime_cdp/searchIndexBuilder.app?mode=edit&recordId={index_id}"
        print(f"📂 Opening index builder: {builder_url}")
        await page.goto(builder_url, wait_until='domcontentloaded', timeout=60000)
        await asyncio.sleep(10)

        # Look for Build/Save button
        print("🔍 Looking for Build/Save button...")
        build_clicked = await page.evaluate("""
            () => {
                const buttons = document.querySelectorAll('button');
                for (let btn of buttons) {
                    const text = (btn.textContent || '').trim();
                    if ((text === 'Build' || text === 'Save' || text === 'Save & Build') && !btn.disabled) {
                        btn.click();
                        return {success: true, text: text};
                    }
                }
                return {success: false};
            }
        """)

        if build_clicked.get('success'):
            print(f"✅ Clicked '{build_clicked.get('text')}' button")
            await asyncio.sleep(5)
        else:
            print("❌ Build button not found or disabled")

        await browser.close()


def main():
    parser = argparse.ArgumentParser(description="Activate API-created Search Index via UI")
    parser.add_argument("--index-id", required=True, help="Index ID to activate")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--instance-url", required=True)
    parser.add_argument("--headless", action="store_true", default=True)
    args = parser.parse_args()

    asyncio.run(activate_index(
        index_id=args.index_id,
        username=args.username,
        password=args.password,
        instance_url=args.instance_url,
        headless=args.headless
    ))
    print("✅ Activation complete")


if __name__ == "__main__":
    main()
