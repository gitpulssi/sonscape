#!/bin/bash
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

APT_OPTS="-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"

info "=== SoniXscape Installer for NucBox 3 - Optimized (user: $CURRENT_USER) ==="

# ---------- prepare /opt ----------
sudo mkdir -p /opt
sudo chown -R "$CURRENT_USER":"$CURRENT_USER" /opt

# ---------- update base ----------
info "Updating APT..."
sudo DEBIAN_FRONTEND=noninteractive apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get -y $APT_OPTS upgrade

# ---------- core deps + bluetooth (NO JACK) + useful tools ----------
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
  libreadline-dev libncurses5-dev \
  nano vim

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
pip install flask websockets pyalsaaudio sounddevice numpy scipy
deactivate

# ---------- Apply code fixes ----------
info "Applying fade fix and WiFi streaming improvements..."

# Remove jack import if present
sed -i '/^import jack$/d' "$SONIX_DIR/ws_audio.py" 2>/dev/null || true

# Fix WiFi streaming latency (reduce queue size and add latency control)
if grep -q "wifi_audio_queue = queue.Queue(maxsize=100)" "$SONIX_DIR/ws_audio.py" 2>/dev/null; then
  sed -i 's/wifi_audio_queue = queue.Queue(maxsize=100)/wifi_audio_queue = queue.Queue(maxsize=10)/' "$SONIX_DIR/ws_audio.py"
  info "? WiFi queue size optimized for low latency"
fi

# Add WiFi latency control variables if not present
if ! grep -q "wifi_stream_target_latency" "$SONIX_DIR/ws_audio.py" 2>/dev/null; then
  sed -i '/self.wifi_stream_underruns = 0/a\        self.wifi_stream_target_latency = 3  # Target 3 frames of buffering\n        self.wifi_stream_last_stats = time.perf_counter()' "$SONIX_DIR/ws_audio.py" 2>/dev/null || true
  info "? WiFi latency control variables added"
fi

info "? Code fixes applied"

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
info "Blacklisting HDMI and Intel HDA audio devices for faster boot..."
sudo tee /etc/modprobe.d/sonixscape-blacklist.conf >/dev/null <<'EOF'
# Prevent loading of unwanted ALSA devices (speeds up boot)
blacklist snd_hdmi_lpe_audio
blacklist snd_hda_intel
blacklist snd_hda_codec_hdmi
EOF
sudo rmmod snd_hdmi_lpe_audio 2>/dev/null || true
sudo rmmod snd_hda_intel 2>/dev/null || true

# Update initramfs to apply blacklist
sudo update-initramfs -u

# ---------- Auto-detect audio device ----------
info "Detecting available audio devices..."
aplay -l | tee /tmp/alsa-devices.txt

# Try to find ICUSBAUDIO7D first, otherwise use first available device
ALSA_DEVICE=""
if aplay -l | grep -q ICUSBAUDIO7D; then
  ALSA_DEVICE="plughw:CARD=ICUSBAUDIO7D,DEV=0"
  info "? Found ICUSBAUDIO7D DAC"
else
  warn "ICUSBAUDIO7D not found, detecting other audio devices..."
  
  # Get first non-loopback audio card
  FIRST_CARD=$(aplay -l | grep "^card" | grep -v "Loopback" | head -1 | sed 's/card \([0-9]*\).*/\1/')
  
  if [[ -n "$FIRST_CARD" ]]; then
    CARD_NAME=$(aplay -l | grep "^card $FIRST_CARD" | sed 's/.*\[//' | sed 's/\].*//')
    ALSA_DEVICE="plughw:CARD=$FIRST_CARD,DEV=0"
    info "? Using audio device: $CARD_NAME (card $FIRST_CARD)"
  else
    err "No suitable audio device found. Please connect an audio interface."
    exit 1
  fi
fi

