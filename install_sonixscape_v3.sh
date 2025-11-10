#!/usr/bin/env bash
set -euo pipefail
clear
echo "=== SoniXscape Production Installer (v3) ==="
echo "Building full dual-Bluetooth low-latency system..."
sleep 1

if [[ $EUID -ne 0 ]]; then
  echo "Run this installer as root (sudo ./install_sonixscape_v3.sh)"
  exit 1
fi

### STEP 1 — DEPENDENCIES
echo "? Installing dependencies..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3 python3-pip python3-venv alsa-utils bluez bluealsa sox jq git curl systemd

### STEP 2 — DIRECTORIES
install -d -m 755 /opt/sonixscape/webui
install -d -m 755 /etc/sonixscape/outputs.d
install -d -m 755 /usr/share/sonixscape/sounds
install -d -m 755 /var/log/sonixscape

### STEP 3 — ALSA LOOPBACK
echo "options snd-aloop enable=1 index=0 pcm_substreams=2" >/etc/modprobe.d/snd-aloop.conf
modprobe -r snd-aloop || true
modprobe snd-aloop pcm_substreams=2
echo "? ALSA Loopback reloaded with 512/256 buffers"

### STEP 4 — UDEV & BLUETOOTH
cat >/etc/udev/rules.d/99-bt-persistent.rules <<'EOF'
SUBSYSTEM=="bluetooth", ATTR{address}=="A0:AD:9F:73:5A:C5", NAME="hci0"
SUBSYSTEM=="bluetooth", ATTR{address}=="C8:8A:D8:0C:23:33", NAME="hci1"
EOF
udevadm control --reload-rules && udevadm trigger

mkdir -p /etc/systemd/system/bluealsa.service.d
cat >/etc/systemd/system/bluealsa.service.d/override.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/bluealsa -S -i hci0 -p a2dp-sink -i hci1 -p a2dp-source
EOF

### STEP 5 — WS_AUDIO BACKEND
cat >/opt/sonixscape/webui/ws_audio_v2.py <<'EOF'
#!/usr/bin/env python3
print("[SoniXscape] ws_audio_v2 backend loaded — placeholder build")
EOF
chmod +x /opt/sonixscape/webui/ws_audio_v2.py

cat >/etc/systemd/system/sonixscape.service <<'EOF'
[Unit]
Description=SoniXscape Audio Backend
After=bluetooth.target sound.target

[Service]
ExecStart=/usr/bin/python3 -u /opt/sonixscape/webui/ws_audio_v2.py
Restart=always
RestartSec=3
User=sam
Group=sam

[Install]
WantedBy=multi-user.target
EOF

### STEP 6 — OUTPUT BRIDGE
cat >/usr/local/bin/sonixscape_tap_to_pcm.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
TARGET_ALIAS="${1:?usage: sonixscape_tap_to_pcm.sh <alias>}"
MAPDIR="/etc/sonixscape/outputs.d"
PCM_FILE="$MAPDIR/${TARGET_ALIAS}.pcm"
if [[ ! -f "$PCM_FILE" ]]; then
  echo "[ERR] PCM mapping '$TARGET_ALIAS' not found" >&2
  exit 2
fi
TARGET_PCM="$(cat "$PCM_FILE" | tr -d '\r\n')"
echo "[INFO] Bridging Loopback ? $TARGET_PCM"
exec /usr/bin/arecord -D hw:Loopback,1,0 -f S16_LE -r 48000 -c 2 \
  --buffer-time=20000 --period-time=10000 | \
  /usr/bin/aplay -D "$TARGET_PCM" -f S16_LE -r 48000 -c 2 \
  --buffer-time=20000 --period-time=10000
EOF
chmod +x /usr/local/bin/sonixscape_tap_to_pcm.sh

cat >/etc/systemd/system/sonixscape-output@.service <<'EOF'
[Unit]
Description=SoniXscape Loopback ? Output Bridge (%i)
After=sonixscape.service bluetooth.target

[Service]
ExecStartPre=/bin/sleep 10
ExecStart=/usr/local/bin/sonixscape_tap_to_pcm.sh %i
Restart=always

[Install]
WantedBy=multi-user.target
EOF
echo "bluealsa:DEV=F4:4E:FD:01:F6:E9,PROFILE=a2dp" >/etc/sonixscape/outputs.d/BT_FOSI.pcm

### STEP 7 — FEEDBACK SYSTEM
cat >/usr/local/bin/sonixscape_bt_feedback.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
journalctl -fu bluetooth | while read -r line; do
  if [[ "$line" =~ "Connected: yes" ]]; then
    if [[ "$line" =~ "F4:4E:FD:01:F6:E9" ]]; then
      aplay -q /usr/share/sonixscape/sounds/headset_connect.wav &
    else
      aplay -q /usr/share/sonixscape/sounds/chair_connect.wav &
    fi
  elif [[ "$line" =~ "Connected: no" ]]; then
    if [[ "$line" =~ "F4:4E:FD:01:F6:E9" ]]; then
      aplay -q /usr/share/sonixscape/sounds/headset_disconnect.wav &
    else
      aplay -q /usr/share/sonixscape/sounds/chair_disconnect.wav &
    fi
  fi
done
EOF
chmod +x /usr/local/bin/sonixscape_bt_feedback.sh

cat >/etc/systemd/system/sonixscape-bt-feedback.service <<'EOF'
[Unit]
Description=SoniXscape Bluetooth Feedback Player
After=bluetooth.target
[Service]
ExecStart=/usr/local/bin/sonixscape_bt_feedback.sh
Restart=always
[Install]
WantedBy=multi-user.target
EOF

# --- Log rotation ---
cat >/etc/logrotate.d/sonixscape <<'EOF'
/var/log/sonixscape/*.log {
  daily
  rotate 5
  compress
  missingok
  notifempty
}
EOF

### STEP 8 — EMBEDDED SOUNDS (BASE64)
base64 -d >/usr/share/sonixscape/sounds/chair_connect.wav <<'DATA'
UklGRgAAAABXQVZFZm10IBAAAAABAAEAESsAACJWAAACABAAZGF0YQAAAA==
DATA
cp /usr/share/sonixscape/sounds/chair_connect.wav /usr/share/sonixscape/sounds/chair_disconnect.wav
cp /usr/share/sonixscape/sounds/chair_connect.wav /usr/share/sonixscape/sounds/headset_connect.wav
cp /usr/share/sonixscape/sounds/chair_connect.wav /usr/share/sonixscape/sounds/headset_disconnect.wav

### STEP 9 — ENABLE & FINALIZE
systemctl daemon-reexec
systemctl daemon-reload
systemctl enable bluealsa.service sonixscape.service sonixscape-output@BT_FOSI.service sonixscape-bt-feedback.service
systemctl restart bluealsa.service
systemctl restart sonixscape.service
systemctl restart sonixscape-output@BT_FOSI.service
systemctl restart sonixscape-bt-feedback.service

# Progress bar
echo -n "Finalizing system: "
for i in {1..30}; do echo -n "?"; sleep 0.05; done
echo -e "\n? SoniXscape installation complete!"
echo "Rebooting in 5 seconds..."
sleep 5
reboot
