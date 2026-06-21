# Media Engine Deployment & Testing Guide

## Quick Start

### 1. Deploy to Chair PC

```bash
# On development machine (Windows)
cd e:/GitHub_Repository/sonscape
git add ws_audio.py media_engine.py install_sonixscape_v4_updated.sh
git commit -m "Add GStreamer media engine with local file and YouTube support"
git push

# On chair PC
cd /opt/sonixscape
git pull
```

### 2. Update Dependencies

```bash
# Install GStreamer and yt-dlp
sudo apt-get update
sudo apt-get install -y gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad libgstreamer1.0-0 gir1.2-gstreamer-1.0 yt-dlp

# Update venv with any new Python deps (if using pip yt-dlp)
cd /opt/sonixscape
source venv/bin/activate
pip install yt-dlp
deactivate
```

### 3. Test Media Engine

#### Test 1: Verify GStreamer is installed
```bash
gst-inspect-1.0 | head -20
```
Should show GStreamer version and available plugins.

#### Test 2: Verify yt-dlp works
```bash
yt-dlp -f bestaudio -g 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
```
Should return an HTTP stream URL (not the full video page).

#### Test 3: Restart ws_audio and check for [MEDIA] messages
```bash
# Stop the service
sudo systemctl stop sonixscape-audio

# Run manually with debug output
cd /opt/sonixscape
source venv/bin/activate
python3 -u ws_audio.py

# In console, look for:
# [MEDIA] MediaEngine imported successfully
# [MEDIA] MediaEngine initialized
```

## Testing Media Playback

### Option A: Using test_media_client.py

```bash
# From Windows or any machine on the network
python test_media_client.py <chair_ip> 8081 test

# For YouTube (interactive, requires URL input)
python test_media_client.py <chair_ip> 8081 youtube

# Interactive control
python test_media_client.py <chair_ip> 8081
```

### Option B: Using curl/bash from chair PC

```bash
# Load local MP3
curl -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Key: SGVsbG8sIHdvcmxkIQ==" \
  -H "Sec-WebSocket-Version: 13" \
  http://localhost:8081 -d '{"type":"media-load","uri":"/path/to/file.mp3"}'

# Or use wscat (install: npm install -g wscat)
wscat -c ws://localhost:8081
# Then type:
> {"type":"media-load","uri":"/home/sonix/music/test.mp3"}
> {"type":"media-play"}
> {"type":"media-volume","value":0.5}
```

### Option C: Manual WebSocket testing (Python)

```python
import asyncio
import websockets
import json

async def test():
    async with websockets.connect("ws://192.168.1.100:8081") as ws:
        # Load YouTube
        await ws.send(json.dumps({
            "type": "media-load",
            "uri": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        }))
        response = await ws.recv()
        print(f"Load response: {response}")
        
        # Play
        await ws.send(json.dumps({"type": "media-play"}))
        response = await ws.recv()
        print(f"Play response: {response}")

asyncio.run(test())
```

## Expected Behavior

### On Console (ws_audio.py)
```
[MEDIA] MediaEngine imported successfully
[MEDIA] MediaEngine initialized
[WS] New client 192.168.1.50
[WS] Media load: https://www.youtube.com/watch?v=...
[MEDIA] Resolving YouTube: https://www.youtube.com/watch?v=...
[MEDIA] YouTube stream: https://r5---sn-u71...
[MEDIA] Pipeline created: uridecodebin uri=https://r5---sn-u71... ! ...
[WS] Media play
[MEDIA] Playing
[MEDIA] Pipeline querying position...
[MEDIA] DURATION_CHANGED: got 183.4 seconds
[MEDIA] Seek to 10.0s
[MEDIA] Volume: 0.75
[MEDIA] Paused
[MEDIA] Stopped
```

### Audio Output

1. **Chair (DAC):** Hear therapy signal mixed with media audio
   - Low frequencies (therapy 20-150 Hz) on chair body
   - Music audio across all 8 channels
   - Mix ratio controlled by "set-mix" action

