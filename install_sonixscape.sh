#!/bin/bash
set -euo pipefail

LOG_FILE="/var/log/sonixscape-install.log"
exec > >(tee -a "$LOG_FILE") 2>&1

info() { echo -e "\033[1;32m[*]\033[0m $*"; }
error() { echo -e "\033[1;31m[!]\033[0m $*"; }

if [[ $EUID -eq 0 ]]; then
    error "Please run as a normal user, not root (use sudo when needed)."
    exit 1
fi

SONIX_DIR=/opt/sonixscape
WIFI_IFACE="wlan0"
CURRENT_USER=$(whoami)

info "=== SoniXscape Installer started as $CURRENT_USER. Log: $LOG_FILE ==="

# 1. Update system
info "Updating system packages..."
sudo apt-get update
sudo apt-get -y upgrade

# 2. Install dependencies
info "Installing dependencies..."
sudo apt-get install -y \
    python3 python3-pip python3-venv \
    python3-numpy python3-scipy python3-flask \
    python3-websockets python3-alsaaudio \
    alsa-utils git curl \
    bluetooth bluez

# Create log dir
sudo mkdir -p /var/log/sonixscape
sudo chown $CURRENT_USER:$CURRENT_USER /var/log/sonixscape

# 3. Build BlueALSA (bluealsa-aplay only)
if ! command -v bluealsa-aplay >/dev/null 2>&1; then
    info "Building BlueALSA (bluealsa-aplay only)..."

    sudo apt-get install -y \
        build-essential autoconf automake libtool pkg-config \
        libasound2-dev libbluetooth-dev libdbus-1-dev libglib2.0-dev \
        libsbc-dev libopenaptx-dev

    cd /opt
    if [[ ! -d bluez-alsa ]]; then
        git clone https://github.com/arkq/bluez-alsa.git
        sudo chown -R "$CURRENT_USER":"$CURRENT_USER" bluez-alsa
    fi

    cd bluez-alsa
    autoreconf --install
    rm -rf build
    mkdir build && cd build

    ../configure --disable-fdk-aac --disable-aac --enable-debug
    make -j"$(nproc)"
    sudo make install
fi

# 4. Install/Update SoniXscape code
if [[ ! -d "$SONIX_DIR" ]]; then
    info "Cloning SoniXscape repo..."
    sudo git clone https://github.com/gitpulssi/sonscape.git "$SONIX_DIR"
else
    info "Updating existing SoniXscape repo..."
    cd "$SONIX_DIR"
    sudo git pull
fi
sudo chown -R "$CURRENT_USER":"$CURRENT_USER" "$SONIX_DIR"

# 5. Python virtual environment
info "Setting up Python venv..."
cd "$SONIX_DIR"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt
deactivate

# 6. Systemd services

info "Creating systemd services..."

# Web UI
sudo tee /etc/systemd/system/sonixscape-main.service >/dev/null <<UNIT
[Unit]
Description=SoniXscape Web UI
After=network-online.target

[Service]
WorkingDirectory=$SONIX_DIR
ExecStart=$SONIX_DIR/venv/bin/python3 app.py
Restart=always
User=$CURRENT_USER
Environment=PYTHONUNBUFFERED=1
StandardOutput=append:/var/log/sonixscape/main.log
StandardError=append:/var/log/sonixscape/main.log

[Install]
WantedBy=multi-user.target
UNIT

# Audio Engine
sudo tee /etc/systemd/system/sonixscape-audio.service >/dev/null <<UNIT
[Unit]
Description=SoniXscape Audio Engine
After=sound.target

[Service]
WorkingDirectory=$SONIX_DIR
ExecStart=$SONIX_DIR/venv/bin/python3 ws_audio.py
Restart=always
User=$CURRENT_USER
StandardOutput=append:/var/log/sonixscape/audio.log
StandardError=append:/var/log/sonixscape/audio.log

[Install]
WantedBy=multi-user.target
UNIT

