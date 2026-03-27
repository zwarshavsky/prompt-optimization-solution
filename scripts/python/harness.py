#!/usr/bin/env python3
"""
Simple test harness for Playwright automation cycles.
Tests Search Index creation with current viewport/timing settings.
"""

import argparse
import asyncio
import sys
import os
from pathlib import Path

# Add current directory to path
script_dir = Path(__file__).resolve().parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from playwright_scripts import _create_search_index_ui
from salesforce_api import get_next_index_name


async def run_test_cycle(instance_url: str, username: str, password: str, headless: bool):
    """Run a single test cycle of Search Index creation"""

    # Get next index name
    index_name = get_next_index_name(instance_url, username, password, prefix="Test_Viewport")
    print(f"\n🧪 Testing Search Index creation: {index_name}")
    print(f"   Instance: {instance_url}")
    print(f"   Headless: {headless}")

    # Create abort function
    def should_abort():
        return False

    # Run the automation
    try:
        index_id, full_name = await _create_search_index_ui(
            username=username,
            password=password,
            instance_url=instance_url,
            index_name=index_name,
            parser_prompt="Test viewport and networkidle timing improvements.",
            run_id=f"test_{index_name}",
            headless=headless,
            should_abort=should_abort,
            skip_api_lookup=True
        )

        if index_id and full_name:
            print(f"\n✅ SUCCESS: Index created")
            print(f"   ID: {index_id}")
            print(f"   Name: {full_name}")
            return True
        else:
            print(f"\n❌ FAILED: Index creation returned None")
            return False

    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main_async(args):
    """Main async entry point"""

    # Get credentials from environment
    username = args.sf_username or os.environ.get('SF_USERNAME')
    password = os.environ.get('SF_PASSWORD')

    if not username or not password:
        print("❌ ERROR: SF_USERNAME and SF_PASSWORD must be set")
        return 1

    print(f"\n{'='*70}")
    print(f"Playwright Test Harness - Viewport & Timing Test")
    print(f"{'='*70}")
    print(f"Cycles: {args.cycles}")
    print(f"Headless: {args.headless}")
    print(f"Timeout: {args.timeout}s")

    success_count = 0
    failure_count = 0

    for cycle in range(1, args.cycles + 1):
        print(f"\n{'─'*70}")
        print(f"CYCLE {cycle}/{args.cycles}")
        print(f"{'─'*70}")

        success = await run_test_cycle(
            instance_url=args.instance_url,
            username=username,
            password=password,
            headless=args.headless
        )

        if success:
            success_count += 1
        else:
            failure_count += 1

    print(f"\n{'='*70}")
    print(f"TEST COMPLETE")
    print(f"{'='*70}")
    print(f"Success: {success_count}/{args.cycles}")
    print(f"Failure: {failure_count}/{args.cycles}")

    return 0 if failure_count == 0 else 1


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='Playwright test harness')
    parser.add_argument('--cycles', type=int, default=1, help='Number of test cycles')
    parser.add_argument('--headless', action='store_true', help='Run in headless mode')
    parser.add_argument('--instance-url', required=True, help='Salesforce instance URL')
    parser.add_argument('--sf-username', help='Salesforce username (or use SF_USERNAME env var)')
    parser.add_argument('--timeout', type=int, default=600, help='Timeout in seconds')

    args = parser.parse_args()

    return asyncio.run(main_async(args))


if __name__ == '__main__':
    sys.exit(main())
