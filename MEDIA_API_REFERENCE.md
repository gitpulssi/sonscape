# SoniXscape Media Control API Reference

## Quick Start Example

```python
import asyncio
import websockets
import json

async def control_media(chair_ip="192.168.1.100", port=8081):
    async with websockets.connect(f"ws://{chair_ip}:{port}") as ws:
        # Load YouTube video
        await ws.send(json.dumps({
            "type": "media-load",
            "uri": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        }))
        print(await ws.recv())  # ack:media-load
        
        # Play
        await ws.send(json.dumps({"type": "media-play"}))
        print(await ws.recv())  # ack:media-play
        
        # Wait 5 seconds
        await asyncio.sleep(5)
        
        # Set volume to 50%
        await ws.send(json.dumps({
            "type": "media-volume",
            "value": 0.5
        }))
        print(await ws.recv())  # ack:media-volume

asyncio.run(control_media())
```

## API Endpoints

### Load Media
**Command:**
```json
{
  "type": "media-load",
  "uri": "string"
}
```

**URI Format:**
- Local file: `/path/to/file.mp3` (full path)
- YouTube: `https://www.youtube.com/watch?v=VIDEO_ID`
- YouTube shortlink: `https://youtu.be/VIDEO_ID`
- HTTP stream: `https://example.com/stream.mp3`

**Supported Formats:**
- MP3, FLAC, WAV, OGG, Opus, AAC
- Any format supported by GStreamer

**Response:**
- Success: `ack:media-load`
- Error: `error:media:load-failed`

**Notes:**
- YouTube URLs are automatically resolved to HTTP stream via yt-dlp
- Local files should exist and be readable
- Automatically stops previous media before loading new

---

### Play
**Command:**
```json
{
  "type": "media-play"
}
```

**Response:**
- Success: `ack:media-play`
- Error: `error:media:play-failed`

**Notes:**
- Starts playback if stopped or paused
- Does nothing if already playing

---

### Pause
**Command:**
```json
{
  "type": "media-pause"
}
```

**Response:**
- Success: `ack:media-pause`
- Error: `error:media:pause-failed`

**Notes:**
- Pauses playback without stopping
- Can resume with `media-play`
- Preserves playback position

---

### Stop
**Command:**
```json
{
  "type": "media-stop"
}
```

**Response:**
- Success: `ack:media-stop`
- Error: `error:media:stop-failed`

**Notes:**
- Completely stops playback
- Resets position to 0
- Clears GStreamer pipeline
- Media must be reloaded to play again

---

### Seek
**Command:**
```json
{
  "type": "media-seek",
  "seconds": 120.5
}
```

**Parameters:**
- `seconds` (float): Target position in seconds

**Response:**
- Success: `ack:media-seek`
- Error: `error:media:seek-failed`

**Notes:**
- Position must be within media duration
- Pauses playback during seek
- Supports fractional seconds (e.g., 1.5 for 1.5 seconds)
- Position resets if media stops

---

### Volume
**Command:**
```json
{
  "type": "media-volume",
  "value": 0.75
}
```

**Parameters:**
- `value` (float): Volume gain 0.0 (silent) to 1.0 (full)

**Response:**
- Success: `ack:media-volume`
- Error: `error:media:volume-failed`

**Notes:**
- Values outside 0.0-1.0 are clamped
- Soft gain (digital amplitude scaling)
- Applies immediately
- Default is 1.0 (full volume)
- Affects audio mix with therapy signal

---

## Status Messages

**Format:**
```json
{
  "type": "media-status",
  "state": "playing|paused|stopped|loading|error|idle",
  "title": "Song Title or Filename",
  "position": 121.4,
  "duration": 306.2,
  "volume": 0.75
}
```

**Status Values:**
- `playing` - Media is currently playing
- `paused` - Media is paused (can resume)
- `stopped` - Media is stopped (must reload)
- `loading` - Media is loading/buffering
- `error` - An error occurred
- `idle` - No media loaded

**Notes:**
- Status messages are sent automatically when state changes
- Position and duration are in seconds
- Currently sent to console; WebSocket broadcasting is pending

---

## Error Responses

**Format:**
```
error:media:<error_code>:<optional_detail>
```

