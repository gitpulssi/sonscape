#!/bin/bash
set -euo pipefail

info() { echo -e "\033[1;32m[*]\033[0m $*"; }
error() { echo -e "\033[1;31m[!]\033[0m $*"; }

if [[ $EUID -eq 0 ]]; then
    error "Please run as a normal user, not root (use sudo when needed)."
    exit 1
fi

SONIX_DIR=/opt/sonixscape
WIFI_IFACE="wlan0"

# 1. Update base system
info "Updating system packages..."
sudo apt-get update
sudo apt-get -y upgrade

# 2. Install dependencies
info "Installing dependencies..."
sudo apt-get install -y \
    python3 python3-pip python3-venv \
    python3-numpy python3-scipy python3-flask \
    python3-websockets python3-alsaaudio \
    alsa-utils bluealsa \
    git curl

# 3. Install SoniXscape code
if [[ ! -d "$SONIX_DIR" ]]; then
    info "Cloning SoniXscape repo..."
    sudo git clone https://github.com/YOUR_REPO/sonixscape.git "$SONIX_DIR"
else
    info "Updating existing SoniXscape repo..."
    cd "$SONIX_DIR"
    sudo git pull
fi
sudo chown -R $USER:$USER "$SONIX_DIR"

# 4. Python virtual environment
info "Setting up Python virtual environment..."
cd "$SONIX_DIR"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt
deactivate

# 5. Systemd services
info "Installing systemd service units..."
sudo cp "$SONIX_DIR/systemd/"*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable sonixscape-main.service
sudo systemctl enable sonixscape-audio.service
sudo systemctl enable sonixscape-bt-agent.service

# 6. Comitup tweaks
info "Configuring Comitup..."

# Custom AP name
sudo tee /etc/comitup.conf >/dev/null <<'CONF'
ap_name: SoniXscape
ap_password: sonixscape123
CONF

# Ensure static IP is applied in AP mode
info "Creating sonixscape-ip-assign.service..."
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

sudo systemctl daemon-reload
sudo systemctl enable sonixscape-ip-assign.service

# 7. Done
info "Installation complete!"
info "Reboot now with: sudo reboot"

