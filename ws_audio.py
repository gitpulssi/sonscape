#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sounddevice as sd, asyncio, os, time, math, numpy as np
import websockets, subprocess, sys, atexit, signal, json, fcntl, re
import threading
from pathlib import Path
import os
os.environ['SDL_AUDIODRIVER'] = 'alsa'  # Force ALSA

# Try ALSA (for Bluetooth capture)
try:
    import alsaaudio; ALSA_AVAILABLE=True; print("[BT] ALSA audio support available")
except ImportError:
    ALSA_AVAILABLE=False; print("[BT] ALSA audio not available - Bluetooth mixing disabled")

# Single instance lock
_lockfd=open("/tmp/ws_audio.lock","w")
try: fcntl.flock(_lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
except OSError: print("[FATAL] Another ws_audio.py is already running; exiting."); sys.exit(1)

# Constants
RATE=48000; BLOCK=1200; CHANNELS=8; PORT=8081; DEVICE_NAME="ICUSBAUDIO7D"
FADE_TIME=4.0; FADE_SAMPLES=int(FADE_TIME*RATE)
CHANNEL_MAP={"neck":(0,1),"back":(4,5),"thighs":(6,7),"legs":(2,3)}
MODE_ROUTING={
0:{0:[0,1],1:[2,3],2:[4,5],3:[6,7]},
1:{0:[6,7],1:[4,5],2:[2,3],3:[0,1]},
2:{0:[0,2],1:[4,6],2:[5,7],3:[1,3]},
3:{0:[0,2],1:[1,3],2:[4,6],3:[5,7]},
4:{0:[0,1],1:[6,7],2:[2,3],3:[4,5]},
5:{0:[2,3],1:[4,5],2:[0,1],3:[6,7]},
6:{0:[0,3],1:[1,2],2:[4,7],3:[5,6]},
7:{0:[0,6],1:[1,7],2:[3,5],3:[2,4]}}

CONFIG_PATH=Path.home()/ "webui"/ "config.json"

def load_config():
    try:
        if CONFIG_PATH.exists(): return json.load(open(CONFIG_PATH))
    except Exception as e: print(f"[CFG] load error: {e}")
    return {}
    
def apply_dual_strength(matrix_val: int, user_val: int | None, min_limit=0, max_limit=9) -> int:
   
    matrix_val = max(min_limit, min(max_limit, matrix_val))
    if user_val is None:
        return matrix_val  # fallback if not provided

    user_val = max(0, min(9, user_val))

    if user_val == 5:
        return matrix_val
    elif user_val < 5:
        # scale down relative to baseline
        scale = user_val / 5.0
        return int(max(min_limit, round(matrix_val * scale)))
    else:
        # scale up relative to baseline
        scale = 1.0 + (user_val - 5) / 5.0
        return int(min(max_limit, round(matrix_val * scale)))

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

# ---- Player ----
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
        self.bt_gain = 0.5
        self.bt_buffer = np.zeros((BLOCK, 2), dtype=np.float32)
        self.bt_enabled = False
        self.bt_mono = True
        self.bt_lpf_fc = 200.0
        self._bt_lpf_coeffs = None
        self._bt_lpf_state = np.zeros((2, 2), dtype=np.float32)
        self._bt_zero_blocks = 0
        self._bt_zero_limit = 60
        self._bt_reinit_cooldown_s = 5.0
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
        self.row_start_time = 0.0

        # Pause/Resume state - INITIALIZE THESE FIRST
        self.is_paused = False
        self.pause_requested = False
        self.resume_requested = False
        self.saved_state = None
        self.pause_start_time = 0.0

        self.ensure_stream()
           
    def _start_audio_thread(self):
        """Start audio generation thread without sounddevice"""
        self._audio_running = True
        self._audio_thread = threading.Thread(target=self._audio_loop, daemon=True)
        self._audio_thread.start()

    def _audio_loop(self):
        """Audio generation loop - replaces sounddevice callback"""
        frames_per_callback = BLOCK  # 2400 frames
        sleep_time = frames_per_callback / RATE  # ~0.05 seconds per callback
        
        while self._audio_running:
            try:
                # Generate the same audio data as before
                mixed_signal = self._generate_therapy_audio(frames_per_callback)
                
                # Write directly to ALSA
                int16_data = (mixed_signal * 32767.0).astype(np.int16)
                if hasattr(self, '_alsa_process') and self._alsa_process.poll() is None:
                    self._alsa_process.stdin.write(int16_data.tobytes())
                    self._alsa_process.stdin.flush()
                    
            except Exception as e:
                print(f"[AUDIO] Loop error: {e}")
                
            time.sleep(sleep_time)
            
    def request_pause(self):
        """Request a pause with fade-out"""
        if self.row and not self.is_paused and not self.pause_requested:
            self.pause_requested = True
            self._start_fade_out()
            print("[PAUSE] Pause requested - starting fade-out")

    def request_resume(self):
        """Request resume from saved state"""
        if self.is_paused and self.saved_state:
            self.resume_requested = True
            self.is_paused = False
            print("[RESUME] Resume requested - restoring state")

    def save_current_state(self):
        """Save current playback state for resume"""
        if not self.row:
            return None
            
        current_time = time.perf_counter()
        elapsed_time = current_time - self.row_start_time
        
        state = {
            'row_data': dict(self.row),
            'elapsed_time': elapsed_time,
            'sequence_rows': self.sequence_rows,
            'current_row_index': self.current_row_index,
            'is_playing_sequence': self.is_playing_sequence,
            'phase_accum': self.phase_accum,
            'mod_phase_accum': self.mod_phase_accum,
            'last_inst_f': self.last_inst_f
        }
        
        print(f"[PAUSE] State saved - elapsed time: {elapsed_time:.2f}s")
        return state

    def restore_state(self, state):
        """Restore playback state for resume"""
        if not state:
            print("[RESUME] No saved state to restore")
            return False
            
        try:
            # Restore the row data and state
            self.row = state['row_data']
            self.sequence_rows = state['sequence_rows'] 
            self.current_row_index = state['current_row_index']
            self.is_playing_sequence = state['is_playing_sequence']
            self.phase_accum = state['phase_accum']
            self.mod_phase_accum = state['mod_phase_accum']
            self.last_inst_f = state['last_inst_f']
            
            # Calculate new start time to resume from correct position
            current_time = time.perf_counter()
            self.row_start_time = current_time - state['elapsed_time']
            
            # Clear pause state
            self.is_paused = False
            
            # Start with fade-in
            self._start_fade_in()
            
            print(f"[RESUME] State restored - resuming from {state['elapsed_time']:.2f}s")
            print(f"[RESUME] Row {self.current_row_index}, sequence: {self.is_playing_sequence}")
            return True
            
        except Exception as e:
            print(f"[RESUME] Error restoring state: {e}")
            return False 

    def _generate_heartbeat_env(self, frames, bpm=60, ratio=0.25):
        """
        Generate heartbeat envelope: "TADAM ... TADAM ..."
        bpm   = beats per minute (controls tempo, driven by modSpeed)
        ratio = fraction of cycle before the 2nd (soft) thump
        """
        dt = 1.0 / RATE
        t = np.arange(frames, dtype=np.float32) * dt

        cycle = 60.0 / bpm  # seconds per beat cycle
        env = np.zeros_like(t)

        for i, ti in enumerate(t):
            pos = ti % cycle

            # First thump (strong, sharp attack, moderate decay)
            if pos < 0.08:  # ~80 ms window
                env[i] = np.exp(-pos / 0.03)

            # Second thump (softer, shorter, later in cycle)
            elif ratio * cycle <= pos < ratio * cycle + 0.06:
                env[i] = 0.6 * np.exp(-(pos - ratio * cycle) / 0.02)

        return env
 
    def _generate_drum_env(self, mod_freq, frames, attack_ms, decay_ms, burst_len=1, burst_gap=1):
        """Envelope with bursts: fast attack, exponential decay, retriggered at mod_freq beats/sec."""
        dt = 1.0 / RATE
        t = np.arange(frames, dtype=np.float32) * dt

        period = 1.0 / max(mod_freq, 0.1)

        phi0 = self.mod_phase_accum
        phi = phi0 + t
        self.mod_phase_accum = (phi0 + frames * dt) % period

        beat_time = (phi % period)
        beat_index = np.floor((phi0 + t) / period).astype(int)

        # Envelope params
        attack_t = attack_ms
        decay_tau = decay_ms / 5.0

        env = np.zeros_like(beat_time)
        for i, (bt, bi) in enumerate(zip(beat_time, beat_index)):
            # Only allow envelope if this beat is inside the burst window
            if (bi % (burst_len + burst_gap)) < burst_len:
                if bt < attack_t:
                    env[i] = bt / attack_t
                else:
                    env[i] = np.exp(-(bt - attack_t) / decay_tau)
        return env
     
    def _generate_therapy_audio(self, frames):
        """Generate therapy audio - extracted from _callback method"""
        try:
            # Handle pause request
            if self.pause_requested and not self.is_paused:
                # Check if fade-out is complete using samples remaining
                if (self.fade_direction == -1 and self.fade_samples_remaining <= 0) or self.fade_multiplier <= 0.001:               
                    # Fade-out complete, now pause
                    self.saved_state = self.save_current_state()
                    self.is_paused = True
                    self.pause_requested = False

                    # Tell clients the exact resume point
                    if self.ws_handler and self.saved_state:
                        try:
                            self.ws_handler.send_treatment_state(self.saved_state)
                        except Exception:
                            pass

                    # Notify WebSocket handler
                    if self.ws_handler:
                        self.ws_handler.send_pause_complete()
                    
                    print("[PAUSE] Paused - audio generation stopped")
                    return np.zeros((frames, CHANNELS), dtype=np.float32)
            
            # Handle resume request
            if self.resume_requested:
                if self.restore_state(self.saved_state):
                    self.resume_requested = False
                    # Notify WebSocket handler
                    if self.ws_handler:
                        self.ws_handler.send_resume_complete()
                    print("[RESUME] Resume successful, continuing audio generation")
                else:
                    print("[RESUME] Failed to restore state")
                    self.resume_requested = False
                    return np.zeros((frames, CHANNELS), dtype=np.float32)
            
            # If paused, return silence
            if self.is_paused:
                return np.zeros((frames, CHANNELS), dtype=np.float32)
            
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
                phase    = float(row.get("phase", 90))
                mode     = int(row.get("mode", 0))

                # Logarithmic mapping for modulation frequency
                mod_val  = float(row.get("modSpeed", 5))  # slider value 1100
                f_min, f_max, N = 0.5, 6.0, 100
                mod_freq = f_min * (f_max / f_min) ** ((mod_val - 1) / (N - 1))

                # Burst grouping logic only used in drum modes
                if mode in (8, 9):
                    burst_len = max(1, int(round(phase / 22.5)))  # map 090° to 14 thumps
                    burst_gap = 1
                else:
                    burst_len = None
                    burst_gap = None
                    
                # Calculate time since current row started
                current_time = time.perf_counter()
                t0 = current_time - self.row_start_time

                # Start fade-out when we're FADE_TIME seconds before the end
                fade_start_time = dur - FADE_TIME
                if t0 >= fade_start_time and self.fade_direction == 0 and dur > FADE_TIME and not self.pause_requested:
                    print(f"[FADE] Starting fade-out at t={t0:.2f}s (fade_start={fade_start_time:.2f}s)")
                    self._start_fade_out()

                # Check for row completion - only after the full duration INCLUDING fade time
                if t0 >= dur:
                    if self.is_playing_sequence:
                        next_index = self.current_row_index + 1
                        if next_index < len(self.sequence_rows):
                            print(f"[SEQ] Transitioning from row {self.current_row_index} to {next_index} at t={t0:.2f}s")
                            self._start_sequence_row(next_index)
                            # Recalculate for new row
                            t0 = current_time - self.row_start_time
                        else:
                            print("[SEQ] Sequence complete")
                            if self.ws_handler:
                                self.ws_handler.queue_clear_highlight()
                            self.row = None
                            self.is_playing_sequence = False
                            self.sequence_rows = None
                            self._reset_state()
                            return np.zeros((frames, CHANNELS), dtype=np.float32)
                    else:
                        print("[PLAY] Single row complete")
                        self.row = None
                        self._reset_state()
                        return np.zeros((frames, CHANNELS), dtype=np.float32)

                # For audio generation during fade-out period, clamp to duration
                # This ensures consistent sine wave timing during the fade
                audio_t0 = min(t0, dur)

                # Generate carriers
                audio_outputs = self._generate_4_channel_audio(f0, fsweep, sspd, audio_t0, tt_block, phase, frames)

                # Modulation
                if mode in (8, 9) and mod_freq > 0:
                    # Drum modes (tight / boomy thumps)
                    if mode == 8:  # Tight kick
                        amp_env = self._generate_drum_env(mod_freq, frames,
                                                          attack_ms=0.005, decay_ms=0.100,
                                                          burst_len=burst_len, burst_gap=burst_gap)
                    else:  # mode == 9: Boomy thump
                        amp_env = self._generate_drum_env(mod_freq, frames,
                                                          attack_ms=0.015, decay_ms=0.400,
                                                          burst_len=burst_len, burst_gap=burst_gap)

                    modulated_outputs = audio_outputs * amp_env[:, None]

                elif mode == 10 and mod_freq > 0:
                    # Heartbeat mode: "TADAM"
                    bpm = int(mod_freq * 60)  # map Hz -> BPM
                    amp_env = self._generate_heartbeat_env(frames, bpm=bpm, ratio=0.25)
                    modulated_outputs = audio_outputs * amp_env[:, None]

                elif mod_freq > 0:
                    # Sine LFO modulation
                    w = 2 * np.pi * mod_freq
                    phi0 = self.mod_phase_accum
                    k = np.arange(frames, dtype=np.float32)
                    mod_phi = phi0 + w * dt * k
                    self.mod_phase_accum = (phi0 + w * dt * frames) % (2*np.pi)

                    mod_lfo = np.sin(mod_phi)
                    amp_env = (mod_lfo + 1.0) * 0.5
                    modulated_outputs = audio_outputs * amp_env[:, None]

                else:
                    modulated_outputs = audio_outputs

                # Route to speakers
                if mode in (8, 9, 10):
                    # Use base routing (mode 0) so all 4 carriers are mapped to body zones
                    speaker_signals = self._route_audio_to_speakers(modulated_outputs, 0)
                else:
                    speaker_signals = self._route_audio_to_speakers(modulated_outputs, mode)

                # Per-speaker gains with dual scaling and fallback
                matrix_master = int(row.get("strength", 5))
                user_master   = getattr(self, "user_strength", None)
                final_master  = apply_dual_strength(matrix_master, 
                                                    int(user_master) if user_master is not None else None)

                base_gains = np.zeros(CHANNELS, dtype=np.float32)
                for col, chans in CHANNEL_MAP.items():
                    matrix_trim = int(row.get(col, 5))
                    user_trim = getattr(self, f"user_{col}", None)
                    final_trim  = apply_dual_strength(matrix_trim, 
                                                      int(user_trim) if user_trim is not None else None)
                    g = scaled_amp(final_master, final_trim)
                    for c in chans:
                        base_gains[c] = g

                therapy_signal = speaker_signals * base_gains[None, :]

                # Apply fade
                therapy_signal = self._apply_fade(therapy_signal, frames)

            # ---- BT path: stereo capture -> 200 Hz LPF -> mono/stereo -> 8ch ----
            bt_8 = None
            if self.bt_gain > 0.0 and self.bt_enabled and self.bt_input:
                bt_stereo = self._read_bt_stereo(frames)   # (N,2) or silence
                bt_8 = self._bt_to_8ch(bt_stereo)          # (N,8)
            else:
                bt_8 = None

            # ---- Single last-stage mixer ----
            music_gain = float(self.bt_gain)
            therapy_mix_gain = float(getattr(self, "therapy_gain", 1.0))
            if bt_8 is not None:
                mixed_signal = therapy_signal * therapy_mix_gain + bt_8 * music_gain
            else:
                mixed_signal = therapy_signal * therapy_mix_gain
            
            # Clip and return
            np.clip(mixed_signal, -1.0, 1.0, out=mixed_signal)
            return mixed_signal
            
        except Exception as e:
            print(f"[AUDIO] Generation error: {e}")
            return np.zeros((frames, CHANNELS), dtype=np.float32)
  
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
        
    def _init_alsa_output(self):
        try:
            if hasattr(self, '_alsa_process') and self._alsa_process:
                try:
                    self._alsa_process.terminate()
                    self._alsa_process.wait(timeout=1)
                except:
                    pass

            # Use stable ALSA card name for ICUSBAUDIO7D
            alsa_dev = "plughw:CARD=ICUSBAUDIO7D,DEV=0"

            self._alsa_process = subprocess.Popen([
                'aplay', '-D', alsa_dev,
                '-f', 'S16_LE', '-r', '48000', '-c', '8', '-t', 'raw',
                '--period-size=1200',
                '--buffer-size=14400'
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            bufsize=0)

            print(f"[ALSA] Using output device: {alsa_dev}")
            return True

        except Exception as e:
            print(f"[ALSA] Failed to start: {e}")
            return False

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
        try:
            if not self._init_alsa_output():
                print("[!] Failed to initialize ALSA output (no DAC found?)")
                return False

            if getattr(self, '_audio_running', False):
                return True  # Already running

            # Start pure threading approach (no sounddevice dependency)
            self._audio_running = True
            self._audio_thread = threading.Thread(
                target=self._pure_audio_loop,
                daemon=True
            )
            self._audio_thread.start()

            print("[+] ALSA output and threading initialized")
            return True

        except Exception as e:
            print(f"[!] Failed to start audio system: {e}")
            return False

    def play_row(self, row):
        if not self.stream or not self.is_device_available():
            self.ensure_stream()
        self.is_playing_sequence = False
        self.sequence_rows = None
        self.row = row
        self.row_start_time = time.perf_counter()
        self.phase_accum = 0.0
        self.mod_phase_accum = 0.0
        self.last_inst_f = float(row.get("frequency", 20.0))
        self._start_fade_in()
        print(f"[PLAY] Starting single row with {row.get('time', 60)}s duration")

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
        self.row_start_time = time.perf_counter()  # Reset timing for new row
        self.phase_accum = 0.0
        self.mod_phase_accum = 0.0
        self.last_inst_f = float(self.row.get("frequency", 20.0))
        self._start_fade_in()
        duration = self.row.get("time", 60)
        print(f"[SEQ] Starting row {index} (freq={self.row.get('frequency')}Hz, dur={duration}s)")
        if self.ws_handler and index != self.last_notified_row:
            self.ws_handler.queue_highlight(index)
            self.last_notified_row = index
            
    def _write_all(self, data_bytes: bytes):
        if not hasattr(self, "_alsa_process") or self._alsa_process is None:
            return
        mv = memoryview(data_bytes)
        total = len(mv)
        off = 0
        while off < total:
            if self._alsa_process.stdin.closed:
                break
            n = self._alsa_process.stdin.write(mv[off:])
            if n is None:
                continue
            off += n

    def stop(self):
        print("[STOP] Stopping audio system")
        
        # Reset pause state
        self.is_paused = False
        self.pause_requested = False
        self.resume_requested = False
        self.saved_state = None
        
        # Reset audio state
        self.row = None
        self.is_playing_sequence = False
        self.sequence_rows = None
        self._reset_state()
        
        # Stop the audio loop
        if hasattr(self, '_audio_running'):
            self._audio_running = False
        
        # Close ALSA pipe safely
        try:
            if hasattr(self, "_alsa_process") and self._alsa_process:
                if self._alsa_process.poll() is None:  # Process is still running
                    try:
                        self._alsa_process.stdin.close()
                    except Exception:
                        pass
                    try:
                        self._alsa_process.terminate()
                        self._alsa_process.wait(timeout=2)
                    except Exception:
                        try:
                            self._alsa_process.kill()
                            self._alsa_process.wait(timeout=1)
                        except Exception:
                            pass
                self._alsa_process = None
        except Exception as e:
            print(f"[STOP] Error cleaning up ALSA: {e}")
          
    def _start_fade_in(self):
        self.fade_samples_remaining = FADE_SAMPLES
        self.fade_direction = 1
        self.fade_multiplier = 0.0
        print(f"[FADE] Starting fade-in ({FADE_TIME}s)")

    def _start_fade_out(self):
        self.fade_samples_remaining = FADE_SAMPLES
        self.fade_direction = -1
        print(f"[FADE] Starting fade-out ({FADE_TIME}s)")

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
                print(f"[FADE] Fade {'in' if self.fade_multiplier >= 0.5 else 'out'} complete (multiplier={self.fade_multiplier:.3f})")
            
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
        duty = duty_percent / 100.0
        w = 2*np.pi*mod_freq
        phi0 = self.mod_phase_accum
        k = np.arange(frames, dtype=np.float32)
        phase = (phi0 + w*dt*k) % (2*np.pi)
        self.mod_phase_accum = (phi0 + w*dt*frames) % (2*np.pi)
        return (phase / (2*np.pi) < duty).astype(np.float32)

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

    def _pure_audio_loop(self):
        while getattr(self, '_audio_running', False):
            try:
                mixed_signal = self._generate_therapy_audio(BLOCK)
                if mixed_signal is not None and hasattr(self, '_alsa_process') and self._alsa_process:
                    # Check if ALSA process is still alive
                    if self._alsa_process.poll() is None:
                        # Efficient conversion without extra copies
                        int16_data = np.clip(mixed_signal, -1.0, 1.0)
                        int16_data = (int16_data * 32767.0).astype(np.int16, copy=False)
                        
                        # Complete blocking write
                        self._write_all(int16_data.tobytes())
                    else:
                        if not hasattr(self, "_alsa_process") or self._alsa_process is None:
                            break
                        if self._alsa_process.poll() is not None:
                            print("[AUDIO] ALSA process ended, stopping audio loop")
                            break
                        
            except BrokenPipeError:
                print("[AUDIO] Broken pipe - ALSA process terminated")
                break
            except Exception as e:
                print(f"[AUDIO] Thread error: {e}")
                break

        print("[AUDIO] Audio loop ended")
        self._audio_running = False
                
    def _reset_state(self):
        self.phase_accum = 0.0
        self.mod_phase_accum = 0.0
        self.last_inst_f = 20.0
        self.last_notified_row = -1
        self.fade_samples_remaining = 0
        self.fade_direction = 0
        self.fade_multiplier = 0.0
        self.row_start_time = 0.0

# ---- WS Handler ----
class WebSocketHandler:
    def __init__(self):
        self.clients=set(); self.player=SineRowPlayer(self)
        self.highlight_queue=[]; self.clear_highlight_pending=False
        self.bt_task=None; self.bt_enabled=False; self.bt_mac_current=None
        cfg=load_config()
        try: self.player.bt_mono=bool(cfg.get("bt_mono",True)); print(f"[BT] Startup mode: {'MONO' if self.player.bt_mono else 'STEREO'}")
        except: pass
        
    def send_pause_complete(self):
        """Send pause completion message to all clients"""
        # Use a synchronous approach since this is called from the audio thread
        self._queue_message("pause:complete")

    def send_resume_complete(self):
        """Send resume completion message to all clients"""
        # Use a synchronous approach since this is called from the audio thread
        self._queue_message("resume:complete")

    def _queue_message(self, message):
        """Queue a message to be sent to all clients"""
        # Add the message to a queue that will be processed by the async loop
        if not hasattr(self, '_message_queue'):
            self._message_queue = []
        self._message_queue.append(message)

    def send_treatment_state(self, state: dict):
        """Queue a serialized treatment-state snapshot for clients."""
        try:
            payload = "treatment-state:" + json.dumps(state, default=float)
            self._queue_message(payload)
        except Exception as e:
            print(f"[WS] Error queuing treatment-state: {e}")
        
    async def _process_queued_messages(self):
        """Process any queued messages and send them to clients"""
        if not hasattr(self, '_message_queue') or not self._message_queue:
            return
        
        messages_to_send = self._message_queue.copy()
        self._message_queue.clear()
        
        for message in messages_to_send:
            await self._send_to_all_clients(message)

    async def _send_to_all_clients(self, message):
        """Send message to all connected clients"""
        if not self.clients:
            return
            
        disconnected = set()
        for client in self.clients:
            try:
                await client.send(message)
            except websockets.exceptions.ConnectionClosed:
                disconnected.add(client)
            except Exception as e:
                print(f"[WS] Error sending message to client: {e}")
        
        self.clients -= disconnected

    async def handle_client(self, ws, path=None):
        print(f"[WS] New client {ws.remote_address}")
        self.clients.add(ws)
        await ws.send(f"ack:bt-set-mono:{self.player.bt_mono}")
        try:
            async for msg in ws:
                await self.send_pending_highlights()
                await self._process_queued_messages()  # Add this line
                
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

                elif action == "pause":
                    print("[WS] Pause request received")
                    self.player.request_pause()
                    await ws.send("ack:pause")

                # In WebSocketHandler._handle_ws (your 'resume' branch)
                elif action == "resume":
                    print("[WS] Resume request received")
                    resume_state = data.get("resumeState")
                    # Only accept a client snapshot if it looks like a proper one
                    if isinstance(resume_state, dict) and 'row_data' in resume_state:
                        self.player.saved_state = resume_state
                    # Otherwise keep the internally saved snapshot from pause()
                    self.player.request_resume()
                    await ws.send("ack:resume")
                    
                elif action == "set-user-control":
                    control = data.get("control")
                    value = int(data.get("value", 5))
                    if control in ("user_strength", "user_neck", "user_back", "user_thighs", "user_legs"):
                        # Store current user control values inside the player
                        setattr(self.player, control, value)
                        await ws.send(f"ack:set-user-control:{control}:{value}")
                        print(f"[USER] Updated {control} = {value}")
                    else:
                        await ws.send("error:bad-user-control")

                elif action == "stop":
                    print("[WS] Stop request received")
                    self.player.stop()
                    await ws.send("ack:stop")
                    
                elif action == "bt-set-mono":
                    try:
                        mono_flag = bool(data.get("mono", True))
                        self.player.bt_mono = mono_flag

                        # Persist to config.json
                        cfg = load_config()
                        cfg["bt_mono"] = mono_flag
                        try:
                            with open(CONFIG_PATH, "w") as f:
                                json.dump(cfg, f, indent=2)
                        except Exception as e:
                            print(f"[CFG] save error: {e}")

                        await ws.send(f"ack:bt-set-mono:{mono_flag}")
                        print(f"[BT] Mono/stereo mode set to {'MONO' if mono_flag else 'STEREO'}")
                    except Exception as e:
                        await ws.send("error:bt-set-mono")
                        print(f"[BT] Error setting mono/stereo: {e}")

                elif action == "set-mix":
                    await self.handle_set_mix(ws, data)
                    
                elif action == "ready":
                    # No-op: just acknowledge so the client doesn't log error:unknown
                    try:
                        await ws.send("ack:ready")
                    except Exception:
                        pass

                elif action == "bt-forget-all":
                    # Safely remove all paired devices and reset BT capture state
                    try:
                        # list paired devices
                        ok, out = self._btctl("paired-devices")
                        macs = re.findall(r"Device\s+([0-9A-F:]{17})", out or "", flags=re.I)

                        # remove each
                        for m in macs:
                            self._btctl("remove", m)

                        # drop any current capture so autoconnect can start fresh
                        try:
                            if self.player.bt_input:
                                self.player.bt_input.close()
                        except Exception:
                            pass
                        self.player.bt_input = None
                        self.player.bt_enabled = False
                        self.player.bt_mac_current = None
                        self.bt_mac_current = None

                        # make adapter discoverable again
                        self._btctl("pairable", "on")
                        self._btctl("discoverable", "on")

                        await ws.send("ack:bt-forget-all")
                        print("[BT] All paired devices removed; capture reset")
                    except Exception as e:
                        await ws.send("error:bt-forget-all")
                        print(f"[BT] forget-all failed: {e}")

                elif action == "toggle-ap-mode":
                    # Placeholder: acknowledge without changing system state.
                    # Wire this to your AP/station switcher later (systemd, flag file, etc).
                    try:
                        await ws.send("ack:toggle-ap-mode:noop")
                        print("[AP] toggle-ap-mode requested (noop)")
                    except Exception:
                        pass

                else:
                    await ws.send("error:unknown")

        except websockets.exceptions.ConnectionClosed:
            print("[WS] Client connection closed")
        except Exception as e:
            print(f"[WS] Error handling client: {e}")
        finally:
            self.clients.discard(ws)

    def _list_a2dp_macs(self):
        """Return list of MAC addresses of connected A2DP devices."""
        try:
            r = subprocess.run(
                ["bluetoothctl", "devices", "Connected"],
                capture_output=True, text=True, timeout=3
            )
            return re.findall(r'Device\s+([0-9A-F:]{17})', r.stdout, flags=re.I)
        except Exception as e:
            print(f"[BT] Error checking bluetoothctl: {e}")
            return []

    def _btctl(self, *args) -> tuple[bool, str]:
        try:
            r = subprocess.run(["bluetoothctl", *args], capture_output=True, text=True, timeout=5)
            out = (r.stdout or "") + (r.stderr or "")
            return (r.returncode == 0), out
        except Exception as e:
            return False, str(e)

    def _check_bt_device_connected(self, mac: str) -> bool:
        """Check if a BT device with MAC is currently connected."""
        try:
            r = subprocess.run(
                ["bluetoothctl", "info", mac],
                capture_output=True, text=True, timeout=5
            )
            return "Connected: yes" in r.stdout
        except Exception:
            return False

    async def _wait_bt_daemons_ready(self, timeout: float = 20.0) -> bool:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            try:
                s1 = subprocess.run(["systemctl", "is-active", "bluetooth"], capture_output=True, text=True, timeout=3)
                ok1 = s1.stdout.strip() == "active"
                _, show = self._btctl("show")
                powered = "Powered: yes" in show
                if ok1 and powered:
                    return True
                else:
                    print("[BT] Daemons not ready yet; retrying...")
            except Exception:
                pass
            await asyncio.sleep(1.0)
        return False

    async def _bt_autoconnect_loop(self, mac: str | None):
        print(f"[BT] autoconnect loop started with MAC: {mac}")
        current_mac = None
        did_agent = False

        def _is_connected(m):
            try:
                r = subprocess.run(["bluetoothctl", "info", m], capture_output=True, text=True, timeout=5)
                return "Connected: yes" in (r.stdout or "")
            except Exception:
                return False

        while self.bt_enabled:
            try:
                auto_mode = self._is_auto(mac)

                # One-time BlueZ agent/power setup
                if not did_agent:
                    if not await self._wait_bt_daemons_ready(timeout=20):
                        print("[BT] Daemons not ready yet; retrying...")
                        await asyncio.sleep(2)
                        continue
                print("[BT] Setting up agent and making discoverable...")
                self._btctl("agent", "NoInputNoOutput")
                self._btctl("default-agent")
                self._btctl("pairable", "on")
                self._btctl("discoverable", "on")
                self._btctl("power", "on")
                
                # Confirm adapter state
                ok, show = self._btctl("show")
                if "Powered: yes" in show and "Discoverable: yes" in show and "Pairable: yes" in show:
                    print("[BT] Adapter is discoverable and pairable – waiting for new device to pair")
                else:
                    print("[BT] WARNING: Adapter did not enter discoverable mode, check bluetoothd")

                # --- AUTO MODE ---
                if auto_mode:
                    macs = self._list_a2dp_macs()
                    if not macs:
                        await asyncio.sleep(3)
                        continue

                    # prefer to stick with the same device if still present
                    pick = current_mac if current_mac in macs else macs[0]
                    if pick != current_mac:
                        subprocess.run(["bluetoothctl", "trust", pick], check=False)
                        if not _is_connected(pick):
                            subprocess.run(["bluetoothctl", "connect", pick], check=False)
                        current_mac = pick

                    active_mac = pick
                    self.bt_mac_current = active_mac

                    # If device disconnected, drop capture so we can reconnect cleanly
                    if not _is_connected(active_mac):
                        if self.player.bt_input:
                            try: self.player.bt_input.close()
                            except Exception: pass
                        self.player.bt_input = None
                        self.player.bt_enabled = False
                        self.player.bt_mac_current = None
                        await asyncio.sleep(2)

                    # Need to (re)setup capture?
                    need_setup = (
                        not self.player.bt_enabled
                        or self.player.bt_input is None
                        or self.player.bt_mac_current != active_mac
                    )

                    if need_setup:
                        print(f"[BT] (auto) setting up capture for {active_mac}...")
                        ok = self.player._setup_bluetooth_input(active_mac)
                        await asyncio.sleep(3 if ok else 6)
                    else:
                        await asyncio.sleep(6)
                    continue

                if self.bt_mac_current and self.bt_mac_current not in self._list_a2dp_macs():
                    print(f"[BT] Forgetting {self.bt_mac_current}, device not present")
                    self.bt_mac_current = None
                    self.player.bt_input = None
                    self.player.bt_enabled = False
                    # Go back to discoverable so new devices can pair
                    self._btctl("discoverable", "on")
                    self._btctl("pairable", "on")

                # --- MANUAL MODE (fixed MAC) ---
                active_mac = mac
                if not _is_connected(active_mac):
                    subprocess.run(["bluetoothctl", "trust", active_mac], check=False)
                    subprocess.run(["bluetoothctl", "connect", active_mac], check=False)

                self.bt_mac_current = active_mac
                current_mac = active_mac

                # If device disconnected, drop capture so we can reconnect cleanly
                if not _is_connected(active_mac):
                    if self.player.bt_input:
                        try: self.player.bt_input.close()
                        except Exception: pass
                    self.player.bt_input = None
                    self.player.bt_enabled = False
                    self.player.bt_mac_current = None
                    await asyncio.sleep(2)

                need_setup = (
                    not self.player.bt_enabled
                    or self.player.bt_input is None
                    or self.player.bt_mac_current != active_mac
                )

                if need_setup:
                    print(f"[BT] (manual) setting up capture for {active_mac}...")
                    ok = self.player._setup_bluetooth_input(active_mac)
                    await asyncio.sleep(3 if ok else 6)
                else:
                    await asyncio.sleep(6)
                continue

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[BT] autoconnect loop error: {e}")
                await asyncio.sleep(2)

        print("[BT] autoconnect loop ended")

    def _is_auto(self, mac):
        return mac is None or str(mac).strip().lower() == "auto"

    def _apply_bt_gain(self, bt_gain: float):
        path = str((Path.home() / "webui" / "mix.json"))
        cur = {}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    cur = json.load(f) or {}
            except Exception:
                cur = {}
        cur["bt_gain"] = float(bt_gain)
        with open(path, "w") as f:
            json.dump(cur, f, indent=2)
        self._last_applied_bt_gain = bt_gain
        print(f"[MIX] Updated bt_gain={bt_gain}")

    def _check_bt_device_connected(self, mac: str) -> bool:
        try:
            r = subprocess.run(["bluetoothctl", "info", mac], capture_output=True, text=True, timeout=5)
            return "Connected: yes" in r.stdout
        except Exception:
            return False

    def _bt_connect_once(self, mac: str) -> bool:
        if self._is_auto(mac):
            return False
        try:
            subprocess.run(["bluetoothctl", "power", "on"], check=False)
            subprocess.run(["bluetoothctl", "trust", mac], check=False)
            subprocess.run(["bluetoothctl", "connect", mac], capture_output=True, timeout=10)
            return self._check_bt_device_connected(mac)
        except Exception:
            return False
            
    def _graceful_stop(self):
        # stop BT loop
        try:
            self.bt_enabled = False
            if self.bt_task and not self.bt_task.done():
                self.bt_task.cancel()
        except Exception:
            pass

        # stop any spawned bluealsa-aplay
        try:
            ba = getattr(self.player, "_ba_proc", None)
            if ba:
                ba.terminate()
                ba.wait(timeout=2)
                self.player._ba_proc = None
        except Exception:
            pass

    def _bt_start(self, mac: str | None):
        self.bt_mac = (mac or "auto")

        # Enable BT 
        self.bt_enabled = True

        # (Re)start the BT autoconnect loop
        if self.bt_task and not self.bt_task.done():
            self.bt_task.cancel()
        self.bt_task = asyncio.create_task(self._bt_autoconnect_loop(self.bt_mac))

    async def handle_set_mix(self, ws, data):
        try:
            x = max(0, min(100, int(data.get("value", 50))))
        except Exception:
            await ws.send("error:bad-mix")
            return
        theta = math.radians(90.0 * (x / 100.0))
        g_music_amp   = math.cos(theta)
        g_therapy_amp = math.sin(theta)
        bt_gain = round(float(g_music_amp), 4)
        
        # Apply gains directly to player
        self.player.bt_gain = bt_gain
        self.player.therapy_gain = g_therapy_amp
        
        # Persist BT gain 
        self._apply_bt_gain(bt_gain)
        await ws.send("ack:set-mix")

    def queue_highlight(self, row_index): self.highlight_queue.append(row_index)
    def queue_clear_highlight(self): self.clear_highlight_pending = True

    async def send_clear_highlight(self):
        message = "clear:highlight"
        disconnected = set()
        for client in self.clients:
            try: await client.send(message)
            except websockets.exceptions.ConnectionClosed: disconnected.add(client)
            except Exception as e: print(f"[WS] Error sending clear highlight to client: {e}")
        self.clients -= disconnected

    async def send_highlight(self, row_index):
        message = f"highlight:{row_index}"
        disconnected = set()
        for client in self.clients:
            try: await client.send(message)
            except websockets.exceptions.ConnectionClosed: disconnected.add(client)
            except Exception as e: print(f"[WS] Error sending highlight to client: {e}")
        self.clients -= disconnected

    async def send_pending_highlights(self):
        if self.clear_highlight_pending:
            await self.send_clear_highlight()
            self.clear_highlight_pending = False
        while self.highlight_queue:
            row_index = self.highlight_queue.pop(0)
            await self.send_highlight(row_index)

ws_handler = WebSocketHandler()

def _release_audio():
    try:
        if ws_handler.player and ws_handler.player.stream:
            if ws_handler.player.stream.active:
                ws_handler.player.stream.stop()
            ws_handler.player.stream.close()
            ws_handler.player.stream = None
        if ws_handler.player and ws_handler.player.bt_input:
            ws_handler.player.bt_input.close()
            ws_handler.player.bt_input = None
    except Exception:
        pass

def _sigterm_handler(signum, frame):
    try:
        # stop BT/autoconnect & helper processes first
        ws_handler._graceful_stop()
    except Exception:
        pass
    _release_audio()
    raise SystemExit(0)

atexit.register(_release_audio)
signal.signal(signal.SIGTERM, _sigterm_handler)
signal.signal(signal.SIGINT, _sigterm_handler)

async def monitor_device():
    last_available = None
    while True:
        await asyncio.sleep(1.0)

        # Direct USB output - no more Loopback switching
        desired = "ICUSBAUDIO7D"

        if desired != getattr(ws_handler.player, "output_device_hint", ""):
            print(f"[AUDIO] switching output to {desired}")
            ws_handler.player.output_device_hint = desired
            ws_handler.player.ensure_stream()
            last_available = None

        now_available = ws_handler.player.is_device_available()
        if now_available != last_available:
            last_available = now_available
            ws_handler.player.ensure_stream()

async def highlight_sender():
    while True:
        await ws_handler.send_pending_highlights()
        await asyncio.sleep(0.1)

async def main():
    asyncio.create_task(monitor_device()); asyncio.create_task(highlight_sender())
    while True:
        try:
            async with websockets.serve(ws_handler.handle_client,"0.0.0.0",PORT):
                print(f"[WS] Listening on :{PORT}")
                ws_handler._bt_start("auto")
                await asyncio.Future()
        except OSError as e:
            if getattr(e,"errno",None)==98: print(f"[WARN] Port {PORT} busy; retrying"); await asyncio.sleep(2); continue
            raise

if __name__=="__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
