#!/usr/bin/env python3
"""
Test client for SoniXscape media playback over WebSocket.
Usage: python3 test_media_client.py <chair_ip> [port]
Example: python3 test_media_client.py 192.168.1.100 8081
"""

import asyncio
import websockets
import json
import sys
import time

async def test_media_playback(chair_ip, port=8081):
    """Test media playback via WebSocket"""
    uri = f"ws://{chair_ip}:{port}"

    try:
        async with websockets.connect(uri) as ws:
            print(f"[+] Connected to {uri}")

            # Test 1: Load local MP3 file
            print("\n[TEST 1] Loading local file...")
            await ws.send(json.dumps({"type": "media-load", "uri": "/home/sonix/music/test.mp3"}))
            response = await asyncio.wait_for(ws.recv(), timeout=2)
            print(f"    Response: {response}")

            # Test 2: Play
            print("\n[TEST 2] Starting playback...")
            await ws.send(json.dumps({"type": "media-play"}))
            response = await asyncio.wait_for(ws.recv(), timeout=2)
            print(f"    Response: {response}")

            # Wait for 5 seconds
            await asyncio.sleep(5)

            # Test 3: Pause
            print("\n[TEST 3] Pausing playback...")
            await ws.send(json.dumps({"type": "media-pause"}))
            response = await asyncio.wait_for(ws.recv(), timeout=2)
            print(f"    Response: {response}")

            # Test 4: Seek
            print("\n[TEST 4] Seeking to 10 seconds...")
            await ws.send(json.dumps({"type": "media-seek", "seconds": 10.0}))
            response = await asyncio.wait_for(ws.recv(), timeout=2)
            print(f"    Response: {response}")

            # Test 5: Set volume
            print("\n[TEST 5] Setting volume to 50%...")
            await ws.send(json.dumps({"type": "media-volume", "value": 0.5}))
            response = await asyncio.wait_for(ws.recv(), timeout=2)
            print(f"    Response: {response}")

            # Test 6: Resume
            print("\n[TEST 6] Resuming playback...")
            await ws.send(json.dumps({"type": "media-play"}))
            response = await asyncio.wait_for(ws.recv(), timeout=2)
            print(f"    Response: {response}")

            # Wait for 5 seconds
            await asyncio.sleep(5)

            # Test 7: Stop
            print("\n[TEST 7] Stopping playback...")
            await ws.send(json.dumps({"type": "media-stop"}))
            response = await asyncio.wait_for(ws.recv(), timeout=2)
            print(f"    Response: {response}")

            print("\n[+] All tests completed!")

    except asyncio.TimeoutError:
        print("[!] Timeout waiting for response")
    except ConnectionRefusedError:
        print(f"[!] Failed to connect to {uri}")
    except Exception as e:
        print(f"[!] Error: {e}")

async def test_youtube_playback(chair_ip, youtube_url, port=8081):
    """Test YouTube playback via WebSocket"""
    uri = f"ws://{chair_ip}:{port}"

    try:
        async with websockets.connect(uri) as ws:
            print(f"[+] Connected to {uri}")

            # Load YouTube URL
            print(f"\n[YOUTUBE] Loading: {youtube_url}")
            await ws.send(json.dumps({"type": "media-load", "uri": youtube_url}))
            response = await asyncio.wait_for(ws.recv(), timeout=10)
            print(f"    Response: {response}")

            # Play
            print("\n[YOUTUBE] Starting playback...")
            await ws.send(json.dumps({"type": "media-play"}))
            response = await asyncio.wait_for(ws.recv(), timeout=2)
            print(f"    Response: {response}")

            print("\n[+] YouTube test started!")
            print("    Listen for audio on chair (DAC) and headset")

    except Exception as e:
        print(f"[!] Error: {e}")

async def interactive_client(chair_ip, port=8081):
    """Interactive media control client"""
    uri = f"ws://{chair_ip}:{port}"

    try:
        async with websockets.connect(uri) as ws:
            print(f"[+] Connected to {uri}")
            print("\nCommands:")
            print("  load <uri>        - Load media file or YouTube URL")
            print("  play              - Start/resume playback")
            print("  pause             - Pause playback")
            print("  stop              - Stop playback")
            print("  seek <seconds>    - Seek to position")
            print("  volume <0-1>      - Set volume (0.0 to 1.0)")
            print("  quit              - Exit")
            print()

            while True:
                try:
                    cmd = input("media> ").strip()

                    if not cmd:
                        continue

                    parts = cmd.split(maxsplit=1)
                    command = parts[0].lower()
                    arg = parts[1] if len(parts) > 1 else None

                    if command == "quit":
                        break

                    elif command == "load" and arg:
                        msg = {"type": "media-load", "uri": arg}
                        await ws.send(json.dumps(msg))

                    elif command == "play":
                        msg = {"type": "media-play"}
                        await ws.send(json.dumps(msg))

                    elif command == "pause":
                        msg = {"type": "media-pause"}
                        await ws.send(json.dumps(msg))

                    elif command == "stop":
                        msg = {"type": "media-stop"}
                        await ws.send(json.dumps(msg))

                    elif command == "seek" and arg:
                        try:
                            seconds = float(arg)
                            msg = {"type": "media-seek", "seconds": seconds}
                            await ws.send(json.dumps(msg))
                        except ValueError:
                            print("  [!] Invalid seconds value")

                    elif command == "volume" and arg:
                        try:
                            value = float(arg)
                            msg = {"type": "media-volume", "value": value}
                            await ws.send(json.dumps(msg))
                        except ValueError:
                            print("  [!] Invalid volume value (0.0-1.0)")

                    else:
                        print(f"  [!] Unknown command: {command}")
                        continue

                    # Try to receive response (non-blocking)
                    try:
                        response = await asyncio.wait_for(ws.recv(), timeout=0.5)
                        print(f"  -> {response}")
                    except asyncio.TimeoutError:
                        pass

                except KeyboardInterrupt:
                    break
                except Exception as e:
                    print(f"  [!] Error: {e}")

            print("\n[+] Disconnected")

    except ConnectionRefusedError:
        print(f"[!] Failed to connect to {uri}")
    except Exception as e:
        print(f"[!] Error: {e}")

def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 test_media_client.py <chair_ip> [port]")
        print("  python3 test_media_client.py <chair_ip> [port] test")
        print("  python3 test_media_client.py <chair_ip> [port] youtube <url>")
        print("\nExamples:")
        print("  python3 test_media_client.py 192.168.1.100")
        print("  python3 test_media_client.py 192.168.1.100 8081 test")
        print("  python3 test_media_client.py 192.168.1.100 youtube 'https://youtu.be/dQw4w9WgXcQ'")
        sys.exit(1)

    chair_ip = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 8081
    mode = sys.argv[3] if len(sys.argv) > 3 else None

    if mode == "test":
        asyncio.run(test_media_playback(chair_ip, port))
    elif mode == "youtube":
        youtube_url = sys.argv[4] if len(sys.argv) > 4 else input("YouTube URL: ")
        asyncio.run(test_youtube_playback(chair_ip, youtube_url, port))
    else:
        asyncio.run(interactive_client(chair_ip, port))

if __name__ == "__main__":
    main()
