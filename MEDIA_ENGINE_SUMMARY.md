# Media Engine Integration Complete

## Summary

Successfully integrated GStreamer-based media playback engine into SoniXscape. The phone can now control local file and YouTube playback via WiFi WebSocket, with audio mixing on the chair PC.

## What Was Done

### 1. Code Integration (ws_audio.py)
- ✓ Imported MediaEngine with fallback handling
- ✓ Created media_ring (deque) for PCM frame storage in SineRowPlayer
- ✓ Implemented _read_media_from_ring() method for frame extraction
- ✓ Modified _generate_therapy_audio() to mix media with therapy and BT audio
- ✓ Media audio has priority for headset output (unfiltered)
- ✓ Added WebSocket command routing for "media-*" message types
- ✓ Implemented handle_media_command() with 6 media control handlers
- ✓ All error handling and fallback mechanisms in place

### 2. Installer Updates (install_sonixscape_v4_updated.sh)
- ✓ Added GStreamer dependencies (plugins-base, plugins-good, plugins-bad)
- ✓ Added gir1.2-gstreamer-1.0 (Python bindings)
- ✓ Added yt-dlp for YouTube stream resolution
- ✓ Added media_engine.py verification
- ✓ Updated installation summary with media engine info

### 3. Testing Tools
- ✓ Created test_media_client.py with 3 modes:
  - test mode: automated test sequence
  - youtube mode: load and play YouTube URL
  - interactive mode: manual media control

### 4. Documentation
- ✓ MEDIA_INTEGRATION_NOTES.md - Technical details
- ✓ MEDIA_DEPLOYMENT_GUIDE.md - Deployment, testing, and troubleshooting

## Architecture

```
Phone (WiFi)
  |
  └─ WebSocket: media-load, media-play, media-seek, media-volume
     |
Chair PC (ws_audio.py)
  |
  ├─ MediaEngine (GStreamer)
  │   ├─ Local files: MP3, FLAC, WAV, OGG, Opus, AAC
  │   ├─ YouTube: resolved via yt-dlp
  │   └─ Output: 48kHz stereo S16_LE PCM
  │
  ├─ media_ring (deque)
  │   ├─ Filled by: GStreamer appsink callback
  │   └─ Read by: _read_media_from_ring() in audio loop
  │
  ├─ Audio Mixing
  │   ├─ Therapy signal (20-150 Hz, 8 channels)
  │   ├─ BT audio (if connected, filtered 200Hz LPF)
  │   ├─ Media audio (full bandwidth)
  │   └─ Mix: therapy*gain + bt*gain + media*gain
  │
  └─ Output
      ├─ 8ch DAC → Chair (therapy + media mixed)
      └─ Headset → Full-bandwidth media (priority over BT)
```

## WebSocket Protocol

### Commands (from phone)
```json
{"type": "media-load", "uri": "https://www.youtube.com/watch?v=..."}
{"type": "media-load", "uri": "/path/to/file.mp3"}
{"type": "media-play"}
{"type": "media-pause"}
{"type": "media-stop"}
{"type": "media-seek", "seconds": 120.5}
{"type": "media-volume", "value": 0.75}
```

### Responses (from chair)
- Success: `ack:media-load`, `ack:media-play`, etc.
- Error: `error:media:load-failed`, `error:media:engine-unavailable`, etc.
- Status: `media-status` with position, duration, title, volume

## Testing Procedure

### On Development Machine
```bash
# Test with chair PC at 192.168.1.100
python test_media_client.py 192.168.1.100 8081 test

# Or interactive control
python test_media_client.py 192.168.1.100 8081
```

### On Chair PC (Manual)
```bash
# Stop service
sudo systemctl stop sonixscape-audio

# Run with console output
cd /opt/sonixscape && source venv/bin/activate
python3 -u ws_audio.py

# Look for:
# [MEDIA] MediaEngine imported successfully
# [MEDIA] MediaEngine initialized
# [MEDIA] Resolving YouTube: ...
# [MEDIA] Playing
```

## Files Changed

| File | Change |
|------|--------|
| ws_audio.py | +860 lines (integration) |
| install_sonixscape_v4_updated.sh | +dependencies |
| media_engine.py | no changes (existing) |
| test_media_client.py | new file |
| MEDIA_INTEGRATION_NOTES.md | new file |
| MEDIA_DEPLOYMENT_GUIDE.md | new file |

## Git Status
```
Commit: 3dc0bc3 "Add GStreamer media engine integration"
Branch: main
Status: Ready for deployment
```

## Next Steps

1. **Deploy to chair PC:**
   ```bash
   cd /opt/sonixscape && git pull
   ```

2. **Install dependencies (on chair PC):**
   ```bash
   sudo apt-get update
   sudo apt-get install -y gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
     gstreamer1.0-plugins-bad libgstreamer1.0-0 gir1.2-gstreamer-1.0 yt-dlp
   ```

3. **Restart audio service:**
   ```bash
   sudo systemctl restart sonixscape-audio
   ```

4. **Test media playback:**
   ```bash
   python test_media_client.py <chair_ip> 8081 test
   ```

5. **Update phone UI** to send media commands instead of BT streaming

6. **End-to-end testing:**
   - Load YouTube video from phone
   - Verify playback on chair DAC (therapy channels)
   - Verify headset audio (full bandwidth)
   - Test seeking, volume, pause/resume

## Performance Notes

- Ring buffer: 96,000 frames (~2 seconds at 48kHz)
- No blocking in audio loop (deque O(1))
- GStreamer runs in background thread
- Media audio has full bandwidth (no filtering)
- Headset output prefers media over BT when both present

## Rollback (if needed)

```bash
# Revert to previous commit
git revert 3dc0bc3
# or
git reset --hard HEAD~1
```

## Known Limitations

1. Media playback only; no recording
2. YouTube requires internet connectivity
3. No media queueing yet
4. No file browsing UI
5. Duration/position updates via console only (not WebSocket status yet)

## Future Enhancements

- [ ] Media status updates over WebSocket
- [ ] Playlist support
- [ ] Cover art display
- [ ] Local media library browsing
- [ ] Recording media playback to storage
- [ ] Media queue management
- [ ] Metadata display (artist, album, etc.)
