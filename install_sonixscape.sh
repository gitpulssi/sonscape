#!/bin/bash
set -euo pipefail

LOG_FILE="/var/log/sonixscape-install.log"
exec > >(tee -a "$LOG_FILE") 2>&1

info() { echo -e "\033[1;32m[*]\033[0m $*"; }
warn() { echo -e "\033[1;33m[~]\033[0m $*"; }
error() { echo -e "\033[1;31m[!]\033[0m $*"; }

if [[ $EUID -eq 0 ]]; then
  error "Run as a normal user (not root). Use sudo inside the script when needed."
  exit 1
fi

# ---------- SETTINGS ----------
SONIX_DIR="/opt/sonixscape"
WIFI_IFACE="wlan0"
CURRENT_USER="$(whoami)"
# ------------------------------

info "=== SoniXscape Installer started as ${CURRENT_USER}. Log: ${LOG_FILE} ==="

# 0) Make /opt writable for the current user
sudo mkdir -p /opt
sudo chown -R "$CURRENT_USER":"$CURRENT_USER" /opt

# 1) Update base system
info "Updating system packages..."
sudo apt-get update
sudo apt-get -y upgrade

# 2) Core dependencies (Python, audio, BT, math runtimes)
info "Installing core dependencies..."
sudo apt-get install -y \
  python3 python3-pip python3-venv \
  python3-numpy python3-flask \
  python3-websockets python3-alsaaudio \
  alsa-utils git curl \
  bluetooth bluez \
  libopenblas0 liblapack3 \
  libportaudio2 libportaudiocpp0 portaudio19-dev

# 2a) Create log directory
sudo mkdir -p /var/log/sonixscape
sudo chown "$CURRENT_USER":"$CURRENT_USER" /var/log/sonixscape

# 3) Build BlueALSA (bluealsa-aplay only) if missing
if ! command -v bluealsa-aplay >/dev/null 2>&1; then
  info "Building BlueALSA (bluealsa-aplay only)..."
  sudo apt-get install -y \
    build-essential autoconf automake libtool pkg-config \
    libasound2-dev libbluetooth-dev libdbus-1-dev libglib2.0-dev \
    libsbc-dev libopenaptx-dev

  cd /opt
  if [[ ! -d bluez-alsa ]]; then
    git clone https://github.com/arkq/bluez-alsa.git
  fi

  cd /opt/bluez-alsa
  autoreconf --install
  rm -rf build
  mkdir build && cd build

  # Disable AAC / FDK to avoid missing/removed deps on Bookworm
  ../configure --disable-fdk-aac --disable-aac --enable-debug
  make -j"$(nproc)"
  sudo make install
else
  info "bluealsa-aplay present. Skipping build."
fi

# 4) Fetch/update SoniXscape app
if [[ ! -d "$SONIX_DIR" ]]; then
  info "Cloning SoniXscape repo..."
  git clone https://github.com/gitpulssi/sonscape.git "$SONIX_DIR"
else
  info "Updating SoniXscape repo..."
  cd "$SONIX_DIR"
  git pull
fi
sudo chown -R "$CURRENT_USER":"$CURRENT_USER" "$SONIX_DIR"

# 5) Python virtual environment + lightweight pip deps
info "Setting up Python venv & packages..."
cd "$SONIX_DIR"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip wheel
# Keep numpy from apt; install only light packages in the venv
pip install flask websockets pyalsaaudio sounddevice
deactivate

# 6) Bluetooth auto-pair agent (creates bt_agent.py if missing)
if [[ ! -f "$SONIX_DIR/bt_agent.py" ]]; then
  info "Writing Bluetooth auto-pairing agent..."
  cat > "$SONIX_DIR/bt_agent.py" <<'PYCODE'
#!/usr/bin/env python3
import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

AGENT_PATH = "/test/agent"

class Agent(dbus.service.Object):
    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self): pass

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestPinCode(self, device): return "0000"

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device): return dbus.UInt32(0)

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered): pass

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode): pass

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey): return

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def AuthorizeService(self, device, uuid): return

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self): pass

def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    obj = bus.get_object("org.bluez", "/org/bluez")
    mgr = dbus.Interface(obj, "org.bluez.AgentManager1")
    agent = Agent(bus, AGENT_PATH)
    mgr.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
    mgr.RequestDefaultAgent(AGENT_PATH)
    print("SoniXscape Bluetooth agent running…")
    GLib.MainLoop().run()

