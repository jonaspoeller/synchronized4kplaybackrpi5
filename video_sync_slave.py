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
import vlc

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

VLC_ARGS_STR = ' '.join([
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
])

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("video-sync-slave")

# VLC instance and player (python-vlc bindings, like Pi4 project)
vlc_instance = None
vlc_player = None
black_media = None
running = True
last_heartbeat = time.time()
current_sequence_id = None
is_prepared = False
video_loaded = False  # True after first prepare (DRM acquired)


def init_vlc():
    """Initialize (or re-initialize) VLC instance and player."""
    global vlc_instance, vlc_player, black_media
    if vlc_player:
        try:
            vlc_player.stop()
        except Exception:
            pass
    vlc_instance = vlc.Instance(VLC_ARGS_STR)
    vlc_player = vlc_instance.media_player_new()
    if os.path.exists(BLACK_IMG):
        black_media = vlc_instance.media_new(BLACK_IMG)
    log.info("VLC instance initialized (python-vlc bindings)")


def shutdown(sig, frame):
    global running
    log.info(f"Signal {sig}, shutting down...")
    running = False
    if vlc_player:
        vlc_player.stop()
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


def show_black():
    """Show black screen via VLC player (no process restart)."""
    try:
        if vlc_player and black_media:
            log.info("Showing black screen")
            vlc_player.set_media(black_media)
            vlc_player.play()
    except Exception as e:
        log.error(f"show_black failed: {e}")


def prepare_player(video_path):
    """Prepare VLC: frame 1 paused. Exact Pi4 approach — no stop(), keeps DRM/HDMI alive.
    Returns True on success.
    """
    if not vlc_instance or not vlc_player:
        log.error("prepare_player: VLC not initialized")
        return False
    try:
        media = vlc_instance.media_new(video_path)
        vlc_player.set_media(media)
        vlc_player.video_set_scale(0)
        vlc_player.play()
        time.sleep(0.2)
        # Seek to start WHILE playing (so display updates to frame 0)
        vlc_player.set_time(0)
        time.sleep(0.1)
        vlc_player.pause()
        state = vlc_player.get_state()
        if state == vlc.State.Error:
            log.error(f"prepare_player failed, VLC state: {state}")
            return False
        log.info(f"Prepared (frame 1 visible): {video_path}")
        return True
    except Exception as e:
        log.error(f"prepare_player exception: {e}")
        return False


def master_watchdog():
    """Background thread: revert to black screen if master heartbeat lost."""
    global last_heartbeat, current_sequence_id, is_prepared, video_loaded
    vlc_error_count = 0
    while running:
        time.sleep(2)
        if time.time() - last_heartbeat > 5:
            log.warning("Master signal lost! Reverting to black screen.")
            show_black()
            last_heartbeat = time.time()
            current_sequence_id = None
            is_prepared = False
            video_loaded = False

        # VLC health check: detect stuck Error state
        try:
            if vlc_player:
                state = vlc_player.get_state()
                if state == vlc.State.Error:
                    vlc_error_count += 1
                    if vlc_error_count >= 3:
                        log.error("VLC stuck in Error — re-initializing")
                        init_vlc()
                        show_black()
                        is_prepared = False
                        video_loaded = False
                        vlc_error_count = 0
                else:
                    vlc_error_count = 0
        except Exception as e:
            log.error(f"VLC health check failed: {e}")


def main():
    global running, last_heartbeat, current_sequence_id, is_prepared, video_loaded

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

    # Initialize VLC (python-vlc bindings — one instance, stays alive forever)
    init_vlc()

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
    log.info("Black screen displayed. Waiting for master commands (python-vlc).")

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
                show_black()

            elif command == "standby":
                log.info("Master: standby (no video)")
                show_black()

            elif command == "prepare":
                log.info("Master: prepare")
                if not playback_path or not os.path.isfile(playback_path):
                    if os.path.isfile(SD_VIDEO_PATH):
                        ram_video = copy_to_ram()
                        playback_path = ram_video or SD_VIDEO_PATH
                        extract_background(playback_path)
                    else:
                        log.warning("No video available for playback")
                        playback_path = None

                if not playback_path:
                    continue

                def _send_ready(seq_id):
                    try:
                        ready_msg = json.dumps({
                            "command": "ready",
                            "sequence_id": seq_id,
                        }).encode("utf-8")
                        rs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        rs.sendto(ready_msg, (master_ip, sync_port + 1))
                        rs.close()
                        log.info("Sent ready to master")
                    except Exception as e:
                        log.error(f"Failed to send ready: {e}")

                if is_prepared:
                    log.info("Already prepared — re-sending ready")
                    _send_ready(current_sequence_id)
                    continue

                # Prepare: show frame 1 paused
                success = prepare_player(playback_path)
                if success:
                    is_prepared = True
                    _send_ready(current_sequence_id)
                else:
                    log.error("Prepare failed — re-initializing VLC")
                    init_vlc()
                    success = prepare_player(playback_path)
                    if success:
                        is_prepared = True
                        _send_ready(current_sequence_id)
                    else:
                        log.error("Prepare failed after VLC re-init")

            elif command == "play":
                is_prepared = False
                log.info("Master: play")
                if vlc_player:
                    try:
                        vlc_player.play()
                        # Verify playback started
                        time.sleep(0.3)
                        state = vlc_player.get_state()
                        if state not in (vlc.State.Playing, vlc.State.Opening, vlc.State.Buffering):
                            log.warning(f"Play may have failed, VLC state: {state}")
                    except Exception as e:
                        log.error(f"Play failed: {e}")
                else:
                    log.warning("Play command but VLC player not initialized")

            elif command == "heartbeat":
                pass  # Just updates last_heartbeat above

    except KeyboardInterrupt:
        pass

    # --- Cleanup ---
    if vlc_player:
        vlc_player.stop()
    shutil.rmtree(RAM_COPY_DIR, ignore_errors=True)
    sock.close()
    log.info("=== Video Sync Slave stopped ===")


if __name__ == "__main__":
    main()
