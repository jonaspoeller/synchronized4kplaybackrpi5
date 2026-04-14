#!/usr/bin/env python3
"""
Synchronized Video Playback - Master Node (Raspberry Pi 5)
Flow: USB → SD → RAM → Synchronized Playback

  1. If USB present: copy video from USB to SD, then eject USB
  2. Copy video from SD to RAM (tmpfs)
  3. Broadcast sync commands to slaves
  4. Play from RAM, re-sync all nodes at each loop boundary
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


def build_vlc_args_str():
    """Build VLC argument string for vlc.Instance(), reading audio config."""
    args = [
        "--no-xlib",
        "--quiet",
        "--fullscreen",
        "--no-video-title-show",
        "--no-osd",
        "--codec=drm_avcodec",
        "--vout=drm_vout",
        f"--drm-vout-display={HDMI_PORT}",
        "--drm-vout-pool-dmabuf",
        "--file-caching=2000",
    ]
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)
    audio_enabled = config.get("audio", "enabled", fallback="no").strip().lower()
    alsa_device = config.get("audio", "alsa_device", fallback="").strip()
    if audio_enabled == "yes" and alsa_device:
        args += ["--aout=alsa", f"--alsa-audio-device={alsa_device}"]
    else:
        args.append("--no-audio")
    return ' '.join(args)


VLC_ARGS_STR = build_vlc_args_str()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("video-sync-master")

vlc_instance = None
vlc_player = None
black_media = None
sock = None
running = True


def set_framebuffer_black():
    """Fill Linux framebuffer with black. Ensures monitor always sees black
    (not 'no signal') when VLC's DRM plane is inactive."""
    try:
        with open("/dev/fb0", "wb") as fb:
            chunk = b'\x00' * (1024 * 1024)  # 1MB of black
            for _ in range(33):  # ~33MB covers 4K (3840x2160x4)
                fb.write(chunk)
        log.info("Framebuffer set to black")
    except Exception as e:
        log.warning(f"Could not set framebuffer black: {e}")


def init_vlc():
    """Initialize (or re-initialize) VLC instance and player."""
    global vlc_instance, vlc_player, black_media
    # Clean up old instance if re-initializing
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


def prepare_player(video_path):
    """Prepare VLC: frame 1 paused. Exact Pi4 approach — no stop(), keeps DRM/HDMI alive.
    Returns True on success.
    """
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
        return True
    except Exception as e:
        log.error(f"prepare_player exception: {e}")
        return False


def show_black():
    """Show black screen via VLC player."""
    try:
        if vlc_player and black_media:
            vlc_player.set_media(black_media)
            vlc_player.play()
    except Exception as e:
        log.error(f"show_black failed: {e}")


def shutdown(sig, frame):
    global running
    log.info(f"Signal {sig}, shutting down...")
    running = False
    if vlc_player:
        vlc_player.stop()
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
    """Show standby image via VLC player."""
    img = BACKGROUND_IMG if os.path.exists(BACKGROUND_IMG) else BLACK_IMG
    if not os.path.exists(img) or not vlc_instance or not vlc_player:
        return
    log.info(f"Showing standby: {img}")
    media = vlc_instance.media_new(img)
    vlc_player.set_media(media)
    vlc_player.play()


def send_broadcast(sock, broadcast_ip, sync_port, message):
    try:
        sock.sendto(json.dumps(message).encode("utf-8"), (broadcast_ip, sync_port))
    except Exception as e:
        log.error(f"Broadcast error: {e}")