if __name__ == '__main__':
    main()
PYCODE
  chmod +x "$SONIX_DIR/bt_agent.py"
fi

# 7) Systemd services (all run as CURRENT_USER, with file logging)
info "Creating systemd services..."

# Web UI (uses main_app.py)
sudo tee /etc/systemd/system/sonixscape-main.service >/dev/null <<EOF
[Unit]
Description=SoniXscape Web UI
After=network-online.target

[Service]
WorkingDirectory=$SONIX_DIR
ExecStart=$SONIX_DIR/venv/bin/python3 $SONIX_DIR/main_app.py
Restart=always
User=$CURRENT_USER
Environment=PYTHONUNBUFFERED=1
StandardOutput=append:/var/log/sonixscape/main.log
StandardError=append:/var/log/sonixscape/main.log

[Install]
WantedBy=multi-user.target
EOF

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

# BlueALSA A2DP Sink (accept any paired device)
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

# Health Check (self-healing)
sudo tee /etc/systemd/system/sonixscape-health.service >/dev/null <<UNIT
[Unit]
Description=SoniXscape Health Check
After=multi-user.target

[Service]
Type=oneshot
ExecStart=$SONIX_DIR/health_check.sh
UNIT

sudo tee /etc/systemd/system/sonixscape-health.timer >/dev/null <<UNIT
[Unit]
Description=Run SoniXscape Health Check at boot and every 5 minutes

[Timer]
OnBootSec=30
OnUnitActiveSec=5min
Unit=sonixscape-health.service

[Install]
WantedBy=multi-user.target
UNIT

# 8) Health-check script (auto-restarts failed services)
info "Writing health-check script..."
cat > "$SONIX_DIR/health_check.sh" <<'EOS'
#!/bin/bash
LOG_FILE="/var/log/sonixscape/health.log"
{
  echo "=== SoniXscape Health Check: $(date) ==="
  for svc in sonixscape-main sonixscape-audio sonixscape-bt-agent sonixscape-bluealsa sonixscape-ip-assign; do
    if systemctl is-active --quiet "$svc"; then
      echo "[OK] $svc is running"
    else
      echo "[FAIL] $svc is NOT running → restarting..."
      systemctl status "$svc" --no-pager -l | head -20
      systemctl restart "$svc"
      sleep 2
      if systemctl is-active --quiet "$svc"; then
        echo "[RECOVERED] $svc restarted successfully"
      else
        echo "[ERROR] $svc restart failed"
      fi
    fi
  done
  echo ""
} >> "$LOG_FILE" 2>&1
EOS
chmod +x "$SONIX_DIR/health_check.sh"

# 9) Comitup tweaks: AP name/password + static IP in AP mode
info "Configuring Comitup (AP SSID + password)..."
sudo tee /etc/comitup.conf >/dev/null <<EOF
ap_name: SoniXscape
ap_password: sonixscape123
EOF

info "Creating AP-mode static IP service..."
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

# 10) Hostname setup
info "Setting hostname to SoniXscape and updating /etc/hosts..."
echo "SoniXscape" | sudo tee /etc/hostname >/dev/null
sudo hostnamectl set-hostname SoniXscape
if ! grep -q "SoniXscape" /etc/hosts; then
  sudo sed -i 's/^127.0.1.1.*/127.0.1.1   SoniXscape/' /etc/hosts || echo "127.0.1.1   SoniXscape" | sudo tee -a /etc/hosts
fi

# 11) Enable all services & timer
info "Enabling services..."
sudo systemctl daemon-reload
sudo systemctl enable sonixscape-main.service
sudo systemctl enable sonixscape-audio.service
sudo systemctl enable sonixscape-bt-agent.service
sudo systemctl enable sonixscape-bluealsa.service
sudo systemctl enable sonixscape-ip-assign.service
sudo systemctl enable sonixscape-health.timer
# (Timer will start at next boot; also safe to start it now)
sudo systemctl start sonixscape-health.timer || true

# 12) Final note & reboot
info "Installation complete! AP SSID will be 'SoniXscape' (password: 'sonixscape123') when in AP mode."
info "Rebooting in 5 seconds..."
sleep 5
sudo reboot