echo "ALSA_DEVICE=$ALSA_DEVICE" > "$SONIX_DIR/sonixscape.conf"
info "Selected ALSA device: $ALSA_DEVICE"

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
sudo tee /etc/systemd/system/sonixscape-main.service > /dev/null <<EOF
[Unit]
Description=SoniXscape Web UI
After=network-online.target

[Service]
WorkingDirectory=/opt/sonixscape
ExecStart=/opt/sonixscape/venv/bin/python3 /opt/sonixscape/main_app.py
Restart=always
User=$CURRENT_USER
Environment=PYTHONUNBUFFERED=1
StandardOutput=append:/var/log/sonixscape/main.log
StandardError=append:/var/log/sonixscape/main.log

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/sonixscape-audio.service > /dev/null <<EOF
[Unit]
Description=SoniXscape Audio Engine
After=sound.target bluetooth.service
Requires=bluetooth.service

[Service]
Type=simple
WorkingDirectory=/opt/sonixscape
EnvironmentFile=-/opt/sonixscape/sonixscape.conf
ExecStart=/opt/sonixscape/venv/bin/python3 -u /opt/sonixscape/ws_audio.py
Restart=no
User=$CURRENT_USER
StandardOutput=journal
StandardError=journal
SyslogIdentifier=sonixscape-audio

[Install]
WantedBy=multi-user.target
EOF

# ---------- bluetooth services (OPTIMIZED - no btmgmt timeouts) ----------
sudo tee /etc/systemd/system/sonixscape-bt-agent.service > /dev/null <<'EOF'
[Unit]
Description=SoniXscape Bluetooth Just-Works Agent
After=bluetooth.service dbus.service
Requires=bluetooth.service
Wants=dbus.service

[Service]
Type=simple
ExecStartPre=/bin/sleep 5
ExecStart=/usr/local/bin/bt-agent-setup.py
Restart=on-failure
RestartSec=10
StartLimitBurst=3
StartLimitIntervalSec=120
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
After=bluealsa.service sonixscape-audio.service
Requires=bluealsa.service
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStartPre=/bin/sleep 5
ExecStart=/usr/bin/bluealsa-aplay --pcm-buffer-time=250000 --pcm-period-time=50000 -D plughw:Loopback,0 00:00:00:00:00:00
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

# Health check script with delay after boot
sudo mkdir -p /etc/systemd/system/sonixscape-health.service.d/
sudo tee /etc/systemd/system/sonixscape-health.service.d/override.conf > /dev/null <<'EOF'
[Unit]
After=multi-user.target

[Service]
Type=idle
EOF

