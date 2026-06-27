#!/usr/bin/env python3
"""
GStreamer-based media engine for SoniXscape.
Decodes local files and YouTube streams into stereo PCM (48kHz, S16_LE).
Writes frames into the shared ring buffer for mixing with therapy.
"""

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

import threading
import subprocess
import json
import time
from pathlib import Path

Gst.init(None)

RATE = 48000
BLOCK = 1200

class MediaEngine:
    def __init__(self, ring_buffer, ring_lock, ws_handler=None):
        """
        Args:
            ring_buffer: deque of PCM frames (shared with audio loop)
            ring_lock: threading.Lock for ring_buffer access
            ws_handler: WebSocketHandler for state updates
        """
        self.buffer = ring_buffer
        self.lock = ring_lock
        self.ws_handler = ws_handler

        self.pipeline = None
        self.appsink = None
        self.bus = None

        self.state = "idle"
        self.uri = None
        self.title = None
        self.duration = 0.0
        self.position = 0.0
        self.volume = 1.0

        # GLib main loop for GStreamer event processing
        self.main_loop = GLib.MainLoop()
        self.glib_thread = threading.Thread(target=self._run_glib_loop, daemon=True)
        self.glib_thread.start()
        print("[MEDIA] GLib main loop thread started")

    def _run_glib_loop(self):
        """Run GLib main loop in background thread"""
        try:
            self.main_loop.run()
        except Exception as e:
            print(f"[MEDIA] GLib loop error: {e}")
        finally:
            print("[MEDIA] GLib main loop stopped")

    def load(self, uri: str) -> bool:
        """Load a local file or YouTube URL."""
        try:
            self.state = "loading"
            self.uri = uri
            self.title = Path(uri).stem if uri.startswith('/') else uri
            self._send_status()

            # Convert local file paths to file:// URIs
            if uri.startswith('/'):
                uri = f"file://{uri}"

            if 'youtube.com' in uri or 'youtu.be' in uri:
                uri = self._resolve_youtube(uri)
                if not uri:
                    self.state = "error"
                    self._send_status()
                    return False

            if not self._create_pipeline(uri):
                self.state = "error"
                self._send_status()
                return False

            self.state = "idle"
            self._send_status()
            return True

        except Exception as e:
            print(f"[MEDIA] Load error: {e}")
            self.state = "error"
            self._send_status()
            return False

    def _resolve_youtube(self, url: str) -> str:
        """Use yt-dlp to get best audio stream from YouTube."""
        try:
            print(f"[MEDIA] Resolving YouTube: {url}")
            result = subprocess.run(
                ['yt-dlp', '-f', 'bestaudio', '-g', url],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                stream_url = result.stdout.strip()
                print(f"[MEDIA] YouTube stream: {stream_url[:80]}...")
                return stream_url
            else:
                print(f"[MEDIA] yt-dlp failed: {result.stderr}")
                return None
        except Exception as e:
            print(f"[MEDIA] YouTube resolution failed: {e}")
            return None

    def _create_pipeline(self, uri: str) -> bool:
        """Create GStreamer pipeline: uridecodebin → audioconvert → audioresample → appsink"""
        try:
            pipeline_str = (
                f"uridecodebin uri={uri} ! "
                "audioconvert ! "
                f"audioresample ! "
                f"audio/x-raw,format=S16LE,rate={RATE},channels=2 ! "
                "appsink name=sink emit-signals=true max-buffers=10"
            )

            self.pipeline = Gst.parse_launch(pipeline_str)
            self.appsink = self.pipeline.get_by_name("sink")
            self.bus = self.pipeline.get_bus()

            self.appsink.connect("new-sample", self._on_new_sample)
            self.bus.connect("message", self._on_bus_message)
            self.bus.add_watch(GLib.PRIORITY_DEFAULT, self._on_bus_message_watch)

            print(f"[MEDIA] Pipeline created: {pipeline_str[:100]}...")
            return True

        except Exception as e:
            print(f"[MEDIA] Pipeline creation failed: {e}")
            return False

    def _on_new_sample(self, sink):
        """GStreamer appsink callback: write PCM frames to ring buffer."""
        print(f"[MEDIA] _on_new_sample called")
        try:
            sample = sink.emit("pull-sample")
            if not sample:
                return Gst.FlowReturn.OK

            buf = sample.get_buffer()
            data = buf.extract_dup(0, buf.get_size())

            if len(data) > 0:
                with self.lock:
                    num_frames = len(data) // 4
                    self.buffer.extend([data[i*4:(i+1)*4] for i in range(num_frames)])
                    print(f"[MEDIA] Wrote {num_frames} frames to ring buffer")

            return Gst.FlowReturn.OK

        except Exception as e:
            print(f"[MEDIA] Sample processing error: {e}")
            return Gst.FlowReturn.ERROR

    def _on_bus_message_watch(self, bus, message):
        """GLib bus watch callback - processes messages from the main loop"""
        self._on_bus_message(bus, message)
        return True

    def _on_bus_message(self, bus, message):
        """Handle GStreamer bus messages."""
        msg_type = message.type

        if msg_type == Gst.MessageType.EOS:
            print("[MEDIA] End of stream")
            self.stop()

        elif msg_type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"[MEDIA] Error: {err.message}")
            self.state = "error"
            self._send_status()
            self.stop()

        elif msg_type == Gst.MessageType.DURATION_CHANGED:
            _, duration = self.pipeline.query_duration(Gst.Format.TIME)
            if duration != Gst.CLOCK_TIME_NONE:
                self.duration = duration / Gst.SECOND
                self._send_status()

    def play(self) -> bool:
        """Start or resume playback."""
        try:
            if not self.pipeline:
                print("[MEDIA] No pipeline loaded")
                return False

            self.pipeline.set_state(Gst.State.PLAYING)
            self.state = "playing"
            print("[MEDIA] Playing")
            self._send_status()
            return True

        except Exception as e:
            print(f"[MEDIA] Play error: {e}")
            return False

    def pause(self) -> bool:
        """Pause playback."""
        try:
            if not self.pipeline:
                return False

            self.pipeline.set_state(Gst.State.PAUSED)
            self.state = "paused"
            print("[MEDIA] Paused")
            self._send_status()
            return True

        except Exception as e:
            print(f"[MEDIA] Pause error: {e}")
            return False

    def stop(self) -> bool:
        """Stop playback and clean up."""
        try:
            if self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline = None
                self.appsink = None

            self.state = "stopped"
            self.position = 0.0
            print("[MEDIA] Stopped")
            self._send_status()
            return True

        except Exception as e:
            print(f"[MEDIA] Stop error: {e}")
            return False

    def seek(self, seconds: float) -> bool:
        """Seek to position (seconds)."""
        try:
            if not self.pipeline:
                return False

            ns = int(seconds * Gst.SECOND)
            self.pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, ns)
            print(f"[MEDIA] Seek to {seconds:.1f}s")
            self._send_status()
            return True

        except Exception as e:
            print(f"[MEDIA] Seek error: {e}")
            return False

    def set_volume(self, gain: float) -> bool:
        """Set playback volume (0.0 to 1.0)."""
        try:
            self.volume = max(0.0, min(1.0, gain))
            print(f"[MEDIA] Volume: {self.volume:.2f}")
            self._send_status()
            return True

        except Exception as e:
            print(f"[MEDIA] Volume error: {e}")
            return False

    def get_position(self) -> float:
        """Query current playback position (seconds)."""
        try:
            if not self.pipeline:
                return 0.0

            ok, pos = self.pipeline.query_position(Gst.Format.TIME)
            if ok and pos != Gst.CLOCK_TIME_NONE:
                self.position = pos / Gst.SECOND

            return self.position

        except Exception as e:
            print(f"[MEDIA] Position query error: {e}")
            return 0.0

    def _send_status(self):
        """Send media status to connected WebSocket clients."""
        if not self.ws_handler:
            return

        status = {
            "type": "media-status",
            "state": self.state,
            "title": self.title,
            "position": self.get_position(),
            "duration": self.duration,
            "volume": self.volume
        }

        try:
            self.ws_handler._queue_message(json.dumps(status))
        except Exception as e:
            print(f"[MEDIA] Status send error: {e}")
