# Media Engine Deployment Checklist

## Code Integration ✓

- [x] MediaEngine imported in ws_audio.py with graceful fallback
- [x] Media ring buffer (deque) created in SineRowPlayer
- [x] _read_media_from_ring() method implemented
- [x] Audio mixing updated in _generate_therapy_audio()
- [x] Media audio prioritized for headset output
- [x] WebSocket command routing implemented
- [x] handle_media_command() method with 6 handlers
- [x] Error handling and logging throughout
- [x] Syntax validation passed

## Files Ready ✓

- [x] ws_audio.py - Updated with media engine
- [x] media_engine.py - GStreamer decoder engine
- [x] install_sonixscape_v4_updated.sh - Updated with dependencies
- [x] test_media_client.py - Test/control client
- [x] MEDIA_INTEGRATION_NOTES.md - Technical details
- [x] MEDIA_DEPLOYMENT_GUIDE.md - Deployment & troubleshooting
- [x] MEDIA_ENGINE_SUMMARY.md - Implementation overview
- [x] MEDIA_API_REFERENCE.md - Complete API documentation

## Documentation ✓

- [x] Architecture diagrams
- [x] WebSocket protocol specification
- [x] Testing procedures
- [x] Troubleshooting guide
- [x] API reference with examples
- [x] Performance notes
- [x] Deployment instructions

## Testing Tools ✓

- [x] Automated test mode (test_media_client.py test)
- [x] YouTube test mode (test_media_client.py youtube)
- [x] Interactive control mode (test_media_client.py interactive)

## Git Status ✓

```
Commits:
  - 3dc0bc3: Add GStreamer media engine integration
  - 3d72e06: Add comprehensive media engine documentation

Branch: main
Status: Ready for deployment
```

## Pre-Deployment Checklist

### On Development Machine
- [x] Code committed to GitHub
- [x] All tests pass locally
- [x] Documentation complete
- [x] Test client created

### On Chair PC (before running)

#### System Requirements
- [ ] Ubuntu 24.04 LTS
- [ ] Python 3.8+
- [ ] Internet connection (for YouTube)
- [ ] ALSA/audio devices configured

#### Dependencies to Install
```bash
# System packages
sudo apt-get update
sudo apt-get install -y \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  libgstreamer1.0-0 \
  gir1.2-gstreamer-1.0 \
  yt-dlp
```

#### Verify Installation
```bash
# Check GStreamer
gst-inspect-1.0 | head -5

# Check yt-dlp
yt-dlp --version

# Check Python gi
python3 -c "import gi; gi.require_version('Gst', '1.0'); print('GStreamer bindings OK')"
```

## Deployment Steps

### 1. Update Repository
```bash
cd /opt/sonixscape
git pull
```

### 2. Install Dependencies
```bash
sudo apt-get install -y gstreamer1.0-plugins-{base,good,bad} \
  libgstreamer1.0-0 gir1.2-gstreamer-1.0 yt-dlp
```

### 3. Restart Audio Service
```bash
sudo systemctl restart sonixscape-audio
```

### 4. Verify Service Started
```bash
sudo systemctl status sonixscape-audio
sudo journalctl -u sonixscape-audio -n 20 -f
```

Should show:
```
[MEDIA] MediaEngine imported successfully
[MEDIA] MediaEngine initialized
```

## Testing Checklist

### Test 1: Local File Playback
- [ ] Load local MP3 file
- [ ] Verify [MEDIA] log messages
- [ ] Hear audio on chair DAC
- [ ] Hear audio on headset (if connected)

### Test 2: YouTube Playback
- [ ] Load YouTube URL
- [ ] yt-dlp resolves to HTTP stream
- [ ] GStreamer pipeline created
- [ ] Playback starts
- [ ] Audio on DAC and headset

### Test 3: Playback Control
- [ ] Pause works (audio stops)
- [ ] Resume works (audio continues)
- [ ] Seek works (jumps to position)
- [ ] Volume control works (audio level changes)

### Test 4: Therapy Mix
- [ ] Use `set-mix` to adjust therapy/music balance
- [ ] Verify DAC hears mixed signal
- [ ] Headset hears media only (no therapy on headset)

### Test 5: Error Handling
- [ ] Invalid YouTube URL fails gracefully
- [ ] Missing local file fails gracefully
- [ ] Service continues running after errors

## Performance Validation

- [ ] Ring buffer maintains audio continuity (no clicks/pops)
- [ ] No playback dropouts during mixing
- [ ] Seek is responsive (< 2 seconds)
- [ ] Volume changes immediately
- [ ] System doesn't crash with rapid commands

## Rollback Plan

If issues occur:
```bash
# Stop service
sudo systemctl stop sonixscape-audio

# Revert commit
cd /opt/sonixscape
git revert 3dc0bc3
git revert 3d72e06

# Or reset to previous state
git reset --hard HEAD~2

# Restart
sudo systemctl start sonixscape-audio
```

## Success Criteria

✓ All deployment steps completed
✓ Audio service starts without errors
✓ [MEDIA] messages appear in logs
✓ media-load command works
✓ media-play produces audio
✓ All control commands work (pause, seek, volume, stop)
✓ Mix control works (therapy + media audible)
✓ No system crashes or hangs
✓ Error messages are clear and informative

## Post-Deployment Tasks

1. [ ] Update phone UI to send media commands
2. [ ] Test full end-to-end: phone → media → audio
3. [ ] Collect user feedback
4. [ ] Monitor for any issues
5. [ ] Plan future enhancements

## Quick Start (After Deployment)

```bash
# From any machine on the network
python test_media_client.py <chair_ip> 8081

# Commands:
# load https://www.youtube.com/watch?v=...
# play
# pause
# seek 120
# volume 0.5
# stop
# quit
```

## Estimated Time

- Deployment: 10-15 minutes
- Testing: 30-45 minutes
- Total: ~1 hour

## Notes

- First media load may take 5-30 seconds (YouTube resolution)
- Subsequent plays are faster
- Internet required for YouTube (not needed for local files)
- VPN may block YouTube URL resolution (use local files for testing)
- Headset output requires Bluetooth pairing first

## Contact

For issues:
1. Check MEDIA_DEPLOYMENT_GUIDE.md troubleshooting section
2. Review console logs ([MEDIA] messages)
3. Verify dependencies installed
4. Check network connectivity (for YouTube)
5. Run test_media_client.py to isolate issues

---

**Status:** Ready for deployment
**Version:** Media Engine v1.0
**Date:** 2025-02-21
**Commit:** 3d72e06
