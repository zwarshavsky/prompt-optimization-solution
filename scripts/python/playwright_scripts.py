#!/usr/bin/env python3
"""
Salesforce Playwright Scripts

This module contains Playwright automation scripts for Salesforce UI interactions.
Currently includes:
- update_search_index_prompt: Updates Search Index LLM Parser Prompt via UI
  (targets the lightning-textarea component with name="prompt")
"""

import asyncio
import base64
import json
import os
import platform
import re
import sys
from pathlib import Path
from datetime import datetime
import subprocess
import urllib.request
import yaml

from playwright.async_api import async_playwright

from worker_utils import check_run_aborted, update_job_progress, consume_pending_mfa_code, reflag_mfa_code_pending


def _is_authenticated_url(url: str) -> bool:
    low = (url or "").lower()
    return ("lightning" in low) or (
        "salesforce.com" in low and "login" not in low and "/_ui/identity/verification/" not in low
    )


def _is_mfa_or_verification_url(url: str) -> bool:
    low = (url or "").lower()
    return any(token in low for token in ["/_ui/identity/verification/", "mfa", "verify", "challenge"])


async def _try_submit_mfa_code(page, code: str) -> bool:
    print(f"   [MFA] _try_submit_mfa_code called with code len={len(code)}", flush=True)
    print(f"   [MFA] Current URL: {page.url}", flush=True)
    page_text = await page.evaluate("() => document.body ? document.body.innerText.substring(0, 400) : 'no-body'")
    print(f"   [MFA] Page text: {page_text[:300]}", flush=True)

    code_locators = [
        "input[type='tel']",
        "input[type='number']",
        "input[type='text']",
        "input[name*='code' i]",
        "input[id*='code' i]",
        "input[name*='verification' i]",
        "input[id*='verification' i]",
        "input[autocomplete='one-time-code']",
    ]
    entered = False
    for selector in code_locators:
        field = page.locator(selector).first
        try:
            if await field.is_visible(timeout=600):
                print(f"   [MFA] Found input via '{selector}', filling code...", flush=True)
                await field.fill(code)
                entered = True
                break
        except Exception:
            continue
    if not entered:
        print("   [MFA] ❌ Could not find any input field for the verification code!", flush=True)
        all_inputs = await page.evaluate("() => Array.from(document.querySelectorAll('input')).map(i => ({type:i.type, name:i.name, id:i.id, placeholder:i.placeholder})).slice(0,10)")
        print(f"   [MFA] Available inputs on page: {all_inputs}", flush=True)
        return False

    try:
        checkbox = page.locator("input[type='checkbox']").first
        if await checkbox.is_visible(timeout=600):
            if not await checkbox.is_checked():
                await checkbox.click()
                print("   [MFA] Checked 'Don't ask again' checkbox", flush=True)
    except Exception:
        pass

    submit_locators = [
        "button:has-text('Verify')",
        "button:has-text('Continue')",
        "button:has-text('Submit')",
        "button:has-text('Next')",
        "input[type='submit']",
        "button[type='submit']",
    ]
    for selector in submit_locators:
        btn = page.locator(selector).first
        try:
            if await btn.is_visible(timeout=600):
                print(f"   [MFA] Clicking submit via '{selector}'", flush=True)
                await btn.click()
                return True
        except Exception:
            continue
    try:
        print("   [MFA] No submit button found, pressing Enter", flush=True)
        await page.keyboard.press("Enter")
        return True
    except Exception:
        print("   [MFA] ❌ Enter keypress failed too", flush=True)
        return False


