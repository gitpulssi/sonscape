#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Simple WebSocket-only audio server for SoniXcape
This handles only WebSocket connections and audio processing
HTTP requests are handled by the separate preset_server.py
"""

import sounddevice as sd
import asyncio, os, time, math, numpy as np
import websockets
import subprocess, shlex, sys, atexit, signal
import json
import threading
import fcntl
import re
from pathlib import Path

# Try to import alsaaudio for Bluetooth integration
try:
    import alsaaudio
    ALSA_AVAILABLE = True
    print("[BT] ALSA audio support available")
except ImportError:
    ALSA_AVAILABLE = False
    print("[BT] ALSA audio not available - Bluetooth mixing disabled")

# Single instance lock
_lockfd = open("/tmp/ws_audio.lock", "w")
try:
    fcntl.flock(_lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    print("[FATAL] Another ws_audio.py is already running; exiting.")
    sys.exit(1)

# Constants
RATE = 48000
BLOCK = 2400
CHANNELS = 8
WS_PORT = 8081  # Only WebSocket port, no HTTP
DEVICE_NAME = "ICUSBAUDIO7D"
FADE_TIME = 4.0
FADE_SAMPLES = int(FADE_TIME * RATE)

# Channel mapping
CHANNEL_MAP = {
    "neck": (0, 1), "back": (4, 5), "thighs": (6, 7), "legs": (2, 3)
}

# MODE_ROUTING (unchanged from your original)
MODE_ROUTING = {
    0: {0: [0, 1], 1: [2, 3], 2: [4, 5], 3: [6, 7]},  # Standard
    1: {0: [0, 1], 1: [2, 3], 2: [4, 5], 3: [6, 7]},  # Linear
    2: {0: [0, 4], 1: [1, 5], 2: [2, 6], 3: [3, 7]},  # Alt Sides
    3: {0: [0, 2], 1: [1, 3], 2: [4, 6], 3: [5, 7]},  # Front-Back
    4: {0: [0, 1], 1: [4, 5], 2: [2, 3], 3: [6, 7]},  # Upper-Lower
    5: {0: [0, 5], 1: [1, 4], 2: [2, 7], 3: [3, 6]},  # Cross
    6: {0: [0, 2], 1: [1, 3], 2: [4, 6], 3: [5, 7]},  # Quad
    7: {0: [0, 3], 1: [1, 2], 2: [4, 7], 3: [5, 6]}   # Custom
}

CONFIG_PATH = Path.home() / "webui" / "config.json"

def load_config():
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
    except Exception as e:
        print(f"[CFG] load error: {e}")
    return {}

def scaled_amp(strength_step: int, trim_step: int) -> float:
    strength_step = max(0, min(9, strength_step))
    trim_step = max(0, min(9, trim_step))
    base = strength_step * 10
    if trim_step == 5:
        amp = base
    elif trim_step < 5:
        amp = base - (5 - trim_step) * (base / 5)
    else:
        amp = base + (trim_step - 5) * ((90 - base) / 5)
    return amp / 90.0

class SineRowPlayer:
    def __init__(self, ws_handler=None):
        self.stream = None
        self.row = None
        self.start_t = 0.0
        self.phase_accum = 0.0
        self.mod_phase_accum = 0.0
        self.last_inst_f = 20.0
        self.device_available = False
        self.ws_handler = ws_handler
        self.output_device_hint = "ICUSBAUDIO7D"
        self.therapy_gain = 1.0

        # Bluetooth audio integration
        self.bt_input = None
        self.bt_mac_current = None
        self.bt_gain = 0.5  # Mix slider (0=all therapy, 1=all music)
        self.bt_buffer = np.zeros((BLOCK, 2), dtype=np.float32)  # Stereo buffer
        self.bt_enabled = False
        self.bt_mono = True                 # False = stereo fan-out, True = mono to all 8
        self.bt_lpf_fc = 200.0              # Hz
        self._bt_lpf_coeffs = None          # (b0,b1,b2,a1,a2)
        self._bt_lpf_state = np.zeros((2, 2), dtype=np.float32)  # per-channel biquad z1,z2
        self._bt_zero_blocks = 0             # how many consecutive empty reads
        self._bt_zero_limit  = 60            # ~3s @ 48k, block 2400 (50ms per block) => 60 blocks ˜ 3.0s
        self._bt_reinit_cooldown_s = 5.0     # don’t thrash; wait at least 5s between re-inits
        self._bt_last_reinit = 0.0
        
        # Sequence state
        self.sequence_rows = None
        self.current_row_index = 0
        self.sequence_start_t = 0.0
        self.is_playing_sequence = False
        self.last_notified_row = -1

        # Fade state
        self.fade_samples_remaining = 0
        self.fade_direction = 0
        self.fade_multiplier = 0.0

        self.ensure_stream()
        
    def _design_biquad_lowpass(self, fc, fs, Q=0.7071):
        """Cookbook biquad LPF (RBJ) ? returns normalized (b0,b1,b2,a1,a2)."""
        w0 = 2.0 * np.pi * float(fc) / float(fs)
        alpha = np.sin(w0) / (2.0 * float(Q))
        cosw0 = np.cos(w0)

        b0 = (1.0 - cosw0) * 0.5
        b1 = 1.0 - cosw0
        b2 = (1.0 - cosw0) * 0.5
        a0 = 1.0 + alpha
        a1 = -2.0 * cosw0
        a2 = 1.0 - alpha

        # normalize
        return (b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)

    def _biquad_process_stereo(self, x_stereo, coeffs, state):
        """Process 2-ch block with a single biquad, transposed direct-form II."""
        if coeffs is None:
            return x_stereo
        b0, b1, b2, a1, a2 = coeffs
        y = np.empty_like(x_stereo, dtype=np.float32)
        # state: shape (2,2) [[z1L,z2L],[z1R,z2R]]
        for ch in (0, 1):
            z1, z2 = float(state[ch, 0]), float(state[ch, 1])
            xs = x_stereo[:, ch].astype(np.float32, copy=False)
            ys = np.empty_like(xs, dtype=np.float32)
            for n in range(xs.shape[0]):
                v = xs[n] - a1 * z1 - a2 * z2
                out = b0 * v + b1 * z1 + b2 * z2
                ys[n] = out
                z2 = z1
                z1 = v
            state[ch, 0], state[ch, 1] = z1, z2
            y[:, ch] = ys
        return y

    def _read_bt_stereo(self, frames):
        """Return (frames,2) float32 in [-1,1] at RATE; silence if BT off/unavailable.
           Auto-recovers if BlueALSA goes silent or the device disappears."""
        if not self.bt_enabled or not self.bt_input:
            self._bt_zero_blocks = 0
            return np.zeros((frames, 2), dtype=np.float32)

        try:
            length, data = self.bt_input.read()

            # Fast path: device disappeared (BlueALSA closed the source)
            if length is None and data is None:
                # Some pyalsaaudio builds return (None, None) on ENODEV; treat as error
                raise RuntimeError("bluealsa read returned (None, None)")

            if length <= 0 or not data:
                # empty read: count it and maybe recycle after ~3s
                self._bt_zero_blocks += 1
                if self._bt_zero_blocks >= self._bt_zero_limit:
                    now = time.monotonic()
                    if (now - self._bt_last_reinit) >= self._bt_reinit_cooldown_s:
                        print("[BT] Watchdog: BlueALSA silent ~3s - recycling capture")
                        try:
                            self.bt_input.close()
                        except Exception:
                            pass
                        self.bt_input = None
                        self.bt_enabled = False
                        self.bt_mac_current = None
                        self._bt_last_reinit = now
                return np.zeros((frames, 2), dtype=np.float32)

            # got data => reset watchdog
            self._bt_zero_blocks = 0

            s = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32767.0
            if s.size < 2:
                return np.zeros((frames, 2), dtype=np.float32)

            st = s.reshape(-1, 2)
            n = st.shape[0]
            if n < frames:
                pad = np.zeros((frames - n, 2), dtype=np.float32)
                st = np.vstack([st, pad])
            elif n > frames:
                st = st[:frames, :]
            return st

        except Exception as e:
            # If the device is gone, recycle immediately (no 3s wait)
            txt = str(e)
            if "No such device" in txt or "ENODEV" in txt or "disconnected" in txt.lower():
                now = time.monotonic()
                if (now - self._bt_last_reinit) >= self._bt_reinit_cooldown_s:
                    print("[BT] Device gone - recycling capture now")
                    try:
                        self.bt_input.close()
                    except Exception:
                        pass
                    self.bt_input = None
                    self.bt_enabled = False
                    self.bt_mac_current = None
                    self._bt_last_reinit = now
                # return silence until autoconnect re-establishes
                return np.zeros((frames, 2), dtype=np.float32)

            # Other errors -> treat like silence and log occasionally
            self._bt_zero_blocks += 1
            if (self._bt_zero_blocks % 40) == 0:
                print(f"[BT] Read error (count {self._bt_zero_blocks}): {e}")
            return np.zeros((frames, 2), dtype=np.float32)

    def _bt_to_8ch(self, bt_stereo_block):
        """LPF 200 Hz, then mono or stereo fan-out to 8 channels."""
        # Lazy init/update LPF if needed
        if self._bt_lpf_coeffs is None:
            self._bt_lpf_coeffs = self._design_biquad_lowpass(self.bt_lpf_fc, RATE, Q=0.7071)

        # 200 Hz LPF
        bt_lp = self._biquad_process_stereo(bt_stereo_block, self._bt_lpf_coeffs, self._bt_lpf_state)

        frames = bt_lp.shape[0]
        out = np.zeros((frames, CHANNELS), dtype=np.float32)

        if self.bt_mono:
            mono = bt_lp.mean(axis=1, keepdims=True)  # (N,1)
            out[:] = mono  # broadcast to all 8 channels
        else:
            L = bt_lp[:, 0:1]  # (N,1)
            R = bt_lp[:, 1:1+1]
            # L ? ch 0,2,4,6 ; R ? ch 1,3,5,7
            out[:, 0] = L[:, 0]
            out[:, 2] = L[:, 0]
            out[:, 4] = L[:, 0]
            out[:, 6] = L[:, 0]
            out[:, 1] = R[:, 0]
            out[:, 3] = R[:, 0]
            out[:, 5] = R[:, 0]
            out[:, 7] = R[:, 0]
        return out

    def is_device_available(self) -> bool:
        try:
            target = getattr(self, "output_device_hint", DEVICE_NAME)
            for dev in sd.query_devices():
                if target in dev["name"]:
                    # For ICUSBAUDIO7D, accept any channel count since sounddevice detection is unreliable
                    if "ICUSBAUDIO7D" in target:
                        return True
                    elif dev["max_output_channels"] >= CHANNELS:
                        return True
        except Exception as e:
            print(f"[!] Error querying devices: {e}")
        return False

    def _list_output_devices(self):
        """Return a list of (index, name, max_out) for all output devices."""
        out = []
        try:
            for i, dev in enumerate(sd.query_devices()):
                max_out = int(dev.get("max_output_channels", 0) or 0)
                name = str(dev.get("name", ""))
                if max_out > 0:
                    out.append((i, name, max_out))
        except Exception as e:
            print(f"[!] Error listing devices: {e}")
        return out

    def _find_best_8ch_device(self, hint="ICUSBAUDIO7D"):
        """Pick the ICUSBAUDIO7D (or hint) device advertising >=8 outputs.
        If multiple, choose the one with the largest channel count.
        Returns (index, name, max_out) or (None, None, 0)."""
        candidates = []
        for i, name, max_out in self._list_output_devices():
            if hint in name and max_out >= 8:
                candidates.append((i, name, max_out))
        if not candidates:
            # As a fallback, accept any device (not just hint) with >=8 outputs
            for i, name, max_out in self._list_output_devices():
                if max_out >= 8:
                    candidates.append((i, name, max_out))
        if not candidates:
            return None, None, 0
        # Prefer the highest channel count, then shortest name (tie-break), then lowest index
        candidates.sort(key=lambda t: (-t[2], len(t[1]), t[0]))
        return candidates[0]

    def _setup_bluetooth_input(self, bt_mac):
        """Setup Bluetooth input.

        Strategy:
        1) Try direct BlueALSA CAPTURE once (will often be unavailable or busy for A2DP sinks).
        2) If /proc/asound/Loopback exists, start bluealsa-aplay ? plughw:Loopback,0 and capture from hw:Loopback,1,0.
        3) If loopback does not exist (no permission to load), cleanly disable BT capture so therapy runs unaffected.
        """
        if not ALSA_AVAILABLE:
            print("[BT] ALSA not available - cannot setup Bluetooth input")
            return False

        # Close any previous capture
        try:
            if self.bt_input:
                try:
                    self.bt_input.close()
                except:
                    pass
            self.bt_input = None
        except Exception:
            pass

        # Kill any previous bluealsa-aplay we may have spawned for this MAC
        try:
            subprocess.run(["pkill", "-f", f"bluealsa-aplay.*{bt_mac}"], check=False)
        except Exception:
            pass

        # --- Attempt direct BlueALSA capture (A2DP sinks are often not capturable) ---
        pcm_device = f"bluealsa:DEV={bt_mac},PROFILE=a2dp"
        print(f"[BT] Attempting to open {pcm_device}")
        try:
            cap = alsaaudio.PCM(
                type=alsaaudio.PCM_CAPTURE,
                mode=alsaaudio.PCM_NONBLOCK,   # non-blocking read loop
                device=pcm_device,
                channels=2,
                rate=RATE,                     # match your 48k pipeline
                format=alsaaudio.PCM_FORMAT_S16_LE,
                periodsize=BLOCK
            )
            self.bt_input = cap
            self.bt_mac_current = bt_mac
            self.bt_enabled = True
            print(f"[BT] Direct BlueALSA CAPTURE established for {bt_mac}")
            return True
        except Exception as direct_err:
            print(f"[BT] Direct CAPTURE not available ({direct_err}); evaluating Loopback fallback...")

        # --- Loopback fallback only if the device already exists (no modprobe here) ---
        if not os.path.exists("/proc/asound/Loopback"):
            print("[BT] Loopback device not present; skipping BT capture (therapy continues).")
            self.bt_input = None
            self.bt_enabled = False
            return False

        # Start bridge writer
        try:
            self._ba_proc = subprocess.Popen(
                ["bluealsa-aplay",
                 "-r", "48000",
                 "-d", "plughw:Loopback,0",
                 bt_mac],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT
            )
            print("[BT] Started bluealsa-aplay ? plughw:Loopback,0 @48k")
        except Exception as e:
            print(f"[BT] Failed to start bluealsa-aplay: {e}")

        # Open mirror capture side
        loop_dev = "hw:Loopback,1,0"
        for _ in range(10):
            try:
                cap = alsaaudio.PCM(alsaaudio.PCM_CAPTURE, device=loop_dev)
                cap.setchannels(2)
                cap.setrate(48000)
                cap.setformat(alsaaudio.PCM_FORMAT_S16_LE)
                cap.setperiodsize(BLOCK)
                self.bt_input = cap
                self.bt_mac_current = bt_mac
                self.bt_enabled = True
                print(f"[BT] Loopback capture established on {loop_dev}")
                return True
            except Exception:
                time.sleep(0.1)

        print("[BT] Loopback capture failed to initialize")
        self.bt_input = None
        self.bt_enabled = False
        return False

    def _read_bluetooth_audio(self, frames):
        """Read Bluetooth audio and convert stereo to 8ch. Return None if BT off."""
        if not getattr(self, "bt_input", None) or not getattr(self, "bt_enabled", False):
            return None
        try:
            # Non-blocking read
            length, data = self.bt_input.read()
            if length <= 0 or not data:
                # Return silence frame-shaped to keep mixer stable
                return np.zeros((frames, CHANNELS), dtype=np.float32)

            # 16-bit little-endian PCM ? float32 [-1..1]
            bt_samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32767.0

            # Reshape to stereo
            if bt_samples.size < 2:
                return np.zeros((frames, CHANNELS), dtype=np.float32)
            bt_stereo = bt_samples.reshape(-1, 2)

            # Fit length to frames (pad or trim)
            n = bt_stereo.shape[0]
            if n < frames:
                pad = np.zeros((frames - n, 2), dtype=np.float32)
                bt_stereo = np.vstack([bt_stereo, pad])
            elif n > frames:
                bt_stereo = bt_stereo[:frames, :]

            # Expand to 8ch by distributing L/R
            out = np.zeros((frames, CHANNELS), dtype=np.float32)
            for ch in range(CHANNELS):
                out[:, ch] = bt_stereo[:, ch % 2]
            return out

        except Exception as e:
            # Log once in a while but don't break therapy
            if not hasattr(self, "_bt_err_count"):
                self._bt_err_count = 0
            self._bt_err_count += 1
            if self._bt_err_count % 50 == 1:
                print(f"[BT] Audio read error: {e}")
            # Return silence so therapy continues
            return np.zeros((frames, CHANNELS), dtype=np.float32)

    def ensure_stream(self):
        current_device_status = self.is_device_available()

        if current_device_status != self.device_available:
            print(f"[+] Device {'available' if current_device_status else 'unavailable'}")
            self.device_available = current_device_status

        if not current_device_status:
            if self.stream:
                try:
                    self.stream.close()
                except:
                    pass
                self.stream = None
            return False

        try:
            if self.stream:
                try:
                    if self.stream.active:
                        self.stream.stop()
                    self.stream.close()
                except:
                    pass
                self.stream = None
                time.sleep(0.2)

            # Find a true 8-channel device (prefer ICUSBAUDIO7D)
            target_hint = getattr(self, "output_device_hint", DEVICE_NAME)
            best_idx, best_name, best_max = self._find_best_8ch_device(target_hint)

            if best_idx is None:
                print("[!] No 8-channel output device found. Available outputs:")
                for i, name, max_out in self._list_output_devices():
                    print(f"    - [{i}] {name} : max_out={max_out}")
                # Fail fast (don't fall back to 2ch silently)
                return False

            print(f"[+] Selected 8-ch device [{best_idx}] {best_name} (max_out={best_max})")

            channels = 8  # hard target for therapy rig
            if best_max < channels:
                print(f"[!] Device reports only {best_max} outputs; cannot open {channels}ch.")
                return False

            self.stream = sd.OutputStream(
                samplerate=RATE, blocksize=BLOCK, channels=channels,
                dtype='int16', device=best_idx, latency=0.05,
                callback=self._callback
            )
            self.stream.start()
            print("[+] 8-channel audio stream started successfully.")
            return True

        except Exception as e:
            print(f"[!] Failed to start 8-ch stream: {e}")
            self.stream = None
            return False

    def play_row(self, row):
        if not self.stream or not self.is_device_available():
            self.ensure_stream()
        self.is_playing_sequence = False
        self.sequence_rows = None
        self.row = row
        self.start_t = time.perf_counter()
        self.phase_accum = 0.0
        self.mod_phase_accum = 0.0
        self.last_inst_f = float(row.get("frequency", 20.0))
        self._start_fade_in()

    def play_sequence(self, rows):
        if not self.stream or not self.is_device_available():
            self.ensure_stream()
        valid_rows = [row for row in rows if row.get("time", 0) > 0 and row.get("frequency", 0) > 0]
        if not valid_rows:
            print("[!] No valid rows to play in sequence")
            return
        print(f"[INFO] Starting sequence with {len(valid_rows)} rows")
        self.sequence_rows = valid_rows
        self.current_row_index = 0
        self.is_playing_sequence = True
        self._start_sequence_row(0)

    def _start_sequence_row(self, index):
        if not self.sequence_rows or index >= len(self.sequence_rows):
            return
        self.current_row_index = index
        self.row = self.sequence_rows[index]
        self.sequence_start_t = time.perf_counter()
        self.phase_accum = 0.0
        self.mod_phase_accum = 0.0
        self.last_inst_f = float(self.row.get("frequency", 20.0))
        self._start_fade_in()
        if self.ws_handler and index != self.last_notified_row:
            self.ws_handler.queue_highlight(index)
            self.last_notified_row = index

    def stop(self):
        if self.ws_handler and self.is_playing_sequence:
            self.ws_handler.queue_clear_highlight()
        self.row = None
        self.is_playing_sequence = False
        self.sequence_rows = None
        self.last_notified_row = -1
        self.fade_samples_remaining = 0
        self.fade_direction = 0
        self.fade_multiplier = 0.0

    def _start_fade_in(self):
        self.fade_samples_remaining = FADE_SAMPLES
        self.fade_direction = 1
        self.fade_multiplier = 0.0

    def _start_fade_out(self):
        self.fade_samples_remaining = FADE_SAMPLES
        self.fade_direction = -1

    def _apply_fade(self, signal, frames):
        if self.fade_direction == 0 and self.fade_samples_remaining <= 0:
            return signal
        fade_envelope = np.ones(frames, dtype=np.float32)
        if self.fade_samples_remaining > 0:
            samples_to_process = min(frames, self.fade_samples_remaining)
            for i in range(samples_to_process):
                if self.fade_direction == 1:
                    progress = (FADE_SAMPLES - self.fade_samples_remaining + i) / FADE_SAMPLES
                    self.fade_multiplier = progress
                elif self.fade_direction == -1:
                    progress = (self.fade_samples_remaining - i) / FADE_SAMPLES
                    self.fade_multiplier = progress
                fade_envelope[i] = self.fade_multiplier
            self.fade_samples_remaining -= samples_to_process
            if self.fade_samples_remaining <= 0:
                if self.fade_direction == 1:
                    self.fade_multiplier = 1.0
                elif self.fade_direction == -1:
                    self.fade_multiplier = 0.0
                self.fade_direction = 0
            if samples_to_process < frames:
                fade_envelope[samples_to_process:] = self.fade_multiplier
        else:
            fade_envelope[:] = self.fade_multiplier
        if signal.ndim == 2:
            return signal * fade_envelope[:, None]
        else:
            return signal * fade_envelope

    def _generate_square_wave(self, duty_percent, mod_freq, frames):
        dt = 1.0 / RATE
        duty_ratio = duty_percent / 100.0
        square_wave = np.zeros(frames)
        for i in range(frames):
            phase_norm = (self.mod_phase_accum / (2 * np.pi)) % 1.0
            square_wave[i] = 1.0 if phase_norm < duty_ratio else 0.0
            self.mod_phase_accum += 2 * np.pi * mod_freq * dt
            self.mod_phase_accum = self.mod_phase_accum % (2 * np.pi)
        return square_wave

    def _generate_4_channel_audio(self, f0, fsweep, sspd, t0, tt_block, base_phase, frames):
        dt = 1.0 / RATE
        if fsweep and sspd:
            lfo = np.sin(2*np.pi*sspd*(t0 + tt_block))
            inst_f = f0 + fsweep * lfo
            inst_f = np.clip(inst_f, 20, 200)
        else:
            inst_f = np.full_like(tt_block, f0)
        audio_outputs = np.zeros((frames, 4), dtype=np.float32)
        for output_idx in range(4):
            total_phase_deg = base_phase * output_idx
            total_phase_rad = np.deg2rad(total_phase_deg)
            if isinstance(inst_f, np.ndarray):
                phase_increments = 2 * np.pi * inst_f * dt
                phi = self.phase_accum + np.cumsum(phase_increments) + total_phase_rad
            else:
                phase_increment = 2 * np.pi * inst_f * dt
                phi = self.phase_accum + np.arange(frames) * phase_increment + total_phase_rad
            carrier = np.sin(phi).astype(np.float32)
            audio_outputs[:, output_idx] = carrier
        if isinstance(inst_f, np.ndarray):
            phase_increments = 2 * np.pi * inst_f * dt
            self.phase_accum += np.sum(phase_increments)
        else:
            self.phase_accum += frames * 2 * np.pi * inst_f * dt
        self.phase_accum %= 2*np.pi
        return audio_outputs

    def _route_audio_to_speakers(self, audio_outputs, mode):
        if mode not in MODE_ROUTING:
            mode = 0
        routing = MODE_ROUTING[mode]
        speaker_outputs = np.zeros((audio_outputs.shape[0], CHANNELS), dtype=np.float32)
        for output_idx, speaker_list in routing.items():
            if output_idx < audio_outputs.shape[1]:
                for speaker_idx in speaker_list:
                    if 0 <= speaker_idx < CHANNELS:
                        speaker_outputs[:, speaker_idx] = audio_outputs[:, output_idx]
        return speaker_outputs

    def _callback(self, outdata, frames, time_info, status):
        try:
            outdata[:] = 0
            
            # Initialize therapy signal
            therapy_signal = np.zeros((frames, CHANNELS), dtype=np.float32)
            
            # Generate therapy audio if active
            row = self.row
            if row:
                dt = 1.0 / RATE
                tt_block = np.arange(frames) * dt

                # Row parameters (use snapshot)
                f0       = float(row.get("frequency", 20.0))
                fsweep   = float(row.get("freqSweep", 0))
                sspd     = float(row.get("sweepSpeed", 0))
                dur      = float(row.get("time", 60))
                phase    = float(row.get("phase", 0))
                mod_freq = float(row.get("modFreq", 1))
                tap_duty = float(row.get("tapDuty", 0))
                mode     = int(row.get("mode", 0))

                # Timing
                t0 = (time.perf_counter() - (self.sequence_start_t if self.is_playing_sequence else self.start_t))

                # Fade-out pre-roll
                fade_start_time = dur - FADE_TIME
                if t0 >= fade_start_time and self.fade_direction == 0 and dur > FADE_TIME:
                    self._start_fade_out()

                if t0 >= dur:
                    if self.is_playing_sequence:
                        next_index = self.current_row_index + 1
                        if next_index < len(self.sequence_rows):
                            self._start_sequence_row(next_index)
                            t0 = (time.perf_counter() - self.sequence_start_t)
                        else:
                            if self.ws_handler:
                                self.ws_handler.queue_clear_highlight()
                            self.row = None
                            self.is_playing_sequence = False
                            self.sequence_rows = None
                            self._reset_state()
                            return
                    else:
                        self.row = None
                        self._reset_state()
                        return

                # Generate carriers
                audio_outputs = self._generate_4_channel_audio(f0, fsweep, sspd, t0, tt_block, phase, frames)

                # Modulation
                if mod_freq > 0:
                    if tap_duty == 0:
                        # Sine LFO
                        mod_phi = np.zeros(frames)
                        for i in range(frames):
                            self.mod_phase_accum += 2 * np.pi * mod_freq * dt
                            mod_phi[i] = self.mod_phase_accum
                            self.mod_phase_accum %= 2 * np.pi
                        mod_lfo = np.sin(mod_phi)
                        amp_env = (mod_lfo + 1.0) * 0.5
                        modulated_outputs = audio_outputs * amp_env[:, None]
                    else:
                        # Square LFO
                        amp_env = self._generate_square_wave(tap_duty, mod_freq, frames)
                        modulated_outputs = audio_outputs * amp_env[:, None]
                else:
                    modulated_outputs = audio_outputs

                # Route 4?8
                speaker_signals = self._route_audio_to_speakers(modulated_outputs, mode)

                # Per-speaker gains
                master = int(row.get("strength", 5))
                base_gains = np.zeros(CHANNELS, dtype=np.float32)
                for col, chans in CHANNEL_MAP.items():
                    trim = int(row.get(col, 5))
                    g = scaled_amp(master, trim)
                    for c in chans:
                        base_gains[c] = g

                therapy_signal = speaker_signals * base_gains[None, :]

                # Apply fade
                therapy_signal = self._apply_fade(therapy_signal, frames)

                # Debug (occasional)
                if hasattr(self, "_debug_counter"):
                    self._debug_counter += 1
                else:
                    self._debug_counter = 0
                if self._debug_counter % 200 == 0:
                    mode_names = ["Standard","Linear","Alt Sides","Front-Back","Upper-Lower","Cross","Quad","Custom"]
                    mapping = MODE_ROUTING.get(mode, {})
                    print(f"[ROUTING] Mode {mode} ({mode_names[mode]}), Phase: {phase}, Mapping: {mapping}")

            # ---- BT path: stereo capture -> 200 Hz LPF -> mono/stereo -> 8ch ----
            bt_stereo = self._read_bt_stereo(frames)    # (N,2) or silence
            bt_8 = self._bt_to_8ch(bt_stereo)           # (N,8)

            # ---- Single last-stage mixer (0..1 amplitudes already computed in handle_set_mix) ----
            music_gain = float(self.bt_gain)
            therapy_mix_gain = float(getattr(self, "therapy_gain", 1.0))
            mixed_signal = therapy_signal * therapy_mix_gain + bt_8 * music_gain
            
            # Output
            np.clip(mixed_signal, -1.0, 1.0, out=mixed_signal)
            outdata[:] = (mixed_signal * 32767.0).astype(np.int16)

        except Exception as e:
            print(f"[!] Audio callback error: {e}")
            outdata[:] = 0

    def _reset_state(self):
        self.phase_accum = 0.0
        self.mod_phase_accum = 0.0
        self.last_inst_f = 20.0
        self.last_notified_row = -1
        self.fade_samples_remaining = 0
        self.fade_direction = 0
        self.fade_multiplier = 0.0

class SineRowPlayer:
    def __init__(self, ws_handler=None):
        self.stream = None
        self.row = None
        self.start_t = 0.0
        self.phase_accum = 0.0
        self.mod_phase_accum = 0.0
        self.last_inst_f = 20.0
        self.device_available = False
        self.ws_handler = ws_handler
        self.output_device_hint = "ICUSBAUDIO7D"
        self.therapy_gain = 1.0
        
        # [Include all your existing SineRowPlayer methods here]
        # This is just a placeholder - copy the complete class from your original
        
    def play_row(self, row):
        print(f"[AUDIO] Playing row: {row}")
        # Your existing implementation
        
    def play_sequence(self, rows):
        print(f"[AUDIO] Playing sequence: {len(rows)} rows")
        # Your existing implementation
        
    def stop(self):
        print("[AUDIO] Stopping playback")
        # Your existing implementation

class WebSocketHandler:
    """Simple WebSocket handler - no HTTP functionality"""
    
    def __init__(self):
        self.clients = set()
        self.player = SineRowPlayer(self)
        self.highlight_queue = []
        self.clear_highlight_pending = False

    async def handle_client(self, ws, path=None):
        client_addr = ws.remote_address
        print(f"[WS] New client connected from {client_addr}")
        self.clients.add(ws)

        try:
            async for msg in ws:
                await self.send_pending_highlights()

                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    await ws.send("error:badjson")
                    continue

                action = data.get("action")
                
                if action == "play-selected":
                    row_stub = data.get("row")
                    if row_stub:
                        self.player.play_row(row_stub)
                    await ws.send("ack:play-selected")

                elif action == "play-all":
                    rows = data.get("rows", [])
                    if rows:
                        self.player.play_sequence(rows)
                    await ws.send("ack:play-all")

                elif action == "stop":
                    self.player.stop()
                    await ws.send("ack:stop")

                elif action == "set-mix":
                    await self.handle_set_mix(ws, data)

                else:
                    await ws.send("error:unknown")

        except websockets.exceptions.ConnectionClosed:
            print("[WS] Client connection closed")
        except Exception as e:
            print(f"[WS] Error handling client: {e}")
        finally:
            self.clients.discard(ws)

    async def handle_set_mix(self, ws, data):
        try:
            x = max(0, min(100, int(data.get("value", 50))))
            # Apply mixing logic
            await ws.send("ack:set-mix")
        except Exception:
            await ws.send("error:bad-mix")

    def queue_highlight(self, row_index): 
        self.highlight_queue.append(row_index)
        
    def queue_clear_highlight(self): 
        self.clear_highlight_pending = True

    async def send_pending_highlights(self):
        if self.clear_highlight_pending:
            await self.send_clear_highlight()
            self.clear_highlight_pending = False
        while self.highlight_queue:
            row_index = self.highlight_queue.pop(0)
            await self.send_highlight(row_index)

    async def send_highlight(self, row_index):
        message = f"highlight:{row_index}"
        disconnected = set()
        for client in self.clients:
            try: 
                await client.send(message)
            except websockets.exceptions.ConnectionClosed: 
                disconnected.add(client)
            except Exception as e: 
                print(f"[WS] Error sending highlight: {e}")
        self.clients -= disconnected

    async def send_clear_highlight(self):
        message = "clear:highlight"
        disconnected = set()
        for client in self.clients:
            try: 
                await client.send(message)
            except websockets.exceptions.ConnectionClosed: 
                disconnected.add(client)
            except Exception as e: 
                print(f"[WS] Error sending clear highlight: {e}")
        self.clients -= disconnected

# Global WebSocket handler
ws_handler = WebSocketHandler()

def _release_audio():
    try:
        if ws_handler.player and ws_handler.player.stream:
            if ws_handler.player.stream.active:
                ws_handler.player.stream.stop()
            ws_handler.player.stream.close()
            ws_handler.player.stream = None
    except Exception:
        pass

def _sigterm_handler(signum, frame):
    _release_audio()
    raise SystemExit(0)

atexit.register(_release_audio)
signal.signal(signal.SIGTERM, _sigterm_handler)
signal.signal(signal.SIGINT, _sigterm_handler)

async def monitor_device():
    """Monitor audio device availability"""
    last_available = None
    while True:
        await asyncio.sleep(1.0)
        desired = "ICUSBAUDIO7D"
        
        if desired != getattr(ws_handler.player, "output_device_hint", ""):
            print(f"[AUDIO] switching output to {desired}")
            ws_handler.player.output_device_hint = desired
            # ws_handler.player.ensure_stream()  # Uncomment when you add the method
            last_available = None

        # now_available = ws_handler.player.is_device_available()  # Uncomment when you add the method
        # if now_available != last_available:
        #     last_available = now_available
        #     ws_handler.player.ensure_stream()

async def highlight_sender():
    """Send row highlights to clients"""
    while True:
        await ws_handler.send_pending_highlights()
        await asyncio.sleep(0.1)

async def main():
    """Main WebSocket server - no HTTP server"""
    asyncio.create_task(monitor_device())
    asyncio.create_task(highlight_sender())
    
    while True:
        try:
            async with websockets.serve(ws_handler.handle_client, "0.0.0.0", WS_PORT):
                print(f"[WS] WebSocket server listening on port {WS_PORT}")
                print(f"[INFO] Audio control ready. Web interface should be on port 8090.")
                await asyncio.Future()  # run forever
        except OSError as e:
            if getattr(e, "errno", None) == 98:
                print(f"[WARN] Port {WS_PORT} busy; retrying in 2s")
                await asyncio.sleep(2)
                continue
            raise
        except asyncio.CancelledError:
            break
        finally:
            _release_audio()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass