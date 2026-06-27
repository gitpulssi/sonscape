#!/usr/bin/env python3
"""
Test script to debug Bluetooth headset audio routing.
Run this to gather diagnostics about the headset initialization and audio write path.
"""

import asyncio
import websockets
import json
import sys
import time
import subprocess

async def test_headset_debug(chair_ip, port=8081):
    """Test headset audio routing with full diagnostics"""
    uri = f"ws://{chair_ip}:{port}"

    try:
        async with websockets.connect(uri) as ws:
            print(f"[+] Connected to {uri}")
            print("[+] Starting headset audio debugging...")
            print()

            # Step 1: Check system Bluetooth status
            print("[STEP 1] System Bluetooth status:")
            try:
                result = subprocess.run(['bluetoothctl', 'show'],
                                      capture_output=True, text=True, timeout=5)
                print(result.stdout)
            except Exception as e:
                print(f"  [!] bluetoothctl not available: {e}")
            print()

            # Step 2: Load a local test file (should not use Bluetooth yet)
            print("[STEP 2] Loading local test file (DAC only)...")
            await ws.send(json.dumps({"type": "media-load", "uri": "/opt/sonixscape/test.mp3"}))
            response = await asyncio.wait_for(ws.recv(), timeout=5)
            print(f"    Response: {response}")
            await asyncio.sleep(1)
            print()

            # Step 3: Play media
            print("[STEP 3] Playing media (watch for [HEADSET] and [AUDIO] diagnostics)...")
            await ws.send(json.dumps({"type": "media-play"}))
            response = await asyncio.wait_for(ws.recv(), timeout=2)
            print(f"    Response: {response}")
            print()
            print("    Listening for diagnostics (should see [HEADSET] and [AUDIO] messages)...")
            print("    Look for:")
            print("      [HEADSET] Output initialized - if headset process started")
            print("      [HEADSET-DEBUG] - if there's an issue with headset writing")
            print("      [AUDIO] Sending ... bytes of media to headset - if data is being sent")
            print()

            # Play for 10 seconds to gather diagnostics
            await asyncio.sleep(10)
            print()

            # Step 4: Set volume to test media-only audio path
            print("[STEP 4] Testing volume control (halves media amplitude)...")
            await ws.send(json.dumps({"type": "media-volume", "value": 0.5}))
            response = await asyncio.wait_for(ws.recv(), timeout=2)
            print(f"    Response: {response}")
            print("    Watching for [AUDIO] Sending ... amplitude should be halved")
            await asyncio.sleep(3)
            print()

            # Step 5: Check final status
            print("[STEP 5] Checking audio status before stopping...")
            await asyncio.sleep(2)
            print()

            # Step 6: Stop
            print("[STEP 6] Stopping playback...")
            await ws.send(json.dumps({"type": "media-stop"}))
            response = await asyncio.wait_for(ws.recv(), timeout=2)
            print(f"    Response: {response}")

            print()
            print("[+] Test completed!")
            print()
            print("SUMMARY: Look for these patterns in the diagnostics above:")
            print("  1. '[HEADSET] Output initialized' with PID - headset process created")
            print("  2. '[AUDIO] Sending X bytes' - data being sent to headset")
            print("  3. '[HEADSET-DEBUG] headset_process is None' - no headset process")
            print("  4. '[HEADSET-DEBUG] Wrote Y bytes' - successful write to headset")
            print()

    except asyncio.TimeoutError:
        print("[!] Timeout waiting for response")
    except ConnectionRefusedError:
        print(f"[!] Failed to connect to {uri}")
    except Exception as e:
        print(f"[!] Error: {e}")

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 test_headset_debug.py <chair_ip> [port]")
        print("Example: python3 test_headset_debug.py 192.168.1.100 8081")
        sys.exit(1)

    chair_ip = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 8081

    asyncio.run(test_headset_debug(chair_ip, port))

if __name__ == "__main__":
    main()
