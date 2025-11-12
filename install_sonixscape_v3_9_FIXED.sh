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

info "=== SoniXscape Production Installer v3.9 (Ubuntu 24.04 LTS) ==="
info "FIXES: BlueALSA bridge template, loopback conflict, zombie processes, mixed ALSA bindings"

sudo mkdir -p /opt /var/log/sonixscape
sudo chown -R "$CURRENT_USER":"$CURRENT_USER" /opt /var/log/sonixscape

info "Updating and installing dependencies..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv python3-flask python3-websockets python3-dbus python3-gi \
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
ExecStart=/usr/bin/bluealsa -S -i hci0 -p a2dp-source -p a2dp-sink
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

info "Creating template-based Bluetooth output service..."
sudo tee /etc/systemd/system/sonixscape-output@.service >/dev/null <<'EOF'
[Unit]
Description=SoniXscape Bluetooth Input → Loopback Bridge for %I
After=bluealsa.service sound.target
Requires=bluealsa.service

[Service]
Type=simple
# Clean any old bridge for this MAC before starting (handles crash/restart cases)
ExecStartPre=/bin/bash -c 'pkill -f "bluealsa-aplay .* ${1#BT_}" || true' ignore %I
# Correct arg passing: use $1 (the instance) and strip 'BT_' prefix; also use -d (lowercase)
ExecStart=/bin/bash -c '/usr/bin/bluealsa-aplay --pcm-buffer-time=250000 --pcm-period-time=50000 -d plughw:Loopback,0 "${1#BT_}"' ignore %I
Restart=always
RestartSec=2
KillMode=control-group
SuccessExitStatus=141 SIGPIPE

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now bluealsa.service

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
pip install flask websockets pyalsaaudio sounddevice numpy scipy dbus-python
deactivate

info "Creating /opt/sonixscape/sonixscape.conf"
cat <<CONF | sudo tee /opt/sonixscape/sonixscape.conf >/dev/null
ALSA_DEVICE=plughw:CARD=ICUSBAUDIO7D,DEV=0
BT_DEVICE=50:16:F4:1B:20:9C
CONF

info "Applying CRITICAL FIX: Restoring loopback fallback in ws_audio.py..."
cd "$SONIX_DIR"
cp ws_audio.py ws_audio.py.backup_preinstall

python3 <<'PYFIX'
import sys

try:
    with open('/opt/sonixscape/ws_audio.py', 'r') as f:
        content = f.read()

    old_code = """        # Loopback fallback - REMOVED
        # We now write directly to loopback in the audio callback (_pure_audio_loop)
        # This old code would conflict with the direct writing
        print("[BT] Loopback fallback not needed - using direct loopback writing")
        self.bt_input = None
        self.bt_enabled = False
        return False"""

    new_code = """        # Loopback fallback - read from Loopback,1 (bluealsa-aplay writes to Loopback,0)
        loopback_device = "plughw:Loopback,1"
        print(f"[BT] Attempting to open {loopback_device}")
        try:
            cap = alsaaudio.PCM(
                type=alsaaudio.PCM_CAPTURE,
                mode=alsaaudio.PCM_NONBLOCK,
                device=loopback_device,
                channels=2,
                rate=RATE,
                format=alsaaudio.PCM_FORMAT_S16_LE,
                periodsize=BLOCK
            )
            self.bt_input = cap
            self.bt_mac_current = bt_mac
            self.bt_enabled = True
            # Clear ring buffer
            with self.bt_ring_lock:
                self.bt_ring_write_pos = 0
                self.bt_ring_read_pos = 0
                self.bt_ring_fill = 0
            # Start read thread
            self.bt_read_running = True
            self.bt_read_thread = threading.Thread(target=self._bt_read_loop, daemon=True)
            self.bt_read_thread.start()
            print(f"[BT] Loopback CAPTURE established for {bt_mac}")
            return True
        except Exception as loopback_err:
            print(f"[BT] Loopback CAPTURE failed ({loopback_err})")
            self.bt_input = None
            self.bt_enabled = False
            return False"""

    if old_code in content:
        content = content.replace(old_code, new_code)
        with open('/opt/sonixscape/ws_audio.py', 'w') as f:
            f.write(content)
        print("[FIX APPLIED] Loopback fallback restored in ws_audio.py")
    else:
        print("[INFO] Loopback code not found - may already be patched")
        
except Exception as e:
    print(f"[ERROR] Failed to patch ws_audio.py: {e}")
    sys.exit(1)
PYFIX

if [[ $? -ne 0 ]]; then
  err "Failed to apply ws_audio.py patch!"
  exit 1
fi

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

info "Locking DAC index for stability"
sudo tee /etc/udev/rules.d/99-usb-audio-sonixscape.rules >/dev/null <<'EOF'
SUBSYSTEM=="sound", ATTRS{idVendor}=="0d8c", ATTRS{idProduct}=="0102", KERNEL=="card*", ATTR{index}="1"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger

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

# --- Prevent ALSA device from being busy ---
ExecStartPre=/bin/bash -c "fuser -kv /dev/snd/pcmC1D0p 2>/dev/null || true"
ExecStartPre=/bin/bash -c "pkill -9 aplay 2>/dev/null || true"

ExecStart=$SONIX_DIR/venv/bin/python3 -u ws_audio.py
Restart=always
User=$CURRENT_USER

[Install]
WantedBy=multi-user.target
EOF

# FIXED: Changed from .service to .target for proper orchestration
sudo tee /etc/systemd/system/sonixscape.target >/dev/null <<'EOF'
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
sudo systemctl enable sonixscape-bt-agent.service sonixscape-web.service sonixscape-audio.service sonixscape.target
sudo systemctl restart bluetooth
sudo systemctl start sonixscape-bt-agent.service

info "Auto-pairing chair device..."
bluetoothctl <<BTCTL || true
power on
discoverable on
pairable on
agent NoInputNoOutput
default-agent
scan on
BTCTL

info "Finalizing and enabling all services..."
sudo systemctl enable bluealsa sonixscape-bt-agent sonixscape-web sonixscape-audio sonixscape.target

info "=== Installation Summary ==="
info "✓ BlueALSA v3.0.0 with single-adapter dual-profile support"
info "✓ Fixed BlueALSA bridge template (correct parameter passing & -d flag)"
info "✓ Loopback mirror disabled by default (set SONIX_LOOPBACK_MIRROR=1 to enable)"
info "✓ Process group killing enabled (no more zombie processes)"
info "✓ Single ALSA Python binding (venv only, no system package conflict)"
info "✓ ws_audio.py with loopback fallback preserved"
info "✓ Template-based BT output service (sonixscape-output@.service)"
info "✓ Web interface, audio engine, and BT agent enabled"
info ""
info "To start BT output for device 50:16:F4:1B:20:9C:"
info "  sudo systemctl start sonixscape-output@BT_50:16:F4:1B:20:9C.service"
info ""
info "Installation complete — rebooting in 10 seconds."
sleep 10
sudo reboot
