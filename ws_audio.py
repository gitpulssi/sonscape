#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sounddevice as sd, asyncio, os, time, math, numpy as np
import websockets, subprocess, sys, atexit, signal, json, fcntl, re
import threading, queue
from pathlib import Path
from collections import deque

# Try to import scipy for optimized filtering
try:
    from scipy import signal as scipy_signal
    SCIPY_AVAILABLE = True
    print("[FILTER] Using scipy optimized lowpass filter")
except ImportError:
    SCIPY_AVAILABLE = False
    print("[FILTER] scipy not available, using simple FIR filter")

os.environ['SDL_AUDIODRIVER'] = 'alsa'

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
CHANNEL_MAP={"neck":(0,1),"back":(2,3),"thighs":(4,5),"legs":(6,7)}
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
        return matrix_val
    user_val = max(0, min(9, user_val))
    if user_val == 5:
        return matrix_val
    elif user_val < 5:
        scale = user_val / 5.0
        return int(max(min_limit, round(matrix_val * scale)))
    else:
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

        # Bluetooth audio integration with ring buffer
        self.bt_input = None
        self.bt_mac_current = None
        self.bt_gain = 0.5
        self.bt_enabled = False
        self.bt_mono = True
        self.bt_lpf_fc = 200.0
        self._bt_lpf_sos = None  # Use scipy SOS (second-order sections) format
        self._bt_lpf_zi = None   # Filter initial conditions
        self._bt_reinit_cooldown_s = 5.0
        self._bt_last_reinit = 0.0
        
        # Ring buffer for BT audio (8x buffer size for more stability)
        self.bt_ring_buffer = np.zeros((BLOCK * 8, 2), dtype=np.float32)
        self.bt_ring_write_pos = 0
        self.bt_ring_read_pos = 0
        self.bt_ring_fill = 0
        self.bt_ring_lock = threading.Lock()
        
        # BT read thread
        self.bt_read_thread = None
        self.bt_read_running = False
        
        # WiFi streaming mode with latency control
        self.wifi_stream_enabled = False
        self.wifi_audio_queue = queue.Queue(maxsize=10)  # REDUCED from 100 to 10 for lower latency
        self.wifi_stream_underruns = 0
        self.wifi_stream_target_latency = 3  # Target 3 frames of buffering
        self.wifi_stream_last_stats = time.perf_counter()

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

        # Pause/Resume state
        self.is_paused = False
        self.pause_requested = False
        self.resume_requested = False
        self.saved_state = None
        self.pause_start_time = 0.0

        self.ensure_stream()
           
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
            self.row = state['row_data']
            self.sequence_rows = state['sequence_rows'] 
            self.current_row_index = state['current_row_index']
            self.is_playing_sequence = state['is_playing_sequence']
            self.phase_accum = state['phase_accum']
            self.mod_phase_accum = state['mod_phase_accum']
            self.last_inst_f = state['last_inst_f']
            
            current_time = time.perf_counter()
            self.row_start_time = current_time - state['elapsed_time']
            
            self.is_paused = False
            self._start_fade_in()
            
            print(f"[RESUME] State restored - resuming from {state['elapsed_time']:.2f}s")
            return True
            
        except Exception as e:
            print(f"[RESUME] Error restoring state: {e}")
            return False 

    def _generate_heartbeat_env(self, frames, bpm=60, ratio=0.25):
        """Generate heartbeat envelope: "TADAM ... TADAM ..." """
        dt = 1.0 / RATE
        t = np.arange(frames, dtype=np.float32) * dt
        cycle = 60.0 / bpm
        env = np.zeros_like(t)
        for i, ti in enumerate(t):
            pos = ti % cycle
            if pos < 0.08:
                env[i] = np.exp(-pos / 0.03)
            elif ratio * cycle <= pos < ratio * cycle + 0.06:
                env[i] = 0.6 * np.exp(-(pos - ratio * cycle) / 0.02)
        return env
        
    def _init_alsa_output(self):
            try:
                # If ALSA output is already running, don't restart it
                if hasattr(self, '_alsa_process') and self._alsa_process:
                    if self._alsa_process.poll() is None:  # Still running
                        print(f"[ALSA] Output already active, reusing existing process")
                        return True

                # Only start new process if needed
                alsa_dev = "plughw:CARD=ICUSBAUDIO7D,DEV=0"
                self._alsa_process = subprocess.Popen([
                    'aplay', '-D', alsa_dev,
                    '-f', 'S16_LE', '-r', '48000', '-c', '8', '-t', 'raw',
                    '--period-size=1200',
                    '--buffer-size=2400'
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
 
    def _generate_drum_env(self, mod_freq, frames, attack_ms, decay_ms, burst_len=1, burst_gap=1):
        """Envelope with bursts: fast attack, exponential decay"""
        dt = 1.0 / RATE
        t = np.arange(frames, dtype=np.float32) * dt
        period = 1.0 / max(mod_freq, 0.1)
        phi0 = self.mod_phase_accum
        phi = phi0 + t
        self.mod_phase_accum = (phi0 + frames * dt) % period
        beat_time = (phi % period)
        beat_index = np.floor((phi0 + t) / period).astype(int)
        attack_t = attack_ms
        decay_tau = decay_ms / 5.0
        env = np.zeros_like(beat_time)
        for i, (bt, bi) in enumerate(zip(beat_time, beat_index)):
            if (bi % (burst_len + burst_gap)) < burst_len:
                if bt < attack_t:
                    env[i] = bt / attack_t
                else:
                    env[i] = np.exp(-(bt - attack_t) / decay_tau)
        return env
     
    def _generate_therapy_audio(self, frames):
        """Generate therapy audio - BT read happens first for consistent timing"""
        try:
            # ---- Read BT audio FIRST before any therapy processing ----
            bt_stereo = np.zeros((frames, 2), dtype=np.float32)
            if self.bt_enabled:
                bt_stereo = self._read_bt_from_ring(frames)
            
            # Initialize therapy signal
            therapy_signal = np.zeros((frames, CHANNELS), dtype=np.float32)
            
            # WiFi streaming mode - use external audio
            if self.wifi_stream_enabled:
                try:
                    # Check queue depth and drop frames if too far behind
                    queue_depth = self.wifi_audio_queue.qsize()
                    
                    # Print stats every 2 seconds
                    now = time.perf_counter()
                    if now - self.wifi_stream_last_stats >= 2.0:
                        latency_ms = (queue_depth * BLOCK / RATE) * 1000
                        print(f"[WIFI] Queue depth: {queue_depth} frames ({latency_ms:.1f}ms latency)")
                        self.wifi_stream_last_stats = now
                    
                    # If queue is too full, drop old frames to reduce latency
                    while queue_depth > self.wifi_stream_target_latency:
                        try:
                            self.wifi_audio_queue.get_nowait()  # Drop oldest frame
                            queue_depth -= 1
                            if queue_depth == self.wifi_stream_target_latency:
                                print(f"[WIFI] Dropped frames to maintain {self.wifi_stream_target_latency}-frame latency")
                        except queue.Empty:
                            break
                    
                    # Get the next frame
                    wifi_data = self.wifi_audio_queue.get(timeout=0.001)
                    if len(wifi_data) == frames * CHANNELS:
                        therapy_signal = wifi_data.reshape((frames, CHANNELS))
                    else:
                        print(f"[WIFI] Size mismatch: expected {frames*CHANNELS}, got {len(wifi_data)}")
                        self.wifi_stream_underruns += 1
                except queue.Empty:
                    self.wifi_stream_underruns += 1
                    if self.wifi_stream_underruns % 100 == 0:
                        print(f"[WIFI] Underruns: {self.wifi_stream_underruns}")
                        
            else:
                # Normal therapy generation
                if self.pause_requested and not self.is_paused:
                    if (self.fade_direction == -1 and self.fade_samples_remaining <= 0) or self.fade_multiplier <= 0.001:               
                        self.saved_state = self.save_current_state()
                        self.is_paused = True
                        self.pause_requested = False
                        if self.ws_handler and self.saved_state:
                            try:
                                self.ws_handler.send_treatment_state(self.saved_state)
                            except Exception:
                                pass
                        if self.ws_handler:
                            self.ws_handler.send_pause_complete()
                        print("[PAUSE] Paused - therapy stopped, BT continues")
                            
                # Handle resume request
                if self.resume_requested:
                    if self.restore_state(self.saved_state):
                        self.resume_requested = False
                        if self.ws_handler:
                            self.ws_handler.send_resume_complete()
                        print("[RESUME] Resume successful")
                    else:
                        print("[RESUME] Failed to restore state")
                        self.resume_requested = False
                
                # Initialize signals
                therapy_signal = np.zeros((frames, CHANNELS), dtype=np.float32)
                
                # Generate therapy audio ONLY if active and not paused
                row = self.row
                if row and not self.is_paused:
                    dt = 1.0 / RATE
                    tt_block = np.arange(frames) * dt
                    f0 = float(row.get("frequency", 20.0))
                    fsweep = float(row.get("freqSweep", 0))
                    sspd = float(row.get("sweepSpeed", 0))
                    dur = float(row.get("time", 60))
                    phase = float(row.get("phase", 90))  # This now controls MODULATION phase
                    mode = int(row.get("mode", 0))
                    mod_val = float(row.get("modSpeed", 5))
                    
                    # Logarithmic mapping: slider 1–100 → 0.03–10 Hz
                    f_min, f_max, N = 0.03, 10.0, 100
                    mod_freq = f_min * (f_max / f_min) ** ((mod_val - 1) / (N - 1))
                    
                    if mode in (8, 9):
                        burst_len = max(1, int(round(phase / 22.5)))
                        burst_gap = 1
                    else:
                        burst_len = None
                        burst_gap = None
                        
                    current_time = time.perf_counter()
                    t0 = current_time - self.row_start_time
                    fade_start_time = dur - FADE_TIME
                    
                    if t0 >= fade_start_time and self.fade_direction == 0 and dur > FADE_TIME and not self.pause_requested:
                        print(f"[FADE] Starting fade-out at t={t0:.2f}s")
                        self._start_fade_out()

                    if t0 >= dur:
                        if self.is_playing_sequence:
                            next_index = self.current_row_index + 1
                            if next_index < len(self.sequence_rows):
                                print(f"[SEQ] Transitioning from row {self.current_row_index} to {next_index}")
                                self._start_sequence_row(next_index)
                                t0 = current_time - self.row_start_time
                            else:
                                print("[SEQ] Sequence complete")
                                if self.ws_handler:
                                    self.ws_handler.queue_clear_highlight()
                                self.row = None
                                self.is_playing_sequence = False
                                self.sequence_rows = None
                                self._reset_state()
                        else:
                            print("[PLAY] Single row complete")
                            self.row = None
                            self._reset_state()

                    # Only generate therapy if we still have an active row
                    if self.row:
                        audio_t0 = min(t0, dur)
                        
                        # Generate 4-channel audio (carriers without phase offset)
                        audio_outputs = self._generate_4_channel_audio(f0, fsweep, sspd, audio_t0, tt_block, frames)

                        # Apply modulation with phase control
                        if mode in (8, 9) and mod_freq > 0:
                            if mode == 8:
                                amp_env = self._generate_drum_env(mod_freq, frames, attack_ms=0.005, decay_ms=0.100,
                                                                  burst_len=burst_len, burst_gap=burst_gap)
                            else:
                                amp_env = self._generate_drum_env(mod_freq, frames, attack_ms=0.015, decay_ms=0.400,
                                                                  burst_len=burst_len, burst_gap=burst_gap)
                            # Apply phase offset to each of the 4 outputs
                            modulated_outputs = np.zeros_like(audio_outputs)
                            for output_idx in range(4):
                                phase_offset_deg = phase * output_idx
                                phase_offset_samples = int((phase_offset_deg / 360.0) * (1.0 / mod_freq) * RATE)
                                shifted_env = np.roll(amp_env, phase_offset_samples)
                                modulated_outputs[:, output_idx] = audio_outputs[:, output_idx] * shifted_env
                                
                        elif mode == 10 and mod_freq > 0:
                            bpm = int(mod_freq * 60)
                            amp_env = self._generate_heartbeat_env(frames, bpm=bpm, ratio=0.25)
                            # Apply phase offset to each of the 4 outputs
                            modulated_outputs = np.zeros_like(audio_outputs)
                            for output_idx in range(4):
                                phase_offset_deg = phase * output_idx
                                phase_offset_samples = int((phase_offset_deg / 360.0) * (60.0 / bpm) * RATE)
                                shifted_env = np.roll(amp_env, phase_offset_samples)
                                modulated_outputs[:, output_idx] = audio_outputs[:, output_idx] * shifted_env
                                
                        elif mod_freq > 0:
                            # Sine wave modulation with phase control
                            w = 2 * np.pi * mod_freq
                            phi0 = self.mod_phase_accum
                            k = np.arange(frames, dtype=np.float32)
                            
                            modulated_outputs = np.zeros_like(audio_outputs)
                            for output_idx in range(4):
                                # Apply phase offset to modulation for each output
                                phase_offset_rad = np.deg2rad(phase * output_idx)
                                mod_phi = phi0 + w * dt * k + phase_offset_rad
                                mod_lfo = np.sin(mod_phi)
                                amp_env = (mod_lfo + 1.0) * 0.5
                                modulated_outputs[:, output_idx] = audio_outputs[:, output_idx] * amp_env
                            
                            self.mod_phase_accum = (phi0 + w * dt * frames) % (2*np.pi)
                        else:
                            modulated_outputs = audio_outputs

                        if mode in (8, 9, 10):
                            speaker_signals = self._route_audio_to_speakers(modulated_outputs, 0)
                        else:
                            speaker_signals = self._route_audio_to_speakers(modulated_outputs, mode)

                        matrix_master = int(row.get("strength", 5))
                        user_master = getattr(self, "user_strength", None)
                        final_master = apply_dual_strength(matrix_master, 
                                                            int(user_master) if user_master is not None else None)

                        base_gains = np.zeros(CHANNELS, dtype=np.float32)
                        for col, chans in CHANNEL_MAP.items():
                            matrix_trim = int(row.get(col, 5))
                            user_trim = getattr(self, f"user_{col}", None)
                            final_trim = apply_dual_strength(matrix_trim, 
                                                              int(user_trim) if user_trim is not None else None)
                            g = scaled_amp(final_master, final_trim)
                            for c in chans:
                                base_gains[c] = g

                        therapy_signal = speaker_signals * base_gains[None, :]
                        therapy_signal = self._apply_fade(therapy_signal, frames)

            # ---- BT audio processing (already read at start) ----
            bt_8 = None
            if self.bt_gain > 0.0:
                bt_8 = self._bt_to_8ch(bt_stereo)

            # Mix therapy + BT
            music_gain = float(self.bt_gain)
            therapy_mix_gain = float(getattr(self, "therapy_gain", 1.0))
            if bt_8 is not None:
                mixed_signal = therapy_signal * therapy_mix_gain + bt_8 * music_gain
            else:
                mixed_signal = therapy_signal * therapy_mix_gain
            
            np.clip(mixed_signal, -1.0, 1.0, out=mixed_signal)
            return mixed_signal
            
        except Exception as e:
            print(f"[AUDIO] Generation error: {e}")
            return np.zeros((frames, CHANNELS), dtype=np.float32)
                
    def _biquad_process_stereo(self, x_stereo, coeffs, state):
        """Process 2-ch block with biquad filter - OPTIMIZED vectorized version"""
        if coeffs is None:
            return x_stereo
        
        b0, b1, b2, a1, a2 = coeffs
        frames = x_stereo.shape[0]
        y = np.empty_like(x_stereo, dtype=np.float32)
        
        # Process both channels using vectorized operations
        for ch in (0, 1):
            z1, z2 = float(state[ch, 0]), float(state[ch, 1])
            xs = x_stereo[:, ch]
            ys = np.empty(frames, dtype=np.float32)
            
            # Vectorized biquad using scipy.signal approach
            # Direct Form II transposed
            for i in range(frames):
                ys[i] = b0 * xs[i] + z1
                z1 = b1 * xs[i] - a1 * ys[i] + z2
                z2 = b2 * xs[i] - a2 * ys[i]
            
            state[ch, 0], state[ch, 1] = z1, z2
            y[:, ch] = ys
        
        return y

    def _bt_read_loop(self):
        """Background thread to continuously read BT audio into ring buffer"""
        print("[BT] Read thread started")
        consecutive_errors = 0
        max_consecutive_errors = 50
        empty_reads = 0
        last_stats_time = time.perf_counter()
        total_frames_read = 0
        
        while self.bt_read_running:
            try:
                if not self.bt_input or not self.bt_enabled:
                    time.sleep(0.1)
                    consecutive_errors = 0
                    continue
                
                # Aggressive read loop - read multiple times per iteration
                frames_read_this_iteration = 0
                for _ in range(10):  # Try up to 10 reads per loop
                    try:
                        length, data = self.bt_input.read()
                        
                        if length is None and data is None:
                            raise RuntimeError("BT device disconnected")
                        
                        if length > 0 and data:
                            consecutive_errors = 0
                            empty_reads = 0
                            samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32767.0
                            
                            if samples.size >= 2:
                                stereo = samples.reshape(-1, 2)
                                total_frames_read += len(stereo)
                                frames_read_this_iteration += len(stereo)
                                
                                # Write to ring buffer
                                with self.bt_ring_lock:
                                    for frame in stereo:
                                        if self.bt_ring_fill < self.bt_ring_buffer.shape[0]:
                                            self.bt_ring_buffer[self.bt_ring_write_pos] = frame
                                            self.bt_ring_write_pos = (self.bt_ring_write_pos + 1) % self.bt_ring_buffer.shape[0]
                                            self.bt_ring_fill += 1
                                        else:
                                            # Buffer full, skip oldest sample
                                            self.bt_ring_read_pos = (self.bt_ring_read_pos + 1) % self.bt_ring_buffer.shape[0]
                                            self.bt_ring_buffer[self.bt_ring_write_pos] = frame
                                            self.bt_ring_write_pos = (self.bt_ring_write_pos + 1) % self.bt_ring_buffer.shape[0]
                        else:
                            # No more data available right now
                            break
                    except Exception as read_err:
                        # Non-blocking read will raise exception when no data
                        break
                
                # Stats every 4 seconds
                now = time.perf_counter()
                if now - last_stats_time >= 4.0:
                    with self.bt_ring_lock:
                        fill_pct = (self.bt_ring_fill / self.bt_ring_buffer.shape[0]) * 100
                    print(f"[BT] Buffer: {fill_pct:.1f}% full ({self.bt_ring_fill}/{self.bt_ring_buffer.shape[0]}), read {total_frames_read} frames in 4s")
                    last_stats_time = now
                    total_frames_read = 0
                
                # Short sleep to prevent tight loop
                time.sleep(0.005)  # 5ms sleep
                    
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    print(f"[BT] Too many consecutive errors ({consecutive_errors}), recycling connection")
                    try:
                        if self.bt_input:
                            self.bt_input.close()
                    except:
                        pass
                    self.bt_input = None
                    self.bt_enabled = False
                    self.bt_mac_current = None
                    self._bt_last_reinit = time.monotonic()
                    consecutive_errors = 0
                    time.sleep(2)
                elif consecutive_errors % 10 == 0:
                    print(f"[BT] Read error ({consecutive_errors}): {e}")
                time.sleep(0.01)
        
        print("[BT] Read thread stopped")

    def _read_bt_from_ring(self, frames):
        """Read audio from ring buffer - OPTIMIZED batch read"""
        output = np.zeros((frames, 2), dtype=np.float32)
        
        with self.bt_ring_lock:
            available = self.bt_ring_fill
            to_read = min(frames, available)
            
            # Log underruns
            if to_read < frames:
                if not hasattr(self, '_underrun_count'):
                    self._underrun_count = 0
                    self._last_underrun_log = time.perf_counter()
                self._underrun_count += 1
                now = time.perf_counter()
                if now - self._last_underrun_log >= 1.0:
                    print(f"[BT] UNDERRUN: requested {frames}, only had {available} (count: {self._underrun_count})")
                    self._last_underrun_log = now
                    self._underrun_count = 0
            
            # Batch read - much faster than frame-by-frame
            if to_read > 0:
                space_until_wrap = self.bt_ring_buffer.shape[0] - self.bt_ring_read_pos
                
                if to_read <= space_until_wrap:
                    # Can read all without wrapping
                    output[:to_read] = self.bt_ring_buffer[self.bt_ring_read_pos:self.bt_ring_read_pos + to_read]
                    self.bt_ring_read_pos = (self.bt_ring_read_pos + to_read) % self.bt_ring_buffer.shape[0]
                else:
                    # Need to wrap around
                    output[:space_until_wrap] = self.bt_ring_buffer[self.bt_ring_read_pos:]
                    remaining = to_read - space_until_wrap
                    output[space_until_wrap:to_read] = self.bt_ring_buffer[:remaining]
                    self.bt_ring_read_pos = remaining
                
                self.bt_ring_fill -= to_read
        
        return output

    def _bt_to_8ch(self, bt_stereo_block):
        """200Hz lowpass then mono/stereo to 8ch - scipy or simple FIR fallback"""
        frames = bt_stereo_block.shape[0]
        
        if SCIPY_AVAILABLE:
            # Fast scipy Butterworth filter
            if self._bt_lpf_sos is None:
                self._bt_lpf_sos = scipy_signal.butter(4, self.bt_lpf_fc, 'low', fs=RATE, output='sos')
                self._bt_lpf_zi = scipy_signal.sosfilt_zi(self._bt_lpf_sos)
                self._bt_lpf_zi = np.stack([self._bt_lpf_zi, self._bt_lpf_zi])
            
            bt_filtered = np.empty_like(bt_stereo_block)
            for ch in range(2):
                bt_filtered[:, ch], self._bt_lpf_zi[ch] = scipy_signal.sosfilt(
                    self._bt_lpf_sos, 
                    bt_stereo_block[:, ch],
                    zi=self._bt_lpf_zi[ch]
                )
        else:
            # Simple 5-tap FIR lowpass (fast, reasonable quality)
            # Approximates 200Hz cutoff at 48kHz
            if not hasattr(self, '_fir_buffer'):
                self._fir_buffer = np.zeros((2, 4), dtype=np.float32)
            
            bt_filtered = np.empty_like(bt_stereo_block)
            # Simple moving average-ish coefficients
            h = np.array([0.1, 0.2, 0.4, 0.2, 0.1], dtype=np.float32)
            
            for ch in range(2):
                signal_in = bt_stereo_block[:, ch]
                signal_out = np.zeros(frames, dtype=np.float32)
                
                # Use numpy convolve for speed
                padded = np.concatenate([self._fir_buffer[ch], signal_in])
                filtered = np.convolve(padded, h, mode='valid')
                signal_out = filtered[:frames]
                
                # Save last 4 samples for next block
                self._fir_buffer[ch] = signal_in[-4:]
                bt_filtered[:, ch] = signal_out
        
        out = np.zeros((frames, CHANNELS), dtype=np.float32)
        
        if self.bt_mono:
            mono = (bt_filtered[:, 0] + bt_filtered[:, 1]) * 0.5
            out[:] = mono[:, np.newaxis]
        else:
            out[:, 0::2] = bt_filtered[:, 0:1]
            out[:, 1::2] = bt_filtered[:, 1:2]
        
        return out

    def is_device_available(self) -> bool:
        try:
            target = getattr(self, "output_device_hint", DEVICE_NAME)
            for dev in sd.query_devices():
                if target in dev["name"]:
                    if "ICUSBAUDIO7D" in target:
                        return True
                    elif dev["max_output_channels"] >= CHANNELS:
                        return True
        except Exception as e:
            print(f"[!] Error querying devices: {e}")
        return False

    def _setup_bluetooth_input(self, bt_mac):
        """Setup Bluetooth input with ring buffer"""
        if not ALSA_AVAILABLE:
            print("[BT] ALSA not available")
            return False

        # Stop existing read thread
        if self.bt_read_running:
            self.bt_read_running = False
            if self.bt_read_thread:
                self.bt_read_thread.join(timeout=2)

        # Close previous input
        try:
            if self.bt_input:
                self.bt_input.close()
            self.bt_input = None
        except Exception:
            pass

        # Kill previous bluealsa-aplay
        try:
            subprocess.run(["pkill", "-f", f"bluealsa-aplay.*{bt_mac}"], check=False)
        except Exception:
            pass

        # Try direct BlueALSA capture with NON-BLOCKING mode
        pcm_device = f"bluealsa:DEV={bt_mac},PROFILE=a2dp"
        print(f"[BT] Attempting to open {pcm_device}")
        try:
            cap = alsaaudio.PCM(
                type=alsaaudio.PCM_CAPTURE,
                mode=alsaaudio.PCM_NONBLOCK,  # NON-BLOCKING for continuous reads
                device=pcm_device,
                channels=2,
                rate=RATE,
                format=alsaaudio.PCM_FORMAT_S16_LE,
                periodsize=BLOCK
            )
            self.bt_input = cap
            self.bt_mac_current = bt_mac
            self.bt_enabled = True
            
            # Clear ring buffer
            with self.bt_ring_lock:
                self.bt_ring_write_pos = 0
                self.bt_ring_read_pos = 0
                self.bt_ring_fill = 0
            
            # Start read thread
            self.bt_read_running = True
            self.bt_read_thread = threading.Thread(target=self._bt_read_loop, daemon=True)
            self.bt_read_thread.start()
            
            print(f"[BT] Direct BlueALSA CAPTURE established for {bt_mac}")
            return True
            
        except Exception as direct_err:
            print(f"[BT] Direct CAPTURE not available ({direct_err}); trying Loopback")

        # Loopback fallback
        if not os.path.exists("/proc/asound/Loopback"):
            print("[BT] Loopback device not present")
            self.bt_input = None
            self.bt_enabled = False
            return False

        try:
            self._ba_proc = subprocess.Popen(
                ["bluealsa-aplay", "-r", "48000", "-d", "plughw:Loopback,0", bt_mac],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT
            )
            print("[BT] Started bluealsa-aplay → plughw:Loopback,0 @48k")
        except Exception as e:
            print(f"[BT] Failed to start bluealsa-aplay: {e}")

        loop_dev = "hw:Loopback,1,0"
        for _ in range(10):
            try:
                cap = alsaaudio.PCM(
                    type=alsaaudio.PCM_CAPTURE,
                    mode=alsaaudio.PCM_NONBLOCK,  # NON-BLOCKING
                    device=loop_dev,
                    channels=2,
                    rate=48000,
                    format=alsaaudio.PCM_FORMAT_S16_LE,
                    periodsize=BLOCK
                )
                self.bt_input = cap
                self.bt_mac_current = bt_mac
                self.bt_enabled = True
                
                # Clear ring buffer
                with self.bt_ring_lock:
                    self.bt_ring_write_pos = 0
                    self.bt_ring_read_pos = 0
                    self.bt_ring_fill = 0
                
                # Start read thread
                self.bt_read_running = True
                self.bt_read_thread = threading.Thread(target=self._bt_read_loop, daemon=True)
                self.bt_read_thread.start()
                
                print(f"[BT] Loopback capture established on {loop_dev}")
                return True
            except Exception:
                time.sleep(0.1)

        print("[BT] Loopback capture failed")
        self.bt_input = None
        self.bt_enabled = False
        return False

    def ensure_stream(self):
        try:
            if not self._init_alsa_output():
                print("[!] Failed to initialize ALSA output")
                return False

            # Check if audio loop is already running
            if getattr(self, '_audio_running', False):
                # Audio loop already running
                # Check if BT thread needs restart (after stop was called)
                if self.bt_enabled and self.bt_input and not self.bt_read_running:
                    print("[BT] Restarting read thread")
                    with self.bt_ring_lock:
                        self.bt_ring_write_pos = 0
                        self.bt_ring_read_pos = 0
                        self.bt_ring_fill = 0
                    self.bt_read_running = True
                    self.bt_read_thread = threading.Thread(target=self._bt_read_loop, daemon=True)
                    self.bt_read_thread.start()
                return True

            # Start audio loop for the first time
            self._audio_running = True
            self._audio_thread = threading.Thread(
                target=self._pure_audio_loop,
                daemon=True
            )
            self._audio_thread.start()

            print("[+] ALSA output and audio loop initialized")
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
        self.row_start_time = time.perf_counter()
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
        print("[STOP] Stopping therapy playback (BT audio continues)")
        
        # DON'T stop BT read thread - keep it running for continuous music
        # DON'T stop audio loop - keep it running for continuous BT audio output
        
        # Reset pause/playback state
        self.is_paused = False
        self.pause_requested = False
        self.resume_requested = False
        self.saved_state = None
        
        # Clear therapy state only
        self.row = None
        self.is_playing_sequence = False
        self.sequence_rows = None
        self._reset_state()
        
        # Keep ALSA output and BT running - only stop therapy signal generation
          
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
        """Apply fade envelope to signal - OPTIMIZED vectorized version without gaps"""
        if self.fade_direction == 0 and self.fade_samples_remaining <= 0:
            return signal
        
        fade_envelope = np.ones(frames, dtype=np.float32)
        
        if self.fade_samples_remaining > 0:
            samples_to_process = min(frames, self.fade_samples_remaining)
            
            # Calculate the starting progress for this block
            if self.fade_direction == 1:
                # Fade in: progress from current position
                start_progress = (FADE_SAMPLES - self.fade_samples_remaining) / FADE_SAMPLES
                # Vectorized calculation of fade envelope
                progress_array = start_progress + np.arange(samples_to_process, dtype=np.float32) / FADE_SAMPLES
                fade_envelope[:samples_to_process] = progress_array
                self.fade_multiplier = progress_array[-1]
                
            elif self.fade_direction == -1:
                # Fade out: progress from current position
                start_progress = self.fade_samples_remaining / FADE_SAMPLES
                # Vectorized calculation of fade envelope
                progress_array = start_progress - np.arange(samples_to_process, dtype=np.float32) / FADE_SAMPLES
                fade_envelope[:samples_to_process] = np.maximum(0.0, progress_array)
                self.fade_multiplier = max(0.0, progress_array[-1])
            
            self.fade_samples_remaining -= samples_to_process
            
            # Handle completion of fade
            if self.fade_samples_remaining <= 0:
                if self.fade_direction == 1:
                    self.fade_multiplier = 1.0
                elif self.fade_direction == -1:
                    self.fade_multiplier = 0.0
                self.fade_direction = 0
            
            # Fill remaining samples with final multiplier value
            if samples_to_process < frames:
                fade_envelope[samples_to_process:] = self.fade_multiplier
        else:
            # No active fade, use constant multiplier
            fade_envelope[:] = self.fade_multiplier
        
        # Apply envelope
        if signal.ndim == 2:
            return signal * fade_envelope[:, None]
        else:
            return signal * fade_envelope
            
    def _generate_4_channel_audio(self, f0, fsweep, sspd, t0, tt_block, frames):
        """Generate 4-channel carrier audio WITHOUT phase offsets (all in-phase)"""
        dt = 1.0 / RATE
        if fsweep and sspd:
            lfo = np.sin(2*np.pi*sspd*(t0 + tt_block))
            inst_f = f0 + fsweep * lfo
            inst_f = np.clip(inst_f, 20, 200)
        else:
            inst_f = np.full_like(tt_block, f0)
        
        audio_outputs = np.zeros((frames, 4), dtype=np.float32)
        
        # Generate phase for first channel
        if isinstance(inst_f, np.ndarray):
            phase_increments = 2 * np.pi * inst_f * dt
            phi = self.phase_accum + np.cumsum(phase_increments)
        else:
            phase_increment = 2 * np.pi * inst_f * dt
            phi = self.phase_accum + np.arange(frames) * phase_increment
        
        carrier = np.sin(phi).astype(np.float32)
        
        # All 4 channels get the SAME carrier signal (in-phase)
        for output_idx in range(4):
            audio_outputs[:, output_idx] = carrier
        
        # Update phase accumulator
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
        """Improved audio loop with precise timing"""
        frames_per_callback = BLOCK
        expected_duration = frames_per_callback / RATE
        next_callback_time = time.perf_counter()
        
        print("[AUDIO] Audio loop started")
        
        while getattr(self, '_audio_running', False):
            try:
                mixed_signal = self._generate_therapy_audio(frames_per_callback)
                
                if mixed_signal is not None and hasattr(self, '_alsa_process') and self._alsa_process:
                    if self._alsa_process.poll() is None:
                        int16_data = np.clip(mixed_signal, -1.0, 1.0)
                        int16_data = (int16_data * 32767.0).astype(np.int16, copy=False)
                        self._write_all(int16_data.tobytes())
                    else:
                        print("[AUDIO] ALSA process ended")
                        break
                
                # Precise timing to maintain consistent sample rate
                next_callback_time += expected_duration
                sleep_time = next_callback_time - time.perf_counter()
                
                if sleep_time > 0:
                    time.sleep(sleep_time)
                else:
                    # We're falling behind - reset timing
                    next_callback_time = time.perf_counter()
                    
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
        self._queue_message("pause:complete")

    def send_resume_complete(self):
        self._queue_message("resume:complete")

    def _queue_message(self, message):
        if not hasattr(self, '_message_queue'):
            self._message_queue = []
        self._message_queue.append(message)

    def send_treatment_state(self, state: dict):
        try:
            payload = "treatment-state:" + json.dumps(state, default=float)
            self._queue_message(payload)
        except Exception as e:
            print(f"[WS] Error queuing treatment-state: {e}")
        
    async def _process_queued_messages(self):
        if not hasattr(self, '_message_queue') or not self._message_queue:
            return
        messages_to_send = self._message_queue.copy()
        self._message_queue.clear()
        for message in messages_to_send:
            await self._send_to_all_clients(message)

    async def _send_to_all_clients(self, message):
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
                await self._process_queued_messages()
                
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    await ws.send("error:badjson")
                    continue

                action = data.get("action")
                await ws.send(f"debug:action-received:{action}")  # Send back to browser instead			
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

                elif action == "resume":
                    print("[WS] Resume request received")
                    resume_state = data.get("resumeState")
                    if isinstance(resume_state, dict) and 'row_data' in resume_state:
                        self.player.saved_state = resume_state
                    self.player.request_resume()
                    await ws.send("ack:resume")
                    
                elif action == "set-user-control":
                    control = data.get("control")
                    value = int(data.get("value", 5))
                    if control in ("user_strength", "user_neck", "user_back", "user_thighs", "user_legs"):
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
                    try:
                        await ws.send("ack:ready")
                    except Exception:
                        pass
                        
                elif action == "bt-remove-device":
                    try:
                        mac = data.get("mac")
                        if not mac:
                            await ws.send("error:bt-remove-device:no-mac")
                            return
                        
                        print(f"[WS] Remove device request for {mac}")
                        
                        # Stop BT read thread if this is the current device
                        if self.player.bt_mac_current == mac:
                            if self.player.bt_read_running:
                                self.player.bt_read_running = False
                                if self.player.bt_read_thread:
                                    self.player.bt_read_thread.join(timeout=2)
                            
                            try:
                                if self.player.bt_input:
                                    self.player.bt_input.close()
                            except Exception:
                                pass
                            
                            self.player.bt_input = None
                            self.player.bt_enabled = False
                            self.player.bt_mac_current = None
                        
                        # Remove the device
                        success = self._remove_device_if_paired(mac)
                        
                        if success:
                            await ws.send(f"ack:bt-remove-device:{mac}")
                            print(f"[BT] Device {mac} removed successfully")
                        else:
                            await ws.send(f"error:bt-remove-device:not-found")
                            print(f"[BT] Device {mac} not found in paired list")
                            
                    except Exception as e:
                        await ws.send("error:bt-remove-device")
                        print(f"[BT] remove-device failed: {e}")
                        
                elif action == "bt-forget-all":
                    try:
                        ok, out = self._btctl("devices", "Paired")
                        macs = re.findall(r"Device\s+([0-9A-F:]{17})", out or "", flags=re.I)
                        for m in macs:
                            self._btctl("remove", m)
                        
                        # Stop BT read thread cleanly
                        if self.player.bt_read_running:
                            self.player.bt_read_running = False
                            if self.player.bt_read_thread:
                                self.player.bt_read_thread.join(timeout=2)
                        
                        # Close BT input
                        try:
                            if self.player.bt_input:
                                self.player.bt_input.close()
                        except Exception:
                            pass
                        
                        self.player.bt_input = None
                        self.player.bt_enabled = False
                        self.player.bt_mac_current = None
                        self.bt_mac_current = None
                        
                        # Restart bluetooth service to clear all state
                        subprocess.run(["sudo", "systemctl", "restart", "bluetooth"], check=False)
                        await asyncio.sleep(3)
                        
                        self._btctl("pairable", "on")
                        self._btctl("discoverable", "on")
                        await ws.send("ack:bt-forget-all")
                        print("[BT] All paired devices removed and Bluetooth restarted")
                    except Exception as e:
                        await ws.send("error:bt-forget-all")
                        print(f"[BT] forget-all failed: {e}")

                elif action == "bt-list-paired":
                    await ws.send("debug:BT-LIST-PAIRED-REACHED")
                    print(f"[BT] Received bt-list-paired request")
                    try:
                        ok, out = self._btctl("devices", "Paired")
                        connected_macs = set(self._list_a2dp_macs())
                        print(f"[BT] Found {len(connected_macs)} connected devices")  # ← ADD THIS LINE
                        
                        if ok:
                            macs = re.findall(r'Device\s+([0-9A-F:]{17})\s+(.+)', out or "", flags=re.I)
                            print(f"[BT] Parsed {len(macs)} paired devices")  # ← ADD THIS LINE
                            devices = []
                            for mac, name in macs:
                                devices.append({
                                    "mac": mac,
                                    "name": name.strip(),
                                    "connected": mac in connected_macs
                                })
                            response = json.dumps({"devices": devices})
                            print(f"[BT] Sending response: {response}")  # ← ADD THIS LINE
                            await ws.send(f"ack:bt-list-paired:{response}")
                        else:
                            print("[BT] bluetoothctl paired-devices failed")  # ← ADD THIS LINE
                            await ws.send("ack:bt-list-paired:{\"devices\":[]}")
                    except Exception as e:
                        await ws.send("error:bt-list-paired")
                        print(f"[BT] list-paired failed: {e}")
                        
                elif action == "toggle-ap-mode":
                    try:
                        await ws.send("ack:toggle-ap-mode:noop")
                        print("[AP] toggle-ap-mode requested (noop)")
                    except Exception:
                        pass
                        
                elif action == "wifi-stream-start":
                    print("[WIFI] Starting WiFi audio streaming mode")
                    self.player.wifi_stream_enabled = True
                    self.player.wifi_stream_underruns = 0
                    self.player.row = None
                    self.player.is_playing_sequence = False
                    while not self.player.wifi_audio_queue.empty():
                        try:
                            self.player.wifi_audio_queue.get_nowait()
                        except queue.Empty:
                            break
                    await ws.send("ack:wifi-stream-start")
                    
                elif action == "wifi-stream-stop":
                    print("[WIFI] Stopping WiFi audio streaming mode")
                    self.player.wifi_stream_enabled = False
                    while not self.player.wifi_audio_queue.empty():
                        try:
                            self.player.wifi_audio_queue.get_nowait()
                        except queue.Empty:
                            break
                    await ws.send("ack:wifi-stream-stop")
                    
                elif action == "wifi-stream-data":
                    try:
                        audio_data = data.get("data")
                        
                        if isinstance(audio_data, str):
                            import base64
                            binary = base64.b64decode(audio_data)
                            audio_array = np.frombuffer(binary, dtype=np.float32)
                        else:
                            audio_array = np.array(audio_data, dtype=np.float32)
                        
                        expected_size = BLOCK * CHANNELS
                        if len(audio_array) == expected_size:
                            try:
                                self.player.wifi_audio_queue.put_nowait(audio_array)
                                await ws.send("ack:wifi-stream-data")
                            except queue.Full:
                                await ws.send("error:wifi-stream-data:queue-full")
                        else:
                            await ws.send(f"error:wifi-stream-data:size-mismatch:{len(audio_array)}:{expected_size}")
                            
                    except Exception as e:
                        print(f"[WIFI] Error processing stream data: {e}")
                        await ws.send("error:wifi-stream-data")
        
                else:
                    await ws.send("error:unknown")

        except websockets.exceptions.ConnectionClosed:
            print("[WS] Client connection closed")
        except Exception as e:
            print(f"[WS] Error handling client: {e}")
        finally:
            self.clients.discard(ws)

    def _remove_device_if_paired(self, mac: str) -> bool:
        """Remove device completely - bluetoothctl, filesystem, and restart service"""
        try:
            print(f"[BT] Starting complete removal of {mac}")
            
            # Step 1: Remove via bluetoothctl
            ok, out = self._btctl("devices", "Paired")
            if mac.upper() in out.upper():
                print(f"[BT] Device found in paired list, removing...")
                self._btctl("remove", mac)
                time.sleep(0.5)
            
            # Step 2: Remove pairing keys from filesystem
            mac_formatted = mac.upper().replace(':', '_')
            try:
                # Use subprocess to find and remove directories
                find_result = subprocess.run(
                    ["sudo", "find", "/var/lib/bluetooth", "-type", "d", "-name", mac_formatted],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                
                for device_dir in find_result.stdout.strip().split('\n'):
                    if device_dir:  # Skip empty lines
                        print(f"[BT] Removing pairing keys from {device_dir}")
                        subprocess.run(["sudo", "rm", "-rf", device_dir], timeout=3)
            except Exception as e:
                print(f"[BT] Could not remove filesystem keys: {e}")
            
            # Step 3: Restart bluetooth to clear all cached state
            print("[BT] Restarting bluetooth service to clear cache")
            subprocess.run(["sudo", "systemctl", "restart", "bluetooth"], timeout=10)
            time.sleep(3)
            
            # Step 4: Re-initialize agent and make discoverable
            self._btctl("power", "on")
            time.sleep(0.5)
            self._btctl("agent", "NoInputNoOutput")
            self._btctl("default-agent")
            self._btctl("pairable", "on")
            self._btctl("discoverable", "on")
            
            print(f"[BT] Successfully removed {mac} and cleared all pairing data")
            return True
            
        except Exception as e:
            print(f"[BT] Error during removal: {e}")
            return False

    def _list_a2dp_macs(self):
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
        connection_failures = 0
        max_failures = 3

        def _is_connected(m):
            try:
                r = subprocess.run(["bluetoothctl", "info", m], capture_output=True, text=True, timeout=5)
                return "Connected: yes" in (r.stdout or "")
            except Exception:
                return False

        while self.bt_enabled:
            try:
                auto_mode = self._is_auto(mac)

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
                    did_agent = True
                
                if auto_mode:
                    # First, check for paired but disconnected devices and remove them
                    ok, paired_output = self._btctl("paired-devices")
                    if ok:
                        paired_macs = re.findall(r'Device\s+([0-9A-F:]{17})', paired_output or "", flags=re.I)
                        connected_macs = self._list_a2dp_macs()
                        
                        # Remove devices that are paired but not connected (stale pairings)
                        stale_found = False
                        for paired_mac in paired_macs:
                            if paired_mac not in connected_macs:
                                print(f"[BT] Found stale paired device: {paired_mac}, removing completely")
                                self._remove_device_if_paired(paired_mac)
                                stale_found = True
                        
                        # After cleanup, wait for bluetooth to stabilize
                        if stale_found:
                            await asyncio.sleep(5)
                    
                    # Now proceed with normal connection logic
                    macs = self._list_a2dp_macs()
                    if not macs:
                        await asyncio.sleep(3)
                        continue
                    pick = current_mac if current_mac in macs else macs[0]
                    if pick != current_mac:
                        subprocess.run(["bluetoothctl", "trust", pick], check=False)
                        if not _is_connected(pick):
                            result = subprocess.run(
                                ["bluetoothctl", "connect", pick], 
                                capture_output=True, 
                                text=True, 
                                timeout=10
                            )
                            
                            # Check for authentication errors even in auto mode
                            if result.returncode != 0:
                                error_text = (result.stdout + result.stderr).lower()
                                if any(err in error_text for err in ["incorrect pin", "authentication failed", "authentication rejected"]):
                                    print(f"[BT] Authentication error in auto mode - removing {pick}")
                                    self._remove_device_if_paired(pick)
                                    await asyncio.sleep(3)
                                    continue
                                    
                        current_mac = pick
                    active_mac = pick
                    self.bt_mac_current = active_mac

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
                        print(f"[BT] (auto) setting up capture for {active_mac}...")
                        ok = self.player._setup_bluetooth_input(active_mac)
                        await asyncio.sleep(3 if ok else 6)
                    else:
                        await asyncio.sleep(6)
                    continue

                # Manual mode - specific MAC
                if self.bt_mac_current and self.bt_mac_current not in self._list_a2dp_macs():
                    print(f"[BT] Forgetting {self.bt_mac_current}, device not present")
                    self.bt_mac_current = None
                    self.player.bt_input = None
                    self.player.bt_enabled = False
                    self._btctl("discoverable", "on")
                    self._btctl("pairable", "on")

                active_mac = mac
                
                if not _is_connected(active_mac):
                    connection_failures += 1
                    
                    # Try to connect
                    result = subprocess.run(
                        ["bluetoothctl", "connect", active_mac], 
                        capture_output=True, 
                        text=True, 
                        timeout=10
                    )
                    
                    # Check for authentication/PIN errors immediately
                    if result.returncode != 0:
                        error_text = (result.stdout + result.stderr).lower()
                        
                        # Immediate cleanup on authentication errors
                        if any(err in error_text for err in ["incorrect pin", "authentication failed", "authentication rejected"]):
                            print(f"[BT] Authentication error for {active_mac} - removing stale pairing immediately")
                            self._remove_device_if_paired(active_mac)
                            self._btctl("discoverable", "on")
                            self._btctl("pairable", "on")
                            connection_failures = 0  # Reset since we cleaned up
                            await asyncio.sleep(5)
                            continue
                        
                        # Other connection errors
                        if any(err in error_text for err in ["not available", "connection refused", "no such device"]):
                            # Check if too many failures
                            if connection_failures >= max_failures:
                                print(f"[BT] {connection_failures} consecutive failures, cleaning up pairing")
                                self._remove_device_if_paired(active_mac)
                                self._btctl("discoverable", "on")
                                self._btctl("pairable", "on")
                                connection_failures = 0
                                await asyncio.sleep(5)
                                continue
                    else:
                        # Connection succeeded, trust the device
                        subprocess.run(["bluetoothctl", "trust", active_mac], check=False)
                        connection_failures = 0  # Reset on success
                    
                    # Wait for connection to stabilize
                    await asyncio.sleep(2)
                else:
                    # Connection already exists - reset failure counter
                    connection_failures = 0

                self.bt_mac_current = active_mac
                current_mac = active_mac

                # Re-check connection status after connection attempt
                if not _is_connected(active_mac):
                    print(f"[BT] Device {active_mac} still not connected after attempt")
                    if self.player.bt_input:
                        try: self.player.bt_input.close()
                        except Exception: pass
                    self.player.bt_input = None
                    self.player.bt_enabled = False
                    self.player.bt_mac_current = None
                    await asyncio.sleep(2)
                    continue  # Go back to top of loop to retry

                # Connection verified - reset failure counter and proceed with setup
                connection_failures = 0
                
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
            
    def _graceful_stop(self):
        try:
            self.bt_enabled = False
            if self.bt_task and not self.bt_task.done():
                self.bt_task.cancel()
        except Exception:
            pass
        try:
            ba = getattr(self.player, "_ba_proc", None)
            if ba:
                ba.terminate()
                ba.wait(timeout=2)
                self.player._ba_proc = None
        except Exception:
            pass

    def _bt_start(self, mac: str | None, clear_first: bool = True):
        """Start BT with optional cleanup of all pairings"""
        if clear_first:
            print("[BT] Clearing all pairings for fresh start")
            ok, out = self._btctl("devices", "Paired")
            if ok:
                macs = re.findall(r"Device\s+([0-9A-F:]{17})", out or "", flags=re.I)
                for m in macs:
                    self._remove_device_if_paired(m)
        
        self.bt_mac = (mac or "auto")
        self.bt_enabled = True
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
        g_music_amp = math.cos(theta)
        g_therapy_amp = math.sin(theta)
        bt_gain = round(float(g_music_amp), 4)
        
        self.player.bt_gain = bt_gain
        self.player.therapy_gain = g_therapy_amp
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
            except Exception as e: print(f"[WS] Error sending clear highlight: {e}")
        self.clients -= disconnected

    async def send_highlight(self, row_index):
        message = f"highlight:{row_index}"
        disconnected = set()
        for client in self.clients:
            try: await client.send(message)
            except websockets.exceptions.ConnectionClosed: disconnected.add(client)
            except Exception as e: print(f"[WS] Error sending highlight: {e}")
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
            async with websockets.serve(
                ws_handler.handle_client,
                "0.0.0.0",
                PORT,
                ping_interval=None,
                ping_timeout=None,
                close_timeout=None,
                max_size=None
            ):
                print(f"[WS] Listening on :{PORT}")
                ws_handler._bt_start("auto", clear_first=False)  # Don't clear on service start
                await asyncio.Future()
        except OSError as e:
            if getattr(e,"errno",None)==98: print(f"[WARN] Port {PORT} busy; retrying"); await asyncio.sleep(2); continue
            raise

if __name__=="__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
