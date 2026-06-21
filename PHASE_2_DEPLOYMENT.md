# Phase 2: Media Engine Deployment & Validation

## Current Status
✓ Code integrated and committed
✓ All documentation complete
✓ Test client ready
✓ Installer updated

## Next Steps (In Order)

### STEP 1: Deploy to Chair PC

On the chair PC, run:
```bash
cd /opt/sonixscape
git pull
```

Expected output:
```
From https://github.com/gitpulssi/sonscape
   3d72e06..e21faf5  main       -> origin/main
Updating 3dc0bc3..e21faf5
Fast-forward
 DEPLOYMENT_CHECKLIST.md           | 247 ++++
 MEDIA_API_REFERENCE.md            | 577 ++++++++
 MEDIA_DEPLOYMENT_GUIDE.md         | 394 ++++++
 MEDIA_ENGINE_SUMMARY.md           | 223 ++++
 MEDIA_INTEGRATION_NOTES.md        | 125 ++
 install_sonixscape_v4_updated.sh  | 28 +-
 test_media_client.py              | 231 ++++
 ws_audio.py                       | 860 ++++++++++++-
 8 files changed, 3636 insertions(+), 2291 deletions(-)
```

---

### STEP 2: Install GStreamer Dependencies

On the chair PC:
```bash
sudo apt-get update
sudo apt-get install -y \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  libgstreamer1.0-0 \
  gir1.2-gstreamer-1.0 \
  yt-dlp
```

Verify installation:
```bash
# Should show GStreamer version
gst-inspect-1.0 --version

# Should show yt-dlp version
yt-dlp --version

# Should work without error
python3 -c "import gi; gi.require_version('Gst', '1.0'); print('[OK] GStreamer bindings')"
```

---

### STEP 3: Restart Audio Service

```bash
# Stop the current service
sudo systemctl stop sonixscape-audio

# Wait 2 seconds
sleep 2

# Check that it stopped
ps aux | grep ws_audio

# Start again
sudo systemctl start sonixscape-audio

# Monitor logs (Ctrl+C to stop)
sudo journalctl -u sonixscape-audio -f
```

Look for these messages:
```
[MEDIA] MediaEngine imported successfully
[MEDIA] MediaEngine initialized
[+] ALSA output and audio loop initialized
```

---

### STEP 4: Run Console Test (Chair PC)

Stop service and run manually to see debug output:
```bash
sudo systemctl stop sonixscape-audio
cd /opt/sonixscape
source venv/bin/activate
python3 -u ws_audio.py
```

This will show real-time debug messages. Keep this running for testing.

---

### STEP 5: Test from Another Machine

From your development machine or phone, run:
```bash
# Get chair IP first
ping sonscape.local
# or
hostname -I  # on chair PC

# Then test (replace 192.168.1.X with actual IP)
python test_media_client.py 192.168.1.X 8081 test
```

Expected sequence:
```
[+] Connected to ws://192.168.1.X:8081
[TEST 1] Loading local file...
    Response: ack:media-load
[TEST 2] Starting playback...
    Response: ack:media-play
    [Listen for 5 seconds of audio]
[TEST 3] Pausing playback...
    Response: ack:media-pause
[TEST 4] Seeking to 10 seconds...
    Response: ack:media-seek
[TEST 5] Setting volume to 50%...
    Response: ack:media-volume
[TEST 6] Resuming playback...
    Response: ack:media-play
    [Listen for 5 more seconds]
[TEST 7] Stopping playback...
    Response: ack:media-stop
[+] All tests completed!
```

---

### STEP 6: Test YouTube Playback

```bash
# Interactive mode (you control playback)
python test_media_client.py 192.168.1.X 8081

# In the prompt, type:
media> load https://www.youtube.com/watch?v=dQw4w9WgXcQ
media> play
media> seek 30
media> volume 0.5
media> pause
media> quit
```