async def _wait_for_mfa_code_and_resume(page, run_id: str, should_abort, timeout_seconds: int = 43200) -> bool:
    if run_id:
        update_job_progress(
            run_id,
            {
                "status": "awaiting_mfa",
                "step": 1,
                "message": "MFA verification required. Enter code from Jobs page to continue.",
                "mfa_required": True,
            },
            output_line="🔐 MFA required: waiting for verification code submission from Jobs page.",
        )

    started = asyncio.get_event_loop().time()
    last_heartbeat = started
    attempt_count = 0
    max_retries_same_code = 2
    current_code_retries = 0
    print(f"   [MFA-WAIT] Entering MFA wait loop (timeout={timeout_seconds}s, run_id={run_id})", flush=True)
    while True:
        if should_abort():
            print("   [MFA-WAIT] Abort signal received", flush=True)
            return False
        now = asyncio.get_event_loop().time()
        elapsed = int(now - started)
        if (now - started) > timeout_seconds:
            print(f"   [MFA-WAIT] ❌ Timeout after {elapsed}s", flush=True)
            if run_id:
                update_job_progress(
                    run_id,
                    {"status": "error", "message": "MFA timeout waiting for verification code."},
                    output_line="❌ MFA timeout: no verification code submitted in time.",
                )
            return False

        code = consume_pending_mfa_code(run_id) if run_id else None
        if code:
            attempt_count += 1
            current_code_retries += 1
            print(f"   [MFA-WAIT] 🔑 Code consumed (attempt #{attempt_count}, retry #{current_code_retries}, len={len(code)}, elapsed={elapsed}s)", flush=True)
            submitted = await _try_submit_mfa_code(page, code)
            print(f"   [MFA-WAIT] _try_submit_mfa_code returned: {submitted}", flush=True)
            if run_id:
                update_job_progress(
                    run_id,
                    {
                        "status": "awaiting_mfa",
                        "step": 1,
                        "message": f"MFA code submitted (attempt #{attempt_count}), verifying login...",
                        "mfa_required": True,
                    },
                    output_line=f"🔐 MFA code submitted (attempt #{attempt_count}, len={len(code)}). Verifying...",
                )
            if submitted:
                # Wait a few seconds for form submission to process
                print(f"   [MFA-WAIT] Waiting 8s for form submission to process...", flush=True)
                await asyncio.sleep(8)

                # Check if the page already navigated away
                if _is_authenticated_url(page.url):
                    print(f"   [MFA-WAIT] ✅ Authenticated after form submit! URL: {page.url}", flush=True)
                    if run_id:
                        update_job_progress(run_id, {"status": "step_start", "step": 1, "message": "MFA verification complete.", "mfa_required": False}, output_line="✅ MFA verification successful.")
                    return True

                # The verification page often stays on the same URL after accepting
                # the code (page text changes from full form to just "Salesforce").
                # Force-navigate to Lightning to see if the session is now valid.
                instance_base = page.url.split("/_ui/")[0] if "/_ui/" in page.url else page.url.split(".com")[0] + ".com"
                lightning_url = f"{instance_base}/lightning/setup/SetupOneHome/home"
                print(f"   [MFA-WAIT] Force-navigating to Lightning: {lightning_url}", flush=True)
                try:
                    await page.goto(lightning_url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(3)
                except Exception as nav_err:
                    print(f"   [MFA-WAIT] Navigation error: {nav_err}", flush=True)

                cur_url = page.url
                print(f"   [MFA-WAIT] After force-nav, URL: {cur_url}", flush=True)

                if _is_authenticated_url(cur_url):
                    print(f"   [MFA-WAIT] ✅ Authenticated via force-nav! URL: {cur_url}", flush=True)
                    if run_id:
                        update_job_progress(run_id, {"status": "step_start", "step": 1, "message": "MFA verification complete.", "mfa_required": False}, output_line="✅ MFA verification successful.")
                    return True

                # If we got redirected back to MFA/login, the code was wrong or expired
                page_text = await page.evaluate("() => document.body ? document.body.innerText.substring(0, 500) : 'no-body'")
                print(f"   [MFA-WAIT] ⚠️ Still not authenticated. URL: {cur_url}", flush=True)
                print(f"   [MFA-WAIT] Page text: {page_text[:400]}", flush=True)

                # Navigate back to the original verification page for retry
                if _is_mfa_or_verification_url(cur_url):
                    print(f"   [MFA-WAIT] Already on verification page for retry.", flush=True)
                else:
                    # Go back to the login flow
                    login_url = f"{instance_base}/secur/frontdoor.jsp?allp=1"
                    print(f"   [MFA-WAIT] Navigating back to login: {login_url}", flush=True)
                    try:
                        await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
                        await asyncio.sleep(3)
                    except Exception:
                        pass

                if current_code_retries < max_retries_same_code:
                    print(f"   [MFA-WAIT] ⚠️ Will retry same code ({current_code_retries}/{max_retries_same_code}).", flush=True)
                    if run_id:
                        reflag_mfa_code_pending(run_id)
                else:
                    print(f"   [MFA-WAIT] ❌ Max retries ({max_retries_same_code}) exhausted. Requesting new code.", flush=True)
                    current_code_retries = 0
                    if run_id:
                        update_job_progress(run_id, {"status": "awaiting_mfa", "step": 1, "message": f"MFA code failed after {max_retries_same_code} attempts. Please submit a new code.", "mfa_required": True}, output_line=f"⚠️ MFA code failed after {max_retries_same_code} retries. Please submit a new code.")
            else:
                print(f"   [MFA-WAIT] ⚠️ _try_submit_mfa_code returned False (code entry or submit failed)", flush=True)
                if run_id:
                    update_job_progress(
                        run_id,
                        {
                            "status": "awaiting_mfa",
                            "step": 1,
                            "message": f"Could not enter MFA code on page (attempt #{attempt_count}). Submit another code.",
                            "mfa_required": True,
                        },
                        output_line=f"⚠️ MFA code entry failed (attempt #{attempt_count}). Please submit a new code.",
                    )

        if run_id and (now - last_heartbeat) >= 10:
            update_job_progress(
                run_id,
                {
                    "status": "awaiting_mfa",
                    "step": 1,
                    "message": f"Still waiting for MFA code submission. ({elapsed}s elapsed, {attempt_count} attempts so far)",
                    "mfa_required": True,
                },
            )
            last_heartbeat = now
        await asyncio.sleep(2)

async def update_search_index_prompt(
    username: str,
    password: str,
    instance_url: str,
    search_index_id: str,
    new_prompt: str,
    run_id: str = None,
    capture_network: bool = False,
    take_screenshots: bool = False,
    headless: bool = False,
    slow_mo: int = 0,
    skip_wait: bool = False
):
    """
    Update the LLM parser prompt for a Search Index.

    .. deprecated::
        Due to platform stability issues with the Edit Index UI flow, this flow is being
        replaced. Future cycles will use: create Search Index → create Retriever →
        Activate (Playwright) + update prompt template (REST API). This function remains
        for Cycle 1 (baseline) when an existing index/retriever is configured in YAML.
    
    Args:
        username: Salesforce username
        password: Salesforce password
        instance_url: Salesforce instance URL
        search_index_id: Search Index record ID (e.g., "18lHu000000CgkCIAS")
        new_prompt: The new prompt text to set
    """
    # Create screenshots directory (only if screenshots are enabled)
    screenshots_dir = Path("playwright_screenshots")
    if take_screenshots:
        screenshots_dir.mkdir(parents=True, exist_ok=True)
    
    async def take_screenshot(name):
        """Take a screenshot for debugging (only if enabled)"""
        if not take_screenshots:
            return None
        timestamp = datetime.now().strftime("%H%M%S")
        screenshot_path = screenshots_dir / f"{timestamp}_{name}.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"   📸 Screenshot saved: {screenshot_path.name}")
        return screenshot_path
    
    def should_abort():
        """Check DB status for kill; return True to abort if not running."""
        if check_run_aborted(run_id):
            print(f"   ❌ Kill detected for run {run_id}, aborting Playwright flow.")
            return True
        return False
    
    async with async_playwright() as p:
        # Launch browser with visible window - normal size
        launch_args = {}
        if headless:
            # Required args for headless Chromium on Heroku/Linux containers
            launch_args['args'] = [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-software-rasterizer',
                '--disable-extensions'
            ]
        browser = await p.chromium.launch(
            headless=headless,
            slow_mo=slow_mo,  # Delay between actions in milliseconds (0 = no delay)
            **launch_args
        )
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 720}  # Normal resolution
        )
        page = await context.new_page()
        
        # Network monitoring - only if flag is enabled
        semantic_search_requests = []
        all_requests = []  # Capture ALL PUT/POST requests for analysis
        
        if capture_network:
            
            async def handle_request(request):
                """Capture all network requests"""
                # Capture PUT/POST requests that might be the update
                if request.method in ['PUT', 'POST']:
                    req_data = {
                        'method': request.method,
                        'url': request.url,
                        'headers': dict(request.headers),
                        'post_data': request.post_data,
                        'timestamp': datetime.now().isoformat()
                    }
                    all_requests.append(req_data)
                    
                    # Specifically log semanticSearch requests
                    if '/v1/semanticSearch/' in request.url or 'semanticSearch' in request.url.lower():
                        semantic_search_requests.append(req_data)
                        print(f"   🔍 CAPTURED semanticSearch REQUEST: {request.method} {request.url}")
                        if request.post_data:
                            print(f"      Payload: {request.post_data[:200]}...")
                    elif 'PUT' in request.method:
                        print(f"   🔍 CAPTURED PUT REQUEST: {request.url[:100]}...")
            
            async def handle_response(response):
                """Capture all network responses"""
                # Match responses to requests
                if response.request.method in ['PUT', 'POST']:
                    # Find matching request
                    matching_req = None
                    for req in all_requests:
                        if req['url'] == response.url and req['method'] == response.request.method:
                            matching_req = req
                            break
                    
                    try:
                        body = await response.body()
                        resp_data = {
                            'url': response.url,
                            'status': response.status,
                            'status_text': response.status_text,
                            'headers': dict(response.headers),
                            'body': body.decode('utf-8', errors='ignore')[:10000],  # First 10KB
                            'timestamp': datetime.now().isoformat()
                        }
                        
                        if matching_req:
                            matching_req['response'] = resp_data
                        
                        # Specifically log semanticSearch responses
                        if '/v1/semanticSearch/' in response.url or 'semanticSearch' in response.url.lower():
                            print(f"   📥 CAPTURED semanticSearch RESPONSE: {response.status} {response.status_text}")
                            print(f"      Body: {resp_data['body'][:200]}...")
                        elif response.request.method == 'PUT':
                            print(f"   📥 CAPTURED PUT RESPONSE: {response.status} {response.url[:100]}...")
                    except Exception as e:
                        print(f"   ⚠️  Error capturing response: {e}")
            
            # Set up network listeners
            page.on('request', handle_request)
            page.on('response', handle_response)
            print("🌐 Browser window opened (should be visible now)")
            print("📡 Network monitoring enabled - capturing all semanticSearch requests")
        else:
            print("🌐 Browser window opened (should be visible now)")
        
        print("🔐 Logging into Salesforce...")
        
        # Try standard Salesforce login URL
        login_urls = [
            "https://login.salesforce.com",
            f"{instance_url.rstrip('/')}/secur/login_portal.jsp",
            f"{instance_url.rstrip('/')}/"
        ]
        
        logged_in = False
        for login_url in login_urls:
            try:
                print(f"   Trying: {login_url}")
                await page.goto(login_url, wait_until='networkidle', timeout=30000)
                await asyncio.sleep(2)
                
                # Check if already logged in
                current_url = page.url
                if 'lightning' in current_url or ('salesforce.com' in current_url and 'login' not in current_url.lower()):
                    print("   ✅ Already logged in!")
                    logged_in = True
                    break
                
                # Try to find and fill login form
                username_field = page.locator("input#username, input[name='username'], input[type='email']").first
                password_field = page.locator("input#password, input[name='password']").first
                
                if await username_field.is_visible(timeout=5000):
                    await username_field.fill(username)
                    await password_field.fill(password)
                    
                    # Click login
                    login_button = page.locator("input#Login, button[name='Login'], input[type='submit']").first
                    await login_button.click()
                    
                    # Wait for redirect
                    await asyncio.sleep(5)
                    
                    # Check for 2FA
                    current_url = page.url
                    if 'mfa' in current_url.lower() or 'verify' in current_url.lower() or 'challenge' in current_url.lower():
                        print("   ⚠️  2FA detected - please complete manually")
                        await page.wait_for_url("**/lightning/**", timeout=300000)
                    
                    # Check if login successful
                    final_url = page.url
                    if 'lightning' in final_url or ('salesforce.com' in final_url and 'login' not in final_url.lower()):
                        print("   ✅ Login successful!")
                        logged_in = True
                        break
            except Exception as e:
                print(f"   ⚠️  Error with {login_url}: {e}")
                continue
        
        if not logged_in:
            print("❌ Could not login automatically - please login manually in the browser")
            print("   Waiting 30 seconds for manual login...")
            await asyncio.sleep(30)
        
        # Navigate to Search Index detail page first
        detail_url = f"{instance_url}/lightning/r/DataSemanticSearch/{search_index_id}/view"
        print(f"📂 Navigating to Search Index detail page...")
        print(f"   URL: {detail_url}")
        try:
            await page.goto(detail_url, wait_until='domcontentloaded', timeout=60000)
            await asyncio.sleep(5)  # Wait for Lightning to load
            await take_screenshot("01_search_index_detail")
        except Exception as e:
            print(f"   ⚠️  Navigation timeout, but continuing... {e}")
            await take_screenshot("01_navigation_timeout")
        
        # First click "Configuration" tab
        print("📑 Clicking 'Configuration' tab...")
        await asyncio.sleep(2)
        
        config_tab_clicked = await page.evaluate("""
            () => {
                // Find Configuration tab
                const tabs = Array.from(document.querySelectorAll('a, button, [role="tab"]'));
                for (let tab of tabs) {
                    const text = (tab.textContent || tab.innerText || '').trim();
                    if (text === 'Configuration') {
                        tab.click();
                        return true;
                    }
                }
                return false;
            }
        """)
        
        if config_tab_clicked:
            print("   ✅ Clicked Configuration tab")
            await asyncio.sleep(2)
            await take_screenshot("02_after_config_tab")
        else:
            print("   ⚠️  Configuration tab not found, trying Playwright...")
            try:
                config_tab = page.locator("text=Configuration, a:has-text('Configuration'), [role='tab']:has-text('Configuration')").first
                if await config_tab.is_visible(timeout=5000):
                    await config_tab.click()
                    await asyncio.sleep(2)
                    await take_screenshot("02_after_config_tab")
                else:
                    print("   ⚠️  Configuration tab not visible")
            except:
                pass
        
        # Now look for Edit button in the Configuration tab content
        print("✏️  Looking for 'Edit' button in Configuration tab...")
        await asyncio.sleep(2)
        
        # Try direct navigation to builder first (most reliable)
        print("   Trying direct navigation to builder...")
        builder_url = f"{instance_url}/runtime_cdp/searchIndexBuilder.app?mode=edit&recordId={search_index_id}"
        try:
            await page.goto(builder_url, wait_until='domcontentloaded', timeout=60000)
            await asyncio.sleep(5)
            await take_screenshot("02_builder_direct_nav")
            print("   ✅ Navigated directly to builder")
        except Exception as e:
            print(f"   ⚠️  Direct navigation failed: {e}")
            # Fallback: try to find Edit button
            edit_clicked = await page.evaluate("""
                () => {
                    // Find buttons in the main content area (not in modals)
                    const mainContent = document.querySelector('[id*="brandBand"], .slds-page-header, main, [role="main"]');
                    const searchArea = mainContent || document.body;
                    const buttons = Array.from(searchArea.querySelectorAll('button'));
                    
                    // Look for Edit button that's NOT in a modal
                    for (let btn of buttons) {
                        const text = (btn.textContent || btn.innerText || '').trim();
                        // Make sure it's not in a modal
                        let parent = btn.parentElement;
                        let inModal = false;
                        while (parent) {
                            if (parent.getAttribute('role') === 'dialog' || 
                                parent.classList.contains('slds-modal') ||
                                parent.classList.contains('modal')) {
                                inModal = true;
                                break;
                            }
                            parent = parent.parentElement;
                        }
                        
                        if (text === 'Edit' && !inModal) {
                            btn.click();
                            return true;
                        }
                    }
                    return false;
                }
            """)
            
            if edit_clicked:
                print("   ✅ Clicked Edit button (not in modal)")
                await asyncio.sleep(5)
                await take_screenshot("02_after_edit_click")
            else:
                print("   ❌ Could not find Edit button")
                await take_screenshot("02_edit_not_found")
        
        # Debug: Check what's on the page
        print("🔍 Debugging: Checking page content...")
        page_title = await page.title()
        page_url = page.url
        print(f"   Title: {page_title}")
        print(f"   URL: {page_url[:120]}")
        
        # Look for Parsing step - AGGRESSIVE approach
        print("📝 Looking for 'Parsing' step...")
        await asyncio.sleep(2)
        
        # Try JavaScript click as primary method
        parsing_clicked = await page.evaluate("""
            () => {
                // Find all elements containing "Parsing"
                const allElements = document.querySelectorAll('*');
                for (let el of allElements) {
                    const text = el.textContent || el.innerText || '';
                    if (text.trim() === 'Parsing' || text.trim().includes('Parsing')) {
                        // Check if it's clickable
                        if (el.onclick || el.getAttribute('role') === 'button' || el.tagName === 'BUTTON' || el.tagName === 'A') {
                            el.click();
                            return true;
                        }
                        // Try parent
                        let parent = el.parentElement;
                        if (parent && (parent.onclick || parent.getAttribute('role') === 'button' || parent.tagName === 'BUTTON' || parent.tagName === 'A')) {
                            parent.click();
                            return true;
                        }
                    }
                }
                return false;
            }
        """)
        
        if parsing_clicked:
            print("   ✅ Clicked Parsing via JavaScript")
            await asyncio.sleep(2)
        else:
            # Fallback to Playwright selectors
            parsing_selectors = [
                "text=Parsing",
                "button:has-text('Parsing')",
                "a:has-text('Parsing')",
                ".slds-nav-vertical__item:has-text('Parsing')",
                "[aria-label*='Parsing']",
                "li:has-text('Parsing')"
            ]
            
            for selector in parsing_selectors:
                try:
                    locator = page.locator(selector).first
                    if await locator.is_visible(timeout=2000):
                        print(f"   ✅ Found Parsing with: {selector}")
                        await locator.click()
                        await asyncio.sleep(2)
                        parsing_clicked = True
                        break
                except:
                    continue
        
        await take_screenshot("03_after_parsing_click")
        
        # Select "LLM-based Parser" if not already selected
        print("🤖 Looking for 'LLM-based Parser' option...")
        await take_screenshot("04_before_parser_selection")
        
        llm_parser_selectors = [
            "text=LLM-based Parser",
            "button:has-text('LLM-based Parser')",
            "[title*='LLM-based']",
            ".slds-card:has-text('LLM-based')"
        ]
        
        llm_parser = None
        for selector in llm_parser_selectors:
            try:
                locator = page.locator(selector).first
                if await locator.is_visible(timeout=2000):
                    print(f"   ✅ Found LLM parser with selector: {selector}")
                    llm_parser = locator
                    break
            except:
                continue
        
        if llm_parser:
            print("   Clicking LLM-based Parser...")
            await llm_parser.click()
            await asyncio.sleep(2)
            await take_screenshot("05_after_parser_selection")
        else:
            print("   ⚠️  LLM-based Parser not found or already selected")
            await take_screenshot("05_parser_not_found")
        
        # Wait for the prompt textarea to be visible
        print("⏳ Waiting for prompt textarea...")
        await asyncio.sleep(3)
        await take_screenshot("06_before_textarea_search")
        
        # Find the textarea - multiple methods
        print("🔍 Finding textarea...")
        
        # Find and fill textarea - Try multiple methods for Lightning components
        print("🔍 Finding and updating textarea...")
        prompt_updated = False
        
        # Method 1: Use Playwright locator with type() to simulate real typing
        try:
            print("   Trying Method 1: Playwright type() (simulates real user input)...")
            textarea_locator = page.locator('lightning-textarea textarea[name="prompt"]').first
            if await textarea_locator.is_visible(timeout=5000):
                # Clear using multiple methods to ensure complete deletion
                await textarea_locator.click()
                await textarea_locator.press('Control+a')  # Select all (Cmd+a on Mac)
                await textarea_locator.press('Backspace')  # Delete selected
                await textarea_locator.press('Delete')  # Delete again to be sure
                await textarea_locator.fill('')  # Explicitly fill with empty string
                await asyncio.sleep(0.5)
                
                # Verify it's actually empty
                current_value = await textarea_locator.input_value()
                if len(current_value) > 0:
                    # Force clear via JavaScript if still not empty
                    await textarea_locator.evaluate("el => { el.value = ''; el.dispatchEvent(new Event('input', { bubbles: true })); }")
                    await asyncio.sleep(0.3)
                
                # Type the new text
                await textarea_locator.type(new_prompt, delay=10)
                await asyncio.sleep(2)
                
                # VERIFY immediately after typing
                typed_value = await textarea_locator.input_value()
                if typed_value.strip() == new_prompt.strip():
                    prompt_updated = True
                    print(f"   ✅ Updated via Playwright type() - verified: {len(typed_value)} chars")
                else:
                    print(f"   ⚠️  Method 1: Value not set correctly")
                    print(f"      Expected: '{new_prompt}'")
                    print(f"      Got: '{typed_value[:100]}...'")
        except Exception as e:
            print(f"   ⚠️  Method 1 failed: {e}")
        
        # Method 2: Use JavaScript with component property setter
        if not prompt_updated:
            try:
                print("   Trying Method 2: JavaScript with component property...")
                result = await page.evaluate(f"""
                    (promptText) => {{
                        const lightningTextareas = document.querySelectorAll('lightning-textarea');
                        for (let lt of lightningTextareas) {{
                            if (lt.shadowRoot) {{
                                const textarea = lt.shadowRoot.querySelector('textarea[name="prompt"]');
                                if (textarea) {{
                                    // Focus and select all
                                    textarea.focus();
                                    textarea.select();
                                    
                                    // Force clear - set to empty string multiple ways
                                    textarea.value = '';
                                    if ('value' in lt) {{
                                        lt.value = '';
                                    }}
                                    textarea.textContent = '';
                                    
                                    // Trigger input event to ensure UI updates
                                    textarea.dispatchEvent(new Event('input', {{ bubbles: true, cancelable: true }}));
                                    
                                    // Now set the new value
                                    textarea.value = promptText;
                                    if ('value' in lt) {{
                                        lt.value = promptText;
                                    }}
                                    
                                    // Trigger events after setting value
                                    textarea.dispatchEvent(new Event('input', {{ bubbles: true, cancelable: true }}));
                                    textarea.dispatchEvent(new Event('change', {{ bubbles: true, cancelable: true }}));
                                    lt.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                    
                                    // Verify it was set correctly
                                    return textarea.value === promptText;
                                }}
                            }}
                        }}
                        return false;
                    }}
                """, new_prompt)
                if result:
                    prompt_updated = True
                    print("   ✅ Updated via JavaScript")
            except Exception as e:
                print(f"   ⚠️  Method 2 failed: {e}")
        
        # Method 3: Use fill() as fallback
        if not prompt_updated:
            try:
                print("   Trying Method 3: Playwright fill()...")
                textarea_locator = page.locator('lightning-textarea textarea[name="prompt"]').first
                if await textarea_locator.is_visible(timeout=3000):
                    # Clear using multiple methods to ensure complete deletion
                    await textarea_locator.click()
                    await textarea_locator.press('Control+a')
                    await textarea_locator.press('Backspace')
                    await textarea_locator.press('Delete')
                    await textarea_locator.fill('')  # Explicitly fill with empty first
                    await asyncio.sleep(0.3)
                    
                    # Verify empty, force clear via JS if needed
                    current = await textarea_locator.input_value()
                    if len(current) > 0:
                        await textarea_locator.evaluate("el => { el.value = ''; }")
                        await asyncio.sleep(0.2)
                    
                    # Now fill with new prompt
                    await textarea_locator.fill(new_prompt)
                    await textarea_locator.evaluate("""
                        el => {
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    """)
                    prompt_updated = True
                    print("   ✅ Updated via Playwright fill()")
            except Exception as e:
                print(f"   ⚠️  Method 3 failed: {e}")
        
        if not prompt_updated:
            print("   ❌ All methods failed - could not update textarea")
            await take_screenshot("07_textarea_not_found")
            await browser.close()
            return False
        
        await take_screenshot("07_after_textarea_fill")
        print("✅ Prompt updated!")
        
        # Click away from the textarea to trigger blur/validation
        print("🖱️  Clicking away from textarea to trigger validation...")
        await page.evaluate("""
            () => {
                // Find the textarea and blur it
                const lightningTextareas = document.querySelectorAll('lightning-textarea');
                for (let lt of lightningTextareas) {
                    if (lt.shadowRoot) {
                        const textarea = lt.shadowRoot.querySelector('textarea[name="prompt"]');
                        if (textarea) {
                            textarea.blur();
                            // Also click on the page body to ensure focus moves
                            document.body.click();
                            return true;
                        }
                    }
                }
                // Fallback: just click on body
                document.body.click();
                return false;
            }
        """)
        await asyncio.sleep(1)  # Quick wait
        
        # Verify the prompt was actually set - check both textarea and component value
        prompt_value = await page.evaluate(f"""
            (expectedPrompt) => {{
                const lightningTextareas = document.querySelectorAll('lightning-textarea');
                for (let lt of lightningTextareas) {{
                    if (lt.shadowRoot) {{
                        const textarea = lt.shadowRoot.querySelector('textarea[name="prompt"]');
                        if (textarea) {{
                            // Check both textarea.value and component.value
                            const textareaValue = textarea.value || '';
                            const componentValue = lt.value || '';
                            // Return whichever has content, or textarea value
                            return textareaValue || componentValue;
                        }}
                    }}
                }}
                return '';
            }}
        """, new_prompt)
        print(f"   📝 Verified prompt value length: {len(prompt_value)} characters")
        
        # If verification shows empty but we just set it, try to re-set it
        if len(prompt_value) == 0 and prompt_updated:
            print("   ⚠️  WARNING: Prompt appears empty after setting. Re-setting...")
            try:
                textarea_locator = page.locator('lightning-textarea textarea[name="prompt"]').first
                await textarea_locator.click()
                await textarea_locator.fill(new_prompt)
                await textarea_locator.press('Tab')  # Tab away to trigger validation
                await asyncio.sleep(0.5)
                # Re-verify
                prompt_value = await textarea_locator.input_value()
                print(f"   📝 Re-verified prompt value length: {len(prompt_value)} characters")
            except Exception as e:
                print(f"   ⚠️  Could not re-set prompt: {e}")
        
        # Validate that the prompt content matches what we're trying to save
        if prompt_value and new_prompt[:100] not in prompt_value:
            print(f"   ⚠️  WARNING: Prompt content doesn't match!")
            print(f"   Expected start: {new_prompt[:100]}")
            print(f"   Actual start: {prompt_value[:100]}")
            print("   ⚠️  Continuing anyway, but save may fail...")
        elif prompt_value:
            print(f"   ✅ Prompt content verified - matches expected value")
        else:
            print(f"   ⚠️  WARNING: Prompt value is empty - save may fail!")
        
        # CRITICAL: Click Next through ALL steps sequentially, waiting for each to fully load
        # This mirrors exactly what a human does: click Next, wait to see step content, then proceed
        print("➡️  Clicking Next through all steps (mimicking human behavior)...")
        
        steps_to_visit = [
            ('Pre-Processing', ['Pre-Processing', 'Preprocessing']),
            ('Chunking', ['Chunking', 'Chunk']),
            ('Vectorization', ['Vectorization', 'Vector']),
            ('Fields for Filtering', ['Fields for Filtering', 'Filtering'])
        ]
        
        for step_name, step_keywords in steps_to_visit:
            print(f"   📍 Step: {step_name}")
            
            # Click Next button using Playwright (real mouse click)
            try:
                next_btn = page.locator("button:has-text('Next')").first
                await next_btn.wait_for(state="visible", timeout=5000)
                await next_btn.wait_for(state="attached", timeout=2000)
                
                if await next_btn.is_enabled(timeout=3000):
                    await next_btn.click()
                    print(f"      ✅ Clicked Next")
                else:
                    print(f"      ⚠️  Next button not enabled, skipping...")
                    continue
            except Exception as e:
                print(f"      ⚠️  Could not click Next: {e}")
                continue
            
            # CRITICAL: Wait for the step to actually load and be interactive
            # A human waits to SEE the step content, not just the step name
            print(f"      ⏳ Waiting for {step_name} to fully load...")
            step_loaded = False
            
            for wait_attempt in range(10):  # Wait up to 10 seconds
                # Check if step-specific content is visible (not just step name)
                step_ready = await page.evaluate(f"""
                    (keywords) => {{
                        const bodyText = document.body.textContent || '';
                        // Check if step keywords are present
                        const hasKeywords = keywords.some(kw => bodyText.includes(kw));
                        
                        // Check if there's actual interactive content (not just text)
                        const hasInteractiveContent = document.querySelectorAll('input, select, button, [role="button"], [class*="input"], [class*="select"]').length > 0;
                        
                        // Check if main content area has loaded
                        const mainContent = document.querySelector('[class*="content"], [class*="main"], [role="main"]');
                        const hasContent = mainContent && mainContent.textContent && mainContent.textContent.length > 100;
                        
                        return hasKeywords && (hasInteractiveContent || hasContent);
                    }}
                """, step_keywords)
                
                if step_ready:
                    step_loaded = True
                    print(f"      ✅ {step_name} is ready (waited {wait_attempt + 1}s)")
                    break
                
                await asyncio.sleep(1)
            
            if not step_loaded:
                print(f"      ⚠️  {step_name} may not have fully loaded, but continuing...")
            
            # Small delay before next step (like a human would pause)
            await asyncio.sleep(1)
        
        # Final Next click to get to Review and Build
        print("   📍 Moving to: Review and Build...")
        try:
            next_btn = page.locator("button:has-text('Next')").first
            if await next_btn.is_enabled(timeout=5000):
                await next_btn.click()
                print("      ✅ Clicked Next to Review and Build")
            else:
                print("      ⚠️  Next button not enabled")
        except Exception as e:
            print(f"      ⚠️  Could not click Next: {e}")
        
        # Now wait for Review and Build to fully load
        print("   ⏳ Waiting for Review and Build to fully load...")
        await asyncio.sleep(3)  # Initial wait for page transition
        await take_screenshot("09_after_navigating_to_review_build")
        
        # CRITICAL: Wait for loading to complete and Save button to be enabled
        # When skipping steps, the page needs time to load existing configs from server
        print("🔍 Waiting for Review and Build page to load all existing configs...")
        print("   (This may take 10-20 seconds as configs load asynchronously)")
        
        body_has_content = False
        save_button_enabled = False
        loading_complete = False
        
        # Wait up to 25 seconds for loading to complete AND Save button to be enabled
        for wait_attempt in range(25):
            page_ready = await page.evaluate("""
                () => {
                    // Check for loading indicators/spinners first
                    const spinners = document.querySelectorAll('[class*="spinner"], [class*="loading"], [class*="slds-spinner"], [aria-busy="true"]');
                    let isLoading = false;
                    for (let spinner of spinners) {
                        if (spinner.offsetParent !== null) {  // Visible
                            isLoading = true;
                            break;
                        }
                    }
                    
                    // Check multiple ways content might appear
                    const bodyText = document.body.textContent || '';
                    let hasContent = false;
                    
                    // Method 1: Check for specific review content keywords
                    if (bodyText.includes('Configuration') || 
                        bodyText.includes('Search Index') ||
                        bodyText.includes('Source DMO') ||
                        bodyText.includes('Parsing') ||
                        bodyText.includes('Review') ||
                        bodyText.includes('Chunking') ||
                        bodyText.includes('Vectorization')) {
                        hasContent = true;
                    }
                    
                    // Method 2: Check if main content area exists and has text
                    if (!hasContent) {
                        const mainContent = document.querySelector('[class*="content"], [class*="main"], [role="main"], [class*="body"]');
                        if (mainContent) {
                            const text = mainContent.textContent || '';
                            if (text.length > 50) {
                                hasContent = true;
                            }
                        }
                    }
                    
                    // Method 3: Check if Save button is visible AND enabled (critical!)
                    // Check both regular DOM and Shadow DOM
                    let saveEnabled = false;
                    
                    // Regular DOM buttons
                    const saveButtons = document.querySelectorAll('button');
                    for (let btn of saveButtons) {
                        if (btn.offsetParent === null) continue;
                        const text = (btn.textContent || btn.innerText || '').trim();
                        if ((text === 'Save' || text === 'Save & Build' || text === 'Build') && !btn.disabled) {
                            saveEnabled = true;
                            break;
                        }
                    }
                    
                    // Also check Shadow DOM (Lightning components)
                    if (!saveEnabled) {
                        const lwcComponents = document.querySelectorAll('lightning-button, button[is]');
                        for (let component of lwcComponents) {
                            if (component.shadowRoot) {
                                const shadowButtons = component.shadowRoot.querySelectorAll('button');
                                for (let btn of shadowButtons) {
                                    const text = (btn.textContent || btn.innerText || '').trim();
                                    if ((text === 'Save' || text === 'Save & Build' || text === 'Build') && !btn.disabled) {
                                        const rect = btn.getBoundingClientRect();
                                        if (rect.width > 0 && rect.height > 0) {
                                            saveEnabled = true;
                                            break;
                                        }
                                    }
                                }
                                if (saveEnabled) break;
                            }
                        }
                    }
                    
                    return { isLoading: isLoading, hasContent: hasContent, saveEnabled: saveEnabled };
                }
            """)
            
            loading_complete = not page_ready.get('isLoading', True)
            
            body_has_content = page_ready.get('hasContent', False)
            save_button_enabled = page_ready.get('saveEnabled', False)
            still_loading = page_ready.get('isLoading', False)
            
            if body_has_content and save_button_enabled and loading_complete:
                print(f"   ✅ Review and Build page fully ready! (waited {wait_attempt + 1}s)")
                print(f"      - Content loaded: ✅")
                print(f"      - Loading complete: ✅")
                print(f"      - Save button enabled: ✅")
                break
            elif still_loading:
                if wait_attempt % 3 == 0:
                    print(f"   ⏳ Still loading configs... (waited {wait_attempt + 1}s)")
            elif body_has_content and not save_button_enabled:
                if wait_attempt % 3 == 0:
                    print(f"   ⏳ Content loaded but Save button not enabled yet (waited {wait_attempt + 1}s)...")
            elif not body_has_content:
                if wait_attempt % 3 == 0:
                    print(f"   ⏳ Waiting for page content to load... (waited {wait_attempt + 1}s)")
            
            await asyncio.sleep(1)
        
        # SIMPLER APPROACH: Use Playwright locator to directly wait for Save button
        # This is more reliable than JavaScript evaluation
        print("   🔍 Using Playwright locator to find Save button directly...")
        try:
            save_locator = page.locator("button:has-text('Save')").first
            await save_locator.wait_for(state="visible", timeout=30000)  # Wait up to 30 seconds
            is_enabled = await save_locator.is_enabled(timeout=5000)
            if is_enabled:
                print("   ✅ Save button found and enabled via Playwright locator!")
                save_button_enabled = True
            else:
                print("   ⚠️  Save button found but disabled - waiting for it to enable...")
                await save_locator.wait_for(state="attached", timeout=10000)
                # Try one more time
                is_enabled = await save_locator.is_enabled(timeout=5000)
                if is_enabled:
                    print("   ✅ Save button is now enabled!")
                    save_button_enabled = True
                else:
                    print("   ⚠️  Save button still disabled, but proceeding anyway...")
        except Exception as e:
            print(f"   ⚠️  Playwright locator wait failed: {e}")
            print("   ⚠️  Falling back to JavaScript check...")
            
            # Fallback to JavaScript check
            if not (body_has_content and save_button_enabled and loading_complete):
                final_check = await page.evaluate("""
                    () => {
                        const buttons = document.querySelectorAll('button');
                        for (let btn of buttons) {
                            const text = (btn.textContent || btn.innerText || '').trim();
                            if ((text === 'Save' || text === 'Save & Build' || text === 'Build') && btn.offsetParent !== null) {
                                const isEnabled = !btn.disabled && !btn.hasAttribute('disabled') && btn.getAttribute('aria-disabled') !== 'true';
                                return { exists: true, enabled: isEnabled };
                            }
                        }
                        return { exists: false, enabled: false };
                    }
                """)
                
                if final_check.get('enabled'):
                    print("   ✅ Save button is enabled (JavaScript check)")
                    save_button_enabled = True
                elif final_check.get('exists'):
                    print("   ⚠️  Save button exists but is disabled")
                else:
                    print("   ❌ FAILED: No Save button found!")
                    await take_screenshot("09_review_build_no_save_button")
                    await browser.close()
                    return False
        
        if save_button_enabled:
            print("   ✅ Review and Build page is ready - Save button is enabled")
        else:
            print("   ⚠️  Proceeding despite Save button status - will try to click anyway")
        
        # CRITICAL: Verify chunking config is loaded before saving
        # If chunking config is empty, the build will fail
        print("🔍 Verifying chunking configuration is loaded on the page...")
        chunking_loaded = await page.evaluate("""
            () => {
                const bodyText = document.body.textContent || '';
                // Look for indicators that chunking config is present
                // Common terms: "Passage Extraction", "max_tokens", "Chunking", "8192", etc.
                const indicators = [
                    'Passage Extraction',
                    'max_tokens',
                    'Chunking',
                    '8192',
                    'chunking',
                    'perFileExtension'
                ];
                
                for (let indicator of indicators) {
                    if (bodyText.includes(indicator)) {
                        return true;
                    }
                }
                
                // Also check if there's a chunking section visible
                const chunkingSections = document.querySelectorAll('[class*="chunking"], [id*="chunking"], [data-id*="chunking"]');
                for (let section of chunkingSections) {
                    if (section.offsetParent !== null) {
                        const text = section.textContent || '';
                        if (text.length > 20) {  // Has some content
                            return true;
                        }
                    }
                }
                
                return false;
            }
        """)
        
        if chunking_loaded:
            print("   ✅ Chunking configuration appears to be loaded on the page")
        else:
            print("   ⚠️  WARNING: Chunking configuration may not be loaded!")
            print("   ⚠️  This could cause the build to fail with empty chunking config")
            print("   ⏳ Waiting additional 5 seconds for configs to load...")
            await asyncio.sleep(5)
        
        # Wait longer before clicking Save to ensure all validations are complete
        print("⏳ Waiting 10 seconds before clicking Save to ensure all validations complete...")
        await asyncio.sleep(10)
        
        await take_screenshot("10_before_save")
        
        # Get all buttons including Shadow DOM
        all_buttons_info = await page.evaluate("""
            () => {
                const buttons = Array.from(document.querySelectorAll('button'));
                const results = [];
                
                // Regular buttons
                for (let btn of buttons) {
                    if (btn.offsetParent !== null) {
                        const text = (btn.textContent || btn.innerText || '').trim();
                        if (text.length > 0) {
                            results.push({
                                text: text,
                                disabled: btn.disabled,
                                classes: btn.className || '',
                                inShadow: false
                            });
                        }
                    }
                }
                
                // Check Shadow DOM (Lightning Web Components)
                const lwcButtons = document.querySelectorAll('lightning-button, lightning-button-menu, button[is]');
                for (let lwc of lwcButtons) {
                    if (lwc.shadowRoot) {
                        const shadowButtons = lwc.shadowRoot.querySelectorAll('button');
                        for (let btn of shadowButtons) {
                            if (btn.offsetParent !== null) {
                                const text = (btn.textContent || btn.innerText || '').trim();
                                if (text.length > 0) {
                                    results.push({
                                        text: text,
                                        disabled: btn.disabled,
                                        classes: btn.className || '',
                                        inShadow: true
                                    });
                                }
                            }
                        }
                    }
                }
                
                return results;
            }
        """)
        
        print(f"   📋 Found {len(all_buttons_info)} visible buttons:")
        for btn in all_buttons_info[:15]:
            marker = "🔘" if any(x in btn.get('text', '') for x in ['Save', 'Build', 'Finish']) else "  "
            shadow = " (Shadow DOM)" if btn.get('inShadow') else ""
            print(f"      {marker} '{btn.get('text', '')}' (disabled={btn.get('disabled')}){shadow}")
        # Verify we're on Review and Build
        page_text = await page.locator("body").text_content()
        is_review_build = ("Review" in page_text and "Build" in page_text)
        
        if is_review_build:
            print("   ✅ Confirmed: On Review & Build step")
        else:
            print("   ⚠️  Warning: May not be on Review & Build step, but proceeding to save...")
        
        # Find and click Save/Finish/Build button - COMPREHENSIVE SEARCH
        print("💾 Looking for Save/Build button on Review & Build step...")
        await take_screenshot("10_before_save")
        
        # First, get comprehensive info about ALL buttons
        print("   🔍 Comprehensive button analysis...")
        all_buttons_info = await page.evaluate("""
            () => {
                const buttons = document.querySelectorAll('button');
                const result = [];
                for (let btn of buttons) {
                    if (btn.offsetParent === null) continue; // Skip hidden
                    const text = (btn.textContent || btn.innerText || '').trim();
                    if (text.length > 0) {
                        result.push({
                            text: text,
                            disabled: btn.disabled,
                            classes: btn.className || '',
                            id: btn.id || '',
                            ariaDisabled: btn.getAttribute('aria-disabled') || 'false',
                            visible: true
                        });
                    }
                }
                return result;
            }
        """)
        
        print(f"   📋 Found {len(all_buttons_info)} visible buttons:")
        for btn in all_buttons_info[:20]:  # Show first 20
            marker = "🔘" if any(x in btn.get('text', '') for x in ['Save', 'Build', 'Finish']) else "  "
            print(f"      {marker} '{btn.get('text', '')}' (disabled={btn.get('disabled')}, aria-disabled={btn.get('ariaDisabled')})")
        
        # FIND AND CLICK SAVE BUTTON - COMPREHENSIVE SEARCH INCLUDING SHADOW DOM
        print("💾 Finding and clicking Save button (checking Shadow DOM too)...")
        save_clicked = False
        current_url_before = page.url
        
        # Comprehensive search for Save button - including Shadow DOM
        save_result = await page.evaluate("""
            () => {
                // Method 1: Find button with exact text "Save" in regular DOM
                const allButtons = document.querySelectorAll('button');
                for (let btn of allButtons) {
                    if (btn.offsetParent === null) continue; // Skip hidden
                    const text = (btn.textContent || btn.innerText || '').trim();
                    if (text === 'Save' || text === 'Save & Build' || text === 'Build') {
                        return {
                            found: true,
                            text: text,
                            disabled: btn.disabled,
                            element: 'button',
                            method: 'regular-dom'
                        };
                    }
                }
                
                // Method 2: Check Shadow DOM (Lightning Web Components)
                const lwcComponents = document.querySelectorAll('lightning-button, lightning-button-menu, button[is], [class*="lightning-button"]');
                for (let component of lwcComponents) {
                    if (component.shadowRoot) {
                        const shadowButtons = component.shadowRoot.querySelectorAll('button');
                        for (let btn of shadowButtons) {
                            const text = (btn.textContent || btn.innerText || '').trim();
                            if (text === 'Save' || text === 'Save & Build' || text === 'Build') {
                                // Check if visible (might need to check parent component)
                                const rect = btn.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0) {
                                    return {
                                        found: true,
                                        text: text,
                                        disabled: btn.disabled,
                                        element: 'shadow-dom-button',
                                        method: 'shadow-dom'
                                    };
                                }
                            }
                        }
                    }
                }
                
                // Method 3: Find by class (brand/primary buttons) - more aggressive
                for (let btn of allButtons) {
                    if (btn.offsetParent === null) continue;
                    const classes = btn.className || '';
                    const text = (btn.textContent || btn.innerText || '').trim();
                    // Look for brand/primary button classes
                    if ((classes.includes('slds-button--brand') || 
                         classes.includes('slds-button_brand') ||
                         classes.includes('brand') ||
                         classes.includes('primary')) && 
                        (text === 'Save' || text.includes('Save') || text === 'Build')) {
                        return {
                            found: true,
                            text: text,
                            disabled: btn.disabled,
                            element: 'brand-button',
                            method: 'class-based'
                        };
                    }
                }
                
                // Method 4: Find by position (top-right header area) - last resort
                const headerArea = document.querySelector('[class*="header"], [class*="toolbar"], [class*="actions"]');
                if (headerArea) {
                    const headerButtons = headerArea.querySelectorAll('button');
                    for (let btn of headerButtons) {
                        if (btn.offsetParent === null) continue;
                        const text = (btn.textContent || btn.innerText || '').trim();
                        if (text === 'Save' || text === 'Save & Build' || text === 'Build') {
                            return {
                                found: true,
                                text: text,
                                disabled: btn.disabled,
                                element: 'header-button',
                                method: 'header-area'
                            };
                        }
                    }
                }
                
                return { found: false };
            }
        """)
        
        if save_result.get('found'):
            btn_text = save_result.get('text', '')
            is_disabled = save_result.get('disabled', True)
            print(f"   ✅ Found Save button: '{btn_text}' (disabled={is_disabled})")
            
            if not is_disabled:
                method_used = save_result.get('method', 'unknown')
                print(f"   🖱️  Clicking Save button (found via: {method_used})...")
                # Click it - handle both regular DOM and Shadow DOM
                clicked = await page.evaluate(f"""
                    (method) => {{
                        // If found via shadow-dom, click in shadow DOM
                        if (method === 'shadow-dom') {{
                            const lwcComponents = document.querySelectorAll('lightning-button, lightning-button-menu, button[is], [class*="lightning-button"]');
                            for (let component of lwcComponents) {{
                                if (component.shadowRoot) {{
                                    const shadowButtons = component.shadowRoot.querySelectorAll('button');
                                    for (let btn of shadowButtons) {{
                                        const text = (btn.textContent || btn.innerText || '').trim();
                                        if ((text === 'Save' || text === 'Save & Build' || text === 'Build') && !btn.disabled) {{
                                            btn.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                                            btn.focus();
                                            btn.click();
                                            const evt = new MouseEvent('click', {{ bubbles: true, cancelable: true }});
                                            btn.dispatchEvent(evt);
                                            return {{ clicked: true, text: text }};
                                        }}
                                    }}
                                }}
                            }}
                        }}
                        
                        // Regular DOM buttons
                        const allButtons = document.querySelectorAll('button');
                        for (let btn of allButtons) {{
                            if (btn.offsetParent === null) continue;
                            const text = (btn.textContent || btn.innerText || '').trim();
                            if ((text === 'Save' || text === 'Save & Build' || text === 'Build') && !btn.disabled) {{
                                btn.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                                btn.focus();
                                btn.click();
                                const evt = new MouseEvent('click', {{ bubbles: true, cancelable: true }});
                                btn.dispatchEvent(evt);
                                return {{ clicked: true, text: text }};
                            }}
                        }}
                        return {{ clicked: false }};
                    }}
                """, method_used)
                
                if clicked.get('clicked'):
                    save_clicked = True
                    await take_screenshot("10b_after_save_click")
                    print(f"   ✅ Clicked Save button: '{clicked.get('text')}'")
                    
                    # IMMEDIATE VALIDATION: Check for URL redirect
                    print(f"   🔍 IMMEDIATE VALIDATION: Checking for redirect...")
                    print(f"      URL before: {current_url_before[:120]}")
                    redirect_detected = False
                    for check_attempt in range(6):
                        await asyncio.sleep(1)
                        current_url_after = page.url
                        if current_url_after != current_url_before:
                            print(f"      ✅ URL CHANGED! Redirect detected - Save worked!")
                            print(f"      URL after: {current_url_after[:120]}")
                            await take_screenshot("10c_redirect_detected")
                            redirect_detected = True
                            break
                    
                    if not redirect_detected:
                        print(f"      ❌ FAILED: No redirect after 6 seconds - Save did not work")
                        await take_screenshot("10c_no_redirect")
                        print("   ❌ Stopping - no further validation needed")
                        await browser.close()
                        return False
                else:
                    print(f"   ❌ JavaScript click failed, trying Playwright locator...")
                    # Fallback: Use Playwright locator (more reliable for Lightning components)
                    try:
                        save_locator = page.locator("button:has-text('Save'), button:has-text('Save & Build'), button:has-text('Build')").first
                        if await save_locator.is_visible(timeout=5000):
                            await save_locator.click()
                            save_clicked = True
                            print(f"   ✅ Clicked Save via Playwright locator!")
                            await take_screenshot("10b_after_save_click_playwright")
                            
                            # Check for redirect - wait longer (Salesforce can take 10-15 seconds)
                            redirect_detected = False
                            for check_attempt in range(15):  # Wait up to 15 seconds
                                await asyncio.sleep(1)
                                current_url_after = page.url
                                if current_url_after != current_url_before:
                                    print(f"      ✅ URL CHANGED! Redirect detected - Save worked! (waited {check_attempt + 1}s)")
                                    redirect_detected = True
                                    break
                                if check_attempt % 3 == 0 and check_attempt > 0:
                                    print(f"      ⏳ Still waiting for redirect... (waited {check_attempt + 1}s)")
                            
                            if not redirect_detected:
                                print(f"      ❌ FAILED: No redirect after 15 seconds")
                                await take_screenshot("10c_no_redirect_after_15s")
                                await browser.close()
                                return False
                        else:
                            print(f"   ❌ Playwright locator also failed - button not visible")
                    except Exception as e:
                        print(f"   ❌ Playwright locator error: {e}")
            else:
                print(f"   ⚠️  Save button is disabled, waiting for it to enable...")
        else:
            print(f"   ❌ Could not find Save button via JavaScript!")
            print(f"   🔄 Trying Playwright locator as fallback...")
            await take_screenshot("10_save_button_not_found")
            
            # Try Playwright locator - often more reliable
            try:
                save_locator = page.locator("button:has-text('Save')").first
                if await save_locator.is_visible(timeout=5000):
                    is_enabled = await save_locator.is_enabled(timeout=2000)
                    if is_enabled:
                        await save_locator.click()
                        save_clicked = True
                        print(f"   ✅ Found and clicked Save via Playwright locator!")
                        await take_screenshot("10b_after_save_click_playwright")
                        
                        # Check for redirect
                        redirect_detected = False
                        for check_attempt in range(6):
                            await asyncio.sleep(1)
                            current_url_after = page.url
                            if current_url_after != current_url_before:
                                print(f"      ✅ URL CHANGED! Redirect detected - Save worked!")
                                redirect_detected = True
                                break
                        
                        if not redirect_detected:
                            print(f"      ❌ FAILED: No redirect after 6 seconds")
                            await browser.close()
                            return False
                    else:
                        print(f"   ⚠️  Save button found but disabled")
                else:
                    print(f"   ❌ Save button not visible via Playwright either")
            except Exception as e:
                print(f"   ❌ Playwright locator error: {e}")
        
        if not save_clicked:
            print("   ⏳ Save button not in header, searching all buttons...")
            for attempt in range(15):  # Try for up to 30 seconds (15 * 2 seconds)
                # Check if Save button exists and is enabled - MORE AGGRESSIVE SEARCH
                button_status = await page.evaluate("""
                    () => {
                        const buttons = document.querySelectorAll('button');
                        const priorityTexts = ['Save & Build', 'Build', 'Save', 'Finish'];
                    
                    // First pass: exact matches
                    for (let priorityText of priorityTexts) {
                        for (let btn of buttons) {
                            if (btn.offsetParent === null) continue;
                            const text = (btn.textContent || btn.innerText || '').trim();
                            if (text === priorityText || text.includes(priorityText)) {
                                // Force enable
                                btn.removeAttribute('disabled');
                                btn.disabled = false;
                                btn.classList.remove('slds-button--disabled', 'disabled');
                                btn.setAttribute('aria-disabled', 'false');
                                
                                return {
                                    found: true,
                                    text: text,
                                    disabled: btn.disabled,
                                    classes: btn.className || '',
                                    element: 'found'
                                };
                            }
                        }
                    }
                    
                    // Second pass: brand buttons (primary action buttons)
                    for (let btn of buttons) {
                        if (btn.offsetParent === null) continue;
                        const text = (btn.textContent || btn.innerText || '').trim();
                        const classes = btn.className || '';
                        if (classes.includes('slds-button--brand') && text.length > 0 && 
                            !text.includes('Cancel') && !text.includes('Back') && 
                            !text.includes('Next') && !text.includes('Previous')) {
                            // Force enable
                            btn.removeAttribute('disabled');
                            btn.disabled = false;
                            btn.classList.remove('slds-button--disabled', 'disabled');
                            btn.setAttribute('aria-disabled', 'false');
                            
                            return {
                                found: true,
                                text: text,
                                disabled: btn.disabled,
                                classes: classes,
                                element: 'brand'
                            };
                        }
                    }
                    
                    return { found: false, text: '', disabled: true, classes: '', element: null };
                }
            """)
                
                if button_status.get('found'):
                    is_disabled = button_status.get('disabled')
                    btn_text = button_status.get('text', '')
                    print(f"      📍 Attempt {attempt + 1}/30: Found button '{btn_text}' (disabled={is_disabled}, type={button_status.get('element')})")
                    
                    if not is_disabled:
                        print(f"      ✅ Save button is enabled! Clicking '{btn_text}'...")
                        # Try to click it
                        clicked = await page.evaluate("""
                        () => {
                            const buttons = document.querySelectorAll('button');
                            const priorityTexts = ['Save & Build', 'Build', 'Save', 'Finish'];
                            
                            // Try priority texts first
                            for (let priorityText of priorityTexts) {
                                for (let btn of buttons) {
                                    if (btn.offsetParent === null) continue;
                                    const text = (btn.textContent || btn.innerText || '').trim();
                                    if ((text === priorityText || text.includes(priorityText)) && !btn.disabled) {
                                        btn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                                        btn.focus();
                                        btn.click();
                                        const evt = new MouseEvent('click', { bubbles: true, cancelable: true });
                                        btn.dispatchEvent(evt);
                                        return { clicked: true, text: text };
                                    }
                                }
                            }
                            
                            // Try brand buttons
                            for (let btn of buttons) {
                                if (btn.offsetParent === null) continue;
                                const text = (btn.textContent || btn.innerText || '').trim();
                                const classes = btn.className || '';
                                if (classes.includes('slds-button--brand') && !btn.disabled && 
                                    !text.includes('Cancel') && !text.includes('Back')) {
                                    btn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                                    btn.focus();
                                    btn.click();
                                    const evt = new MouseEvent('click', { bubbles: true, cancelable: true });
                                    btn.dispatchEvent(evt);
                                    return { clicked: true, text: text };
                                }
                            }
                            
                            return { clicked: false, text: '' };
                        }
                    """)
                    
                    if clicked.get('clicked'):
                        print(f"      ✅ Clicked Save via JavaScript! (Button: '{clicked.get('text')}')")
                        save_clicked = True
                        await take_screenshot("10b_immediately_after_save_click")
                        
                        # IMMEDIATE VALIDATION: Check for URL redirect (fastest way to know if Save worked)
                        print("   🔍 IMMEDIATE VALIDATION: Checking for URL redirect...")
                        current_url_before = page.url
                        print(f"      URL before Save: {current_url_before[:100]}")
                        
                        # Wait a few seconds and check if URL changed
                        redirect_detected = False
                        for check_attempt in range(6):  # Check for 6 seconds
                            await asyncio.sleep(1)
                            current_url_after = page.url
                            if current_url_after != current_url_before:
                                print(f"      ✅ URL CHANGED! Redirect detected - Save worked!")
                                print(f"      URL after Save: {current_url_after[:100]}")
                                await take_screenshot("10c_after_redirect_detected")
                                redirect_detected = True
                                break
                        
                        if not redirect_detected:
                            print(f"      ❌ FAILED: No URL redirect after 6 seconds - Save did not work")
                            await take_screenshot("10c_no_redirect_after_6s")
                            print("   ❌ Stopping - no further validation needed")
                            await browser.close()
                            return False
                        
                        # Break out of the attempt loop since we clicked Save
                        break
                    else:
                        print(f"      ⚠️  Click attempt failed, button may have become disabled")
                else:
                    print(f"      ⏳ Save button found but still disabled (attempt {attempt + 1}/30)...")
            else:
                if attempt % 5 == 0:  # Print every 5th attempt to reduce spam
                    print(f"      ⏳ Save button not found yet (attempt {attempt + 1}/30)...")
            
            await asyncio.sleep(2)
        
        # Fallback: Try Playwright if JavaScript didn't work
        if not save_clicked:
            print("   🔄 Trying Playwright locator method...")
            try:
                # Wait for Save button to be enabled
                save_button = page.locator("button:has-text('Save'), button:has-text('Build'), button:has-text('Save & Build')").first
                await save_button.wait_for(state='visible', timeout=10000)
                
                # Wait for it to be enabled
                for attempt in range(20):
                    is_enabled = await save_button.is_enabled()
                    if is_enabled:
                        await save_button.scroll_into_view_if_needed()
                        await asyncio.sleep(0.5)
                        current_url_before = page.url
                        await save_button.click()
                        print("✅ Clicked Save via Playwright!")
                        save_clicked = True
                        await take_screenshot("10b_immediately_after_save_click_playwright")
                        
                        # IMMEDIATE VALIDATION: Check for URL redirect
                        print("   🔍 IMMEDIATE VALIDATION: Checking for URL redirect...")
                        redirect_detected = False
                        for check_attempt in range(6):
                            await asyncio.sleep(1)
                            current_url_after = page.url
                            if current_url_after != current_url_before:
                                print(f"      ✅ URL CHANGED! Redirect detected - Save worked!")
                                await take_screenshot("10c_after_redirect_detected_playwright")
                                redirect_detected = True
                                break
                        
                        if not redirect_detected:
                            print(f"      ❌ FAILED: No URL redirect after 6 seconds - Save did not work")
                            await take_screenshot("10c_no_redirect_after_6s_playwright")
                            print("   ❌ Stopping - no further validation needed")
                            await browser.close()
                            return False
                        break
                    else:
                        print(f"      ⏳ Waiting for Save button to enable (attempt {attempt + 1}/20)...")
                        await asyncio.sleep(2)
            except Exception as e:
                print(f"⚠️  Playwright method failed: {e}")
        
        status_check_success = False
        
        if save_clicked:
            print("⏳ Waiting for automatic redirect to detail page after Save...")
            print("   (Redirect to detail page is the clearest UI indication of successful save)")
            
            # Wait for navigation to detail page (Save should redirect automatically)
            redirect_success = False
            try:
                detail_url_pattern = f"**/DataSemanticSearch/{search_index_id}/view"
                await page.wait_for_url(detail_url_pattern, timeout=30000)
                print("   ✅ Redirected to detail page - Save was successful!")
                redirect_success = True
                await asyncio.sleep(2)  # Wait for page to load
                await take_screenshot("11_after_save_on_detail_page")
            except Exception as e:
                print(f"   ⚠️  Did not redirect within 30 seconds: {e}")
                print("   Checking current page...")
                await take_screenshot("11_after_save_current_page")
                current_url = page.url
                print(f"   Current URL: {current_url[:120]}")
            
            # Keep browser open for potential rebuild if build fails
            # We'll close it after confirming success or after rebuild
            browser_closed = False
            if redirect_success:
                print("\n✅ UI session complete - browser will stay open for potential rebuild")
            else:
                print("   ⚠️  Warning: Expected redirect to detail page did not occur")
                print("   Browser will stay open for potential rebuild")
            
            # Save network capture immediately (before API polling that might crash)
            if capture_network:
                if all_requests:
                    network_log_file = screenshots_dir / f"network_capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                    with open(network_log_file, 'w') as f:
                        json.dump(all_requests, f, indent=2)
                    print(f"\n📡 Network capture saved: {network_log_file}")
                    print(f"   Captured {len(all_requests)} PUT/POST request(s)")
                    for i, req in enumerate(all_requests, 1):
                        print(f"   Request {i}: {req['method']} {req['url'][:80]}...")
                        if 'response' in req:
                            print(f"      Response: {req['response']['status']} {req['response']['status_text']}")
                    
                    if semantic_search_requests:
                        print(f"\n   ✅ Found {len(semantic_search_requests)} semanticSearch-specific request(s)")
                    else:
                        print(f"\n   ⚠️  No semanticSearch requests found in {len(all_requests)} captured requests")
                else:
                    print("\n⚠️  No PUT/POST requests were captured.")
            
            # Now poll API status until it changes to SUBMITTED (or another terminal state)
            # Emit keepalive heartbeats/logs during long waits to prevent false dead-job detection.
            print("\n📊 Polling Search Index status via API (waiting for status to update to SUBMITTED)...")
            await asyncio.sleep(5)  # Initial wait before first check
            
            rebuild_attempts = 0
            max_rebuild_attempts = 3  # Limit rebuild attempts to prevent infinite loops
            
            # Keepalive settings
            keepalive_interval_seconds = 180  # 3 minutes
            last_keepalive = datetime.utcnow()
            
            try:
                # Get access token via SOAP authentication (use provided credentials)
                from salesforce_api import get_salesforce_credentials
                api_instance_url, access_token = get_salesforce_credentials(
                    username=username,
                    password=password,
                    instance_url=instance_url
                )
                
                import urllib.request
                api_url = f"{api_instance_url}/services/data/v65.0/ssot/search-index/{search_index_id}"
                
                # Phase 1: Wait for SUBMITTED status (confirms save was processed)
                print("   Phase 1: Waiting for status to change to SUBMITTED...")
                max_attempts_phase1 = 36  # 3 minutes (36 * 5 seconds)
                submitted_reached = False
                
                for attempt in range(max_attempts_phase1):
                    if should_abort():
                        status_check_success = False
                        break
                    req = urllib.request.Request(api_url)
                    req.add_header('Authorization', f'Bearer {access_token}')
                    req.add_header('Content-Type', 'application/json')
                    
                    with urllib.request.urlopen(req, timeout=30) as response:
                        index_data = json.loads(response.read().decode())
                        runtime_status = index_data.get('runtimeStatus', 'Unknown')
                        label = index_data.get('label', 'Unknown')
                        
                        if attempt == 0:
                            print(f"   Label: {label}")
                        
                        if runtime_status == 'SUBMITTED':
                            print(f"\n   ✅ Status: {runtime_status}")
                            print(f"   ✅ Index build has been submitted successfully!")
                            submitted_reached = True
                            break
                        elif runtime_status in ['Active', 'Ready']:
                            # Jumped straight to Ready (unlikely but possible)
                            print(f"\n   ✅ Status: {runtime_status}")
                            print(f"   ✅ Index is already ready!")
                            submitted_reached = True
                            break
                        elif runtime_status == 'Failed' or 'FAILED' in runtime_status.upper():
                            print(f"\n   ❌ Status: {runtime_status}")
                            print(f"   ❌ Index build failed!")
                            if 'errorMessage' in index_data and index_data.get('errorMessage'):
                                print(f"   Error Message: {index_data.get('errorMessage')}")
                            
                            # Trigger rebuild if browser is still open and we haven't exceeded max attempts
                            if not browser_closed and rebuild_attempts < max_rebuild_attempts:
                                rebuild_attempts += 1
                                print(f"\n   🔄 Attempting to trigger rebuild (attempt {rebuild_attempts}/{max_rebuild_attempts})...")
                                try:
                                    detail_url = f"{instance_url}/lightning/r/DataSemanticSearch/{search_index_id}/view"
                                    if page.url != detail_url:
                                        await page.goto(detail_url, wait_until='domcontentloaded', timeout=30000)
                                        await asyncio.sleep(2)
                                    
                                    rebuild_clicked = False
                                    try:
                                        rebuild_btn = page.locator("button:has-text('Rebuild')").first
                                        if await rebuild_btn.is_visible(timeout=5000):
                                            await rebuild_btn.click()
                                            rebuild_clicked = True
                                            print(f"   ✅ Clicked Rebuild button!")
                                            await take_screenshot("12_after_rebuild_click")
                                            await asyncio.sleep(2)
                                            print(f"   ⏳ Restarting status polling after rebuild...")
                                            submitted_reached = False  # Reset to continue polling
                                            await asyncio.sleep(5)
                                            continue
                                    except Exception as e:
                                        print(f"   ⚠️  Could not click Rebuild: {e}")
                                    
                                    if not rebuild_clicked:
                                        status_check_success = False
                                        break
                                except Exception as e:
                                    print(f"   ⚠️  Error during rebuild attempt: {e}")
                                    status_check_success = False
                                    break
                            elif rebuild_attempts >= max_rebuild_attempts:
                                print(f"\n   ⚠️  Maximum rebuild attempts ({max_rebuild_attempts}) reached")
                                print(f"   Please manually investigate and rebuild if needed")
                                status_check_success = False
                                break
                            else:
                                print(f"   ⚠️  Browser is closed - cannot trigger rebuild automatically")
                                status_check_success = False
                                break
                        else:
                            if attempt % 6 == 0:  # Print every 30 seconds
                                print(f"   ⏳ Status: {runtime_status} (waiting for SUBMITTED, attempt {attempt + 1}/{max_attempts_phase1})...")
                    
                    # Emit keepalive for long waits
                    now = datetime.utcnow()
                    if (now - last_keepalive).total_seconds() >= keepalive_interval_seconds:
                        print(f"   🟢 Keepalive: still waiting for SUBMITTED (attempt {attempt + 1}/{max_attempts_phase1})")
                        last_keepalive = now
                    
                    await asyncio.sleep(5)
                
                if not submitted_reached:
                    print(f"\n   ⚠️  Did not reach SUBMITTED status within timeout. Current status: {runtime_status}")
                    print(f"   The save may have failed. Check manually.")
                    status_check_success = False
                
                # Phase 2: Wait for Ready/Active status (confirms build is complete)
                if skip_wait:
                    print("\n   ⏭️  Skipping Phase 2 wait (skip_wait=True)")
                    print("   ⚠️  Note: Index rebuild may still be in progress")
                    print("   ✅ Assuming success for memory testing purposes")
                    ready_reached = True
                    status_check_success = True
                else:
                    print("\n   Phase 2: Waiting for status to change to Ready/Active...")
                    print("   (This may take several minutes depending on index size)")
                    print("   ⏳ Will wait indefinitely until READY status + indexRefreshedOn timestamp (heartbeat every 3 min)...")
                    ready_reached = False
                    attempt = 0
                    
                    while not ready_reached:
                        if should_abort():
                            status_check_success = False
                            break
                        req = urllib.request.Request(api_url)
                        req.add_header('Authorization', f'Bearer {access_token}')
                        req.add_header('Content-Type', 'application/json')
                        
                        with urllib.request.urlopen(req, timeout=30) as response:
                            index_data = json.loads(response.read().decode())
                            runtime_status = index_data.get('runtimeStatus', 'Unknown')
                            build_status = index_data.get('buildStatus', 'Unknown')
                            
                            # Check status (case-insensitive)
                            runtime_status_upper = runtime_status.upper() if runtime_status else ''
                            if runtime_status_upper in ['ACTIVE', 'READY']:
                                # Check if indexRefreshedOn timestamp exists (required for success)
                                index_refreshed_on = index_data.get('indexRefreshedOn')
                                if index_refreshed_on:
                                    print(f"\n   ✅ Status: {runtime_status}")
                                    print(f"   ✅ Index Refreshed On: {index_refreshed_on}")
                                    print(f"   ✅ Index build is complete and ready to use!")
                                    if build_status and build_status != 'N/A':
                                        print(f"   Build Status: {build_status}")
                                    ready_reached = True
                                    status_check_success = True
                                    break
                                else:
                                    # READY but no timestamp - rebuild may not have started yet, keep waiting
                                    if attempt % 12 == 0:  # Print every 60 seconds
                                        print(f"   ⏳ Status: {runtime_status} but indexRefreshedOn is null (waiting for rebuild to start, attempt {attempt + 1})...")
                            elif runtime_status == 'Failed' or 'FAILED' in runtime_status.upper():
                                print(f"\n   ❌ Status: {runtime_status}")
                                print(f"   ❌ Index build failed!")
                                if 'errorMessage' in index_data and index_data.get('errorMessage'):
                                    print(f"   Error Message: {index_data.get('errorMessage')}")
                                
                                # Trigger rebuild if browser is still open and we haven't exceeded max attempts
                                if not browser_closed and rebuild_attempts < max_rebuild_attempts:
                                    rebuild_attempts += 1
                                    print(f"\n   🔄 Attempting to trigger rebuild (attempt {rebuild_attempts}/{max_rebuild_attempts})...")
                                    try:
                                        # Navigate to detail page if not already there
                                        detail_url = f"{instance_url}/lightning/r/DataSemanticSearch/{search_index_id}/view"
                                        if page.url != detail_url:
                                            await page.goto(detail_url, wait_until='domcontentloaded', timeout=30000)
                                            await asyncio.sleep(2)
                                        
                                        # Find and click Rebuild button (between Delete and Edit)
                                        rebuild_clicked = False
                                        try:
                                            rebuild_btn = page.locator("button:has-text('Rebuild')").first
                                            if await rebuild_btn.is_visible(timeout=5000):
                                                await rebuild_btn.click()
                                                print(f"   ✅ Clicked Rebuild button!")
                                                await take_screenshot("12_after_rebuild_click")
                                                await asyncio.sleep(2)
                                                
                                                # Wait for confirmation modal to appear
                                                print(f"   ⏳ Waiting for rebuild confirmation modal...")
                                                try:
                                                    # Look for the confirmation modal's Rebuild button
                                                    # The modal has a "Rebuild" button (not just the page button)
                                                    modal_rebuild_btn = page.locator("button:has-text('Rebuild')").filter(has_text="Rebuild").last
                                                    # Alternative: look for button in modal dialog
                                                    # Try multiple selectors for the modal confirm button
                                                    modal_confirm_clicked = False
                                                    
                                                    # Wait for modal to appear (up to 5 seconds)
                                                    await asyncio.sleep(2)
                                                    
                                                    # Try to find the modal's Rebuild button
                                                    # The modal typically has a button with text "Rebuild" that's different from the page button
                                                    modal_buttons = page.locator("button:has-text('Rebuild')")
                                                    button_count = await modal_buttons.count()
                                                    
                                                    if button_count > 1:
                                                        # Click the last one (which should be the modal button)
                                                        await modal_buttons.last.click()
                                                        modal_confirm_clicked = True
                                                        print(f"   ✅ Clicked Rebuild confirmation in modal!")
                                                    else:
                                                        # Try alternative: look for button in a modal/dialog
                                                        modal_dialog = page.locator("div[role='dialog'], lightning-modal, c-modal")
                                                        if await modal_dialog.count() > 0:
                                                            modal_rebuild = modal_dialog.locator("button:has-text('Rebuild')").last
                                                            if await modal_rebuild.is_visible(timeout=3000):
                                                                await modal_rebuild.click()
                                                                modal_confirm_clicked = True
                                                                print(f"   ✅ Clicked Rebuild confirmation in modal!")
                                                    
                                                    if not modal_confirm_clicked:
                                                        print(f"   ⚠️  Could not find confirmation modal button, trying direct click...")
                                                        # Fallback: try clicking any visible Rebuild button again
                                                        all_rebuild_buttons = page.locator("button:has-text('Rebuild')")
                                                        for i in range(await all_rebuild_buttons.count()):
                                                            btn = all_rebuild_buttons.nth(i)
                                                            if await btn.is_visible(timeout=1000):
                                                                await btn.click()
                                                                modal_confirm_clicked = True
                                                                print(f"   ✅ Clicked Rebuild button (fallback method)!")
                                                                break
                                                    
                                                    if modal_confirm_clicked:
                                                        rebuild_clicked = True
                                                        await take_screenshot("12_after_modal_confirm")
                                                        await asyncio.sleep(2)
                                                    else:
                                                        print(f"   ⚠️  Could not confirm rebuild in modal")
                                                        
                                                except Exception as e:
                                                    print(f"   ⚠️  Error handling confirmation modal: {e}")
                                                    await take_screenshot("12_modal_error")
                                                
                                                if rebuild_clicked:
                                                    # Reset polling - start over
                                                    print(f"   ⏳ Restarting status polling after rebuild...")
                                                    print(f"   ⏳ Waiting 60 seconds for rebuild to be submitted and status to update...")
                                                    ready_reached = False  # Reset to continue polling
                                                    await asyncio.sleep(60)  # Wait 60 seconds for status to change
                                                    continue  # Continue the loop to check status again
                                        except Exception as e:
                                            print(f"   ⚠️  Could not click Rebuild: {e}")
                                        
                                        if not rebuild_clicked:
                                            print(f"   ⚠️  Rebuild button not found or not clickable")
                                            status_check_success = False
                                            break
                                    except Exception as e:
                                        print(f"   ⚠️  Error during rebuild attempt: {e}")
                                        status_check_success = False
                                        break
                                elif rebuild_attempts >= max_rebuild_attempts:
                                    print(f"\n   ⚠️  Maximum rebuild attempts ({max_rebuild_attempts}) reached")
                                    print(f"   Please manually investigate and rebuild if needed")
                                    status_check_success = False
                                    break
                                else:
                                    print(f"   ⚠️  Browser is closed - cannot trigger rebuild automatically")
                                    print(f"   Please manually click Rebuild on the index detail page")
                                    status_check_success = False
                                    break
                            else:
                                if attempt % 12 == 0:  # Print every 60 seconds
                                    print(f"   ⏳ Status: {runtime_status} (waiting for Ready + timestamp, attempt {attempt + 1})...")
                        
                        attempt += 1
                        # Emit keepalive for long waits
                        now = datetime.utcnow()
                        if (now - last_keepalive).total_seconds() >= keepalive_interval_seconds:
                            print(f"   🟢 Keepalive: still waiting for READY (attempt {attempt + 1}) status={runtime_status}")
                            last_keepalive = now
                        
                        await asyncio.sleep(5)
                    
                    # FINAL VALIDATION: Verify the prompt was actually saved (only if not skipping wait)
                    if not skip_wait:
                        print(f"\n📊 Final Status: {runtime_status} - Index is ready!")
                        print("\n🔍 FINAL VALIDATION: Checking if prompt was actually saved...")
                        
                        # Get the saved prompt from API
                        req_final = urllib.request.Request(api_url)
                        req_final.add_header('Authorization', f'Bearer {access_token}')
                        req_final.add_header('Content-Type', 'application/json')
                        
                        with urllib.request.urlopen(req_final, timeout=30) as response_final:
                            index_data_final = json.loads(response_final.read().decode())
                            parsing_configs = index_data_final.get('parsingConfigurations', [])
                            prompt_saved = False
                            
                            for config in parsing_configs:
                                user_values = config.get('config', {}).get('userValues', [])
                                for uv in user_values:
                                    if uv.get('id') == 'prompt':
                                        saved_prompt = uv.get('value', '')
                                        # Check if our prompt text is in the saved prompt
                                        if new_prompt[:100] in saved_prompt or new_prompt in saved_prompt:
                                            print(f"   ✅ VALIDATION PASSED: Prompt was saved correctly!")
                                            print(f"   ✅ Saved prompt contains our text (first 100 chars match)")
                                            prompt_saved = True
                                            status_check_success = True
                                        else:
                                            print(f"   ❌ VALIDATION FAILED: Prompt was NOT saved!")
                                            print(f"   Expected (first 100 chars): {new_prompt[:100]}")
                                            print(f"   Actual (first 100 chars): {saved_prompt[:100]}")
                                            status_check_success = False
                                        break
                            
                            if not prompt_saved:
                                print(f"   ❌ VALIDATION FAILED: Could not find prompt in saved configuration")
                                status_check_success = False
                    
            except Exception as e:
                print(f"   ⚠️  Could not check status via API: {e}")
                status_check_success = False
        else:
            print("⚠️  Could not click Save button after all attempts")
            await take_screenshot("11_save_failed")
            status_check_success = False
        
        # Close browser when done (either success or final failure)
        if not browser_closed:
            print("\n🔒 Closing browser - session complete")
            await browser.close()
            browser_closed = True
        
        if save_clicked and status_check_success:
            print("\n✅ SUCCESS! Prompt has been updated and index build is complete.")
        elif save_clicked:
            print("\n⚠️  Prompt was updated, but status verification had issues.")
        else:
            print("\n❌ FAILED! Could not update prompt.")
        
        return save_clicked and status_check_success


