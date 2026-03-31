#!/usr/bin/env python3
"""
Focused test script for dropdown index selection issue.
Tests the shadow DOM traversal fix for LWC dropdown options.
"""

import asyncio
from playwright.async_api import async_playwright

async def test_dropdown():
    username = "zwarshavsky+ritehitesdo@salesforce.com"
    password = "salesforce1"
    instance_url = "https://jamespark-250401-251-demo.my.salesforce.com"
    index_name = "Test_20260324_V4"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=500)
        context = await browser.new_context()
        page = await context.new_page()

        # Login
        print(f"🔐 Logging in to {instance_url}...")
        await page.goto(f"{instance_url}/")
        await page.fill("#username", username)
        await page.fill("#password", password)
        await page.click("#Login")
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(3)

        # Navigate to Einstein Studio Retrievers
        print("🔍 Navigating to Retrievers...")
        await page.goto(f"{instance_url}/lightning/n/sfdc_ai__AiEvaluationWorkspace", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        await page.get_by_text("Retrievers", exact=True).click()
        await asyncio.sleep(1)

        # Click New Retriever
        print("➕ Creating new retriever...")
        await page.get_by_role("button", name="New Retriever").click()
        await asyncio.sleep(2)

        # Wait for popup and select Data Cloud
        popup = page
        print("📦 Selecting Data Cloud...")
        await popup.get_by_text("Data Cloud", exact=True).click()
        await asyncio.sleep(0.3)
        await popup.get_by_role("button", name="Next").click()
        await asyncio.sleep(0.5)

        # Wait for DMO combobox and select RagFileUDMO
        print("📋 Selecting RagFileUDMO...")
        dmo_combobox = popup.get_by_role("combobox", name="Select a data model object")
        for _attempt in range(60):
            if await dmo_combobox.is_enabled():
                break
            await asyncio.sleep(1)
        await dmo_combobox.click()
        rag_option = popup.get_by_text("RagFileUDMO", exact=True).first
        await rag_option.wait_for(state="visible", timeout=30000)
        await rag_option.click()
        await asyncio.sleep(0.5)

        # Wait for dropdown to be ready
        print(f"⏳ Waiting for index dropdown...")
        await asyncio.sleep(3)

        # Open dropdown and select index
        print(f"🔍 Searching for index '{index_name}' in dropdown...")
        search_combobox = popup.get_by_role("combobox", name="Data model object's search")

        # Open the dropdown
        await search_combobox.click()
        await asyncio.sleep(2)

        # Wait for options to exist in DOM
        await popup.locator('[role="option"]').first.wait_for(state="attached", timeout=10000)
        await asyncio.sleep(1)

        option_count = await popup.locator('[role="option"]').count()
        print(f"   ↳ Dropdown has {option_count} options")

        # TEST 1: JavaScript with shadow DOM traversal
        print("\n🧪 TEST 1: JavaScript with shadow DOM traversal")
        found = await popup.evaluate("""
            (indexName) => {
                // Function to get all text from element including shadow DOM
                function getAllText(element) {
                    let text = '';
                    // Get direct text nodes
                    for (const node of element.childNodes) {
                        if (node.nodeType === Node.TEXT_NODE) {
                            text += node.textContent;
                        }
                    }
                    // Traverse shadow DOM
                    if (element.shadowRoot) {
                        text += getAllText(element.shadowRoot);
                    }
                    // Traverse children
                    for (const child of element.children) {
                        text += getAllText(child);
                    }
                    return text;
                }

                const options = document.querySelectorAll('[role="option"]');
                console.log(`Found ${options.length} options`);

                // Debug: show first 10 option texts
                for (let i = 0; i < Math.min(10, options.length); i++) {
                    const text = getAllText(options[i]);
                    console.log(`Option ${i}: "${text.trim()}"`);
                }

                // Find and click the target
                for (let i = 0; i < options.length; i++) {
                    const text = getAllText(options[i]);
                    if (text && text.includes(indexName)) {
                        console.log(`✅ Found index at option ${i}: ${text.trim()}`);
                        options[i].click();
                        return true;
                    }
                }
                return false;
            }
        """, index_name)

        if found:
            print(f"✅ SUCCESS: Found and clicked '{index_name}'")
        else:
            print(f"❌ FAILED: Index '{index_name}' not found")

            # TEST 2: Try innerText property
            print("\n🧪 TEST 2: Trying innerText property")
            options = await popup.locator('[role="option"]').all()
            for i in range(min(10, len(options))):
                try:
                    text = await options[i].evaluate("el => el.innerText")
                    print(f"   Option {i} innerText: '{text}'")
                except Exception as e:
                    print(f"   Option {i} error: {e}")

        print("\n⏸️  Pausing for manual inspection (30s)...")
        await asyncio.sleep(30)

        await browser.close()

if __name__ == "__main__":
    asyncio.run(test_dropdown())
