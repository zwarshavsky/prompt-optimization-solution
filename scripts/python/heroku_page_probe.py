#!/usr/bin/env python3
import asyncio
import base64
import json
import os

from playwright.async_api import async_playwright


async def dump_page(tag, page):
    title = await page.title()
    data = await page.evaluate(
        """() => {
            const txt = (el) => (el && (el.innerText || el.textContent || '').trim()) || '';
            const controls = Array.from(document.querySelectorAll('button,[role="button"],a,label'))
                .map(txt).filter(Boolean).slice(0, 120);
            const body = (document.body && document.body.innerText) ? document.body.innerText.toLowerCase() : '';
            return {
                controls,
                hasHybrid: body.includes('hybrid search'),
                hasSearchboxPhrase: body.includes('search data model objects')
            };
        }"""
    )
    print(f"URL_{tag}: {page.url}")
    print(f"TITLE_{tag}: {title}")
    print(f"FLAGS_{tag}: {json.dumps({'hasHybrid': data['hasHybrid'], 'hasSearchboxPhrase': data['hasSearchboxPhrase']})}")
    print(f"CTRLS_{tag}: {json.dumps(data['controls'][:40])}")


async def main():
    auth_b64 = os.getenv("SF_AUTH_STATE_B64", "").strip()
    state = None
    if auth_b64:
        try:
            state = json.loads(base64.b64decode(auth_b64).decode("utf-8"))
        except Exception as e:
            print(f"AUTH_DECODE_ERR: {e}")

    base = "https://jamespark-250401-251-demo.my.salesforce.com"
    setup_home = f"{base}/lightning/setup/DataSemanticSearch/home"
    obj_list = f"{base}/lightning/o/SearchIndex/list"
    obj_new = f"{base}/lightning/o/SearchIndex/new"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(storage_state=state) if state else await browser.new_context()
        page = await context.new_page()

        await page.goto(setup_home, wait_until="domcontentloaded", timeout=90000)
        await asyncio.sleep(2)
        await dump_page("SETUP_HOME", page)

        await page.goto(obj_list, wait_until="domcontentloaded", timeout=90000)
        await asyncio.sleep(2)
        await dump_page("OBJ_LIST", page)

        await page.goto(obj_new, wait_until="domcontentloaded", timeout=90000)
        await asyncio.sleep(2)
        await dump_page("OBJ_NEW", page)

        shot = "/tmp/heroku_probe_obj_new.png"
        await page.screenshot(path=shot, full_page=True)
        print(f"SCREENSHOT: {shot}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
