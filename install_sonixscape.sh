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
CURRENT_USER=$(whoami)

# 1. Update base system
info "Updating system packages..."
sudo apt-get update
sudo apt-get -y upgrade

# 2. Install dependencies (excluding bluealsa – handled separately)
info "Installing dependencies..."
sudo apt-get install -y \
    python3 python3-pip python3-venv \
    python3-numpy python3-scipy python3-flask \
    python3-websockets python3-alsaaudio \
    alsa-utils git curl

# 2b. Build BlueALSA (bluealsa-aplay) from source if missing
if ! command -v bluealsa-aplay >/dev/null 2>&1; then
    info "Building BlueALSA (bluealsa-aplay only) from source..."

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

    # Disable AAC / FDK to avoid missing deps
    ../configure --disable-fdk-aac --disable-aac --enable-debug
    make -j"$(nproc)"
    sudo make install
fi

# 2c. Install systemd unit for bluealsa-aplay
info "Installing bluealsa-aplay systemd unit..."
sudo tee /etc/systemd/system/sonixscape-bluealsa.service >/dev/null <<'UNIT'
[Unit]
Description=SoniXscape BlueALSA audio sink
After=bluetooth.service sound.target
Requires=bluetooth.service

[Service]
Type=simple
ExecStart=/usr/local/bin/bluealsa-aplay --profile-a2dp 00:00:00:00:00:00
Restart=always
User='"$CURRENT_USER"'

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable sonixscape-bluealsa.service

# 3. Install SoniXscape code
if [[ ! -d "$SONIX_DIR" ]]; then
    info "Cloning SoniXscape repo..."
    sudo git clone https://github.com/gitpulssi/sonscape.git "$SONIX_DIR"
else
    info "Updating existing SoniXscape repo..."
    cd "$SONIX_DIR"
    sudo git pull
fi
sudo chown -R "$CURRENT_USER":"$CURRENT_USER" "$SONIX_DIR"

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
sudo cp "$SONIX_DIR/systemd/"*.service /etc/systemd/system/ || true
sudo systemctl daemon-reload
sudo systemctl enable sonixscape-main.service || true
sudo systemctl enable sonixscape-audio.service || true
sudo systemctl enable sonixscape-bt-agent.service || true

# 6. Comitup tweaks
info "Configuring Comitup..."

# Custom AP name + password
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

# 7. Hostname setup
info "Setting hostname to SoniXscape..."

# Change system hostname
echo "SoniXscape" | sudo tee /etc/hostname >/dev/null
sudo hostnamectl set-hostname SoniXscape

# Ensure /etc/hosts entry exists
if ! grep -q "SoniXscape" /etc/hosts; then
  sudo sed -i 's/^127.0.1.1.*/127.0.1.1   SoniXscape/' /etc/hosts || \
  echo "127.0.1.1   SoniXscape" | sudo tee -a /etc/hosts
fi

# 7. Done
info "Installation complete!"
info "Reboot now with: sudo reboot"
eboot"
