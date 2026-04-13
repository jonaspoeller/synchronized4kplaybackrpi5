#!/bin/bash
# ============================================================================
# Automated Setup for Synchronized Video Playback on Raspberry Pi 5
# ============================================================================
set -e

INSTALL_DIR="/opt/video-sync"
SERVICE_NAME="video-sync"
MOUNT_POINT="/mnt/usb"

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ============================================================================
info "=== Synchronized Video Playback Installer (Pi 5) ==="

if [ "$EUID" -ne 0 ]; then
    error "This script must be run as root (sudo)."
    exit 1
fi

ARCH=$(uname -m)
if [ "$ARCH" != "aarch64" ]; then
    error "This script requires a 64-bit ARM system (aarch64). Detected: $ARCH"
    exit 1
fi

info "System check passed (root, aarch64)."

# ============================================================================
# --- Interactive User Input ---
# ============================================================================
echo ""
echo "================================================="
echo " Interactive Setup for Video Sync System (Pi 5)"
echo "================================================="
echo ""
echo "--- Network Configuration ---"
read -p "Enter the static IP address and subnet for this device (e.g., 192.168.1.10/24): " device_ip_cidr
read -p "Enter the sync port for this group (e.g., 5555): " sync_port

# --- Robust Broadcast-IP-Calculation ---
IP=$(echo $device_ip_cidr | cut -d/ -f1)
CIDR=$(echo $device_ip_cidr | cut -d/ -f2)
if ! [[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || ! [[ "$CIDR" =~ ^[0-9]+$ ]] || [ "$CIDR" -gt 32 ]; then
    error "Invalid IP address or CIDR format."
    exit 1
fi
IFS=. read -r i1 i2 i3 i4 <<< "$IP"
ip_int=$(( (i1 << 24) + (i2 << 16) + (i3 << 8) + i4 ))
mask_int=$(( 0xFFFFFFFF << (32 - CIDR) ))
bcast_int=$(( (ip_int & mask_int) | ~mask_int & 0xFFFFFFFF ))
BROADCAST_IP="$(( (bcast_int >> 24) & 0xFF )).$(( (bcast_int >> 16) & 0xFF )).$(( (bcast_int >> 8) & 0xFF )).$(( bcast_int & 0xFF ))"
info "Calculated Broadcast IP: $BROADCAST_IP"

echo ""
echo "--- Role Configuration ---"
echo "1) Master Node (controls playback)"
echo "2) Slave Node (controlled by Master)"
read -p "Select role [1-2]: " node_type

if [ "$node_type" == "2" ]; then
    read -p "Enter the Master's IP address: " master_ip
else
    node_type="1"
    master_ip=$IP
fi
echo ""

# ============================================================================
info "Installing dependencies..."
apt-get update -y
apt-get install -y vlc ffmpeg python3 ntfs-3g exfatprogs

info "Dependencies installed."

# ============================================================================
info "Configuring user permissions..."

TARGET_USER=$(id -un 1000 2>/dev/null || echo "pi")
info "Target user: ${TARGET_USER}"

for group in render video audio input; do
    usermod -aG $group "$TARGET_USER" 2>/dev/null || true
done

loginctl enable-linger "$TARGET_USER" 2>/dev/null || true

info "User permissions configured."

# ============================================================================
info "Configuring silent boot..."

systemctl set-default multi-user.target
systemctl disable getty@tty1.service 2>/dev/null || true

CMDLINE_FILE="/boot/firmware/cmdline.txt"
if [ -f "$CMDLINE_FILE" ]; then
    sed -i 's/ quiet//g' "$CMDLINE_FILE"
    sed -i 's/ loglevel=[0-9]*//g' "$CMDLINE_FILE"
    sed -i 's/ consoleblank=[0-9]*//g' "$CMDLINE_FILE"
    sed -i 's/ splash//g' "$CMDLINE_FILE"
    sed -i 's/ vt.global_cursor_default=[0-9]*//g' "$CMDLINE_FILE"
    sed -i 's/ logo.nologo//g' "$CMDLINE_FILE"
    sed -i 's/console=tty1/console=tty3/g' "$CMDLINE_FILE"
    sed -i '1 s/$/ quiet loglevel=0 vt.global_cursor_default=0 logo.nologo consoleblank=0/' "$CMDLINE_FILE"
    # Ensure trailing newline (some tools break without it)
    sed -i -e '$a\' "$CMDLINE_FILE"
    info "Kernel parameters configured."
else
    warn "$CMDLINE_FILE not found, skipping."
fi

# ============================================================================
info "Hardening system for unattended operation..."

# --- Limit journal size to prevent SD card fill-up ---
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/video-sync.conf << 'EOF'
[Journal]
SystemMaxUse=50M
SystemMaxFileSize=10M
MaxRetentionSec=7day
EOF
systemctl restart systemd-journald 2>/dev/null || true
info "Journal limited to 50MB."

# --- Disable automatic apt updates (prevent mid-playback interruptions) ---
systemctl disable --now apt-daily.timer 2>/dev/null || true
systemctl disable --now apt-daily-upgrade.timer 2>/dev/null || true
systemctl mask apt-daily.service apt-daily-upgrade.service 2>/dev/null || true
info "Automatic apt updates disabled."

# --- Protect /tmp/video-sync from tmpfiles-clean (runs after 10d) ---
cat > /etc/tmpfiles.d/video-sync.conf << 'EOF'
x /tmp/video-sync
x /tmp/video-sync/*
EOF
info "RAM copy protected from tmpfiles-clean."

# --- Hardware watchdog: auto-reboot on kernel panic / hang ---
cat > /etc/sysctl.d/99-video-sync-watchdog.conf << 'EOF'
kernel.panic = 10
kernel.panic_on_oops = 1
EOF
sysctl -p /etc/sysctl.d/99-video-sync-watchdog.conf 2>/dev/null || true

# Enable Pi 5 hardware watchdog via systemd
if [ ! -f /etc/systemd/system.conf.d/watchdog.conf ]; then
    mkdir -p /etc/systemd/system.conf.d
    cat > /etc/systemd/system.conf.d/watchdog.conf << 'EOF'
[Manager]
RuntimeWatchdog=15s
RebootWatchdog=2min
EOF
fi
info "Hardware watchdog enabled (auto-reboot on hang/panic)."

# --- Disable unnecessary services ---
for svc in bluetooth.service ModemManager.service avahi-daemon.service; do
    systemctl disable --now "$svc" 2>/dev/null || true
done

# Serial getty on debug UART — not needed
systemctl disable --now serial-getty@ttyAMA10.service 2>/dev/null || true

# wpa_supplicant + NetworkManager + ssh STAY ENABLED (WiFi management)
info "Unnecessary services disabled (bluetooth, ModemManager, avahi, serial-getty)."

# --- Disable unnecessary timers ---
for tmr in fstrim.timer e2scrub_all.timer man-db.timer dpkg-db-backup.timer; do
    systemctl disable --now "$tmr" 2>/dev/null || true
done
info "Unnecessary timers disabled."

info "System hardening complete."

# ============================================================================
info "Configuring GPU settings..."

CONFIG_FILE="/boot/firmware/config.txt"
if [ -f "$CONFIG_FILE" ]; then
    sed -i '/# --- Video Sync Setup ---/,/# --- End Video Sync Setup ---/d' "$CONFIG_FILE"
    sed -i '/^dtoverlay=rpivid-v4l2/d' "$CONFIG_FILE"
    sed -i '/^hdmi_group=/d' "$CONFIG_FILE"
    sed -i '/^hdmi_mode=/d' "$CONFIG_FILE"

    if grep -q '^dtoverlay=vc4-kms-v3d$' "$CONFIG_FILE"; then
        sed -i 's/^dtoverlay=vc4-kms-v3d$/dtoverlay=vc4-kms-v3d,cma-512/' "$CONFIG_FILE"
    elif ! grep -q 'cma-512' "$CONFIG_FILE"; then
        sed -i 's/^dtoverlay=vc4-kms-v3d.*/dtoverlay=vc4-kms-v3d,cma-512/' "$CONFIG_FILE"
    fi

    # Remove trailing blank lines before appending (prevents accumulation on re-runs)
    sed -i -e :a -e '/^\s*$/{ $d; N; ba; }' "$CONFIG_FILE"

    cat >> "$CONFIG_FILE" << 'EOF'

# --- Video Sync Setup ---
disable_overscan=1
# --- End Video Sync Setup ---
EOF

    info "GPU settings configured."
else
    warn "$CONFIG_FILE not found, skipping."
fi

# ============================================================================
info "Installing application to ${INSTALL_DIR}..."

# Stop running service first (safe for fresh install too)
systemctl stop ${SERVICE_NAME}.service 2>/dev/null || true
pkill -f "cvlc.*drm_vout" 2>/dev/null || true

mkdir -p "$INSTALL_DIR"
mkdir -p "$MOUNT_POINT"

ffmpeg -f lavfi -i "color=c=black:s=1920x1080:d=1" -vframes 1 \
    "${INSTALL_DIR}/black.png" -y >/dev/null 2>&1

# --- Write Python scripts inline ---
if [ "$node_type" == "1" ]; then
    # --- MASTER SETUP ---
    info "Creating Master configuration for port $sync_port..."
    cat > "${INSTALL_DIR}/sync_config.ini" << EOF
[network]
master_ip = $master_ip
broadcast_ip = $BROADCAST_IP
sync_port = $sync_port
EOF

    info "Installing Master script..."
    cat > "${INSTALL_DIR}/video_sync_master.py" << 'MASTER_SCRIPT'
#!/usr/bin/env python3
"""
Synchronized Video Playback - Master Node (Raspberry Pi 5)
Flow: USB → SD → RAM → Synchronized Playback

  1. If USB present: copy video from USB to SD, then eject USB
  2. Copy video from SD to RAM (tmpfs)
  3. Broadcast sync commands to slaves
  4. Play from RAM in a seamless loop (cvlc --input-repeat)
  5. New USB inserted → udev restarts service → video updated
"""

import subprocess
import signal
import socket
import sys
import os
import json
import logging
import shutil
import time
import glob
import hashlib
import configparser

# --- Configuration ---
MOUNT_POINT = "/mnt/usb"
VIDEO_FILENAME = "loop.mp4"
INSTALL_DIR = "/opt/video-sync"
SD_VIDEO_DIR = os.path.join(INSTALL_DIR, "video")
SD_VIDEO_PATH = os.path.join(SD_VIDEO_DIR, VIDEO_FILENAME)
RAM_COPY_DIR = "/tmp/video-sync"
RAM_VIDEO_PATH = os.path.join(RAM_COPY_DIR, VIDEO_FILENAME)
BACKGROUND_IMG = os.path.join(INSTALL_DIR, "background.png")
BLACK_IMG = os.path.join(INSTALL_DIR, "black.png")
CONFIG_FILE = os.path.join(INSTALL_DIR, "sync_config.ini")


def detect_hdmi_port():
    """Auto-detect which HDMI port has a connected display.
    Reads /sys/class/drm/card*-HDMI-A-*/status. Returns 'HDMI-1' or 'HDMI-2'.
    Falls back to 'HDMI-1' if detection fails.
    """
    try:
        for status_file in sorted(glob.glob("/sys/class/drm/card*-HDMI-A-*/status")):
            with open(status_file) as f:
                if f.read().strip() == "connected":
                    port = status_file.split("HDMI-A-")[1].split("/")[0]
                    return f"HDMI-{port}"
    except Exception:
        pass
    return "HDMI-1"


HDMI_PORT = detect_hdmi_port()

VLC_ARGS = [
    "cvlc",
    "--no-xlib",
    "--quiet",
    "--fullscreen",
    "--no-video-title-show",
    "--no-osd",
    "--codec=drm_avcodec",
    "--vout=drm_vout",
    f"--drm-vout-display={HDMI_PORT}",
    "--drm-vout-pool-dmabuf",
    "--no-audio",
    "--file-caching=2000",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("video-sync-master")

vlc_process = None
sock = None
running = True


def shutdown(sig, frame):
    global running
    log.info(f"Signal {sig}, shutting down...")
    running = False
    if vlc_process and vlc_process.poll() is None:
        vlc_process.terminate()
        try:
            vlc_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            vlc_process.kill()
    if sock:
        try:
            sock.close()
        except Exception:
            pass
    sys.exit(0)


def file_checksum(path, chunk_size=1024 * 1024):
    """Fast partial MD5: first+last 1MB. Good enough to detect changed videos."""
    h = hashlib.md5()
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            h.update(f.read(chunk_size))
            if size > chunk_size * 2:
                f.seek(-chunk_size, 2)
                h.update(f.read(chunk_size))
        h.update(str(size).encode())
    except Exception:
        return None
    return h.hexdigest()


def find_usb_partition():
    """Find the first mountable partition (or raw disk) on a USB storage device."""
    try:
        result = subprocess.run(
            ["lsblk", "-o", "PATH,FSTYPE,TRAN,TYPE", "-J", "-T"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        for dev in data.get("blockdevices", []):
            if dev.get("tran") != "usb" or dev.get("type") != "disk":
                continue
            for child in dev.get("children", []):
                if child.get("fstype"):
                    return child["path"]
            if dev.get("fstype"):
                return dev["path"]
    except Exception as e:
        log.error(f"USB scan error: {e}")
    return None


def mount_usb(partition):
    os.makedirs(MOUNT_POINT, exist_ok=True)
    try:
        r = subprocess.run(["findmnt", "-n", "-o", "SOURCE", MOUNT_POINT],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip() == partition:
            return True
        if r.returncode == 0 and r.stdout.strip():
            subprocess.run(["sudo", "umount", MOUNT_POINT], timeout=10)
    except Exception:
        pass
    try:
        r = subprocess.run(["sudo", "mount", "-o", "ro", partition, MOUNT_POINT],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            log.info(f"Mounted {partition}")
            return True
        log.error(f"Mount failed: {r.stderr.strip()}")
    except Exception as e:
        log.error(f"Mount error: {e}")
    return False


def unmount_usb():
    try:
        subprocess.run(["sudo", "umount", MOUNT_POINT],
                       capture_output=True, timeout=10)
        log.info("USB unmounted — stick can be removed")
    except Exception:
        pass


def import_from_usb():
    """Check USB for video, copy to SD if new/changed, then eject USB.
    Returns True if a new video was imported.
    """
    partition = find_usb_partition()
    if not partition:
        return False

    if not mount_usb(partition):
        return False

    usb_video = os.path.join(MOUNT_POINT, VIDEO_FILENAME)
    if not os.path.isfile(usb_video):
        log.info(f"No {VIDEO_FILENAME} on USB — ignoring stick")
        unmount_usb()
        return False

    # Compare checksums — skip copy if identical
    usb_hash = file_checksum(usb_video)
    sd_hash = file_checksum(SD_VIDEO_PATH) if os.path.isfile(SD_VIDEO_PATH) else None

    if usb_hash and usb_hash == sd_hash:
        log.info("Video on USB identical to SD — skipping import")
        unmount_usb()
        return False

    # Copy USB → SD (delete old first to avoid two copies filling the disk)
    os.makedirs(SD_VIDEO_DIR, exist_ok=True)
    usb_size = os.path.getsize(usb_video)
    MIN_FREE_AFTER_COPY = 2 * 1024 * 1024 * 1024  # 2 GB

    old_existed = os.path.isfile(SD_VIDEO_PATH)
    if old_existed:
        try:
            os.remove(SD_VIDEO_PATH)
            log.info("Old video on SD removed")
        except Exception as e:
            log.error(f"Failed to remove old video: {e}")
            unmount_usb()
            return False

    stat = os.statvfs(SD_VIDEO_DIR)
    free = stat.f_bavail * stat.f_frsize
    if usb_size > free - MIN_FREE_AFTER_COPY:
        log.error(f"Not enough space on SD: {free//(1024*1024)}MB free, need {usb_size//(1024*1024)}MB + 2GB reserve")
        unmount_usb()
        return False

    log.info(f"Importing video from USB to SD ({usb_size // (1024*1024)}MB)...")
    tmp_path = SD_VIDEO_PATH + ".tmp"
    try:
        shutil.copy2(usb_video, tmp_path)
        os.replace(tmp_path, SD_VIDEO_PATH)
        log.info("Video imported to SD successfully")
    except Exception as e:
        log.error(f"USB→SD copy failed: {e}")
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
        unmount_usb()
        return False

    unmount_usb()
    return True


def copy_to_ram():
    """Copy video from SD to RAM (tmpfs). Returns path or None."""
    os.makedirs(RAM_COPY_DIR, exist_ok=True)
    try:
        src_size = os.path.getsize(SD_VIDEO_PATH)
        stat = os.statvfs("/tmp")
        free = stat.f_bavail * stat.f_frsize
        if src_size > free - 512 * 1024 * 1024:
            log.warning("Video too large for RAM, playing from SD")
            return None
        log.info(f"Copying video to RAM ({src_size // (1024*1024)}MB)...")
        shutil.copy2(SD_VIDEO_PATH, RAM_VIDEO_PATH)
        return RAM_VIDEO_PATH
    except Exception as e:
        log.error(f"RAM copy failed: {e}")
        return None


def extract_background(video_path):
    try:
        os.remove(BACKGROUND_IMG)
    except FileNotFoundError:
        pass
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-vframes", "1", "-update", "1", "-q:v", "2", BACKGROUND_IMG],
            capture_output=True, timeout=30,
        )
    except Exception:
        pass


def show_standby():
    img = BACKGROUND_IMG if os.path.exists(BACKGROUND_IMG) else BLACK_IMG
    if not os.path.exists(img):
        return None
    log.info(f"Showing standby: {img}")
    try:
        return subprocess.Popen(
            VLC_ARGS + ["--image-duration=-1", img],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except Exception:
        return None


def send_broadcast(sock, broadcast_ip, sync_port, message):
    try:
        sock.sendto(json.dumps(message).encode("utf-8"), (broadcast_ip, sync_port))
    except Exception as e:
        log.error(f"Broadcast error: {e}")


def main():
    global vlc_process, sock, running

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # --- Load config ---
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)
    broadcast_ip = config.get("network", "broadcast_ip")
    sync_port = config.getint("network", "sync_port")
    master_ip = config.get("network", "master_ip")

    sequence_id = int(time.time())

    log.info(f"=== Video Sync Master started === (HDMI: {HDMI_PORT}, Seq: {sequence_id})")

    # --- Setup UDP socket ---
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    base_msg = {"master_ip": master_ip, "sequence_id": sequence_id}

    # Remove stale background
    try:
        os.remove(BACKGROUND_IMG)
    except FileNotFoundError:
        pass

    # --- Step 1: Import from USB if present (USB → SD), then eject ---
    imported = import_from_usb()
    if imported:
        shutil.rmtree(RAM_COPY_DIR, ignore_errors=True)

    # --- Step 2: Check if we have a video on SD ---
    if not os.path.isfile(SD_VIDEO_PATH):
        log.info("No video on SD — showing standby (insert USB with loop.mp4)")
        send_broadcast(sock, broadcast_ip, sync_port, {**base_msg, "command": "standby"})
        bg = show_standby()
        if bg:
            bg.wait()
        time.sleep(5)
        return

    # --- Step 3: Copy SD → RAM ---
    ram_video = copy_to_ram()
    playback_path = ram_video or SD_VIDEO_PATH
    video_hash = file_checksum(playback_path)
    extract_background(playback_path)

    # --- Step 4: Notify slaves to stop and prepare ---
    send_broadcast(sock, broadcast_ip, sync_port, {**base_msg, "command": "stop"})
    time.sleep(0.5)
    send_broadcast(sock, broadcast_ip, sync_port, {
        **base_msg, "command": "prepare", "video_hash": video_hash,
    })

    # Give slaves time to prepare their own USB→SD→RAM pipeline
    time.sleep(2)

    # --- Step 5: Synchronized start ---
    log.info(f"Playing: {playback_path}")
    send_broadcast(sock, broadcast_ip, sync_port, {**base_msg, "command": "play"})

    cmd = VLC_ARGS + ["--input-repeat=65535", playback_path]
    vlc_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    # --- Step 6: Heartbeat loop ---
    try:
        while running:
            if vlc_process.poll() is not None:
                log.info("VLC exited unexpectedly, restarting...")
                break
            send_broadcast(sock, broadcast_ip, sync_port, {**base_msg, "command": "heartbeat"})
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    # --- Cleanup ---
    exit_code = vlc_process.returncode if vlc_process.returncode is not None else -1
    try:
        stderr_out = vlc_process.stderr.read().decode(errors="replace") if vlc_process.stderr else ""
    except Exception:
        stderr_out = ""
    if stderr_out:
        log.info(f"VLC exited ({exit_code}): {stderr_out[:500]}")
    else:
        log.info(f"VLC exited ({exit_code})")

    send_broadcast(sock, broadcast_ip, sync_port, {**base_msg, "command": "stop"})
    shutil.rmtree(RAM_COPY_DIR, ignore_errors=True)
    sock.close()
    log.info("=== Video Sync Master stopped ===")


if __name__ == "__main__":
    main()
MASTER_SCRIPT
    chmod +x "${INSTALL_DIR}/video_sync_master.py"
    PYTHON_EXEC_PATH="${INSTALL_DIR}/video_sync_master.py"

else
    # --- SLAVE SETUP ---
    info "Creating Slave configuration for port $sync_port..."
    cat > "${INSTALL_DIR}/sync_config.ini" << EOF
[network]
master_ip = $master_ip
sync_port = $sync_port
EOF

    info "Installing Slave script..."
    cat > "${INSTALL_DIR}/video_sync_slave.py" << 'SLAVE_SCRIPT'
#!/usr/bin/env python3
"""
Synchronized Video Playback - Slave Node (Raspberry Pi 5)
Flow: USB → SD → RAM → Synchronized Playback

  1. If USB present: copy video from USB to SD, then eject USB
  2. Copy video from SD to RAM (tmpfs)
  3. Wait for master's play command, then start cvlc
  4. Watchdog: revert to black screen if master signal lost
  5. New USB inserted → udev restarts service → video updated
"""

import subprocess
import signal
import socket
import sys
import os
import json
import logging
import shutil
import time
import glob
import hashlib
import configparser
import threading

# --- Configuration ---
MOUNT_POINT = "/mnt/usb"
VIDEO_FILENAME = "loop.mp4"
INSTALL_DIR = "/opt/video-sync"
SD_VIDEO_DIR = os.path.join(INSTALL_DIR, "video")
SD_VIDEO_PATH = os.path.join(SD_VIDEO_DIR, VIDEO_FILENAME)
RAM_COPY_DIR = "/tmp/video-sync"
RAM_VIDEO_PATH = os.path.join(RAM_COPY_DIR, VIDEO_FILENAME)
BACKGROUND_IMG = os.path.join(INSTALL_DIR, "background.png")
BLACK_IMG = os.path.join(INSTALL_DIR, "black.png")
CONFIG_FILE = os.path.join(INSTALL_DIR, "sync_config.ini")


def detect_hdmi_port():
    """Auto-detect which HDMI port has a connected display.
    Reads /sys/class/drm/card*-HDMI-A-*/status. Returns 'HDMI-1' or 'HDMI-2'.
    Falls back to 'HDMI-1' if detection fails.
    """
    try:
        for status_file in sorted(glob.glob("/sys/class/drm/card*-HDMI-A-*/status")):
            with open(status_file) as f:
                if f.read().strip() == "connected":
                    port = status_file.split("HDMI-A-")[1].split("/")[0]
                    return f"HDMI-{port}"
    except Exception:
        pass
    return "HDMI-1"


HDMI_PORT = detect_hdmi_port()

VLC_ARGS = [
    "cvlc",
    "--no-xlib",
    "--quiet",
    "--fullscreen",
    "--no-video-title-show",
    "--no-osd",
    "--codec=drm_avcodec",
    "--vout=drm_vout",
    f"--drm-vout-display={HDMI_PORT}",
    "--drm-vout-pool-dmabuf",
    "--no-audio",
    "--file-caching=2000",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("video-sync-slave")

vlc_process = None
standby_process = None
running = True
last_heartbeat = time.time()
current_sequence_id = None


def shutdown(sig, frame):
    global running
    log.info(f"Signal {sig}, shutting down...")
    running = False
    kill_vlc()
    kill_standby()
    sys.exit(0)


def kill_vlc():
    global vlc_process
    if vlc_process and vlc_process.poll() is None:
        vlc_process.terminate()
        try:
            vlc_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            vlc_process.kill()
    vlc_process = None


def kill_standby():
    global standby_process
    if standby_process and standby_process.poll() is None:
        standby_process.terminate()
        try:
            standby_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            standby_process.kill()
    standby_process = None


def file_checksum(path, chunk_size=1024 * 1024):
    """Fast partial MD5: first+last 1MB. Good enough to detect changed videos."""
    h = hashlib.md5()
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            h.update(f.read(chunk_size))
            if size > chunk_size * 2:
                f.seek(-chunk_size, 2)
                h.update(f.read(chunk_size))
        h.update(str(size).encode())
    except Exception:
        return None
    return h.hexdigest()


def find_usb_partition():
    """Find the first mountable partition (or raw disk) on a USB storage device."""
    try:
        result = subprocess.run(
            ["lsblk", "-o", "PATH,FSTYPE,TRAN,TYPE", "-J", "-T"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        for dev in data.get("blockdevices", []):
            if dev.get("tran") != "usb" or dev.get("type") != "disk":
                continue
            for child in dev.get("children", []):
                if child.get("fstype"):
                    return child["path"]
            if dev.get("fstype"):
                return dev["path"]
    except Exception as e:
        log.error(f"USB scan error: {e}")
    return None


def mount_usb(partition):
    os.makedirs(MOUNT_POINT, exist_ok=True)
    try:
        r = subprocess.run(["findmnt", "-n", "-o", "SOURCE", MOUNT_POINT],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip() == partition:
            return True
        if r.returncode == 0 and r.stdout.strip():
            subprocess.run(["sudo", "umount", MOUNT_POINT], timeout=10)
    except Exception:
        pass
    try:
        r = subprocess.run(["sudo", "mount", "-o", "ro", partition, MOUNT_POINT],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            log.info(f"Mounted {partition}")
            return True
        log.error(f"Mount failed: {r.stderr.strip()}")
    except Exception as e:
        log.error(f"Mount error: {e}")
    return False


def unmount_usb():
    try:
        subprocess.run(["sudo", "umount", MOUNT_POINT],
                       capture_output=True, timeout=10)
        log.info("USB unmounted — stick can be removed")
    except Exception:
        pass


def import_from_usb():
    """Check USB for video, copy to SD if new/changed, then eject USB.
    Returns True if a new video was imported.
    """
    partition = find_usb_partition()
    if not partition:
        return False

    if not mount_usb(partition):
        return False

    usb_video = os.path.join(MOUNT_POINT, VIDEO_FILENAME)
    if not os.path.isfile(usb_video):
        log.info(f"No {VIDEO_FILENAME} on USB — ignoring stick")
        unmount_usb()
        return False

    # Compare checksums — skip copy if identical
    usb_hash = file_checksum(usb_video)
    sd_hash = file_checksum(SD_VIDEO_PATH) if os.path.isfile(SD_VIDEO_PATH) else None

    if usb_hash and usb_hash == sd_hash:
        log.info("Video on USB identical to SD — skipping import")
        unmount_usb()
        return False

    # Copy USB → SD (delete old first to avoid two copies filling the disk)
    os.makedirs(SD_VIDEO_DIR, exist_ok=True)
    usb_size = os.path.getsize(usb_video)
    MIN_FREE_AFTER_COPY = 2 * 1024 * 1024 * 1024  # 2 GB

    old_existed = os.path.isfile(SD_VIDEO_PATH)
    if old_existed:
        try:
            os.remove(SD_VIDEO_PATH)
            log.info("Old video on SD removed")
        except Exception as e:
            log.error(f"Failed to remove old video: {e}")
            unmount_usb()
            return False

    stat = os.statvfs(SD_VIDEO_DIR)
    free = stat.f_bavail * stat.f_frsize
    if usb_size > free - MIN_FREE_AFTER_COPY:
        log.error(f"Not enough space on SD: {free//(1024*1024)}MB free, need {usb_size//(1024*1024)}MB + 2GB reserve")
        unmount_usb()
        return False

    log.info(f"Importing video from USB to SD ({usb_size // (1024*1024)}MB)...")
    tmp_path = SD_VIDEO_PATH + ".tmp"
    try:
        shutil.copy2(usb_video, tmp_path)
        os.replace(tmp_path, SD_VIDEO_PATH)
        log.info("Video imported to SD successfully")
    except Exception as e:
        log.error(f"USB→SD copy failed: {e}")
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
        unmount_usb()
        return False

    unmount_usb()
    return True


def copy_to_ram():
    """Copy video from SD to RAM (tmpfs). Returns path or None."""
    os.makedirs(RAM_COPY_DIR, exist_ok=True)
    try:
        src_size = os.path.getsize(SD_VIDEO_PATH)
        stat = os.statvfs("/tmp")
        free = stat.f_bavail * stat.f_frsize
        if src_size > free - 512 * 1024 * 1024:
            log.warning("Video too large for RAM, playing from SD")
            return None
        log.info(f"Copying video to RAM ({src_size // (1024*1024)}MB)...")
        shutil.copy2(SD_VIDEO_PATH, RAM_VIDEO_PATH)
        return RAM_VIDEO_PATH
    except Exception as e:
        log.error(f"RAM copy failed: {e}")
        return None


def extract_background(video_path):
    try:
        os.remove(BACKGROUND_IMG)
    except FileNotFoundError:
        pass
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-vframes", "1", "-update", "1", "-q:v", "2", BACKGROUND_IMG],
            capture_output=True, timeout=30,
        )
    except Exception:
        pass


def show_standby():
    global standby_process
    kill_standby()
    img = BACKGROUND_IMG if os.path.exists(BACKGROUND_IMG) else BLACK_IMG
    if not os.path.exists(img):
        return
    log.info(f"Showing standby: {img}")
    try:
        standby_process = subprocess.Popen(
            VLC_ARGS + ["--image-duration=-1", img],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except Exception:
        pass


def show_black():
    global standby_process
    kill_standby()
    if not os.path.exists(BLACK_IMG):
        return
    log.info("Showing black screen")
    try:
        standby_process = subprocess.Popen(
            VLC_ARGS + ["--image-duration=-1", BLACK_IMG],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except Exception:
        pass


def start_playback(playback_path):
    global vlc_process
    kill_vlc()
    kill_standby()
    log.info(f"Playing: {playback_path}")
    cmd = VLC_ARGS + ["--input-repeat=65535", playback_path]
    vlc_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def master_watchdog():
    """Background thread: revert to black screen if master heartbeat lost."""
    global last_heartbeat, current_sequence_id
    while running:
        time.sleep(2)
        if time.time() - last_heartbeat > 5:
            log.warning("Master signal lost! Reverting to black screen.")
            kill_vlc()
            show_black()
            last_heartbeat = time.time()
            current_sequence_id = None


def main():
    global vlc_process, running, last_heartbeat, current_sequence_id

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # --- Load config ---
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)
    master_ip = config.get("network", "master_ip")
    sync_port = config.getint("network", "sync_port")

    log.info(f"=== Video Sync Slave started === (HDMI: {HDMI_PORT})")

    # --- Setup UDP socket ---
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", sync_port))
    sock.settimeout(1.0)

    # Remove stale background
    try:
        os.remove(BACKGROUND_IMG)
    except FileNotFoundError:
        pass

    # --- Step 1: Import from USB if present (USB → SD), then eject ---
    imported = import_from_usb()
    if imported:
        shutil.rmtree(RAM_COPY_DIR, ignore_errors=True)

    # --- Step 2: Prepare video (SD → RAM) ---
    playback_path = None
    if os.path.isfile(SD_VIDEO_PATH):
        ram_video = copy_to_ram()
        playback_path = ram_video or SD_VIDEO_PATH
        extract_background(playback_path)

    # --- Show black screen while waiting ---
    show_black()
    log.info("Black screen displayed. Waiting for master commands.")

    # --- Start watchdog ---
    watchdog_thread = threading.Thread(target=master_watchdog, daemon=True)
    watchdog_thread.start()

    # --- Step 3: Listen for master commands ---
    try:
        while running:
            try:
                data, _ = sock.recvfrom(4096)
                message = json.loads(data.decode("utf-8"))
            except socket.timeout:
                continue
            except Exception as e:
                log.error(f"Error receiving command: {e}")
                continue

            # Ignore commands from wrong master
            if message.get("master_ip") != master_ip:
                continue

            # Sequence ID management (detect master restarts)
            incoming_seq = message.get("sequence_id")
            if not incoming_seq:
                continue

            if current_sequence_id is None or incoming_seq > current_sequence_id:
                log.info(f"Following new Master sequence: {incoming_seq}")
                current_sequence_id = incoming_seq
            elif incoming_seq < current_sequence_id:
                continue

            last_heartbeat = time.time()
            command = message.get("command")

            if command == "stop":
                log.info("Master: stop")
                kill_vlc()
                show_black()

            elif command == "standby":
                log.info("Master: standby (no video)")
                kill_vlc()
                show_standby()

            elif command == "prepare":
                log.info("Master: prepare")
                # Ensure we have a video ready
                if not playback_path or not os.path.isfile(playback_path):
                    if os.path.isfile(SD_VIDEO_PATH):
                        ram_video = copy_to_ram()
                        playback_path = ram_video or SD_VIDEO_PATH
                        extract_background(playback_path)
                    else:
                        log.warning("No video available for playback")
                        playback_path = None

            elif command == "play":
                if playback_path and os.path.isfile(playback_path):
                    log.info("Master: play")
                    start_playback(playback_path)
                else:
                    log.warning("Play command received but no video available")

            elif command == "heartbeat":
                pass  # Just updates last_heartbeat above

    except KeyboardInterrupt:
        pass

    # --- Cleanup ---
    kill_vlc()
    kill_standby()
    shutil.rmtree(RAM_COPY_DIR, ignore_errors=True)
    sock.close()
    log.info("=== Video Sync Slave stopped ===")


if __name__ == "__main__":
    main()
SLAVE_SCRIPT
    chmod +x "${INSTALL_DIR}/video_sync_slave.py"
    PYTHON_EXEC_PATH="${INSTALL_DIR}/video_sync_slave.py"
fi

mkdir -p "${INSTALL_DIR}/video"
chown -R "$TARGET_USER":"$TARGET_USER" "${INSTALL_DIR}"

# Unmount USB first (may be mounted from previous install), then chown
umount "${MOUNT_POINT}" 2>/dev/null || true
chown "$TARGET_USER":"$TARGET_USER" "${MOUNT_POINT}"
info "Application installed."

# ============================================================================
info "Configuring udev rules..."

cat > /etc/udev/rules.d/99-video-sync-usb.rules << 'EOF'
ACTION=="add", SUBSYSTEM=="block", ENV{ID_BUS}=="usb", TAG+="systemd", RUN+="/bin/systemctl restart video-sync.service"
EOF

# HDMI hotplug: restart service when display is connected/disconnected
cat > /etc/udev/rules.d/99-video-sync-hdmi.rules << 'EOF'
ACTION=="change", SUBSYSTEM=="drm", RUN+="/bin/systemctl restart video-sync.service"
EOF

udevadm control --reload-rules
info "USB + HDMI hotplug rules configured."

# ============================================================================
info "Creating systemd service..."

cat > /etc/systemd/system/${SERVICE_NAME}.service << EOF
[Unit]
Description=Video Sync Service ($(if [ "$node_type" == "1" ]; then echo "Master"; else echo "Slave"; fi) on Port $sync_port)
After=multi-user.target network-online.target

[Service]
Type=simple
User=${TARGET_USER}
Group=${TARGET_USER}
ExecStartPre=/bin/sleep 3
ExecStart=/usr/bin/python3 ${PYTHON_EXEC_PATH}
Restart=always
RestartSec=3
Environment="XDG_RUNTIME_DIR=/run/user/1000"
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/sudoers.d/video-sync << SUDOEOF
${TARGET_USER} ALL=(root) NOPASSWD: /bin/mount -o ro * /mnt/usb, /bin/umount /mnt/usb
SUDOEOF
chmod 0440 /etc/sudoers.d/video-sync

systemctl daemon-reload
systemctl enable ${SERVICE_NAME}.service

info "Systemd service created and enabled."

# ============================================================================
info "Installing helper scripts..."

cat > /usr/local/bin/video-sync-start << 'EOF'
#!/bin/bash
sudo systemctl start video-sync.service
echo "Video sync started."
EOF

cat > /usr/local/bin/video-sync-stop << 'EOF'
#!/bin/bash
sudo systemctl stop video-sync.service
sudo pkill -f "cvlc.*drm_vout" 2>/dev/null || true
echo "Video sync stopped."
EOF

cat > /usr/local/bin/video-sync-status << 'EOF'
#!/bin/bash
systemctl status video-sync.service
EOF

cat > /usr/local/bin/video-sync-logs << 'EOF'
#!/bin/bash
journalctl -u video-sync.service -f
EOF

cat > /usr/local/bin/video-sync-restart << 'EOF'
#!/bin/bash
sudo systemctl restart video-sync.service
echo "Video sync restarted."
EOF

chmod +x /usr/local/bin/video-sync-start /usr/local/bin/video-sync-stop /usr/local/bin/video-sync-status /usr/local/bin/video-sync-logs /usr/local/bin/video-sync-restart

info "Helper scripts installed."

# ============================================================================
echo ""
info "============================================"
info "  Installation complete!"
info "============================================"
echo ""
info "After reboot: Insert FAT32/exFAT/NTFS USB with 'loop.mp4'"
info "Commands: video-sync-start, video-sync-stop, video-sync-restart, video-sync-status, video-sync-logs"
echo ""
warn "Rebooting in 10 seconds... (Ctrl+C to cancel)"
sleep 10
reboot
