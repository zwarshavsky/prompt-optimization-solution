#!/usr/bin/env python3
"""
Salesforce Playwright Scripts

This module contains Playwright automation scripts for Salesforce UI interactions.
Currently includes:
- update_search_index_prompt: Updates Search Index LLM Parser Prompt via UI
  (targets the lightning-textarea component with name="prompt")
"""

import asyncio
from playwright.async_api import async_playwright
import sys
from pathlib import Path
from datetime import datetime
import subprocess
import json
import urllib.request
import yaml

async def update_search_index_prompt(
    username: str,
    password: str,
    instance_url: str,
    search_index_id: str,
    new_prompt: str,
    capture_network: bool = False,
    take_screenshots: bool = False,
    headless: bool = False,
    slow_mo: int = 0
):
    """
    Update the LLM parser prompt for a Search Index.
    
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
    
    async with async_playwright() as p:
        # Launch browser with visible window - normal size
        browser = await p.chromium.launch(
            headless=headless,
            slow_mo=slow_mo  # Delay between actions in milliseconds (0 = no delay)
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
            print("\n📊 Polling Search Index status via API (waiting for status to update to SUBMITTED)...")
            await asyncio.sleep(5)  # Initial wait before first check
            
            rebuild_attempts = 0
            max_rebuild_attempts = 3  # Limit rebuild attempts to prevent infinite loops
            
            try:
                # Get access token via SOAP authentication (from YAML or CLI fallback)
                from salesforce_api import get_salesforce_credentials
                api_instance_url, access_token = get_salesforce_credentials()
                
                import urllib.request
                api_url = f"{api_instance_url}/services/data/v65.0/ssot/search-index/{search_index_id}"
                
                # Phase 1: Wait for SUBMITTED status (confirms save was processed)
                print("   Phase 1: Waiting for status to change to SUBMITTED...")
                max_attempts_phase1 = 36  # 3 minutes (36 * 5 seconds)
                submitted_reached = False
                
                for attempt in range(max_attempts_phase1):
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
                    
                    await asyncio.sleep(5)
                
                if not submitted_reached:
                    print(f"\n   ⚠️  Did not reach SUBMITTED status within timeout. Current status: {runtime_status}")
                    print(f"   The save may have failed. Check manually.")
                    status_check_success = False
                
                # Phase 2: Wait for Ready/Active status (confirms build is complete)
                print("\n   Phase 2: Waiting for status to change to Ready/Active...")
                print("   (This may take several minutes depending on index size)")
                print("   ⏳ Will wait indefinitely until READY status + indexRefreshedOn timestamp...")
                ready_reached = False
                attempt = 0
                
                while not ready_reached:
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
                    await asyncio.sleep(5)
                else:
                    # FINAL VALIDATION: Verify the prompt was actually saved
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

