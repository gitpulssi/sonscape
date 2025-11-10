#!/usr/bin/env bash
set -euo pipefail

LOG_FILE="/var/log/sonixscape-install.log"
mkdir -p /var/log 2>/dev/null || true
exec > >(tee -a "$LOG_FILE") 2>&1

info() { echo -e "\033[1;32m[*]\033[0m $*"; }
err()  { echo -e "\033[1;31m[!]\033[0m $*"; }
warn() { echo -e "\033[1;33m[!]\033[0m $*"; }

if [[ $EUID -eq 0 ]]; then
  err "Run as a normal user (not root)."
  exit 1
fi

SONIX_DIR="/opt/sonixscape"
CURRENT_USER="$(whoami)"

info "=== SoniXscape Production Installer v3.4 (Ubuntu 24.04 LTS) ==="
info "Full Stack – Low-Latency Edition with Auto-Reboot"

sudo mkdir -p /opt /var/log/sonixscape
sudo chown -R "$CURRENT_USER":"$CURRENT_USER" /opt /var/log/sonixscape

info "Updating system and installing dependencies..."
sudo DEBIAN_FRONTEND=noninteractive apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3 python3-pip python3-venv python3-numpy python3-flask python3-websockets python3-alsaaudio \
  python3-dbus python3-gi alsa-utils git curl bluetooth bluez bluez-tools sox \
  libasound2-dev libbluetooth-dev libdbus-1-dev libglib2.0-dev libsbc-dev libopenaptx-dev \
  build-essential autoconf automake libtool pkg-config libreadline-dev libncurses5-dev

info "Configuring ALSA Loopback (low-latency)"
sudo modprobe snd-aloop || true
echo "snd-aloop" | sudo tee -a /etc/modules >/dev/null
sudo tee /etc/modprobe.d/snd-aloop-lowlatency.conf >/dev/null <<'EOF'
options snd-aloop timer_source=1 pcm_substreams=1
EOF

info "Building BlueALSA v3.0.0 from source..."
cd /opt
if [[ ! -d "bluez-alsa-3.0.0" ]]; then
  wget -q https://github.com/Arkq/bluez-alsa/archive/v3.0.0.tar.gz
  tar -xzf v3.0.0.tar.gz
  cd bluez-alsa-3.0.0
  autoreconf -fiv
  mkdir build && cd build
  ../configure --enable-cli --enable-rfcomm --enable-a2dpconf
  make -j$(nproc)
  sudo make install
  sudo ldconfig
fi

sudo tee /etc/systemd/system/bluealsa.service >/dev/null <<'EOF'
[Unit]
Description=BlueALSA Bluetooth Audio Daemon
After=bluetooth.service
Requires=bluetooth.service

[Service]
ExecStart=/usr/bin/bluealsa -S -i hci0 -p a2dp-sink -p a2dp-source
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

info "Cloning SoniXscape application..."
if [[ ! -d "$SONIX_DIR" ]]; then
  git clone https://github.com/gitpulssi/sonscape.git "$SONIX_DIR"
else
  cd "$SONIX_DIR" && git pull
fi
sudo chown -R "$CURRENT_USER":"$CURRENT_USER" "$SONIX_DIR"

info "Creating Python environment and installing dependencies..."
cd "$SONIX_DIR"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip wheel
pip install flask websockets pyalsaaudio sounddevice numpy scipy
deactivate

info "Configuring Bluetooth NoInputNoOutput agent..."
sudo tee /usr/local/bin/bt-agent-setup.py >/dev/null <<'EOF'
#!/usr/bin/env python3
import dbus, dbus.mainloop.glib, dbus.service
from gi.repository import GLib
import sys
AGENT_PATH = "/test/agent"
class Agent(dbus.service.Object):
    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self): pass
    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey): return
    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid): return
    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self): pass

def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    mgr = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.AgentManager1")
    agent = Agent(bus, AGENT_PATH)
    mgr.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
    mgr.RequestDefaultAgent(AGENT_PATH)
    print("[BT_AGENT] Registered NoInputNoOutput agent")
    GLib.MainLoop().run()
if __name__ == "__main__": main()
EOF
sudo chmod +x /usr/local/bin/bt-agent-setup.py

info "Creating systemd services..."
sudo tee /etc/systemd/system/sonixscape.service >/dev/null <<EOF
[Unit]
Description=SoniXscape Web + Audio Core
After=network-online.target sound.target bluetooth.service
Requires=bluetooth.service

[Service]
Type=simple
WorkingDirectory=$SONIX_DIR
EnvironmentFile=-$SONIX_DIR/sonixscape.conf
ExecStart=/bin/bash -c 'cd $SONIX_DIR && exec $SONIX_DIR/venv/bin/python3 -u main_app.py & exec $SONIX_DIR/venv/bin/python3 -u ws_audio.py'
Restart=always
RestartSec=5
User=$CURRENT_USER

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/sonixscape-bt-agent.service >/dev/null <<'EOF'
[Unit]
Description=SoniXscape Bluetooth Auto Agent
After=bluetooth.service
Requires=bluetooth.service

[Service]
ExecStart=/usr/local/bin/bt-agent-setup.py
Restart=on-failure
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/sonixscape-health.service >/dev/null <<'EOF'
[Unit]
Description=SoniXscape Health Monitor

[Service]
Type=oneshot
ExecStart=/opt/sonixscape/health_check.sh
EOF

sudo tee /etc/systemd/system/sonixscape-health.timer >/dev/null <<'EOF'
[Unit]
Description=Run SoniXscape Health Check every 5 minutes

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
echo "=== Health Check: $(date) ===" >> "$LOG_FILE"
for svc in sonixscape sonixscape-bt-agent bluealsa; do
  if systemctl is-active --quiet "$svc"; then
    echo "[OK] $svc running" >> "$LOG_FILE"
  else
    echo "[FAIL] $svc down – restarting..." >> "$LOG_FILE"
    systemctl restart "$svc"
  fi
done
EOF
chmod +x "$SONIX_DIR/health_check.sh"

info "Finalizing configuration..."
sudo systemctl daemon-reload
sudo systemctl enable sonixscape sonixscape-bt-agent bluealsa sonixscape-health.timer
sudo systemctl start sonixscape-health.timer

info "Bluetooth setup..."
sudo bash -c 'bluetoothctl << EOF
power on
discoverable on
pairable on
exit
EOF' || true

info "Setting hostname to SoniXscape"
echo "SoniXscape" | sudo tee /etc/hostname >/dev/null
sudo hostnamectl set-hostname SoniXscape
if ! grep -q "SoniXscape" /etc/hosts; then
echo "127.0.1.1   SoniXscape" | sudo tee -a /etc/hosts >/dev/null
fi

info "Installation complete. System will reboot in 10 seconds."
sleep 10
sudo reboot
