#!/usr/bin/env python3
"""
Standalone test script for index update memory leak detection.
Tests ONLY the index update operation (no prompt invocation, no Gemini).

Usage:
    # Local test
    python temp/memory_testing/test_index_update_memory.py --iterations 5
    
    # Heroku test (one-off dyno)
    heroku run python temp/memory_testing/test_index_update_memory.py --iterations 10 -a sf-rag-optimizer
"""

import asyncio
import sys
import os
import psutil
import gc
import argparse
import time
from pathlib import Path
from datetime import datetime
import yaml

# Force unbuffered output for live terminal updates
sys.stdout.reconfigure(line_buffering=True)
os.environ['PYTHONUNBUFFERED'] = '1'

# Add scripts/python to path to import modules
script_dir = Path(__file__).parent.parent  # scripts/python
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from playwright_scripts import update_search_index_prompt


def get_memory_usage():
    """Get current process memory usage"""
    process = psutil.Process(os.getpid())
    mem_info = process.memory_info()
    return {
        'rss_mb': mem_info.rss / 1024 / 1024,  # Resident Set Size
        'vms_mb': mem_info.vms / 1024 / 1024,  # Virtual Memory Size
        'percent': process.memory_percent(),
        'available_mb': psutil.virtual_memory().available / 1024 / 1024
    }


def count_browser_processes():
    """Count Chromium/Chrome browser processes"""
    count = 0
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            name = proc.info['name'].lower()
            if 'chromium' in name or 'chrome' in name:
                count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return count


