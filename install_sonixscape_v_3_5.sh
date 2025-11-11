#!/usr/bin/env bash
set -euo pipefail

LOG_FILE="/var/log/sonixscape-install.log"
mkdir -p /var/log 2>/dev/null || true
exec > >(tee -a "$LOG_FILE") 2>&1

info() { echo -e "\033[1;32m[*]\033[0m $*"; }
err()  { echo -e "\033[1;31m[!]\033[0m $*"; }

if [[ $EUID -eq 0 ]]; then
  err "Run this installer as a normal user (not root)."
  exit 1
fi

SONIX_DIR="/opt/sonixscape"
CURRENT_USER="$(whoami)"

info "=== SoniXscape Production Installer v3.5 (Ubuntu 24.04 LTS) ==="
info "Dual-Bluetooth + Full Audio Routing + Auto Pair/Reboot"

sudo mkdir -p /opt /var/log/sonixscape
sudo chown -R "$CURRENT_USER":"$CURRENT_USER" /opt /var/log/sonixscape

info "Updating and installing dependencies..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv python3-flask python3-websockets python3-alsaaudio python3-dbus python3-gi \
  alsa-utils git curl bluez bluez-tools build-essential autoconf automake libtool pkg-config \
  libasound2-dev libbluetooth-dev libdbus-1-dev libglib2.0-dev libsbc-dev libopenaptx-dev \
  libportaudio2 portaudio19-dev sox

info "Ensuring snd-aloop is enabled (low-latency)"
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

[[ -x /usr/local/bin/bluealsa ]] && sudo ln -sf /usr/local/bin/bluealsa /usr/bin/bluealsa
[[ -x /usr/local/bin/bluealsa-aplay ]] && sudo ln -sf /usr/local/bin/bluealsa-aplay /usr/bin/bluealsa-aplay

sudo tee /etc/systemd/system/bluealsa.service >/dev/null <<'EOF'
[Unit]
Description=BlueALSA Bluetooth Audio Daemon
After=bluetooth.service
Requires=bluetooth.service

[Service]
ExecStart=/usr/bin/bluealsa -S -i hci0 -p a2dp-sink -i hci1 -p a2dp-source
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

info "Creating bluealsa-aplay routing service..."
sudo tee /etc/systemd/system/bluealsa-aplay.service >/dev/null <<'EOF'
[Unit]
Description=BlueALSA Playback Bridge
After=bluealsa.service
Requires=bluealsa.service

[Service]
ExecStart=/usr/local/bin/bluealsa-aplay --pcm-buffer-time=200000 --pcm-period-time=50000 -D plughw:Loopback,0 00:00:00:00:00:00
Restart=on-failure
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now bluealsa.service bluealsa-aplay.service

info "Cloning latest SoniXscape repo..."
if [[ ! -d "$SONIX_DIR" ]]; then
  git clone https://github.com/gitpulssi/sonscape.git "$SONIX_DIR"
else
  cd "$SONIX_DIR" && git pull
fi
sudo chown -R "$CURRENT_USER":"$CURRENT_USER" "$SONIX_DIR"

info "Setting up Python virtual environment..."
cd "$SONIX_DIR"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip wheel
pip install flask websockets pyalsaaudio sounddevice numpy scipy
deactivate

info "Creating /opt/sonixscape/sonixscape.conf"
cat <<CONF | sudo tee /opt/sonixscape/sonixscape.conf >/dev/null
ALSA_DEVICE=plughw:CARD=ICUSBAUDIO7D,DEV=0
BT_DEVICE=50:16:F4:1B:20:9C
CONF

info "Adding ALSA routing configuration..."
sudo tee /etc/asound.conf >/dev/null <<'EOF'
pcm.chair_out {
  type hw
  card ICUSBAUDIO7D
}

pcm.bt_in {
  type plug
  slave.pcm "bluealsa:DEV=50:16:F4:1B:20:9C,PROFILE=a2dp"
}

pcm.loopback {
  type plug
  slave.pcm "hw:Loopback,0,0"
}

ctl.!default {
  type hw
  card ICUSBAUDIO7D
}
EOF

info "Creating independent web and audio services..."

sudo tee /etc/systemd/system/sonixscape-web.service >/dev/null <<EOF
[Unit]
Description=SoniXscape Web Interface
After=network-online.target bluetooth.service
Requires=bluetooth.service

[Service]
WorkingDirectory=$SONIX_DIR
ExecStart=$SONIX_DIR/venv/bin/python3 -u main_app.py
Restart=always
User=$CURRENT_USER

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/sonixscape-audio.service >/dev/null <<EOF
[Unit]
Description=SoniXscape Audio Engine
After=sonixscape-web.service bluealsa.service
Requires=bluealsa.service

[Service]
WorkingDirectory=$SONIX_DIR
ExecStart=$SONIX_DIR/venv/bin/python3 -u ws_audio.py
Restart=always
User=$CURRENT_USER

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/sonixscape.service >/dev/null <<'EOF'
[Unit]
Description=SoniXscape Master Target
Requires=sonixscape-web.service sonixscape-audio.service
After=sonixscape-web.service sonixscape-audio.service

[Install]
WantedBy=multi-user.target
EOF

info "Setting up automatic Bluetooth agent..."
sudo tee /usr/local/bin/bt-agent-setup.py >/dev/null <<'EOF'
#!/usr/bin/env python3
import dbus, dbus.mainloop.glib, dbus.service
from gi.repository import GLib
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

sudo tee /etc/systemd/system/sonixscape-bt-agent.service >/dev/null <<'EOF'
[Unit]
Description=SoniXscape Bluetooth Auto-Agent
After=bluetooth.service
Requires=bluetooth.service

[Service]
ExecStart=/usr/local/bin/bt-agent-setup.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable sonixscape-bt-agent.service sonixscape-web.service sonixscape-audio.service sonixscape.service
sudo systemctl restart bluetooth
sudo systemctl start sonixscape-bt-agent.service

info "Auto-pairing chair device..."
bluetoothctl <<EOF || true
power on
discoverable on
pairable on
agent NoInputNoOutput
default-agent
scan on
EOF

info "Finalizing and enabling all services..."
sudo systemctl enable bluealsa bluealsa-aplay sonixscape-bt-agent sonixscape-web sonixscape-audio sonixscape.service

info "Installation complete — rebooting in 10 seconds."
sleep 10
sudo reboot