Console on chair PC should show:
```
[WS] Media load: https://www.youtube.com/watch?v=...
[MEDIA] Resolving YouTube: https://www.youtube.com/watch?v=...
[MEDIA] YouTube stream: https://r5---sn-u71e.c.youtube.com/...
[MEDIA] Pipeline created: uridecodebin uri=https://r5---sn-u71...
[WS] Media play
[MEDIA] Playing
[MEDIA] Seek to 30.0s
[MEDIA] Volume: 0.50
[MEDIA] Paused
[MEDIA] Stopped
```

---

### STEP 7: Verify Audio on Both Outputs

**On Chair DAC:**
- Connect oscilloscope to stereo audio output (not the 8-channel)
- Load media and play
- Should see sinusoidal signal (therapy waveform mixed with media)
- Frequency should vary if therapy is running

**On Bluetooth Headset:**
- Pair headset to chair PC (if not already done)
- Play media via test_media_client
- Should hear full-bandwidth audio
- Should NOT hear therapy tones (only media)

**Mix Control:**
```bash
# From test client, use set-mix to adjust balance:
# (This is an existing command, not new media command)

# In interactive mode or via WebSocket:
{"action": "set-mix", "value": 0}     # Therapy only
{"action": "set-mix", "value": 50}    # Equal mix
{"action": "set-mix", "value": 100}   # Music only
```

---

### STEP 8: Test Error Handling

Try these to verify error messages:

```bash
# Invalid YouTube URL
media> load https://www.youtube.com/watch?v=invalid_video_id

# Non-existent local file
media> load /nonexistent/file.mp3

# Seek without loading media
media> seek 30

# Invalid volume
media> volume 5.0   # Should clamp to 1.0
```

Expected behavior: Graceful errors, service stays running.

---

## Troubleshooting Quick Reference

### No [MEDIA] messages
- Check if media_engine.py exists: `ls /opt/sonixscape/media_engine.py`
- Check Python gi module: `python3 -c "import gi; gi.require_version('Gst', '1.0')"`
- Install: `sudo apt-get install python3-gi gir1.2-gstreamer-1.0`

### "engine-unavailable" error
- MediaEngine import failed
- Check dependencies installed
- Check /opt/sonixscape/media_engine.py permissions

### "load-failed" on YouTube
- Check internet: `ping 8.8.8.8`
- Check yt-dlp: `yt-dlp --version`
- Try direct YouTube URL: `yt-dlp -f bestaudio -g "https://www.youtube.com/watch?v=..."`

### "load-failed" on local file
- Check file exists: `ls -la /path/to/file.mp3`
- Check file permissions: `file /path/to/file.mp3`
- Test GStreamer: `gst-launch-1.0 filesrc location=/path/to/file.mp3 ! decodebin ! fakesink`

### No audio output on DAC
- Check ALSA output: `ps aux | grep aplay`
- Check DAC: `aplay -l` (should show ICUSBAUDIO7D)
- Test DAC: `speaker-test -D plughw:CARD=ICUSBAUDIO7D,DEV=0`

### No audio on headset
- Check Bluetooth: `bluetoothctl devices`
- Check pairing: `bluetoothctl info <MAC>`
- Check headset process: `ps aux | grep bluealsa-aplay`

---

## Decision Point

### Option A: Quick Validation (15 mins)
1. Deploy code to chair
2. Install dependencies
3. Restart service
4. Run test_media_client.py test mode
5. Verify audio on DAC and headset

### Option B: Deep Testing (45 mins)
1. Do Option A
2. Test YouTube playback
3. Test all control commands (pause, seek, volume)
4. Test error handling
5. Test mix control with therapy
6. Monitor system resources

### Option C: Phone UI Integration (1-2 hours)
1. Do Option A
2. Update phone web UI to send media commands
3. Test end-to-end: phone → media → audio
4. Collect user feedback

---

## What to Do Next

**Choose one:**

1. **"Deploy now"** - I'll guide you through deployment step-by-step
2. **"Test first"** - I'll help you prepare test cases
3. **"Phone UI"** - I'll help you update the web interface for media control
4. **"Full guide"** - I'll create a complete deployment walkthrough

Which would you like to do first?