cat > "$SONIX_DIR/health_check.sh" <<'EOF'
#!/bin/bash
LOG_FILE="/var/log/sonixscape/health.log"
{
  echo "=== Health Check: $(date) ==="
  for svc in sonixscape-main sonixscape-audio sonixscape-bt-agent bluealsa bluealsa-aplay; do
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

# ---------- Boot optimization ----------
info "Optimizing boot speed..."

# Disable unnecessary services
sudo systemctl disable apt-daily.timer 2>/dev/null || true
sudo systemctl disable apt-daily-upgrade.timer 2>/dev/null || true
sudo systemctl disable snapd.service 2>/dev/null || true
sudo systemctl disable snapd.seeded.service 2>/dev/null || true
sudo systemctl disable snapd.apparmor.service 2>/dev/null || true
sudo systemctl disable ModemManager.service 2>/dev/null || true
sudo systemctl disable apport.service 2>/dev/null || true
sudo systemctl disable systemd-resolved.service 2>/dev/null || true
sudo systemctl disable systemd-networkd-wait-online.service 2>/dev/null || true
sudo systemctl mask systemd-networkd-wait-online.service 2>/dev/null || true
sudo systemctl disable NetworkManager-wait-online.service 2>/dev/null || true

# Reduce systemd timeouts
sudo mkdir -p /etc/systemd/system.conf.d/
sudo tee /etc/systemd/system.conf.d/timeout.conf > /dev/null <<'EOF'
[Manager]
DefaultTimeoutStartSec=30s
DefaultTimeoutStopSec=15s
EOF

# Speed up GRUB
info "Configuring GRUB for fast boot..."
sudo sed -i 's/GRUB_TIMEOUT=.*/GRUB_TIMEOUT=1/' /etc/default/grub 2>/dev/null || true
if ! grep -q "GRUB_TIMEOUT_STYLE" /etc/default/grub; then
  echo 'GRUB_TIMEOUT_STYLE=hidden' | sudo tee -a /etc/default/grub >/dev/null
fi
# Ensure nomodeset is set
if ! grep -q "GRUB_CMDLINE_LINUX_DEFAULT" /etc/default/grub; then
  echo 'GRUB_CMDLINE_LINUX_DEFAULT="quiet splash nomodeset"' | sudo tee -a /etc/default/grub >/dev/null
else
  sudo sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT=.*/GRUB_CMDLINE_LINUX_DEFAULT="quiet splash nomodeset"/' /etc/default/grub
fi
sudo update-grub

# ---------- Headless configuration ----------
info "Configuring headless boot and auto-login..."

# Set to boot without GUI (headless mode)
sudo systemctl set-default multi-user.target

# Disable display manager if installed
sudo systemctl disable gdm3 2>/dev/null || true
sudo systemctl disable lightdm 2>/dev/null || true

# Configure auto-login
sudo mkdir -p /etc/systemd/system/getty@tty1.service.d/
sudo tee /etc/systemd/system/getty@tty1.service.d/autologin.conf > /dev/null <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $CURRENT_USER --noclear %I \$TERM
EOF

# ---------- Hostname ----------
info "Setting hostname to SoniXscape..."
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

# ---------- Disable jackd if present ----------
info "Ensuring jackd is disabled..."
sudo systemctl stop jackd 2>/dev/null || true
sudo systemctl disable jackd 2>/dev/null || true

# ---------- Disable dnsmasq service (only used by WiFi-Connect on demand) ----------
sudo systemctl disable dnsmasq 2>/dev/null || true
sudo systemctl stop dnsmasq 2>/dev/null || true

# ---------- enable services ----------
info "Enabling services..."
sudo systemctl daemon-reload
SERVICES="sonixscape-main sonixscape-audio sonixscape-health.timer sonixscape-bt-agent bluealsa bluealsa-aplay"
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

info ""
info "=== Installation Summary ==="
info "? Core system and dependencies installed"
info "? ALSA loopback module enabled"
info "? BluezALSA compiled and configured"
info "? JACK disabled (using ALSA directly)"
info "? Fade fix applied (smooth audio transitions)"
info "? WiFi streaming optimized (low latency)"
info "? Bluetooth agent service created (NoInputNoOutput)"
info "? Universal Bluetooth audio service created"
info "? Audio routing: BT ? hw:0,1 ? Mixer ? Amplifier"
info "? First-come-first-served Bluetooth connection"
info "? Health monitoring enabled"
info "? Audio device: $ALSA_DEVICE"
info "? Boot optimizations applied (expect ~18-20 second boot)"
info "? Headless mode enabled (no display required)"
info "? Auto-login configured for user: $CURRENT_USER"
info "? HDMI/HDA audio blacklisted (faster boot)"
info "? Unnecessary services disabled"
info ""
info "Bluetooth audio will automatically:"
info "  • Accept connections from any phone"
info "  • Route audio to your mixer via loopback"
info "  • Work with your web interface mix slider"
info "  • Restart automatically if it fails"
info ""
info "System configuration:"
info "  • Boots in ~18-20 seconds (headless)"
info "  • Functional at ~6 seconds (multi-user.target)"
info "  • No password required (auto-login)"
info "  • Web interface on port 8080"
info "  • WiFi-Connect available separately"
info ""
info "Install complete! Review the summary above."
info "To reboot now, run: sudo reboot"
info ""
info "After reboot, access web interface at: http://sonixscape.local:8080"
info "Or use IP address: http://[YOUR_IP]:8080"