# =============================================================================
# New pipeline: Create Index + Retriever (Cycle 2+)
# =============================================================================

def _index_full_name(version):
    """Return the index name as-is. Caller (get_next_index_name) is responsible for producing the full name."""
    return (version or "").strip()


async def _create_search_index_ui(
    username,
    password,
    instance_url,
    index_name,
    parser_prompt,
    state_dir,
    run_id,
    headless,
    should_abort,
    access_token=None,
    skip_api_lookup=False,
):
    """Create Search Index via Playwright. Returns (index_id, full_index_name). Uses API lookup by name (no URL extraction)."""
    base = instance_url.rstrip("/")
    login_url = "https://login.salesforce.com" if "salesforce.com" in base else base
    async with async_playwright() as p:
        launch_args = {'slow_mo': 100}
        if headless:
            # Required args for headless Chromium on Heroku/Linux containers
            launch_args['args'] = [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-software-rasterizer',
                '--disable-extensions'
            ]
        browser = await p.chromium.launch(headless=headless, **launch_args)
        # Optional browser session bootstrap from env (captured Playwright storageState).
        # Useful for Heroku/headless runs where interactive login isn't possible each run.
        storage_state = None
        try:
            auth_b64 = os.getenv("SF_AUTH_STATE_B64", "").strip()
            if auth_b64:
                storage_state = json.loads(base64.b64decode(auth_b64).decode("utf-8"))
                print("   [create_index] Found SF_AUTH_STATE_B64, attempting session restore...", flush=True)
        except Exception as e:
            print(f"   [create_index] ⚠️ Could not decode SF_AUTH_STATE_B64: {e}", flush=True)
            storage_state = None

        context = await browser.new_context(viewport={"width": 1400, "height": 900}, storage_state=storage_state)
        page = await context.new_page()
        if should_abort():
            print("   ⚠️ DIAG: Abort at start (before login)", flush=True)
            await browser.close()
            return (None, None)
        # First try direct setup navigation using restored session.
        setup_url = f"{base}/lightning/setup/DataSemanticSearch/home"
        await page.goto(setup_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(1)
        current_url = page.url

        if _is_authenticated_url(current_url):
            print(f"   [create_index] ✅ Session restore worked. URL: {current_url}", flush=True)
        else:
            print("   [create_index] Logging in...", flush=True)
            await page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
            await page.get_by_role("textbox", name="Username").fill(username)
            await page.get_by_role("textbox", name="Password").fill(password)
            await page.get_by_role("button", name="Log In").click()
            await page.wait_for_load_state("domcontentloaded", timeout=60000)
            await asyncio.sleep(1)
            current_url = page.url
        if _is_mfa_or_verification_url(current_url):
            page_text = await page.evaluate("() => document.body ? document.body.innerText.substring(0, 600) : 'no-body'")
            print(f"   [create_index] MFA/verification page detected.\n   URL: {current_url}\n   Page text: {page_text}", flush=True)
            try:
                timeout_seconds = int(os.getenv("SF_AUTH_WAIT_SECONDS", "43200"))
            except Exception:
                timeout_seconds = 43200
            resumed = await _wait_for_mfa_code_and_resume(page, run_id, should_abort, timeout_seconds=timeout_seconds)
            if not resumed:
                await browser.close()
                return (None, None)
        elif not _is_authenticated_url(current_url):
            print(f"   ❌ Login did not reach authenticated URL. Current URL: {current_url}", flush=True)
            await browser.close()
            return (None, None)
        lightning_base = base.replace(".my.salesforce.com", ".lightning.force.com")

        # Force an authenticated Lightning app-context handoff when possible.
        # This mitigates session restore landing on non-app login contexts.
        if access_token:
            frontdoor_candidates = [
                f"{lightning_base}/secur/frontdoor.jsp?sid={access_token}&retURL=%2Flightning%2Fo%2FDataSemanticSearch%2Flist%3FfilterName%3D__Recent",
                f"{base}/secur/frontdoor.jsp?sid={access_token}&retURL=%2Flightning%2Fo%2FDataSemanticSearch%2Flist%3FfilterName%3D__Recent",
            ]
            for fdc in frontdoor_candidates:
                try:
                    await page.goto(fdc, wait_until="domcontentloaded", timeout=60000)
                    await asyncio.sleep(1.0)
                    fd_url = page.url
                    new_btn_probe = page.get_by_role("button", name="New")
                    if await new_btn_probe.count() > 0:
                        print(f"   [create_index] frontdoor selected (New visible): {fd_url}", flush=True)
                        break
                    print(f"   [create_index] frontdoor landed without New: {fd_url}", flush=True)
                except Exception as e:
                    print(f"   [create_index] frontdoor candidate failed: {fdc} err={e}", flush=True)

        # Prefer object-list candidates where New button is rendered deterministically.
        setup_candidates = [
            f"{lightning_base}/lightning/o/DataSemanticSearch/list?filterName=__Recent",
            f"{lightning_base}/lightning/o/DataSemanticSearch/home",
            f"{base}/lightning/o/DataSemanticSearch/list?filterName=__Recent",
            f"{base}/lightning/o/DataSemanticSearch/home",
            setup_url,
        ]
        for cand in setup_candidates:
            try:
                await page.goto(cand, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(1.0)
                cur = page.url
                new_btn_probe = page.get_by_role("button", name="New")
                if await new_btn_probe.count() > 0:
                    print(f"   [create_index] object-list candidate selected (New visible): {cur}", flush=True)
                    break
            except Exception:
                continue

        if should_abort():
            print("   ⚠️ DIAG: Abort after login, before Search Indexes", flush=True)
            await browser.close()
            return (None, None)
        print("   [create_index] Navigate to Search Indexes...", flush=True)
        try:
            # Some org layouts already land directly on Search Indexes and do not render
            # the "Show more navigation items" control.
            more_nav = page.get_by_role("button", name="Show more navigation items")
            if await more_nav.is_visible(timeout=4000):
                await more_nav.click(timeout=8000)
                await page.get_by_role("menuitem", name="Search Indexes").click(timeout=12000)
                await page.wait_for_load_state("domcontentloaded", timeout=20000)
                await asyncio.sleep(1)
            else:
                print("   [create_index] Nav drawer button not visible; proceeding on current page.", flush=True)
        except Exception as nav_err:
            print(f"   [create_index] Nav drawer path failed ({nav_err}); retrying direct setup URL.", flush=True)
            await page.goto(setup_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(1)
        print("   [create_index] Waiting for New button...", flush=True)
        new_btn = page.get_by_role("button", name="New")
        opened_new_flow_direct = False
        new_button_clicked = False
        try:
            await new_btn.wait_for(state="visible", timeout=12000)
            await new_btn.click()
            new_button_clicked = True
            print("   [create_index] New button clicked (visible)", flush=True)
        except Exception:
            print("   [create_index] 'New' not visible on current page; trying SearchIndex list fallback URL...", flush=True)
            # Fallback: go straight to the SearchIndex object list view where "New" is rendered.
            await page.goto(f"{base}/lightning/o/SearchIndex/list", wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3.0)
            # Check if we got redirected to login page (session expired)
            page_title = await page.title()
            if "Login" in page_title or "/login" in page.url.lower():
                print(f"   [create_index] ⚠️ Redirected to login page! Session expired. Re-authenticating...", flush=True)
                # Re-login
                username_field = page.get_by_role("textbox", name="Username")
                await username_field.wait_for(state="visible", timeout=10000)
                await username_field.fill(username)
                password_field = page.get_by_role("textbox", name="Password")
                await password_field.fill(password)
                # Wait a moment for form validation
                await asyncio.sleep(0.5)
                login_button = page.get_by_role("button", name="Log In")
                await login_button.wait_for(state="visible", timeout=5000)
                print(f"   [create_index] 🔑 Clicking Login button and waiting for navigation...", flush=True)
                # Click and wait for navigation to complete
                try:
                    async with page.expect_navigation(timeout=30000):
                        await login_button.click()
                    print(f"   [create_index] ✅ Login navigation completed", flush=True)
                except Exception as nav_err:
                    print(f"   [create_index] ⚠️ Navigation timeout or error: {nav_err}", flush=True)
                    print(f"   [create_index] Current page: {await page.title()} at {page.url}", flush=True)

                # Check if login actually succeeded
                await asyncio.sleep(1)
                current_title = await page.title()
                current_url = page.url
                if "Login" in current_title or "/login" in current_url.lower():
                    print(f"   [create_index] ❌ Login FAILED - still on login page after submission", flush=True)
                    print(f"   [create_index] Title: {current_title}, URL: {current_url}", flush=True)
                    print(f"   [create_index] Possible causes: Wrong credentials (SF_PASSWORD), CAPTCHA, MFA required, or account locked", flush=True)
                    # Take screenshot for debugging
                    try:
                        screenshot_path = f"/tmp/login_failed_{run_id}.png"
                        await page.screenshot(path=screenshot_path, full_page=True)
                        print(f"   [create_index] 📸 Login failure screenshot: {screenshot_path}", flush=True)
                    except Exception:
                        pass
                    await browser.close()
                    return (None, None)

                print(f"   [create_index] ✅ Login succeeded! Now on: {current_title}", flush=True)
                # Now navigate to SearchIndex list
                await page.goto(f"{base}/lightning/o/SearchIndex/list", wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(3.0)
                # Log page state after re-auth
                page_title_after = await page.title()
                visible_buttons_after = await page.evaluate("""() => {
                    const buttons = Array.from(document.querySelectorAll('button, a'));
                    return buttons.slice(0, 30).map(b => ({
                        tag: b.tagName,
                        text: b.textContent?.trim().substring(0, 50),
                        title: b.getAttribute('title'),
                        name: b.getAttribute('name'),
                        visible: b.offsetParent !== null
                    })).filter(b => b.visible || b.title || b.text);
                }""")
                print(f"   [create_index] ✅ Re-authenticated. Page: {page_title_after}", flush=True)
                print(f"   [create_index] 🔘 Visible elements after re-auth: {visible_buttons_after}", flush=True)
            try:
                await new_btn.wait_for(state="visible", timeout=12000)
                await new_btn.click()
                new_button_clicked = True
                print("   [create_index] New button clicked at list URL (visible)", flush=True)
            except Exception:
                # Try comprehensive JavaScript search including shadow DOM
                print("   [create_index] 'New' still not visible; trying comprehensive JS search...", flush=True)
                js_new_clicked = await page.evaluate("""() => {
                    // Search in main DOM and all shadow roots
                    function findInShadowDOM(root, selector) {
                        if (!root) return null;

                        // Try direct query
                        let el = root.querySelector(selector);
                        if (el) return el;

                        // Recursively search shadow roots
                        const allElements = root.querySelectorAll('*');
                        for (const element of allElements) {
                            if (element.shadowRoot) {
                                el = findInShadowDOM(element.shadowRoot, selector);
                                if (el) return el;
                            }
                        }
                        return null;
                    }

                    // Try multiple selectors (only valid CSS selectors for querySelector)
                    const selectors = [
                        'button[title="New"]',
                        'a[title="New"]'
                    ];

                    for (const sel of selectors) {
                        const btn = findInShadowDOM(document, sel);
                        if (btn) {
                            btn.scrollIntoView();
                            btn.click();
                            return true;
                        }
                    }

                    // Fallback: find any button/link with "New" text
                    const allElements = Array.from(document.querySelectorAll('button, a, div[role="button"]'));
                    for (const el of allElements) {
                        const text = (el.textContent || el.innerText || '').trim();
                        const title = el.getAttribute('title') || '';
                        if ((text === 'New' || title === 'New') && !el.disabled) {
                            el.scrollIntoView();
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }""")
                if js_new_clicked:
                    new_button_clicked = True
                    print("   [create_index] New button clicked via comprehensive JS search", flush=True)
                    await asyncio.sleep(1.5)
                else:
                    print("   [create_index] New button not found anywhere; opening SearchIndex new-record URL directly...", flush=True)
                    await page.goto(f"{base}/lightning/o/SearchIndex/new", wait_until="domcontentloaded", timeout=60000)
                    await asyncio.sleep(3.0)
                    opened_new_flow_direct = True
                    # Check if we got redirected to login page again
                    page_title = await page.title()
                    if "Login" in page_title or "/login" in page.url.lower():
                        print(f"   [create_index] ⚠️ Redirected to login on /new URL! Re-authenticating...", flush=True)
                        # Re-login
                        username_field = page.get_by_role("textbox", name="Username")
                        await username_field.wait_for(state="visible", timeout=10000)
                        await username_field.fill(username)
                        password_field = page.get_by_role("textbox", name="Password")
                        await password_field.fill(password)
                        # Wait a moment for form validation
                        await asyncio.sleep(0.5)
                        login_button = page.get_by_role("button", name="Log In")
                        await login_button.wait_for(state="visible", timeout=5000)
                        print(f"   [create_index] 🔑 Clicking Login button and waiting for navigation...", flush=True)
                        # Click and wait for navigation to complete
                        try:
                            async with page.expect_navigation(timeout=30000):
                                await login_button.click()
                            print(f"   [create_index] ✅ Login navigation completed", flush=True)
                        except Exception as nav_err:
                            print(f"   [create_index] ⚠️ Navigation timeout or error: {nav_err}", flush=True)
                            print(f"   [create_index] Current page: {await page.title()} at {page.url}", flush=True)

                        # Check if login actually succeeded
                        await asyncio.sleep(1)
                        current_title_after_login = await page.title()
                        current_url_after_login = page.url
                        if "Login" in current_title_after_login or "/login" in current_url_after_login.lower():
                            print(f"   [create_index] ❌ Login FAILED - still on login page after submission", flush=True)
                            print(f"   [create_index] Title: {current_title_after_login}, URL: {current_url_after_login}", flush=True)
                            print(f"   [create_index] Possible causes: Wrong credentials (SF_PASSWORD), CAPTCHA, MFA required, or account locked", flush=True)
                            # Take screenshot for debugging
                            try:
                                screenshot_path = f"/tmp/login_failed_new_{run_id}.png"
                                await page.screenshot(path=screenshot_path, full_page=True)
                                print(f"   [create_index] 📸 Login failure screenshot: {screenshot_path}", flush=True)
                            except Exception:
                                pass
                            await browser.close()
                            return (None, None)

                        print(f"   [create_index] ✅ Login succeeded! Now on: {current_title_after_login}", flush=True)
                        # Navigate back to SearchIndex new
                        await page.goto(f"{base}/lightning/o/SearchIndex/new", wait_until="domcontentloaded", timeout=60000)
                        await asyncio.sleep(2)
                        print(f"   [create_index] ✅ Re-authenticated and navigated to SearchIndex /new", flush=True)
        print("   [create_index] New→Advanced Setup→Next (builder popup)...", flush=True)
        print(f"   [create_index] Current page URL before Advanced Setup: {page.url}", flush=True)
        if new_button_clicked:
            await asyncio.sleep(0.5)
        advanced_clicked = False
        if new_button_clicked:
            # Give the dialog time to appear after clicking New
            await asyncio.sleep(1.0)
        advanced_candidates = [
            page.get_by_text("Advanced Setup", exact=True),
            page.get_by_text("Advanced setup", exact=True),
            page.get_by_role("button", name="Advanced Setup"),
            page.get_by_role("button", name="Advanced setup"),
            page.get_by_text("Advanced", exact=False).first,
        ]
        for cand in advanced_candidates:
            try:
                if await cand.is_visible(timeout=8000):
                    await cand.click(timeout=8000)
                    advanced_clicked = True
                    print("   [create_index] Advanced Setup clicked", flush=True)
                    break
            except Exception:
                pass
        if not advanced_clicked and new_button_clicked:
            # Try JavaScript click for Advanced Setup
            js_advanced_clicked = await page.evaluate("""() => {
                const elements = Array.from(document.querySelectorAll('button, a, span, div'));
                const advBtn = elements.find(el => {
                    const text = (el.textContent || el.innerText || '').trim();
                    return text.toLowerCase().includes('advanced') && text.toLowerCase().includes('setup');
                });
                if (advBtn) {
                    advBtn.click();
                    return true;
                }
                return false;
            }""")
            if js_advanced_clicked:
                advanced_clicked = True
                print("   [create_index] Advanced Setup clicked via JavaScript", flush=True)
                await asyncio.sleep(0.5)
        if not advanced_clicked:
            print("   [create_index] ⚠️ Advanced Setup control not found; continuing to Next fallback.", flush=True)

        # If we opened via direct URL and didn't find Advanced Setup, diagnose what's on page
        if opened_new_flow_direct and not advanced_clicked:
            print("   [create_index] ❌ Direct URL navigation failed - no Advanced Setup dialog found", flush=True)

            # DIAGNOSTICS: Capture page state
            page_title = await page.title()
            page_url = page.url
            print(f"   [create_index] 📊 DIAGNOSTICS:", flush=True)
            print(f"   [create_index]    Title: {page_title}", flush=True)
            print(f"   [create_index]    URL: {page_url}", flush=True)

            # Get visible buttons/elements
            try:
                visible_elements = await page.evaluate("""() => {
                    const buttons = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                    return buttons.slice(0, 30).map(el => ({
                        tag: el.tagName,
                        text: (el.textContent || '').trim().substring(0, 60),
                        title: el.getAttribute('title'),
                        visible: el.offsetParent !== null
                    })).filter(el => el.visible || el.title);
                }""")
                print(f"   [create_index]    Visible elements: {visible_elements[:10]}", flush=True)
            except Exception as e:
                print(f"   [create_index]    Could not get elements: {e}", flush=True)

            # Take screenshot
            try:
                screenshot_path = f"/tmp/no_advanced_setup_{run_id}.png"
                await page.screenshot(path=screenshot_path, full_page=True)
                print(f"   [create_index]    📸 Screenshot: {screenshot_path}", flush=True)
            except Exception as e:
                print(f"   [create_index]    Screenshot failed: {e}", flush=True)

            print("   [create_index] Page may not have loaded wizard. Aborting to avoid wrong page navigation.", flush=True)
            await browser.close()
            return (None, None)

        await asyncio.sleep(0.3)
        next_clicked = False
        next_candidates = [
            page.get_by_role("button", name="Next").first,
            page.get_by_text("Next", exact=True).first,
            page.locator("button:has-text('Next')").first,
            page.locator("input[type='submit'][value='Next']").first,
        ]
        for cand in next_candidates:
            try:
                if await cand.is_visible(timeout=2500):
                    await cand.click(timeout=10000)
                    next_clicked = True
                    break
            except Exception:
                pass
        if not next_clicked:
            # Last-resort click for brittle setup dialogs.
            await page.evaluate("""() => {
                const btn =
                  Array.from(document.querySelectorAll('button,input[type=\"submit\"]'))
                    .find(el => ((el.innerText || el.value || '').trim().toLowerCase()) === 'next');
                if (btn) btn.click();
            }""")

        # Some org UIs open builder in a popup, others navigate in same tab.
        builder = None
        try:
            async with page.expect_popup(timeout=8000) as popup_info:
                # Nudge the UI once more in case the first click only focused the dialog.
                await page.keyboard.press("Enter")
            builder = await popup_info.value
            print("   [create_index] Builder opened in popup.", flush=True)
        except Exception:
            print("   [create_index] No popup detected; using current tab as builder.", flush=True)
            builder = page
        await builder.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(1)
        builder_url = builder.url
        print(f"   [create_index] Builder opened at URL: {builder_url}", flush=True)
        print("   [create_index] Builder opened. Hybrid + RagFileUDMO... [baseline_resilient]", flush=True)
        searchbox = builder.get_by_role("searchbox", name="Search data model objects…")
        try:
            await searchbox.wait_for(state="visible", timeout=7000)
            print("   [create_index] searchbox visible without Hybrid click.", flush=True)
        except Exception:
            hybrid_candidates = [
                builder.get_by_role("radio", name=re.compile("hybrid", re.IGNORECASE)).first,
                builder.get_by_role("button", name=re.compile("hybrid", re.IGNORECASE)).first,
                builder.get_by_text("Hybrid search", exact=False).first,
                builder.get_by_text("Hybrid Search", exact=False).first,
            ]
            hybrid_clicked = False
            for cand in hybrid_candidates:
                try:
                    if await cand.count() > 0:
                        await cand.first.wait_for(state="visible", timeout=4000)
                        await cand.first.click(timeout=6000)
                        hybrid_clicked = True
                        break
                except Exception:
                    pass
            if not hybrid_clicked:
                js_clicked = await builder.evaluate("""() => {
                    const roots = [document];
                    const seen = new Set([document]);
                    const stack = [document];
                    while (stack.length) {
                        const root = stack.pop();
                        if (!root || !root.querySelectorAll) continue;
                        root.querySelectorAll('*').forEach((el) => {
                            if (el.shadowRoot && !seen.has(el.shadowRoot)) {
                                seen.add(el.shadowRoot);
                                roots.push(el.shadowRoot);
                                stack.push(el.shadowRoot);
                            }
                        });
                    }
                    const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        const s = window.getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
                    };
                    const hybridish = [];
                    for (const r of roots) {
                        if (!r.querySelectorAll) continue;
                        r.querySelectorAll('button,[role="button"],[role="radio"],label,span,div').forEach((el) => {
                            const t = ((el.innerText || el.textContent || '') + '').trim().toLowerCase();
                            if (t.includes('hybrid')) hybridish.push(el);
                        });
                    }
                    for (const el of hybridish) {
                        if (!isVisible(el)) continue;
                        try { el.click(); return true; } catch (_) {}
                    }
                    return false;
                }""")
                print(f"   [create_index] hybrid js_click={js_clicked}", flush=True)
                await asyncio.sleep(0.6)
            searchbox = builder.get_by_role("searchbox", name="Search data model objects…")
            broad_search = builder.locator(
                "input[placeholder*='Search data model objects']:not([tabindex='-1']), "
                "input[placeholder*='Search Data Model Objects']:not([tabindex='-1']), "
                "input[type='search']:not([tabindex='-1']), "
                "[role='searchbox']:not([tabindex='-1'])"
            ).first
            try:
                await searchbox.wait_for(state="visible", timeout=10000)
                await searchbox.fill("rag")
            except Exception:
                try:
                    await broad_search.wait_for(state="visible", timeout=10000)
                    await broad_search.fill("rag")
                except Exception:
                    # Last-resort: locate a visible searchable input in DOM/shadow DOM and set value via JS.
                    js_search_filled = await builder.evaluate("""() => {
                        const roots = [document];
                        const seen = new Set([document]);
                        const stack = [document];
                        while (stack.length) {
                            const root = stack.pop();
                            if (!root || !root.querySelectorAll) continue;
                            root.querySelectorAll('*').forEach((el) => {
                                if (el.shadowRoot && !seen.has(el.shadowRoot)) {
                                    seen.add(el.shadowRoot);
                                    roots.push(el.shadowRoot);
                                    stack.push(el.shadowRoot);
                                }
                            });
                        }
                        const isVisible = (el) => {
                            const r = el.getBoundingClientRect();
                            const s = window.getComputedStyle(el);
                            return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
                        };
                        for (const r of roots) {
                            if (!r.querySelectorAll) continue;
                            const candidates = r.querySelectorAll("input[type='search'], input[placeholder*='Search data model objects'], input[placeholder*='Search Data Model Objects'], [role='searchbox']");
                            for (const el of candidates) {
                                const tabindex = (el.getAttribute('tabindex') || '').trim();
                                if (tabindex === '-1') continue;
                                if (!isVisible(el)) continue;
                                try {
                                    el.focus();
                                    el.value = 'rag';
                                    el.dispatchEvent(new Event('input', { bubbles: true }));
                                    el.dispatchEvent(new Event('change', { bubbles: true }));
                                    return true;
                                } catch (_) {}
                            }
                        }
                        return false;
                    }""")
                    print(f"   [create_index] js search fill fallback={js_search_filled}", flush=True)
                    if not js_search_filled:
                        # Some org variants do not expose a usable searchbox in this step.
                        # Continue and attempt direct row selection below.
                        page_title = await builder.title()
                        page_url = builder.url
                        print(f"   [create_index] ⚠️ No usable searchbox found; proceeding with direct DMO row lookup.", flush=True)
                        print(f"   [create_index] DIAG: Page title='{page_title}', URL={page_url}", flush=True)
            await asyncio.sleep(0.3)
        else:
            await searchbox.fill("rag")
        await asyncio.sleep(2.5)
        row_with_dmo = builder.locator("tr,li,div,[role='row']").filter(has_text=re.compile(r"rag\\s*file\\s*udmo|ragfileudmo", re.I))
        row_selected_via_js = False
        row_selected_via_locator = False
        row_selected_via_keyboard = False
        try:
            await row_with_dmo.first.wait_for(state="visible", timeout=15000)
        except Exception:
            # Fallback for variants that render rows in non-table containers or shadow roots.
            js_row_clicked = await builder.evaluate("""() => {
                const roots = [document];
                const seen = new Set([document]);
                const stack = [document];
                while (stack.length) {
                    const root = stack.pop();
                    if (!root || !root.querySelectorAll) continue;
                    root.querySelectorAll('*').forEach((el) => {
                        if (el.shadowRoot && !seen.has(el.shadowRoot)) {
                            seen.add(el.shadowRoot);
                            roots.push(el.shadowRoot);
                            stack.push(el.shadowRoot);
                        }
                    });
                }
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
                };
                for (const r of roots) {
                    if (!r.querySelectorAll) continue;
                    const candidates = r.querySelectorAll("tr,li,div,[role='row']");
                    for (const el of candidates) {
                        const txt = ((el.innerText || el.textContent || "") + "").toLowerCase();
                        if (!(txt.includes("ragfileudmo") || txt.includes("rag file udmo") || (txt.includes("rag") && txt.includes("udmo")))) continue;
                        if (!isVisible(el)) continue;
                        const radio = el.querySelector("input[type='radio'], [role='radio'], label, .slds-radio__label");
                        try {
                            (radio || el).click();
                            return true;
                        } catch (_) {}
                    }
                }
                return false;
            }""")
            print(f"   [create_index] js row select fallback={js_row_clicked}", flush=True)
            if not js_row_clicked:
                print("   [create_index] ⚠️ RagFileUDMO row not directly selectable; attempting locator row/radio selection next.", flush=True)
            # IMPORTANT: only treat JS row selection as successful when it actually clicked.
            row_selected_via_js = bool(js_row_clicked)
        if not row_selected_via_js:
            try:
                await row_with_dmo.locator("label.slds-radio__label, .slds-radio__label").first.click(timeout=12000)
                # CRITICAL: Wait for Lightning to process the selection (headless mode needs extra time)
                await asyncio.sleep(1.5)
                row_selected_via_locator = True
            except Exception:
                try:
                    await row_with_dmo.locator("input[type='radio'], [role='radio']").first.click(force=True, timeout=8000)
                    # CRITICAL: Wait for Lightning to process the selection (headless mode needs extra time)
                    await asyncio.sleep(1.5)
                    row_selected_via_locator = True
                except Exception:
                    print("   [create_index] ⚠️ Locator row click failed; attempting Next fallback.", flush=True)
        # In some Salesforce datatable variants, Playwright locator click cannot focus/select the row.
        # Use a JS fallback that traverses shadow roots and performs focus + keyboard activation in-page.
        if not (row_selected_via_js or row_selected_via_locator):
            row_selected_via_keyboard = await builder.evaluate("""() => {
                const roots = [document];
                const seen = new Set([document]);
                const stack = [document];
                while (stack.length) {
                    const root = stack.pop();
                    if (!root || !root.querySelectorAll) continue;
                    root.querySelectorAll('*').forEach((el) => {
                        if (el.shadowRoot && !seen.has(el.shadowRoot)) {
                            seen.add(el.shadowRoot);
                            roots.push(el.shadowRoot);
                            stack.push(el.shadowRoot);
                        }
                    });
                }
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
                };
                const isRagRow = (el) => {
                    const txt = ((el.innerText || el.textContent || "") + "").toLowerCase();
                    return txt.includes("ragfileudmo") || txt.includes("rag file udmo") || (txt.includes("rag") && txt.includes("udmo"));
                };
                const press = (target, key) => {
                    try {
                        target.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
                        target.dispatchEvent(new KeyboardEvent('keyup', { key, bubbles: true }));
                    } catch (_) {}
                };
                for (const r of roots) {
                    if (!r.querySelectorAll) continue;
                    const rows = r.querySelectorAll("tr,li,div,[role='row']");
                    for (const row of rows) {
                        if (!isRagRow(row) || !isVisible(row)) continue;
                        const clickable = row.querySelector("input[type='radio'], [role='radio'], label, .slds-radio__label") || row;
                        try { clickable.click(); } catch (_) {}
                        try { row.focus(); } catch (_) {}
                        press(row, 'Enter');
                        press(row, ' ');
                        const checkedInside = row.querySelector("input[type='radio']:checked, [role='radio'][aria-checked='true']");
                        const ariaSelected = row.getAttribute("aria-selected") === "true";
                        if (checkedInside || ariaSelected) return true;
                    }
                }
                return false;
            }""")
            print(f"   [create_index] js keyboard row selection fallback={row_selected_via_keyboard}", flush=True)

        # CRITICAL: Give Lightning extra time to process selection in headless mode, then verify
        # Retry verification up to 3 times with delays (Lightning may need time to update DOM)
        dmo_selected_confirmed = False
        for verify_attempt in range(3):
            if verify_attempt > 0:
                await asyncio.sleep(1.0)  # Wait before retry
            dmo_selected_confirmed = await builder.evaluate("""() => {
            const roots = [document];
            const seen = new Set([document]);
            const stack = [document];
            while (stack.length) {
                const root = stack.pop();
                if (!root || !root.querySelectorAll) continue;
                root.querySelectorAll('*').forEach((el) => {
                    if (el.shadowRoot && !seen.has(el.shadowRoot)) {
                        seen.add(el.shadowRoot);
                        roots.push(el.shadowRoot);
                        stack.push(el.shadowRoot);
                    }
                });
            }
            const isRagRow = (el) => {
                const txt = ((el.innerText || el.textContent || "") + "").toLowerCase();
                return txt.includes("ragfileudmo") || txt.includes("rag file udmo") || (txt.includes("rag") && txt.includes("udmo"));
            };
            for (const r of roots) {
                if (!r.querySelectorAll) continue;
                const rows = r.querySelectorAll("tr,li,div,[role='row']");
                for (const row of rows) {
                    if (!isRagRow(row)) continue;
                    const ariaSelected = row.getAttribute("aria-selected") === "true";
                    const checkedInside = row.querySelector("input[type='radio']:checked, [role='radio'][aria-checked='true']");
                    if (ariaSelected || checkedInside) return true;
                }
                // Some variants render radio controls outside strict row semantics.
                const checked = r.querySelectorAll("input[type='radio']:checked, [role='radio'][aria-checked='true']");
                for (const c of checked) {
                    const host = c.closest("tr,li,div,[role='row']") || c.parentElement;
                    const txt = ((host?.innerText || host?.textContent || c.innerText || c.textContent || "") + "").toLowerCase();
                    if (txt.includes("ragfileudmo") || txt.includes("rag file udmo") || (txt.includes("rag") && txt.includes("udmo"))) {
                        return true;
                    }
                }
            }
            return false;
        }""")
            if dmo_selected_confirmed:
                print(f"   [create_index] ✅ DMO selection verified after {verify_attempt + 1} attempt(s)", flush=True)
                break
            elif verify_attempt < 2:
                print(f"   [create_index] ⚠️  DMO not confirmed yet, retrying verification (attempt {verify_attempt + 2}/3)...", flush=True)

        print(
            f"   [create_index] dmo selection confirmed={dmo_selected_confirmed} "
            f"(js={row_selected_via_js}, locator={row_selected_via_locator}, keyboard={row_selected_via_keyboard})",
            flush=True,
        )
        async def _click_next_resilient(stage_label: str) -> None:
            try:
                await builder.get_by_role("button", name="Next").click(timeout=10000)
                return
            except Exception:
                pass
            try:
                await builder.locator("button:has-text('Next'), [role='button']:has-text('Next')").first.click(timeout=8000)
                return
            except Exception:
                pass
            js_next_clicked = await builder.evaluate("""() => {
                const roots = [document];
                const seen = new Set([document]);
                const stack = [document];
                while (stack.length) {
                    const root = stack.pop();
                    if (!root || !root.querySelectorAll) continue;
                    root.querySelectorAll('*').forEach((el) => {
                        if (el.shadowRoot && !seen.has(el.shadowRoot)) {
                            seen.add(el.shadowRoot);
                            roots.push(el.shadowRoot);
                            stack.push(el.shadowRoot);
                        }
                    });
                }
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
                };
                for (const r of roots) {
                    if (!r.querySelectorAll) continue;
                    const candidates = r.querySelectorAll("button,[role='button'],lightning-button,lightning-button button");
                    for (const el of candidates) {
                        const txt = ((el.innerText || el.textContent || '') + '').trim().toLowerCase();
                        if (txt !== 'next') continue;
                        if (!isVisible(el)) continue;
                        const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true';
                        if (disabled) continue;
                        try { el.click(); return true; } catch (_) {}
                    }
                }
                return false;
            }""")
            print(f"   [create_index] next js_click({stage_label})={js_next_clicked}", flush=True)
            if js_next_clicked:
                return
            raise RuntimeError(f"Unable to click Next at stage '{stage_label}'")

        await asyncio.sleep(0.5)
        try:
            if row_selected_via_js or row_selected_via_locator or row_selected_via_keyboard or dmo_selected_confirmed:
                await _click_next_resilient("post-dmo-selection")
            else:
                # Last-resort: if the UI preselected a default object, Next can still work
                # after a brief render delay.
                await asyncio.sleep(1.0)
                await _click_next_resilient("post-dmo-selection-preselected")
        except Exception as next_err:
            # Some UI variants auto-advance to parser step without exposing a clickable Next.
            parser_step_visible = False
            for parser_label in ["LLM-based Parser", "LLM Parser"]:
                try:
                    if await builder.get_by_text(parser_label, exact=True).first.is_visible():
                        parser_step_visible = True
                        break
                except Exception:
                    pass
            if not parser_step_visible:
                raise next_err
            print("   [create_index] ⚠️ Next unavailable but parser step already visible; continuing.", flush=True)
        await asyncio.sleep(1)
        for parser_label in ["LLM-based Parser", "LLM Parser"]:
            loc = builder.get_by_text(parser_label, exact=True)
            if await loc.is_visible():
                await loc.click()
                break
        else:
            await builder.get_by_text("LLM", exact=False).first.click()
        await asyncio.sleep(0.5)
        textarea = builder.locator("textarea[name='prompt']").first
        await textarea.wait_for(state="visible", timeout=10000)
        await textarea.fill(parser_prompt)
        # Two Next clicks to reach the chunking configuration step.
        await _click_next_resilient("to-chunking-1")
        await asyncio.sleep(3)
        await _click_next_resilient("to-chunking-2")
        await asyncio.sleep(3)

        pdf_row = builder.locator("tr").filter(has_text="pdf").first
        await pdf_row.wait_for(state="visible", timeout=15000)
        pdf_row_text = await pdf_row.inner_text()
        print(f"   [create_index] pdf_row text: '{pdf_row_text}'", flush=True)

        # Dump the pdf row's inner HTML to understand its structure
        pdf_row_html = await pdf_row.evaluate("el => el.innerHTML.substring(0, 1500)")
        print(f"   [create_index] pdf_row HTML: {pdf_row_html}", flush=True)

        chunk_inputs = builder.locator("input[type='number'], input[inputmode='numeric'], [role='spinbutton']")

        # Check if chunking inputs are already visible (component may have auto-loaded)
        print(f"   [create_index] Checking for pre-loaded chunking inputs...", flush=True)
        await asyncio.sleep(3)
        initial_count = await chunk_inputs.count()
        print(f"   [create_index] Initial chunk input count: {initial_count}", flush=True)

        if initial_count < 2:
            # Strategy 1: Click the lightning-button-icon in the 3rd td (settings/expand)
            settings_btn = pdf_row.locator("lightning-button-icon").first
            if await settings_btn.count() > 0:
                print(f"   [create_index] Strategy 1: clicking lightning-button-icon (settings)", flush=True)
                try:
                    await settings_btn.click(timeout=5000)
                    await asyncio.sleep(3)
                except Exception as e:
                    print(f"   [create_index] Strategy 1 click failed: {e}", flush=True)

        if await chunk_inputs.count() < 2:
            # Strategy 2: Click the chunking-strategy component itself
            chunking_comp = pdf_row.locator("runtime_cdp-search-index-chunking-strategy").first
            if await chunking_comp.count() > 0:
                print(f"   [create_index] Strategy 2: clicking chunking-strategy component", flush=True)
                try:
                    await chunking_comp.click(timeout=5000)
                    await asyncio.sleep(3)
                except Exception as e:
                    print(f"   [create_index] Strategy 2 click failed: {e}", flush=True)

        if await chunk_inputs.count() < 2:
            # Strategy 3: Click the cell-edit button (pencil icon)
            edit_btn = pdf_row.locator("button.slds-cell-edit__button, button[title='pdf']").first
            if await edit_btn.count() > 0:
                print(f"   [create_index] Strategy 3: clicking cell-edit button", flush=True)
                try:
                    await edit_btn.click(timeout=5000)
                    await asyncio.sleep(3)
                except Exception as e:
                    print(f"   [create_index] Strategy 3 click failed: {e}", flush=True)

        if await chunk_inputs.count() < 2:
            # Strategy 4: Use JS to search inside shadow DOMs for any numeric inputs
            print(f"   [create_index] Strategy 4: JS shadow DOM search", flush=True)
            shadow_inputs = await builder.evaluate("""() => {
                function findInShadow(root, results) {
                    if (!root) return;
                    const inputs = root.querySelectorAll('input[type="number"], input[inputmode="numeric"], [role="spinbutton"]');
                    inputs.forEach(i => results.push({tag: i.tagName, type: i.type, name: i.name, id: i.id}));
                    root.querySelectorAll('*').forEach(el => {
                        if (el.shadowRoot) findInShadow(el.shadowRoot, results);
                    });
                }
                const results = [];
                findInShadow(document, results);
                return results;
            }""")
            print(f"   [create_index] Shadow DOM inputs found: {shadow_inputs}", flush=True)

        if await chunk_inputs.count() < 2:
            # Strategy 5: Click the pdf_row's second td (where chunking component is)
            second_td = pdf_row.locator("td").nth(1)
            if await second_td.count() > 0:
                print(f"   [create_index] Strategy 5: clicking second td (chunking area)", flush=True)
                try:
                    await second_td.click(timeout=5000)
                    await asyncio.sleep(3)
                except Exception as e:
                    print(f"   [create_index] Strategy 5 click failed: {e}", flush=True)

        final_count = await chunk_inputs.count()
        print(f"   [create_index] After all strategies: {final_count} chunk input(s) found", flush=True)
        if final_count < 2:
            all_inputs = await builder.locator("input").count()
            all_btns = await builder.locator("button").count()
            spin_count = await builder.locator("[role='spinbutton']").count()
            number_count = await builder.locator("input[type='number']").count()
            numeric_count = await builder.locator("input[inputmode='numeric']").count()
            print(f"   [create_index] Page: {all_inputs} inputs, {all_btns} buttons, spin={spin_count} number={number_count} numeric={numeric_count}", flush=True)
            raise RuntimeError(f"Chunking inputs not found after 5 expand strategies. pdf_row text='{pdf_row_text}'")

        await chunk_inputs.nth(0).wait_for(state="visible", timeout=10000)
        # Max Tokens
        max_tokens_input = chunk_inputs.nth(0)
        await max_tokens_input.click()
        await max_tokens_input.fill("")
        await max_tokens_input.type("8000")
        await max_tokens_input.press("Tab")
        await asyncio.sleep(0.3)
        # Overlap Tokens — use click+clear+type+Tab to trigger change detection
        overlap_input = chunk_inputs.nth(1)
        await overlap_input.click()
        await overlap_input.fill("")
        await overlap_input.type("512")
        await overlap_input.press("Tab")
        await asyncio.sleep(0.3)
        save_btn = builder.get_by_role("table").get_by_role("button", name="Save")
        if await save_btn.is_visible():
            await save_btn.click()
            await asyncio.sleep(1)
        await builder.get_by_role("button", name="Next").click()
        await asyncio.sleep(1)
        emb_dropdown = builder.get_by_label("Select embedding Model").or_(builder.get_by_label("Select embedding Models"))
        if await emb_dropdown.count() > 0:
            await emb_dropdown.first.click()
            await asyncio.sleep(0.3)
            await builder.get_by_text("Salesforce Embedding V2 Small", exact=True).click()
        else:
            for model_name in ["Salesforce Embedding V2 Small", "Embedding V2 Small", "V2 Small"]:
                loc = builder.get_by_text(model_name, exact=False)
                if await loc.count() > 0:
                    await loc.first.click()
                    break
        await asyncio.sleep(0.3)
        await builder.get_by_role("button", name="Next").click()
        await asyncio.sleep(1)
        await builder.get_by_role("button", name="Next").click()
        await asyncio.sleep(1)
        await builder.get_by_role("button", name="Next").click()
        await asyncio.sleep(1)
        for name_sel in [("textbox", "Search Index Configuration Name"), ("textbox", "Configuration Name"), ("textbox", "Name")]:
            inp = builder.get_by_role(name_sel[0], name=name_sel[1])
            if await inp.count() > 0:
                await inp.first.fill(index_name, timeout=10000)
                break
        else:
            await builder.locator("input[type='text']").last.fill(index_name, timeout=10000)
        await builder.get_by_role("button", name="Save").click()
        redirect_ok = False
        try:
            await builder.wait_for_url("**/DataSemanticSearch/**", timeout=90000)
            redirect_ok = True
        except Exception:
            try:
                await builder.wait_for_url("**searchIndex**", timeout=15000)
                redirect_ok = True
            except Exception:
                pass
        if not redirect_ok:
            builder_url = builder.url or ""
            print(f"   ❌ Save did NOT redirect to detail page — index was NOT created. URL: {builder_url[:120]}", flush=True)
            print("   ⚠️ Common cause: index name starts with a digit (Salesforce requires letter prefix)", flush=True)
            await browser.close()
            return (None, None)
        full_name = _index_full_name(index_name)
        await browser.close()
        if skip_api_lookup:
            print("   [create_index] Save complete; skipping API lookup (UI-only mode).", flush=True)
            return (None, full_name)

        print("   [create_index] Save complete; looking up index ID via API...", flush=True)
        await asyncio.sleep(3)
        from salesforce_api import get_salesforce_credentials, find_index_id_by_name
        if not access_token:
            _, access_token = get_salesforce_credentials(username=username, password=password, instance_url=instance_url)
        index_id = find_index_id_by_name(instance_url, access_token, index_name, max_attempts=8, retry_delay_seconds=10)
        if not index_id:
            print("   ⚠️ API lookup failed: index not found in list_indexes after create (8 attempts / 80s)", flush=True)
        if index_id:
            state_dir.mkdir(parents=True, exist_ok=True)
            fn = state_dir / (f"run_{run_id}_latest_index.json" if run_id else "latest_index.json")
            fn.write_text(json.dumps({"indexId": index_id, "indexName": full_name}, indent=2), encoding="utf-8")
            print(f"   Saved {fn.name}", flush=True)
        return (index_id, full_name if index_id else None)


async def _create_retriever_ui(username, password, instance_url, index_name, state_dir, run_id, headless, should_abort):
    """Create Retriever via Playwright. Returns (retriever_display_name, activate_clicked)."""
    base = instance_url.rstrip("/")
    login_url = "https://login.salesforce.com" if "salesforce.com" in base else base
    retriever_name = f"{index_name} Retriever {datetime.now().strftime('%Y-%m-%d_%H%M%S')}"
    delay = 0.5

    def _valid(label, value):
        v = (value or "").strip().lower()
        if not v or "select a field" in v:
            return False
        if label == "Chunk":
            return "chunk" in v or "grounding source" in v or "> chunk" in v
        if label == "SourceRecordId":
            return "source record id" in v or "reference id" in v
        if label == "DataSource":
            return "data source" in v and "object" not in v
        if label == "DataSourceObject":
            return "data source ob" in v
        return True

    async def _click_add(frame):
        for loc in [frame.locator("text=Fields to Return").locator("..").locator("text=Add Field"), frame.locator("text=Add Field")]:
            try:
                if await loc.count() > 0:
                    await loc.first.scroll_into_view_if_needed(timeout=3000)
                    await loc.first.click(timeout=5000, force=True)
                    await asyncio.sleep(0.5)
                    return True
            except Exception:
                continue
        return False

    async with async_playwright() as p:
        launch_args = {'slow_mo': 100}
        if headless:
            # Required args for headless Chromium on Heroku/Linux containers
            launch_args['args'] = [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-software-rasterizer',
                '--disable-extensions'
            ]
        browser = await p.chromium.launch(headless=headless, **launch_args)
        storage_state = None
        try:
            auth_b64 = os.getenv("SF_AUTH_STATE_B64", "").strip()
            if auth_b64:
                storage_state = json.loads(base64.b64decode(auth_b64).decode("utf-8"))
                print("   [create_retriever] Found SF_AUTH_STATE_B64, attempting session restore...", flush=True)
        except Exception as e:
            print(f"   [create_retriever] ⚠️ Could not decode SF_AUTH_STATE_B64: {e}", flush=True)
            storage_state = None
        context = await browser.new_context(viewport={"width": 1280, "height": 720}, storage_state=storage_state)
        page = await context.new_page()
        if should_abort():
            await browser.close()
            return ("", False)
        await page.goto(login_url, wait_until="networkidle", timeout=60000)
        await page.get_by_role("textbox", name="Username").fill(username)
        await page.get_by_role("textbox", name="Password").fill(password)
        await page.get_by_role("button", name="Log In").click()
        await page.wait_for_load_state("domcontentloaded", timeout=60000)
        await asyncio.sleep(1)
        current_url = page.url
        if _is_mfa_or_verification_url(current_url):
            page_text = await page.evaluate("() => document.body ? document.body.innerText.substring(0, 600) : 'no-body'")
            print(f"   [create_retriever] MFA/verification page detected.\n   URL: {current_url}\n   Page text: {page_text}", flush=True)
            resumed = await _wait_for_mfa_code_and_resume(page, run_id, should_abort, timeout_seconds=1800)
            if not resumed:
                await browser.close()
                return ("", False)
        elif not _is_authenticated_url(current_url):
            print(f"   ❌ Login did not reach authenticated URL. Current URL: {current_url}", flush=True)
            await browser.close()
            return ("", False)
        if should_abort():
            await browser.close()
            return ("", False)
        await page.get_by_role("button", name="Show more navigation items").click()
        await page.get_by_role("menuitem", name="Einstein Studio").click()
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        await asyncio.sleep(1.5)
        await page.get_by_role("link", name="Retrievers").click()
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        await asyncio.sleep(1)
        await page.get_by_role("button", name="New Retriever").click()
        await asyncio.sleep(0.5)
        async with page.expect_popup(timeout=45000) as popup_info:
            await page.get_by_role("button", name="Next").click()
        popup = await popup_info.value
        await popup.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(1)
        await popup.get_by_text("Data Cloud", exact=True).click()
        await asyncio.sleep(0.3)
        await popup.get_by_role("button", name="Next").click()
        await asyncio.sleep(0.5)
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
        await popup.get_by_role("combobox", name="Data model object's search").click()
        idx_option = popup.get_by_text(index_name, exact=False).first
        await idx_option.wait_for(state="visible", timeout=30000)
        await idx_option.click()
        await asyncio.sleep(0.3)
        await popup.get_by_role("button", name="Next").click()
        await asyncio.sleep(0.5)
        await popup.get_by_role("button", name="Next").click()
        await asyncio.sleep(1)
        cfg_frame = popup
        for f in [popup] + list(popup.frames):
            try:
                if await f.locator("text=Add Field").count() > 0:
                    cfg_frame = f
                    break
            except Exception:
                continue
        await cfg_frame.locator("text=Add Field").first.wait_for(state="visible", timeout=15000)
        await asyncio.sleep(0.5)
        field_specs = [
            ("Chunk", "chunk", "Chunk", False),
            ("SourceRecordId", "source record", "Source Record Id", False),
            ("DataSource", "data source", "Data Source", False),
            ("DataSourceObject", "data source object", "Data Source Object", True),
        ]
        for fi, (label_text, filter_text, display_label, check_cb) in enumerate(field_specs):
            if should_abort():
                await browser.close()
                return ("", False)
            empty_labels = cfg_frame.locator("input[placeholder*='Enter a field label']")
            label_input = empty_labels.last if await empty_labels.count() > 0 else cfg_frame.get_by_role("textbox", name="Field Label").last
            row = label_input.locator("xpath=ancestor::*[.//input[contains(@placeholder,'Select a field')]][1]")
            await label_input.wait_for(state="visible", timeout=15000)
            combo = row.locator("input[placeholder*='Select a field']").first
            await label_input.fill(display_label, timeout=10000)
            await asyncio.sleep(delay)
            await combo.click()
            await asyncio.sleep(0.3)
            await combo.fill(filter_text)
            await asyncio.sleep(delay * 2)
            dropdown = cfg_frame.locator(".slds-dropdown:visible, [role='listbox']:visible").last
            try:
                await dropdown.wait_for(state="visible", timeout=8000)
            except Exception:
                dropdown = cfg_frame.locator(".slds-dropdown, [role='listbox']").last
            for _loc in [dropdown.get_by_text("Related Attributes", exact=True), dropdown.get_by_role("option", name="Related Attributes")]:
                try:
                    if await _loc.count() > 0:
                        await _loc.first.click(timeout=6000)
                        break
                except Exception:
                    continue
            await asyncio.sleep(delay)
            try:
                chunk_node = dropdown.get_by_text(f"{index_name} chunk", exact=False).or_(dropdown.get_by_text("chunk", exact=False))
                if await chunk_node.count() > 0:
                    await chunk_node.first.click(timeout=3000)
            except Exception:
                pass
            await asyncio.sleep(delay)
            dd_visible = await cfg_frame.locator(".slds-dropdown:visible, [role='listbox']:visible").count()
            if dd_visible > 0:
                dropdown = cfg_frame.locator(".slds-dropdown:visible, [role='listbox']:visible").last
                for loc in [dropdown.get_by_text(display_label, exact=True), dropdown.get_by_role("option", name=display_label)]:
                    try:
                        ct = await loc.count()
                        if ct > 0:
                            await loc.last.click(timeout=4000)
                            print(f"   [retriever] Field '{display_label}' selected (count={ct})", flush=True)
                            break
                    except Exception:
                        continue
                await asyncio.sleep(delay)
            else:
                try:
                    await combo.click(timeout=3000)
                    await asyncio.sleep(delay)
                    await combo.fill(filter_text)
                    await asyncio.sleep(delay * 2)
                    dropdown = cfg_frame.locator(".slds-dropdown:visible, [role='listbox']:visible").last
                    await dropdown.wait_for(state="visible", timeout=5000)
                    for loc in [dropdown.get_by_text(display_label, exact=True), dropdown.get_by_role("option", name=display_label)]:
                        try:
                            if await loc.count() > 0:
                                await loc.last.click(timeout=4000)
                                print(f"   [retriever] Field '{display_label}' selected after reopen", flush=True)
                                break
                        except Exception:
                            continue
                    await asyncio.sleep(delay)
                except Exception as e:
                    print(f"   [retriever] WARNING: Field '{display_label}' selection failed: {e}", flush=True)
            if check_cb:
                try:
                    await row.locator(".slds-checkbox_faux").first.click(timeout=3000)
                except Exception:
                    pass
            if fi < len(field_specs) - 1:
                await _click_add(cfg_frame)
            await asyncio.sleep(delay)
        for loc in [cfg_frame.get_by_role("switch", name="Enable Citations"), cfg_frame.get_by_text("Enable Citations").locator("..").locator("input, [role='switch']")]:
            try:
                if await loc.count() > 0:
                    await loc.first.click(timeout=2000)
                    break
            except Exception:
                continue
        await cfg_frame.get_by_role("button", name="Next").or_(popup.get_by_role("button", name="Next")).first.click(force=True)
        await asyncio.sleep(2.5)
        if "configureretriever" in (popup.url or "").lower():
            await popup.keyboard.press("Escape")
            await asyncio.sleep(0.3)
        # Find best frame for Einstein Studio (content may be in iframe)
        def _get_root():
            frames = list(getattr(popup, "frames", []))
            if len(frames) > 1:
                for f in frames[1:]:
                    if "Retriever" in (f.url or "") or "einstein" in (f.url or "").lower():
                        return f
            return popup
        ctx = _get_root()
        pages_before = set(context.pages) if hasattr(context, "pages") else set()
        save_clicked = False
        for target in [ctx, popup, cfg_frame] + [f for f in list(getattr(popup, "frames", [])) if f != popup]:
            for btn_loc in [
                target.get_by_role("button", name="Save"),
                target.get_by_role("button", name="Save Retriever"),
                target.get_by_text("Save", exact=True),
                target.get_by_text("Save Retriever", exact=True),
                target.locator("button").filter(has_text="Save"),
                target.locator("lightning-button:has-text('Save') >> button"),
            ]:
                try:
                    if await btn_loc.count() > 0:
                        await btn_loc.first.scroll_into_view_if_needed(timeout=3000)
                        await btn_loc.first.click(timeout=8000)
                        save_clicked = True
                        break
                except Exception:
                    continue
            if save_clicked:
                break
        await asyncio.sleep(2)
        candidates = [popup] + (list(set(context.pages) - pages_before) if hasattr(context, "pages") else [popup])
        # Wait for modal to be interactive (Save button enabled, up to 30s)
        modal_ready = False
        for _ in range(60):
            await asyncio.sleep(0.5)
            for pg in candidates:
                for target in [pg] + list(getattr(pg, "frames", [])):
                    try:
                        dialog_save = target.get_by_role("dialog").get_by_role("button", name="Save")
                        if await dialog_save.count() > 0:
                            dis = await dialog_save.first.get_attribute("disabled")
                            if dis is None or dis == "false":
                                modal_ready = True
                                break
                    except Exception:
                        pass
                if modal_ready:
                    break
            if modal_ready:
                break
        name_locators = [
            ("textbox Name", lambda t: t.get_by_role("textbox", name="Name")),
            ("textbox * Name", lambda t: t.get_by_role("textbox", name=re.compile(r"^[\s*]*Name$", re.I))),
            ("label Retriever Name", lambda t: t.get_by_label("Retriever Name")),
            ("label Name", lambda t: t.get_by_label("Name")),
            ("dialog input", lambda t: t.get_by_role("dialog").locator("input[type='text']").first),
            ("dialog input first", lambda t: t.locator("[role='dialog'] input").first),
        ]
        name_filled = False
        max_fill_attempts = 3
        if save_clicked:
            for fill_attempt in range(max_fill_attempts):
                for pg in candidates:
                    for target in [pg] + list(getattr(pg, "frames", [])):
                        for _desc, loc_fn in name_locators:
                            try:
                                loc = loc_fn(target)
                                if await loc.count() > 0:
                                    inp = loc.first
                                    await inp.wait_for(state="visible", timeout=3000)
                                    await inp.click()
                                    await asyncio.sleep(0.15)
                                    mod = "Meta" if platform.system() == "Darwin" else "Control"
                                    await inp.press(f"{mod}+a")
                                    await inp.fill(retriever_name, timeout=5000)
                                    got = (await inp.input_value()).strip()
                                    if got != retriever_name:
                                        raise RuntimeError(f"Fill attempt {fill_attempt + 1}: expected {retriever_name!r}, got {got!r}")
                                    name_filled = True
                                    break
                            except Exception:
                                continue
                        if name_filled:
                            break
                    if name_filled:
                        break
                if name_filled:
                    break
                await asyncio.sleep(1)
            if not name_filled:
                for pg in candidates:
                    try:
                        filled = await pg.evaluate("""(name) => {
                            function* walk(root, d) {
                                if (!root || d > 18) return;
                                for (const el of root.querySelectorAll('*')) {
                                    yield el;
                                    if (el.shadowRoot) yield* walk(el.shadowRoot, d + 1);
                                }
                            }
                            for (const el of walk(document, 0)) {
                                const lbl = (el.getAttribute('data-label') || el.getAttribute('aria-label') || el.getAttribute('label') || '').toLowerCase();
                                const ph = (el.getAttribute('placeholder') || '').toLowerCase();
                                if (lbl.includes('name') || ph.includes('retriever')) {
                                    const inp = el.tagName === 'INPUT' ? el : el.querySelector('input');
                                    if (inp) {
                                        inp.focus();
                                        inp.value = name;
                                        inp.dispatchEvent(new Event('input', { bubbles: true }));
                                        inp.dispatchEvent(new Event('change', { bubbles: true }));
                                        return true;
                                    }
                                }
                            }
                            return false;
                        }""", retriever_name)
                        if filled:
                            name_filled = True
                            break
                    except Exception:
                        continue
        if not name_filled:
            await browser.close()
            raise RuntimeError("Could not fill Retriever Name in popup.")
        for pg in [popup] + list(getattr(popup, "frames", [])):
            try:
                ds = pg.get_by_role("dialog").get_by_role("button", name="Save")
                if await ds.count() > 0:
                    await ds.first.click(timeout=5000)
                    break
            except Exception:
                pass
        for _ in range(30):
            if "configureretriever" not in (popup.url or "").lower() and "review" not in (popup.url or "").lower():
                break
            await asyncio.sleep(0.5)
        state_dir.mkdir(parents=True, exist_ok=True)
        fn = state_dir / (f"run_{run_id}_latest_retriever.json" if run_id else "latest_retriever.json")
        fn.write_text(json.dumps({"retrieverName": retriever_name, "indexName": index_name, "createdAt": datetime.now().isoformat()}, indent=2), encoding="utf-8")
        for _ in range(20):
            for target in [popup, cfg_frame] + list(getattr(popup, "frames", [])):
                btn = target.get_by_role("button", name="Activate").or_(target.get_by_text("Activate", exact=True))
                if await btn.count() > 0 and await btn.first.is_visible():
                    break
            else:
                await asyncio.sleep(1)
                continue
            break
        activate_clicked = False
        for target in [popup, cfg_frame] + list(getattr(popup, "frames", [])):
            for btn in [target.get_by_role("button", name="Activate"), target.get_by_text("Activate", exact=True)]:
                try:
                    if await btn.count() > 0 and await btn.first.is_visible():
                        await btn.first.click(timeout=5000)
                        activate_clicked = True
                        for t in [popup] + list(getattr(popup, "frames", [])):
                            try:
                                mb = t.get_by_role("dialog").get_by_role("button", name="Activate")
                                if await mb.count() > 0:
                                    await mb.first.click(timeout=5000)
                                    break
                            except Exception:
                                pass
                        break
                except Exception:
                    continue
            if activate_clicked:
                break
        await browser.close()
        return (retriever_name, activate_clicked)


async def run_new_index_pipeline(
    username, password, instance_url, prompt_template_api_name, previous_cycle_prompt,
    state_dir, run_id=None, headless=False, index_prefix=None
):
    """
    Full Cycle 2+ pipeline: Create Index → Poll → Create Retriever → Poll retriever → Update prompt template.
    Returns (new_search_index_id, new_retriever_api_name) or (None, None) on failure/abort.
    index_prefix: pipeline-specific prefix like "Opt_P1" (defaults to legacy global name).
    """
    from salesforce_api import (
        get_salesforce_credentials, get_next_index_name, poll_index_until_ready,
        poll_retriever_until_activated, update_genai_prompt_with_retriever, find_index_id_by_name,
    )

    def should_abort():
        return bool(run_id and check_run_aborted(run_id))

    _, access_token = get_salesforce_credentials(username=username, password=password, instance_url=instance_url)
    if not index_prefix:
        raise ValueError("indexPrefix is required in YAML configuration. No hardcoded fallback.")
    if index_prefix[0].isdigit():
        raise ValueError(f"indexPrefix '{index_prefix}' starts with a digit. Salesforce developer names must start with a letter.")
    index_name = get_next_index_name(instance_url, access_token, base_name=index_prefix)
    print(f"\n   Creating Search Index: {index_name}", flush=True)
    try:
        index_id, full_index_name = await _create_search_index_ui(
            username, password, instance_url, index_name, previous_cycle_prompt,
            state_dir, run_id, headless, should_abort, access_token=access_token
        )
    except Exception as e:
        import traceback
        print(f"   ❌ Create index failed: {e}", flush=True)
        print("   Traceback:", flush=True)
        traceback.print_exc()
        latest = state_dir / (f"run_{run_id}_latest_index.json" if run_id else "latest_index.json")
        if latest.exists():
            try:
                d = json.loads(latest.read_text(encoding="utf-8"))
                idx = d.get("indexId")
                if idx:
                    print(f"   ⚠️ Partial failure: Index ID {idx} created - manual cleanup may be needed.", flush=True)
            except Exception:
                pass
        raise

    # UI save can beat API list consistency; recover by name lookup before failing step 1.
    if not index_id and full_index_name and not should_abort():
        print(f"   [create_index] index_id missing after UI save; retrying lookup by name: {full_index_name}", flush=True)
        try:
            index_id = find_index_id_by_name(
                instance_url=instance_url,
                access_token=access_token,
                index_name=full_index_name,
                max_attempts=6,
                retry_delay_seconds=5,
            )
            if index_id:
                print(f"   [create_index] recovered index_id via name lookup: {index_id}", flush=True)
        except Exception as lookup_err:
            print(f"   [create_index] lookup-by-name recovery failed: {lookup_err}", flush=True)

    if not index_id or should_abort():
        if should_abort():
            print("   ⚠️ DIAG: Pipeline stopped - run aborted (user cancelled or status changed)", flush=True)
        else:
            print("   ⚠️ DIAG: Pipeline stopped - create_search_index returned no index_id (API lookup failed)", flush=True)
        return (None, None)
    print(f"   Polling index until Ready...", flush=True)
    if not poll_index_until_ready(index_id, instance_url, access_token, run_id=run_id):
        return (None, None)
    print(f"   Creating Retriever...", flush=True)
    try:
        retriever_display_name, _ = await _create_retriever_ui(
            username, password, instance_url, full_index_name, state_dir, run_id, headless, should_abort
        )
    except Exception as e:
        print(f"   ❌ Create retriever failed: {e}", flush=True)
        print(f"   ⚠️ Index ID {index_id} - manual cleanup may be needed.", flush=True)
        raise

    if not retriever_display_name or should_abort():
        return (None, None)
    print(f"   Polling retriever until activated...", flush=True)
    retriever_api_name, retriever_label = poll_retriever_until_activated(
        instance_url, access_token, retriever_display_name, run_id=run_id
    )
    if not retriever_api_name:
        return (None, None)
    print(f"   Updating prompt template with retriever...", flush=True)
    if not update_genai_prompt_with_retriever(
        instance_url, access_token, prompt_template_api_name, retriever_api_name, retriever_label or retriever_display_name
    ):
        return (None, None)
    print(f"   ✅ Pipeline complete. New index: {index_id}, retriever: {retriever_api_name}", flush=True)
    return (index_id, retriever_api_name)


if __name__ == "__main__":
    # Parse command line arguments
    capture_network = '--capture-network' in sys.argv
    if capture_network:
        sys.argv.remove('--capture-network')
    
    # Load YAML configuration for login credentials
    yaml_path = Path(__file__).parent.parent.parent / "inputs" / "prompt_optimization_input.yaml"
    if not yaml_path.exists():
        print(f"❌ YAML config file not found: {yaml_path}")
        print("   Please ensure the YAML file exists with salesforce credentials.")
        sys.exit(1)
    
    try:
        with open(yaml_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        salesforce_config = config.get('configuration', {}).get('salesforce', {})
        username = salesforce_config.get('username')
        password = salesforce_config.get('password')
        instance_url = salesforce_config.get('instanceUrl')
        
        if not all([username, password, instance_url]):
            print("❌ Missing Salesforce credentials in YAML config.")
            print("   Required: configuration.salesforce.username, password, instanceUrl")
            sys.exit(1)
        
        print(f"✅ Loaded Salesforce credentials from YAML: {yaml_path}")
        print(f"   Username: {username}")
        print(f"   Instance: {instance_url}")
    except Exception as e:
        print(f"❌ Error loading YAML config: {e}")
        sys.exit(1)
    
    # Get search index ID from YAML config
    search_index_id = config.get('configuration', {}).get('searchIndexId')
    if not search_index_id:
        print("❌ Missing searchIndexId in YAML config.")
        print("   Required: configuration.searchIndexId")
        sys.exit(1)
    
    print(f"✅ Loaded Search Index ID from YAML: {search_index_id}")
    
    # Get screenshot flag from YAML config (default: false)
    take_screenshots = config.get('configuration', {}).get('takeScreenshots', False)
    print(f"✅ Screenshots: {'Enabled' if take_screenshots else 'Disabled (default)'}")
    
    # Parse remaining command line arguments (prompt text)
    if len(sys.argv) < 2:
        print("Usage: python3 playwright_scripts.py <new_prompt> [--capture-network]")
        print("\nExample:")
        print('  python3 playwright_scripts.py \\')
        print('    "Your new prompt text here" \\')
        print('    --capture-network')
        print("\nNote: Login credentials and search index ID are loaded from prompt_optimization_input.yaml")
        sys.exit(1)
    
    prompt_arg = sys.argv[1]
    
    # Check if it's a file path (file exists) or prompt text
    if Path(prompt_arg).is_file():
        # Read from file
        try:
            with open(prompt_arg, 'r', encoding='utf-8') as f:
                new_prompt = f.read().strip()
            print(f"📝 Loaded prompt from file: {prompt_arg} ({len(new_prompt)} characters)")
        except Exception as e:
            print(f"❌ Error reading file: {e}")
            sys.exit(1)
    else:
        # Use as prompt text directly
        new_prompt = prompt_arg
        print(f"📝 Using prompt from argument: {len(new_prompt)} characters")
    
    if len(new_prompt) > 100:
        print(f"   Preview: {new_prompt[:100]}...")
    else:
        print(f"   Full prompt: {new_prompt}")
    
    if capture_network:
        print("📡 Network capture enabled")
    
    asyncio.run(update_search_index_prompt(
        username, password, instance_url, search_index_id, new_prompt, capture_network, take_screenshots
    ))

