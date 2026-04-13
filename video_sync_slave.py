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

    # Remove old video first so we never have two on disk at once
    old_existed = os.path.isfile(SD_VIDEO_PATH)
    if old_existed:
        try:
            os.remove(SD_VIDEO_PATH)
            log.info("Old video on SD removed")
        except Exception as e:
            log.error(f"Failed to remove old video: {e}")
            unmount_usb()
            return False

    # Check free space (after deletion, before copy)
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