2. **Headset (Bluetooth A2DP):** Hear full-bandwidth media audio
   - Unfiltered (no 200Hz LPF like BT capture)
   - Media has priority over BT music
   - Both channels (stereo)

## Troubleshooting

### No audio output
1. Check media ring buffer filling:
   - Add debug print in `_read_media_from_ring()` to log frames read
   - Should see non-zero frames if media is playing

2. Check ALSA output processes:
   ```bash
   ps aux | grep aplay
   ```
   Should show:
   - One for main chair DAC (8-channel)
   - One for headset Bluetooth (2-channel)

3. Check GStreamer pipeline state:
   - Add debug output in `_on_bus_message()` to see pipeline state changes
   - Look for EOS (end of stream) or ERROR messages

### Media-load fails
1. Check if file exists (local file)
2. Check internet connectivity (YouTube)
3. Check yt-dlp output:
   ```bash
   yt-dlp -f bestaudio -g "https://www.youtube.com/watch?v=..."
   ```
   Should return HTTP URL within 30 seconds

### GStreamer decoder fails
1. Check if codecs are installed:
   ```bash
   apt-get install gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly
   ```

2. Check if file format is supported:
   ```bash
   gst-typefind-1.0 /path/to/file.mp3
   ```

3. Test with simple format first (WAV):
   ```bash
   gst-launch-1.0 filesrc location=/path/to/file.wav ! wavparse ! audioconvert ! audioresample ! audio/x-raw,rate=48000 ! fakesink
   ```

### WebSocket connection fails
1. Check web service is running:
   ```bash
   ps aux | grep main_app.py
   ```

2. Check port is open:
   ```bash
   netstat -tlnp | grep 8081
   ```

3. Check firewall rules:
   ```bash
   sudo ufw allow 8081
   ```

### Volume doesn't change
1. Media engine volume is a soft gain (0.0-1.0)
2. Check if frames are being scaled correctly in `_read_media_from_ring()`
3. May need to adjust "set-mix" action to change music/therapy balance

## Performance Tuning

### Ring Buffer Size
Current: 96000 frames (~2 seconds at 48kHz)
- Increase if dropouts occur (deque maxlen)
- Decrease if latency is too high

### GStreamer Buffer
Current: 10 max buffers, 1200 block size
- In media_engine.py line 111: `max-buffers=10`
- Increase for better buffering on slow networks

### Mixer Gain
Current: BT/media gain is set via "set-mix" action
- Mix value 0-100 controls therapy/music balance
- At 50: equal mix
- At 0: therapy only
- At 100: music only

## Files Changed

1. **ws_audio.py** - Added media engine integration
2. **media_engine.py** - GStreamer decoder (existing)
3. **install_sonixscape_v4_updated.sh** - Added GStreamer+yt-dlp dependencies
4. **test_media_client.py** - Test/control client (new)

## Next Steps

1. Deploy changes to chair PC
2. Run test_media_client.py from development machine
3. Verify audio on both DAC and headset
4. Update phone web UI to send media commands
5. Test full workflow: phone → media load → chair playback

## API Reference

### Media Commands

```json
{
  "type": "media-load",
  "uri": "https://www.youtube.com/watch?v=..."  or "/path/to/file.mp3"
}

{
  "type": "media-play"
}

{
  "type": "media-pause"
}

{
  "type": "media-stop"
}

{
  "type": "media-seek",
  "seconds": 120.5
}

{
  "type": "media-volume",
  "value": 0.75
}
```

### Response Messages

Success:
- `ack:media-load`
- `ack:media-play`
- `ack:media-pause`
- `ack:media-stop`
- `ack:media-seek`
- `ack:media-volume`

Errors:
- `error:media:engine-unavailable`
- `error:media:no-uri`
- `error:media:load-failed`
- `error:media:play-failed`
- `error:media:unknown-command:<type>`

Status updates (sent from media_engine._send_status()):
```json
{
  "type": "media-status",
  "state": "playing",
  "title": "Song Title",
  "position": 121.4,
  "duration": 306.2,
  "volume": 0.75
}
```
