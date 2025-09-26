#!/bin/bash
set -euo pipefail

LOG_FILE="/var/log/sonixscape-install.log"
mkdir -p /var/log 2>/dev/null || true
exec > >(tee -a "$LOG_FILE") 2>&1

info() { echo -e "\033[1;32m[*]\033[0m $*"; }
err()  { echo -e "\033[1;31m[!]\033[0m $*"; }

if [[ $EUID -eq 0 ]]; then
  err "Run as a normal user (not root)."
  exit 1
fi

SONIX_DIR="/opt/sonixscape"
CURRENT_USER="$(whoami)"

APT_OPTS="-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"

info "=== SoniXscape Installer (user: $CURRENT_USER) ==="

# ---------- prepare /opt ----------
sudo mkdir -p /opt
sudo chown -R "$CURRENT_USER":"$CURRENT_USER" /opt

# ---------- update base ----------
info "Updating APT..."
sudo DEBIAN_FRONTEND=noninteractive apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get -y $APT_OPTS upgrade

# ---------- core deps ----------
info "Installing dependencies..."
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y $APT_OPTS \
  python3 python3-pip python3-venv \
  python3-numpy python3-flask python3-websockets python3-alsaaudio \
  python3-dbus python3-gi \
  alsa-utils git curl bluetooth bluez \
  libopenblas0 liblapack3 \
  libportaudio2 libportaudiocpp0 portaudio19-dev \
  build-essential autoconf automake libtool pkg-config \
  libasound2-dev libbluetooth-dev libdbus-1-dev libglib2.0-dev \
  libsbc-dev libopenaptx-dev

sudo mkdir -p /var/log/sonixscape
sudo chown "$CURRENT_USER":"$CURRENT_USER" /var/log/sonixscape

# ---------- BlueALSA ----------
if ! command -v bluealsa-aplay >/dev/null 2>&1; then
  info "Building BlueALSA (forcing utils build)..."
  cd /opt
  if [[ ! -d bluez-alsa ]]; then
    git clone https://github.com/arkq/bluez-alsa.git
  fi
  cd /opt/bluez-alsa
  autoreconf --install
  rm -rf build && mkdir build && cd build
  ../configure --disable-fdk-aac --disable-aac --enable-debug --enable-utils
  make -j"$(nproc)"
  sudo make install || true
  if [[ -f utils/aplay/bluealsa-aplay ]]; then
    sudo cp utils/aplay/bluealsa-aplay /usr/local/bin/
  fi
  sudo chmod +x /usr/local/bin/bluealsa-aplay || true
fi

# ---------- ALSA Loopback ----------
info "Enabling ALSA Loopback (snd-aloop)..."
if ! lsmod | grep -q snd_aloop; then
  sudo modprobe snd-aloop || true
fi
if ! grep -q "snd-aloop" /etc/modules; then
  echo "snd-aloop" | sudo tee -a /etc/modules >/dev/null
fi

# ---------- fetch/update app ----------
if [[ ! -d "$SONIX_DIR" ]]; then
  info "Cloning app repo..."
  git clone https://github.com/gitpulssi/sonscape.git "$SONIX_DIR"
else
  info "Updating app repo..."
  cd "$SONIX_DIR" && git pull
fi
sudo chown -R "$CURRENT_USER":"$CURRENT_USER" "$SONIX_DIR"

# ---------- python env ----------
info "Creating Python venv..."
cd "$SONIX_DIR"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip wheel
pip install flask websockets pyalsaaudio sounddevice numpy
deactivate

# ---------- webui config ----------
info "Creating webui config directory..."
mkdir -p /home/$CURRENT_USER/webui

# mix.json
if [ ! -f /home/$CURRENT_USER/webui/mix.json ]; then
  echo '{}' > /home/$CURRENT_USER/webui/mix.json
fi

# config.json
if [ ! -f /home/$CURRENT_USER/webui/config.json ]; then
  echo '{}' > /home/$CURRENT_USER/webui/config.json
fi

chown -R "$CURRENT_USER":"$CURRENT_USER" /home/$CURRENT_USER/webui

# ---------- blacklist unwanted ALSA devices ----------
info "Blacklisting Loopback and HDMI sound devices..."

sudo tee /etc/modprobe.d/sonixscape-blacklist.conf >/dev/null <<'EOF'
# Prevent loading of unwanted ALSA sound devices
blacklist snd_aloop
blacklist snd_hdmi_lpe_audio
EOF

# Remove them if already loaded in this session
sudo rmmod snd_aloop 2>/dev/null || true
sudo rmmod snd_hdmi_lpe_audio 2>/dev/null || true

# ---------- detect ALSA ----------
# Prefer ICUSBAUDIO7D card if present, otherwise fall back to first card
CARD=$(aplay -l | awk '/ICUSBAUDIO7D/{print $2; exit}' | tr -d ':')
if [ -z "$CARD" ]; then
  CARD=$(aplay -l | awk '/^card [0-9]+:/{print $2; exit}' | tr -d ':')
