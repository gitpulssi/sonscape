# SoniXscape

Control software for a **sonic therapy chair** — 8 transducers across 4 body zones (neck, back,
thighs, legs) that deliver vibroacoustic sine-wave therapies. It can blend the low frequencies of
Bluetooth-streamed music into the chair vibration while sending the full-bandwidth music in sync to
a Bluetooth headset.

Runs on a small Linux SBC (Ubuntu 24.04 LTS), driven from a browser PWA.

## Architecture

Three Python processes managed by systemd, plus a browser front-end:

| Component        | File                  | Port | Role                                                        |
|------------------|-----------------------|------|------------------------------------------------------------|
| Audio engine     | `ws_audio.py`         | 8081 | Therapy synthesis, music mixing, ALSA/Bluetooth output     |
| Web/preset server| `main_app.py`         | 8080 | Serves the web UI + preset CRUD API (Flask)                |
| Alt preset server| `preset_server.py`    | 8090 | Alternate preset manager (aiohttp)                         |
| Web UI           | `webui/index.html`    | —    | Single-page app; talks to the engine over WebSocket        |
| Installer        | `install_sonixscape_v3_9_FIXED.sh` | — | BlueALSA build, systemd units, BT pairing agent |

Audio hardware: an 8-channel USB DAC (`ICUSBAUDIO7D`), an ALSA Loopback device, and a Bluetooth A2DP
sink for the headset. Install location on the device: `/opt/sonixscape`.

## systemd services

The installer creates and enables these units:

- `bluealsa.service` — BlueALSA daemon (A2DP source + sink on `hci0`)
- `sonixscape-bt-agent.service` — auto-pairing agent (NoInputNoOutput)
- `sonixscape-web.service` — runs `main_app.py` (web UI + presets)
- `sonixscape-audio.service` — runs `ws_audio.py` (the audio engine)
- `sonixscape.target` — master target that pulls in web + audio
- `sonixscape-output@.service` — template for per-device BT output bridges

## Developing locally (on the device)

**The audio engine is managed by systemd with a restart policy. Do not `kill` the PID — systemd will
immediately respawn it (new PID, parent = PID 1).** Use `systemctl` instead.

### Stop the engine so it stays stopped

```bash
sudo systemctl stop sonixscape-audio.service
ps -ef | grep '[w]s_audio.py'   # should print nothing
```

A deliberate `systemctl stop` is honored even under `Restart=always`. A raw `kill`/`kill -9` is treated
as a process exit and gets restarted — that is the "something keeps respawning it" symptom.

### Run the engine manually with live logs

```bash
sudo systemctl stop sonixscape-audio.service   # free the ALSA device first
cd /opt/sonixscape
./venv/bin/python3 -u ws_audio.py              # Ctrl-C to stop
```

A single-instance lock (`~/.ws_audio.lock`) prevents two engines running at once, so make sure the
service is stopped before launching manually.

### Restore normal appliance operation

```bash
sudo systemctl start sonixscape-audio.service
```

### Stop / start everything together

```bash
sudo systemctl stop  sonixscape.target sonixscape-audio.service sonixscape-web.service
sudo systemctl start sonixscape.target
```

### Logs

```bash
journalctl -u sonixscape-audio.service -f      # follow audio engine
journalctl -u sonixscape-web.service   -f      # follow web/preset server
```

## Restart policy

`sonixscape-audio.service` uses:

```ini
[Unit]
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Restart=on-failure
RestartSec=2
```

`Restart=on-failure` means a clean `systemctl stop` stays stopped (good for development), while a real
crash still auto-recovers — but only up to 5 times per minute, so a crash loop becomes visible instead
of silently hammering the ALSA device.

> **Already-installed devices:** the live unit was written at install time, so editing the install
> script does not change a running box. Apply the policy with a drop-in:
>
> ```bash
> sudo systemctl edit sonixscape-audio.service
> ```
>
> Paste the `[Unit]` and `[Service]` blocks above, save, then `sudo systemctl daemon-reload`.

## Installation

Run as a normal user (not root) on a fresh Ubuntu 24.04 device:

```bash
./install_sonixscape_v3_9_FIXED.sh
```

The script builds BlueALSA v3.0.0 from source, installs dependencies, writes all systemd units,
enables them, and reboots.

## Requirements

See `requirements.txt`. Core: `flask`, `websockets`, `numpy`, `sounddevice`, `pyalsaaudio`.
`scipy` is optional but recommended (used for the 200 Hz low-pass on the music-to-chair mix; a simple
FIR fallback is used if absent).
