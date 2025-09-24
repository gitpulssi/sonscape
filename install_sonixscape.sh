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

# ---------- detect ALSA ----------
CARD=$(aplay -l | awk '/^card [0-9]+:/{print $2; exit}' | tr -d ':')
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

# ---------- bt_agent.py ----------
cat > "$SONIX_DIR/bt_agent.py" <<'PY'
#!/usr/bin/env python3
import dbus, dbus.mainloop.glib, dbus.service
from gi.repository import GLib
AGENT_PATH = "/test/agent"
class Agent(dbus.service.Object):
    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="") 
    def Release(self): pass
    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="") 
    def RequestPinCode(self, device): return "0000"
    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u") 
    def RequestPasskey(self, device): return dbus.UInt32(0)
    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="") 
    def RequestConfirmation(self, device, passkey): return
    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="") 
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
    GLib.MainLoop().run()
if __name__ == "__main__":
    main()
PY
chmod +x "$SONIX_DIR/bt_agent.py"