def run_memory_test(iterations, test_prompt, username, password, instance_url, search_index_id, skip_wait=False):
    """Run multiple index update iterations and track memory"""
    results = []
    baseline_memory = get_memory_usage()
    
    print(f"\n{'='*80}", flush=True)
    print(f"Memory Leak Test - {iterations} iterations", flush=True)
    print(f"{'='*80}", flush=True)
    print(f"Baseline Memory: RSS={baseline_memory['rss_mb']:.1f}MB, "
          f"VMS={baseline_memory['vms_mb']:.1f}MB, "
          f"Percent={baseline_memory['percent']:.1f}%", flush=True)
    print(f"Available Memory: {baseline_memory['available_mb']:.1f}MB", flush=True)
    print(f"Browser Processes: {count_browser_processes()}", flush=True)
    print(f"Test Prompt Length: {len(test_prompt)} characters", flush=True)
    print(f"Skip Wait: {skip_wait}", flush=True)
    print(f"{'='*80}\n", flush=True)
    
    for iteration in range(iterations):
        print(f"\n{'='*60}", flush=True)
        print(f"[Iteration {iteration + 1}/{iterations}]", flush=True)
        print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
        print(f"{'='*60}", flush=True)
        
        # Memory before
        mem_before = get_memory_usage()
        browser_count_before = count_browser_processes()
        
        print(f"\n📊 BEFORE UPDATE:", flush=True)
        print(f"  Memory: RSS={mem_before['rss_mb']:.1f}MB, "
              f"VMS={mem_before['vms_mb']:.1f}MB, "
              f"Percent={mem_before['percent']:.1f}%", flush=True)
        print(f"  Browser Processes: {browser_count_before}", flush=True)
        print(f"  Available Memory: {mem_before['available_mb']:.1f}MB", flush=True)
        
        try:
            # Execute index update
            print(f"\n🔄 Starting index update...", flush=True)
            start_time = time.time()
            result = asyncio.run(update_search_index_prompt(
                username=username,
                password=password,
                instance_url=instance_url,
                search_index_id=search_index_id,
                new_prompt=test_prompt,
                capture_network=False,
                take_screenshots=False,
                headless=True,  # Headless for testing
                slow_mo=0,
                skip_wait=skip_wait
            ))
            
            elapsed_time = time.time() - start_time
            print(f"✅ Index update completed in {elapsed_time:.1f} seconds", flush=True)
            
            # Memory after update (before cleanup)
            mem_after = get_memory_usage()
            browser_count_after = count_browser_processes()
            
            print(f"\n📊 AFTER UPDATE (before cleanup):", flush=True)
            print(f"  Memory: RSS={mem_after['rss_mb']:.1f}MB, "
                  f"VMS={mem_after['vms_mb']:.1f}MB, "
                  f"Percent={mem_after['percent']:.1f}%", flush=True)
            print(f"  Browser Processes: {browser_count_after}", flush=True)
            print(f"  Available Memory: {mem_after['available_mb']:.1f}MB", flush=True)
            
            # Calculate immediate delta
            immediate_delta = mem_after['rss_mb'] - mem_before['rss_mb']
            print(f"  Immediate Growth: {immediate_delta:+.1f}MB", flush=True)
            
            # Force cleanup
            print(f"\n🧹 Forcing garbage collection...", flush=True)
            gc.collect()
            time.sleep(1)  # Give GC time to work
            
            # Memory after cleanup
            mem_after_gc = get_memory_usage()
            browser_count_after_gc = count_browser_processes()
            
            print(f"\n📊 AFTER CLEANUP:", flush=True)
            print(f"  Memory: RSS={mem_after_gc['rss_mb']:.1f}MB, "
                  f"VMS={mem_after_gc['vms_mb']:.1f}MB, "
                  f"Percent={mem_after_gc['percent']:.1f}%", flush=True)
            print(f"  Browser Processes: {browser_count_after_gc}", flush=True)
            print(f"  Available Memory: {mem_after_gc['available_mb']:.1f}MB", flush=True)
            
            # Calculate deltas
            delta_rss = mem_after_gc['rss_mb'] - mem_before['rss_mb']
            delta_vms = mem_after_gc['vms_mb'] - mem_before['vms_mb']
            delta_percent = mem_after_gc['percent'] - mem_before['percent']
            
            print(f"\n📈 MEMORY DELTA (final):", flush=True)
            print(f"  RSS: {delta_rss:+.1f}MB", flush=True)
            print(f"  VMS: {delta_vms:+.1f}MB", flush=True)
            print(f"  Percent: {delta_percent:+.1f}%", flush=True)
            
            # Store results
            results.append({
                'iteration': iteration + 1,
                'memory_before': mem_before,
                'memory_after': mem_after,
                'memory_after_gc': mem_after_gc,
                'delta_rss': delta_rss,
                'delta_vms': delta_vms,
                'delta_percent': delta_percent,
                'browser_before': browser_count_before,
                'browser_after': browser_count_after,
                'browser_after_gc': browser_count_after_gc,
                'success': result
            })
            
            # Leak detection
            print(f"\n🔍 LEAK ANALYSIS:", flush=True)
            if delta_rss > 20:  # More than 20MB growth
                print(f"  ⚠️  WARNING: Potential memory leak detected (+{delta_rss:.1f}MB)", flush=True)
            elif delta_rss < -5:  # Memory freed
                print(f"  ✅ Memory cleaned up ({delta_rss:.1f}MB freed)", flush=True)
            else:
                print(f"  ✅ Memory stable ({delta_rss:+.1f}MB)", flush=True)
            
            if browser_count_after_gc > browser_count_before:
                print(f"  ⚠️  WARNING: Browser processes not cleaned up ({browser_count_after_gc} > {browser_count_before})", flush=True)
            elif browser_count_after_gc == 0:
                print(f"  ✅ Browser cleanup successful", flush=True)
            else:
                print(f"  ℹ️  Browser processes: {browser_count_before} → {browser_count_after_gc}", flush=True)
                
        except Exception as e:
            print(f"\n❌ ERROR during iteration {iteration + 1}:", flush=True)
            print(f"  {str(e)}", flush=True)
            import traceback
            traceback.print_exc()
            
            # Still measure memory after error
            mem_after_error = get_memory_usage()
            browser_count_after_error = count_browser_processes()
            
            print(f"\n📊 AFTER ERROR:", flush=True)
            print(f"  Memory: RSS={mem_after_error['rss_mb']:.1f}MB, "
                  f"Percent={mem_after_error['percent']:.1f}%", flush=True)
            print(f"  Browser Processes: {browser_count_after_error}", flush=True)
            
            results.append({
                'iteration': iteration + 1,
                'error': str(e),
                'success': False,
                'memory_before': mem_before,
                'memory_after_error': mem_after_error,
                'browser_before': browser_count_before,
                'browser_after_error': browser_count_after_error
            })
            
            # Force cleanup even after error
            print(f"🧹 Forcing cleanup after error...", flush=True)
            gc.collect()
            time.sleep(1)
        
        # Wait between iterations
        if iteration < iterations - 1:
            print(f"\n⏳ Waiting 5 seconds before next iteration...\n", flush=True)
            time.sleep(5)
    
    # Final report
    print_report(results, baseline_memory)
    
    return results


