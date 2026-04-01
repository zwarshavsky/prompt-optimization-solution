#!/usr/bin/env python3
"""
Simple focused test: Can we create a Search Index via browser automation on Heroku?
No Gemini, no prompt invocations, no full workflow - just browser automation.
"""
import asyncio
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright_scripts import _create_search_index_ui

async def main():
    print("=" * 80)
    print("BROWSER AUTOMATION TEST: Search Index Creation")
    print("=" * 80)
    print()

    # Credentials from env
    username = os.getenv("SF_USERNAME", "zwarshavsky+ritehitesdo@salesforce.com")
    password = os.getenv("SF_PASSWORD", "salesforce1")
    instance_url = "https://jamespark-250401-251-demo.my.salesforce.com"

    # Minimal test parameters
    index_name = "BrowserTest_Simple"
    parser_prompt = "Test prompt for browser automation validation."
    state_dir = "/tmp"
    run_id = "browser_test_001"
    headless = True  # Always headless on Heroku

    def should_abort():
        return False

    print(f"🔑 Username: {username}")
    print(f"🏢 Instance: {instance_url}")
    print(f"📝 Index Name: {index_name}")
    print(f"🎭 Headless: {headless}")
    print()

    try:
        print("🚀 Starting browser automation...")
        index_id, full_index_name = await _create_search_index_ui(
            username=username,
            password=password,
            instance_url=instance_url,
            index_name=index_name,
            parser_prompt=parser_prompt,
            state_dir=state_dir,
            run_id=run_id,
            headless=headless,
            should_abort=should_abort,
            skip_api_lookup=False
        )

        if index_id and full_index_name:
            print()
            print("=" * 80)
            print("✅ SUCCESS: Search Index Created")
            print("=" * 80)
            print(f"Index ID: {index_id}")
            print(f"Full Name: {full_index_name}")
            print()
            return 0
        else:
            print()
            print("=" * 80)
            print("❌ FAILED: No index created")
            print("=" * 80)
            print()
            return 1

    except Exception as e:
        print()
        print("=" * 80)
        print(f"❌ ERROR: {e}")
        print("=" * 80)
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