# Bluetooth Auto-Pair Agent
sudo tee /etc/systemd/system/sonixscape-bt-agent.service >/dev/null <<UNIT
[Unit]
Description=SoniXscape Bluetooth Auto-Pairing Agent
After=bluetooth.service
Requires=bluetooth.service

[Service]
ExecStart=$SONIX_DIR/venv/bin/python3 $SONIX_DIR/bt_agent.py
Restart=always
User=$CURRENT_USER
StandardOutput=append:/var/log/sonixscape/bt-agent.log
StandardError=append:/var/log/sonixscape/bt-agent.log

[Install]
WantedBy=multi-user.target
UNIT

# BlueALSA A2DP Sink
sudo tee /etc/systemd/system/sonixscape-bluealsa.service >/dev/null <<UNIT
[Unit]
Description=SoniXscape BlueALSA audio sink
After=bluetooth.service sound.target
Requires=bluetooth.service

[Service]
Type=simple
ExecStart=/usr/local/bin/bluealsa-aplay --profile-a2dp 00:00:00:00:00:00
Restart=always
User=$CURRENT_USER
StandardOutput=append:/var/log/sonixscape/bluealsa.log
StandardError=append:/var/log/sonixscape/bluealsa.log

[Install]
WantedBy=multi-user.target
UNIT

# Health Check
sudo tee /etc/systemd/system/sonixscape-health.service >/dev/null <<UNIT
[Unit]
Description=SoniXscape Health Check

[Service]
Type=oneshot
ExecStart=$SONIX_DIR/health_check.sh
UNIT

sudo tee /etc/systemd/system/sonixscape-health.timer >/dev/null <<UNIT
[Unit]
Description=Run SoniXscape Health Check every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Unit=sonixscape-health.service

[Install]
WantedBy=multi-user.target
UNIT

# 7. Comitup tweaks
info "Configuring Comitup..."
sudo tee /etc/comitup.conf >/dev/null <<EOF
ap_name: SoniXscape
ap_password: sonixscape123
EOF

sudo tee /etc/systemd/system/sonixscape-ip-assign.service >/dev/null <<UNIT
[Unit]
Description=Force static IP on wlan0 for AP mode
After=network-pre.target
Before=network.target
Wants=network.target

[Service]
Type=oneshot
ExecStart=/sbin/ip addr add 10.42.0.1/24 dev $WIFI_IFACE || true
ExecStart=/sbin/ip link set $WIFI_IFACE up
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT

# 8. Hostname
info "Setting hostname..."
echo "SoniXscape" | sudo tee /etc/hostname >/dev/null
sudo hostnamectl set-hostname SoniXscape
if ! grep -q "SoniXscape" /etc/hosts; then
  sudo sed -i 's/^127.0.1.1.*/127.0.1.1   SoniXscape/' /etc/hosts || \
  echo "127.0.1.1   SoniXscape" | sudo tee -a /etc/hosts
fi

# 9. Health check script
info "Installing health check script..."
cat > "$SONIX_DIR/health_check.sh" <<'EOS'
#!/bin/bash
LOG_FILE="/var/log/sonixscape/health.log"

{
  echo "=== SoniXscape Health Check: $(date) ==="
  for svc in sonixscape-main sonixscape-audio sonixscape-bt-agent sonixscape-bluealsa sonixscape-ip-assign; do
    if systemctl is-active --quiet $svc; then
      echo "[OK] $svc is running"
    else
      echo "[FAIL] $svc is NOT running"
      systemctl status $svc --no-pager -l | head -20
    fi
  done
  echo ""
} >> "$LOG_FILE" 2>&1
EOS
chmod +x "$SONIX_DIR/health_check.sh"

# 10. Enable services
info "Enabling services..."
sudo systemctl daemon-reload
sudo systemctl enable sonixscape-main.service
sudo systemctl enable sonixscape-audio.service
sudo systemctl enable sonixscape-bt-agent.service
sudo systemctl enable sonixscape-bluealsa.service
sudo systemctl enable sonixscape-ip-assign.service
sudo systemctl enable sonixscape-health.timer

# 11. Finish
info "Installation complete! Rebooting in 5 seconds..."
sleep 5
sudo reboot
