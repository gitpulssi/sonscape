# Media Engine Integration

## What was added to ws_audio.py

### 1. MediaEngine Import (Lines 8-13)
- Imports MediaEngine from media_engine.py
- Gracefully handles import failure (MEDIA_ENGINE_AVAILABLE flag)
- Falls back to disabled media playback if import fails

### 2. Media Ring Buffer in SineRowPlayer.__init__ (Lines 163-172)
- `self.media_ring` - deque for PCM frame storage (~2 second buffer)
- `self.media_ring_lock` - threading.Lock for thread-safe access
- `self.media_engine` - MediaEngine instance
- MediaEngine initialized with ring buffer and WebSocketHandler reference

### 3. _read_media_from_ring() Method (Lines 715-739)
- Reads stereo frames from media_ring deque
- Converts S16 samples to float32 (-1.0 to 1.0 range)
- Returns numpy array (frames, 2) or silence if no media playing
- Thread-safe via media_ring_lock

### 4. Media Audio Mixing in _generate_therapy_audio() (Lines 547-564)
- Reads media frames using _read_media_from_ring()
- Spreads stereo media across 8 channels like BT audio
- Mixes media with therapy and BT audio: 
  - `mixed = therapy * therapy_gain + bt_8 * music_gain + media_8ch * bt_gain`
- Media audio sent to headset (full bandwidth, unfiltered)
- **Priority:** Media audio takes precedence over BT audio for headset output

### 5. WebSocket Media Command Handling

#### Media Command Routing (Lines 1283-1290)
- Checks for "type" field in JSON messages
- Routes "media-*" commands to handle_media_command()
- Skips action-based handlers for media messages

#### handle_media_command() Method (Lines 2067-2122)
Handles these commands:
- **media-load** - Load local file or YouTube URL
- **media-play** - Start/resume playback  
- **media-pause** - Pause playback
- **media-stop** - Stop and cleanup
- **media-seek** - Seek to position (seconds)
- **media-volume** - Set playback gain (0.0-1.0)

Each command:
- Validates media_engine is available
- Calls appropriate MediaEngine method
- Sends acknowledgment or error to all clients
- Logs to console

## WebSocket Protocol

### Media Load Command
```json
{"type": "media-load", "uri": "/path/to/file.mp3"}
{"type": "media-load", "uri": "https://www.youtube.com/watch?v=..."}
```
Response: `ack:media-load` or `error:media:load-failed`

### Media Playback
```json
{"type": "media-play"}
{"type": "media-pause"}
{"type": "media-stop"}
```
Response: `ack:media-play`, `ack:media-pause`, `ack:media-stop` or errors

### Media Seek
```json
{"type": "media-seek", "seconds": 120.5}
```
Response: `ack:media-seek` or `error:media:seek-failed`

### Media Volume
```json
{"type": "media-volume", "value": 0.75}
```
Response: `ack:media-volume` or `error:media:volume-failed`

## Audio Flow

```
Phone (WiFi) 
  |
  +-- media-load "https://youtube.com/watch?v=..." (or local file)
  +-- media-play
  |
MediaEngine (Chair PC)
  |
  +-- GStreamer decoder
  |   |-- uridecodebin (YouTube or local)
  |   |-- audioconvert
  |   |-- audioresample → 48kHz stereo
  |   |-- appsink → PCM frames
  |
  +-- media_ring (deque) ← Frames written here
      |
      _read_media_from_ring() ← App reads frames
      |
      Audio Loop
      |
      +-- Mix with therapy + BT audio
      |   (media gets full priority for headset)
      |
      +-- 8ch DAC (therapy mix)
      +-- 2ch Bluetooth headset (media)
```

## Testing on Target System

1. Install dependencies:
```bash
pip install yt-dlp gir1.2-gstreamer-1.0
```

2. Start ws_audio.py
3. Send WebSocket message to load and play media:
```json
{"type": "media-load", "uri": "https://www.youtube.com/watch?v=..."}
{"type": "media-play"}
```

4. Monitor console for [MEDIA] debug messages
5. Listen for audio on chair DAC (therapy channels) and headset

## Error Handling

- If media_engine import fails: MEDIA_ENGINE_AVAILABLE = False, media commands return "engine-unavailable"
- If GStreamer pipeline fails: media_engine returns error, WebSocket client notified
- If URI is invalid: media-load fails, client gets "load-failed"
- Ring buffer thread-safe via locks
- Graceful degradation: therapy continues if media fails

## Performance Notes

- Media ring buffer: 96000 frames max (~2 seconds at 48kHz)
- No blocking in audio loop (deque popleft is O(1))
- GStreamer runs in MediaEngine's appsink callback (separate thread)
- Headset output prefers media over BT audio when both present