fi
DEVICE=$(aplay -l | awk -v c="$CARD" '$0 ~ "^card "c":" {print $6; exit}' | tr -d ':')
ALSA_DEV="hw:${CARD},${DEVICE}"

echo "ALSA_DEVICE=$ALSA_DEV" > "$SONIX_DIR/sonixscape.conf"
info "Selected ALSA device: $ALSA_DEV"

sudo tee /etc/asound.conf >/dev/null <<EOF
pcm.!default {
  type plug
  slave.pcm "$ALSA_DEV"
}
ctl.!default {
  type hw
  card $CARD
}
EOF

# ---------- systemd services ----------
sudo tee /etc/systemd/system/sonixscape-main.service > /dev/null <<'EOF'
[Unit]
Description=SoniXscape Web UI
After=network-online.target

[Service]
WorkingDirectory=/opt/sonixscape
ExecStart=/opt/sonixscape/venv/bin/python3 /opt/sonixscape/main_app.py
Restart=always
User=comitup
Environment=PYTHONUNBUFFERED=1
StandardOutput=append:/var/log/sonixscape/main.log
StandardError=append:/var/log/sonixscape/main.log

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/sonixscape-audio.service > /dev/null <<'EOF'
[Unit]
Description=SoniXscape Audio Engine
After=sound.target bluetooth.service
Requires=bluetooth.service

[Service]
WorkingDirectory=/opt/sonixscape
EnvironmentFile=/opt/sonixscape/sonixscape.conf
ExecStart=/opt/sonixscape/venv/bin/python3 /opt/sonixscape/ws_audio.py
Restart=always
User=comitup
StandardOutput=append:/var/log/sonixscape/audio.log
StandardError=append:/var/log/sonixscape/audio.log

[Install]
WantedBy=multi-user.target
EOF

# ---------- REMOVE old BlueALSA sink service ----------
if [ -f /etc/systemd/system/sonixscape-bluealsa.service ]; then
  sudo systemctl disable --now sonixscape-bluealsa.service || true
  sudo rm -f /etc/systemd/system/sonixscape-bluealsa.service
fi

sudo tee /etc/systemd/system/sonixscape-health.service > /dev/null <<'EOF'
[Unit]
Description=SoniXscape Health Check
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/opt/sonixscape/health_check.sh
EOF

sudo tee /etc/systemd/system/sonixscape-health.timer > /dev/null <<'EOF'
[Unit]
Description=Run SoniXscape Health Check at boot and every 5 minutes

[Timer]
OnBootSec=30
OnUnitActiveSec=5min
Unit=sonixscape-health.service

[Install]
WantedBy=multi-user.target
EOF

cat > "$SONIX_DIR/health_check.sh" <<'EOF'
#!/bin/bash
LOG_FILE="/var/log/sonixscape/health.log"
{
  echo "=== Health Check: $(date) ==="
  for svc in sonixscape-main sonixscape-audio sonixscape-ip-assign; do
    if systemctl is-active --quiet "$svc"; then
      echo "[OK] $svc running"
    else
      echo "[FAIL] $svc down – restarting..."
      systemctl restart "$svc"
      sleep 2
      systemctl is-active --quiet "$svc" && echo "[RECOVERED] $svc back up" || echo "[ERROR] $svc still down"
    fi
  done
  echo ""
} >> "$LOG_FILE" 2>&1
EOF
chmod +x "$SONIX_DIR/health_check.sh"

# ---------- Comitup ----------
sudo tee /etc/comitup.conf >/dev/null <<'EOF'
ap_name: SoniXscape
ap_password: sonixscape123
EOF

sudo tee /etc/systemd/system/sonixscape-ip-assign.service > /dev/null <<'EOF'
[Unit]
Description=Force static IP on wlan0 for AP mode
After=network-pre.target
Before=network.target
Wants=network.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c '/sbin/ip addr add 10.42.0.1/24 dev wlan0 || true'
ExecStart=/sbin/ip link set wlan0 up
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

# ---------- Hostname ----------
echo "SoniXscape" | sudo tee /etc/hostname >/dev/null
sudo hostnamectl set-hostname SoniXscape
if ! grep -q "SoniXscape" /etc/hosts; then
  echo "127.0.1.1   SoniXscape" | sudo tee -a /etc/hosts >/dev/null
fi

# ---------- enable services ----------
sudo systemctl daemon-reload
SERVICES="sonixscape-main sonixscape-audio sonixscape-ip-assign sonixscape-health.timer"
for S in $SERVICES; do
  sudo systemctl enable "$S"
done
sudo systemctl start sonixscape-health.timer || true

info "Install complete. Rebooting in 5 seconds..."
sleep 5
sudo reboot
