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

info "=== SoniXscape Installer with Bluetooth Audio (user: $CURRENT_USER) ==="

# ---------- prepare /opt ----------
sudo mkdir -p /opt
sudo chown -R "$CURRENT_USER":"$CURRENT_USER" /opt

# ---------- update base ----------
info "Updating APT..."
sudo DEBIAN_FRONTEND=noninteractive apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get -y $APT_OPTS upgrade

# ---------- core deps + bluetooth ----------
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
  libsbc-dev libopenaptx-dev \
  bluez-alsa-utils bluez-tools \
  libreadline-dev libncurses5-dev

sudo mkdir -p /var/log/sonixscape
sudo chown "$CURRENT_USER":"$CURRENT_USER" /var/log/sonixscape

# ---------- ALSA Loopback ----------
info "Enabling ALSA Loopback (snd-aloop)..."
if ! lsmod | grep -q snd_aloop; then
  sudo modprobe snd-aloop || true
fi
if ! grep -q "snd-aloop" /etc/modules; then
  echo "snd-aloop" | sudo tee -a /etc/modules >/dev/null
fi

# ---------- Build bluez-alsa from source ----------
info "Building bluez-alsa from source for better compatibility..."
cd /opt
if [[ ! -d "bluez-alsa-3.0.0" ]]; then
  wget https://github.com/Arkq/bluez-alsa/archive/v3.0.0.tar.gz
  tar -xzf v3.0.0.tar.gz
  cd bluez-alsa-3.0.0
  autoreconf -fiv
  mkdir build && cd build
  ../configure --enable-cli --enable-rfcomm --enable-a2dpconf
  make -j$(nproc)
  sudo make install
  sudo ldconfig
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
info "Blacklisting HDMI sound devices..."
sudo tee /etc/modprobe.d/sonixscape-blacklist.conf >/dev/null <<'EOF'
# Prevent loading of unwanted ALSA devices
blacklist snd_hdmi_lpe_audio
EOF
sudo rmmod snd_hdmi_lpe_audio 2>/dev/null || true

# ---------- verify DAC ----------
if ! aplay -l | grep -q ICUSBAUDIO7D; then
  err "ICUSBAUDIO7D DAC not detected – cannot continue."
  exit 1
fi

echo "ALSA_DEVICE=plughw:CARD=ICUSBAUDIO7D,DEV=0" > "$SONIX_DIR/sonixscape.conf"
info "Selected ALSA device: plughw:CARD=ICUSBAUDIO7D,DEV=0"

# ---------- bluetooth agent service ----------
info "Creating Bluetooth NoInputNoOutput agent..."
sudo tee /usr/local/bin/bt-agent-setup.py > /dev/null <<'EOF'
#!/usr/bin/env python3
import dbus, dbus.mainloop.glib, dbus.service
from gi.repository import GLib
import sys

AGENT_PATH = "/test/agent"

class Agent(dbus.service.Object):
    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self): 
        print("[BT_AGENT] Agent released")

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        print(f"[BT_AGENT] Auto-confirmed {passkey} for {device}")
        return

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        print(f"[BT_AGENT] Auto-authorized service {uuid} for {device}")
        return

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self): 
        print("[BT_AGENT] Agent cancelled")

def main():
    try:
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SystemBus()
        mgr = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.AgentManager1")

        agent = Agent(bus, AGENT_PATH)
        mgr.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
        mgr.RequestDefaultAgent(AGENT_PATH)

        print("[BT_AGENT] NoInputNoOutput agent registered and set as default")
        GLib.MainLoop().run()
    except Exception as e:
        print(f"[BT_AGENT] Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
EOF

sudo chmod +x /usr/local/bin/bt-agent-setup.py

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

# ---------- bluetooth services ----------
sudo tee /etc/systemd/system/sonixscape-bt-agent.service > /dev/null <<'EOF'
[Unit]
Description=SoniXscape Bluetooth Just-Works Agent
After=bluetooth.service
Requires=bluetooth.service

[Service]
Type=simple
ExecStart=/usr/local/bin/bt-agent-setup.py
Restart=on-failure
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/bluealsa.service > /dev/null <<'EOF'
[Unit]
Description=BlueALSA daemon
After=bluetooth.service
Requires=bluetooth.service

[Service]
Type=simple
ExecStart=/usr/bin/bluealsa -S -i hci0 -p a2dp-sink
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/bluealsa-aplay.service > /dev/null <<'EOF'
[Unit]
Description=BlueALSA Audio Player - Universal
After=bluealsa.service
Requires=bluealsa.service
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStartPre=/bin/sleep 10
ExecStart=/usr/bin/bluealsa-aplay --pcm-buffer-time=1000000 --pcm-period-time=250000 -D hw:0,1 00:00:00:00:00:00
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF

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
  for svc in sonixscape-main sonixscape-audio sonixscape-ip-assign sonixscape-bt-agent bluealsa bluealsa-aplay; do
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

# ---------- Bluetooth configuration ----------
info "Configuring Bluetooth for audio..."
sudo tee /etc/bluetooth/main.conf >/dev/null <<'EOF'
[General]
Name = SoniXscape
Class = 0x20041C
DiscoverableTimeout = 0
PairableTimeout = 0
Discoverable = true
Pairable = true

[Policy]
AutoEnable = true
EOF

# ---------- enable services ----------
sudo systemctl daemon-reload
SERVICES="sonixscape-main sonixscape-audio sonixscape-ip-assign sonixscape-health.timer sonixscape-bt-agent bluealsa bluealsa-aplay"
for S in $SERVICES; do
  sudo systemctl enable "$S"
done
sudo systemctl start sonixscape-health.timer || true

# ---------- ensure bluetooth is properly configured ----------
info "Configuring Bluetooth controller..."
sudo systemctl restart bluetooth
sleep 3

# Configure bluetooth controller via bluetoothctl
sudo bash -c 'bluetoothctl << EOF
power on
discoverable on
pairable on
exit
EOF' || true

info "=== Installation Summary ==="
info "✓ Core system and dependencies installed"
info "✓ ALSA loopback module enabled"
info "✓ BluezALSA compiled and configured"
info "✓ Bluetooth agent service created (NoInputNoOutput)"
info "✓ Universal Bluetooth audio service created"
info "✓ Audio routing: BT → hw:0,1 → Mixer → 8ch Amplifier"
info "✓ First-come-first-served Bluetooth connection"
info "✓ Health monitoring enabled"
info ""
info "Bluetooth audio will automatically:"
info "  • Accept connections from any phone"
info "  • Route audio to your mixer via loopback"
info "  • Work with your web interface mix slider"
info "  • Restart automatically if it fails"
info ""
info "Install complete. Rebooting in 5 seconds..."
sleep 5
sudo reboot