def main():
    global sock, running

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

    # Wait for network + slaves to be ready
    log.info("Waiting 5s for network and slaves to come up...")
    time.sleep(5)

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
        init_vlc()
        show_standby()
        while running:
            time.sleep(5)
        return

    # --- Step 3: Copy SD → RAM ---
    ram_video = copy_to_ram()
    playback_path = ram_video or SD_VIDEO_PATH
    video_hash = file_checksum(playback_path)
    extract_background(playback_path)

    # Initialize VLC (python-vlc bindings — one instance, stays alive forever)
    init_vlc()

    # --- Step 4: Initial stop + notify slaves ---
    send_broadcast(sock, broadcast_ip, sync_port, {**base_msg, "command": "stop"})
    time.sleep(0.5)

    # Persistent ready-acknowledgment socket (reused across loops)
    ready_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ready_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ready_sock.bind(("", sync_port + 1))
    ready_sock.settimeout(1.0)

    PREPARE_LEAD_TIME = 5  # seconds before video end to send prepare
    loop_delay = 0.5  # seconds between loop detection polls

    def drain_ready_sock():
        """Drain stale ready messages from previous round."""
        while True:
            try:
                ready_sock.setblocking(False)
                ready_sock.recvfrom(4096)
            except BlockingIOError:
                break
            except Exception:
                break
        ready_sock.setblocking(True)
        ready_sock.settimeout(1.0)

    def wait_for_slaves(timeout):
        """Wait for slave ready acknowledgments. Early exit when no new slaves for 0.5s."""
        ready_slaves = set()
        prepare_start = time.time()
        last_new_slave_time = None
        log.info(f"Waiting up to {timeout}s for slaves to be ready...")
        while time.time() - prepare_start < timeout:
            # Early exit: have slaves and no new ones for 0.5s
            if ready_slaves and last_new_slave_time and time.time() - last_new_slave_time > 0.5:
                break
            # Keep sending heartbeats so slave watchdog doesn't fire
            send_broadcast(sock, broadcast_ip, sync_port, {**base_msg, "command": "heartbeat"})
            try:
                data, addr = ready_sock.recvfrom(4096)
                msg = json.loads(data.decode("utf-8"))
                if msg.get("command") == "ready" and msg.get("sequence_id") == sequence_id:
                    if addr[0] not in ready_slaves:
                        last_new_slave_time = time.time()
                    ready_slaves.add(addr[0])
                    log.info(f"Slave ready: {addr[0]} ({len(ready_slaves)} total)")
            except socket.timeout:
                send_broadcast(sock, broadcast_ip, sync_port, {
                    **base_msg, "command": "prepare", "video_hash": video_hash,
                })
                continue
            except Exception:
                continue
        log.info(f"{len(ready_slaves)} slave(s) ready after {time.time() - prepare_start:.1f}s")
        return ready_slaves

    def broadcast_play():
        """Send play command 3x for UDP reliability, then unpause local VLC."""
        play_msg = {**base_msg, "command": "play"}
        for _ in range(3):
            send_broadcast(sock, broadcast_ip, sync_port, play_msg)
            time.sleep(0.05)
        vlc_player.play()

    # --- Step 5: Initial synchronized start ---
    drain_ready_sock()
    send_broadcast(sock, broadcast_ip, sync_port, {
        **base_msg, "command": "prepare", "video_hash": video_hash,
    })

    # Prepare master VLC: show frame 1 paused
    prepare_player(playback_path)
    log.info(f"Pre-buffered: {playback_path}")

    wait_for_slaves(timeout=10)
    broadcast_play()
    log.info(f"Playing: {playback_path}")

    # --- Step 6: Playback loop (hardened) ---
    is_playing = True
    consecutive_failures = 0
    MAX_FAILURES = 5  # re-init VLC after this many consecutive failures
    STUCK_TIMEOUT = 30  # seconds without state change before considering stuck
    last_state_change = time.time()
    last_state = None
    try:
        while running:
            while is_playing and running:
                send_broadcast(sock, broadcast_ip, sync_port, {**base_msg, "command": "heartbeat"})
                time.sleep(1)
                try:
                    state = vlc_player.get_state()
                except Exception as e:
                    log.error(f"VLC state query failed: {e}")
                    is_playing = False
                    break

                if state != last_state:
                    last_state_change = time.time()
                    last_state = state

                if state == vlc.State.Ended:
                    is_playing = False
                elif state == vlc.State.Error:
                    log.error("VLC entered Error state during playback")
                    is_playing = False
                elif state in (vlc.State.Stopped, vlc.State.NothingSpecial):
                    # VLC stopped unexpectedly
                    if time.time() - last_state_change > 5:
                        log.error(f"VLC stuck in {state} for 5s — treating as ended")
                        is_playing = False
                elif state == vlc.State.Paused:
                    if time.time() - last_state_change > STUCK_TIMEOUT:
                        log.warning(f"VLC stuck in Paused for {STUCK_TIMEOUT}s — forcing unpause")
                        vlc_player.play()
                        last_state_change = time.time()

            if not running:
                break

            log.info("--- Video ended. Resetting for loop. ---")
            # Tell slaves to prepare FIRST
            drain_ready_sock()
            send_broadcast(sock, broadcast_ip, sync_port, {
                **base_msg, "command": "prepare", "video_hash": video_hash,
            })
            # Prepare master
            success = prepare_player(playback_path)
            if not success:
                consecutive_failures += 1
                log.error(f"prepare_player failed ({consecutive_failures}/{MAX_FAILURES})")
                if consecutive_failures >= MAX_FAILURES:
                    log.warning("Too many failures — re-initializing VLC")
                    init_vlc()
                    consecutive_failures = 0
                time.sleep(1)
                continue

            # Wait for slaves (sends heartbeats during wait)
            wait_for_slaves(timeout=3)
            # Play all
            broadcast_play()

            # Verify play started
            time.sleep(0.5)
            verify_state = vlc_player.get_state()
            if verify_state not in (vlc.State.Playing, vlc.State.Opening, vlc.State.Buffering):
                log.warning(f"Play may have failed, VLC state: {verify_state} — retrying")
                consecutive_failures += 1
                if consecutive_failures >= MAX_FAILURES:
                    log.warning("Too many failures — re-initializing VLC")
                    init_vlc()
                    consecutive_failures = 0
                continue

            consecutive_failures = 0
            is_playing = True
            last_state_change = time.time()
            last_state = vlc.State.Playing
            log.info("Loop restarted — all nodes playing")

    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.error(f"Unhandled exception in playback loop: {e}")

    # --- Cleanup ---
    if vlc_player:
        vlc_player.stop()
    ready_sock.close()
    send_broadcast(sock, broadcast_ip, sync_port, {**base_msg, "command": "stop"})
    shutil.rmtree(RAM_COPY_DIR, ignore_errors=True)
    sock.close()
    log.info("=== Video Sync Master stopped ===")


if __name__ == "__main__":
    main()