**Error Codes:**
- `engine-unavailable` - MediaEngine not initialized
- `no-uri` - URI parameter missing from media-load
- `load-failed` - Failed to load media file
- `play-failed` - Failed to start playback
- `pause-failed` - Failed to pause
- `stop-failed` - Failed to stop
- `seek-failed` - Failed to seek
- `volume-failed` - Failed to set volume
- `unknown-command` - Unknown media command type
- `command-error` - General command execution error

---

## Audio Output

### Chair (8-Channel DAC)
- **Channels 0-1 (Neck):** Therapy + Media mix
- **Channels 2-3 (Back):** Therapy + Media mix
- **Channels 4-5 (Thighs):** Therapy + Media mix
- **Channels 6-7 (Legs):** Therapy + Media mix

**Mix Formula:** `output = therapy * therapy_gain + media * media_gain`
- therapy_gain and media_gain controlled by `set-mix` action
- Default mix: 50% therapy, 50% music (when audio playing)

### Headset (Bluetooth A2DP)
- **Stereo Output:** Full-bandwidth media audio
- **Filter:** None (unfiltered)
- **Priority:** Media takes precedence over BT capture audio

---

## Integration with Therapy

### Set Mix Control
**Command:**
```json
{
  "action": "set-mix",
  "value": 75
}
```

**Value:** 0-100
- 0: Therapy only (no music)
- 50: Equal mix
- 100: Music only (no therapy)

**Notes:**
- This is an existing action (not media-specific)
- Controls balance between therapy and all audio sources (BT + media)

---

## Response Flow Example

```
Client → Server: {"type": "media-load", "uri": "https://youtu.be/..."}
Server → Client: ack:media-load

Client → Server: {"type": "media-play"}
Server → Client: ack:media-play
[Audio starts]

Client → Server: {"type": "media-volume", "value": 0.5}
Server → Client: ack:media-volume

Client → Server: {"type": "media-seek", "seconds": 60.0}
Server → Client: ack:media-seek

Client → Server: {"type": "media-pause"}
Server → Client: ack:media-pause

Client → Server: {"type": "media-play"}
Server → Client: ack:media-play

Client → Server: {"type": "media-stop"}
Server → Client: ack:media-stop
```

---

## Common Use Cases

### Load and Play YouTube
```json
{"type": "media-load", "uri": "https://www.youtube.com/watch?v=..."}
{"type": "media-play"}
```

### Skip to Middle of Song
```json
{"type": "media-seek", "seconds": 150.0}
```

### Reduce Volume
```json
{"type": "media-volume", "value": 0.3}
```

### Change Audio Source
```json
{"type": "media-stop"}
{"type": "media-load", "uri": "https://youtu.be/..."}
{"type": "media-play"}
```

### Pause Therapy and Keep Music
```json
{"action": "set-mix", "value": 100}
```

### Resume Therapy with Music
```json
{"action": "set-mix", "value": 50}
```

---

## Troubleshooting Commands

### Verify Connection
```json
{"action": "ready"}
```
Response: `ack:ready`

### Clear Highlight (UI related)
```json
{"action": "clear-highlight"}
```

### Check Status (prints to console)
Just wait for automatic status messages after load/play/seek

---

## Rate Limits

- No rate limits on WebSocket commands
- GStreamer pipeline may buffer large files
- YouTube streaming depends on internet speed
- Typical load time: 1-5 seconds (depends on file size and network)

---

## WebSocket Connection Details

- **Protocol:** WebSocket (ws://)
- **Host:** Chair PC IP (e.g., 192.168.1.100)
- **Port:** 8081 (default)
- **Message Format:** JSON
- **Timeout:** No server-side timeout (client can maintain connection)

---

## Performance Characteristics

- **Ring Buffer:** 96,000 frames (~2 seconds at 48kHz)
- **Latency:** ~50-100ms (GStreamer + mixing)
- **Max Files:** Unlimited (limited by storage)
- **Concurrent:** 1 media stream (single GStreamer pipeline)

---

## Future Enhancements

Planned additions:
- Media status WebSocket broadcast (currently console only)
- Playlist queue support
- Media metadata (artist, album, cover art)
- Local file browser
- Recording to storage
- Bitrate/quality selection for YouTube