def print_report(results, baseline_memory):
    """Print comprehensive test report"""
    print(f"\n{'='*80}", flush=True)
    print("MEMORY LEAK TEST RESULTS", flush=True)
    print(f"{'='*80}", flush=True)
    print(f"Test Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"Iterations: {len(results)}", flush=True)
    print(f"\nBaseline Memory: RSS={baseline_memory['rss_mb']:.1f}MB", flush=True)
    
    if not results:
        print("No results to report.", flush=True)
        return
    
    # Calculate totals
    successful = [r for r in results if r.get('success', False)]
    failed = [r for r in results if not r.get('success', False)]
    
    print(f"Successful Iterations: {len(successful)}", flush=True)
    print(f"Failed Iterations: {len(failed)}", flush=True)
    
    if successful:
        final_memory = successful[-1]['memory_after_gc']
        total_growth = final_memory['rss_mb'] - baseline_memory['rss_mb']
        avg_growth = sum(r.get('delta_rss', 0) for r in successful) / len(successful) if successful else 0
        
        print(f"\n📊 FINAL STATISTICS:", flush=True)
        print(f"  Final Memory: RSS={final_memory['rss_mb']:.1f}MB", flush=True)
        print(f"  Total Growth: {total_growth:+.1f}MB", flush=True)
        if successful:
            print(f"  Average Growth per Iteration: {avg_growth:+.1f}MB", flush=True)
        
        # Browser cleanup
        browser_cleanup_success = sum(1 for r in successful if r.get('browser_after_gc', 0) == 0)
        print(f"\n🔧 BROWSER CLEANUP:", flush=True)
        print(f"  Success Rate: {browser_cleanup_success}/{len(successful)} ({browser_cleanup_success*100/len(successful):.1f}%)", flush=True)
        
        # Leak detection
        print(f"\n🎯 LEAK DETECTION:", flush=True)
        if total_growth > 50 or avg_growth > 10:
            print(f"  ❌ MEMORY LEAK DETECTED", flush=True)
            print(f"     Total growth: {total_growth:.1f}MB", flush=True)
            print(f"     Average per iteration: {avg_growth:.1f}MB", flush=True)
            print(f"     RECOMMENDATION: Fix browser cleanup and memory management", flush=True)
        elif total_growth < -10:
            print(f"  ✅ NO MEMORY LEAK", flush=True)
            print(f"     Memory actually decreased: {total_growth:.1f}MB", flush=True)
            print(f"     Cleanup is working correctly", flush=True)
        else:
            print(f"  ✅ NO MEMORY LEAK", flush=True)
            print(f"     Memory growth is minimal: {total_growth:.1f}MB", flush=True)
            print(f"     Design is working as expected", flush=True)
    
    if failed:
        print(f"\n⚠️  FAILED ITERATIONS: {len(failed)}", flush=True)
        for r in failed:
            print(f"  Iteration {r['iteration']}: {r.get('error', 'Unknown error')[:100]}", flush=True)
    
    print(f"\n{'='*80}\n", flush=True)


# Main entry point
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Test memory leaks during index updates')
    parser.add_argument('--iterations', type=int, default=10, help='Number of iterations (default: 10)')
    parser.add_argument('--test-prompt', type=str, default='Test prompt for memory leak detection. This is a simple test to verify memory cleanup between index updates.', help='Test prompt text')
    parser.add_argument('--skip-wait', action='store_true', help='Skip Phase 2 wait (READY/ACTIVE status)')
    parser.add_argument('--yaml-config', type=str, help='Path to YAML config file (default: inputs/prompt_optimization_input.yaml)')
    
    args = parser.parse_args()
    
    # Load config
    if args.yaml_config:
        yaml_path = Path(args.yaml_config)
    else:
        # Try multiple possible locations
        possible_paths = [
            Path(__file__).parent.parent.parent / "inputs" / "prompt_optimization_input.yaml",  # From repo root
            Path("/app/inputs/prompt_optimization_input.yaml"),  # Heroku absolute path
            Path("inputs/prompt_optimization_input.yaml"),  # Relative from current dir
        ]
        yaml_path = None
        for path in possible_paths:
            if path.exists():
                yaml_path = path
                break
        
        if not yaml_path:
            # Last resort: try environment variables
            username = os.getenv('SALESFORCE_USERNAME')
            password = os.getenv('SALESFORCE_PASSWORD')
            instance_url = os.getenv('SALESFORCE_INSTANCE_URL')
            search_index_id = os.getenv('SALESFORCE_SEARCH_INDEX_ID')
            
            if all([username, password, instance_url, search_index_id]):
                print(f"✅ Using environment variables for configuration", flush=True)
                # Run test with env vars
                results = run_memory_test(
                    iterations=args.iterations,
                    test_prompt=args.test_prompt,
                    username=username,
                    password=password,
                    instance_url=instance_url,
                    search_index_id=search_index_id,
                    skip_wait=args.skip_wait
                )
                sys.exit(0)
            else:
                print(f"❌ YAML config file not found in any expected location")
                print(f"   Tried: {possible_paths}")
                print(f"   Environment variables also not set")
                sys.exit(1)
    
    if not yaml_path.exists():
        print(f"❌ YAML config file not found: {yaml_path}")
        sys.exit(1)
    
    try:
        with open(yaml_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        salesforce_config = config.get('configuration', {}).get('salesforce', {})
        username = salesforce_config.get('username')
        password = salesforce_config.get('password')
        instance_url = salesforce_config.get('instanceUrl')
        search_index_id = config.get('configuration', {}).get('searchIndexId')
        
        if not all([username, password, instance_url, search_index_id]):
            print("❌ Missing required configuration in YAML")
            print("   Required: configuration.salesforce.username, password, instanceUrl")
            print("   Required: configuration.searchIndexId")
            sys.exit(1)
        
        print(f"✅ Loaded configuration from: {yaml_path}", flush=True)
        print(f"   Search Index ID: {search_index_id}", flush=True)
        print(f"   Instance: {instance_url}", flush=True)
        print(f"   Iterations: {args.iterations}", flush=True)
        print(f"   Skip Wait: {args.skip_wait}", flush=True)
        print(f"   Test Prompt Length: {len(args.test_prompt)} characters", flush=True)
        
        # Run test
        results = run_memory_test(
            iterations=args.iterations,
            test_prompt=args.test_prompt,
            username=username,
            password=password,
            instance_url=instance_url,
            search_index_id=search_index_id,
            skip_wait=args.skip_wait
        )
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

