#!/usr/bin/env bash
set -euo pipefail
echo "=== SoniXscape Production Installer v3.3 (Ubuntu 24.04 LTS) ==="
echo "Preparing clean environment..."
sleep 1

# Progress function
progress() { echo -ne "\r[####] $1..."; }

progress "Updating apt repositories"
sudo apt update -y >/dev/null

progress "Installing dependencies"
sudo apt install -y git build-essential autoconf automake libtool pkg-config         libasound2-dev libbluetooth-dev libdbus-1-dev libbsd-dev libncurses-dev         bluez bluez-tools sox >/dev/null

echo
echo "Cleaning old installations..."
sudo systemctl stop sonixscape.service sonixscape-output@BT_FOSI.service 2>/dev/null || true
sudo pkill -f "aplay|arecord|bluealsa" 2>/dev/null || true
sudo rm -rf /opt/sonixscape /tmp/bluez-alsa /etc/systemd/system/bluealsa.service 2>/dev/null || true

mkdir -p /opt/sonixscape/sounds

progress "Compiling BlueALSA from source"
cd /tmp
git clone https://github.com/Arkq/bluez-alsa.git >/dev/null 2>&1
cd bluez-alsa
autoreconf --install >/dev/null 2>&1
mkdir build && cd build
../configure --enable-a2dp --enable-hcitop >/dev/null
make -j$(nproc) >/dev/null
sudo make install >/dev/null
sudo ldconfig

echo
echo "Creating BlueALSA systemd service..."
sudo tee /etc/systemd/system/bluealsa.service >/dev/null <<'EOF'
[Unit]
Description=BlueALSA Bluetooth Audio Daemon
After=bluetooth.service
Requires=bluetooth.service

[Service]
ExecStart=/usr/bin/bluealsa -S -i hci0 -p a2dp-sink -i hci1 -p a2dp-source
Restart=on-failure
User=root

[Install]
WantedBy=multi-user.target
EOF

echo "Creating D-Bus policy for BlueALSA..."
sudo tee /etc/dbus-1/system.d/org.bluealsa.conf >/dev/null <<'EOF'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy user="root">
    <allow own="org.bluealsa"/>
    <allow send_destination="org.bluealsa"/>
  </policy>
  <policy context="default">
    <allow send_destination="org.bluealsa"/>
  </policy>
</busconfig>
EOF

progress "Enabling bluetooth + bluealsa"
sudo systemctl daemon-reexec
sudo systemctl daemon-reload
sudo systemctl enable --now bluetooth.service
sudo systemctl enable --now bluealsa.service

echo "Embedding placeholder WAV feedback sounds..."
echo "WAV files will be embedded here in production build."

echo
echo "Installation complete. System will reboot in 10 seconds."
sleep 10
sudo reboot
