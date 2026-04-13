# Synchronized Video Playback for Raspberry Pi 5

A solution for synchronized, looping video playback across multiple Raspberry Pi 5 devices. Designed for continuous 24/7 operation in video walls and digital signage installations. Content updates via USB stick with automatic USB → SD → RAM flow.

## Key Features

*   **Synchronized Playback:** Master-Slave architecture coordinates playback across multiple Pi 5 devices via UDP broadcast.
*   **Seamless HEVC Loop:** Hardware-accelerated H.265 decoding with `--input-repeat=65535` for ~30ms loop gap.
*   **USB → SD → RAM Flow:** Insert a USB stick with `loop.mp4` — video is imported to SD, USB ejected, playback runs from RAM.
*   **Persistent Video on SD:** Survives reboots. USB only needed to update the video.
*   **Smart Import:** Checksum comparison skips copy if video is unchanged.
*   **Zero-Copy DMA Pipeline:** Decoder buffers go directly to display controller (~6-9% CPU usage).
*   **HDMI Auto-Detect:** Automatically finds connected HDMI port (1 or 2), supports hotplug.
*   **Auto-Detect Any Resolution:** Via EDID (4K, 1080p, 1280x1024, etc.)
*   **Universal USB Support:** FAT32, exFAT, NTFS, ext2/3/4, with or without partition table.
*   **Master Failure Recovery:** Watchdog on each slave reverts to black screen if master signal lost.
*   **Automatic Slave Integration:** Late-started slaves are integrated on next master restart.
*   **Multi-Group Support:** Multiple independent player groups on the same network via different ports.
*   **Silent Boot:** No console output, no splash screen, no cursor — black from power-on to playback.
*   **System Hardening:** Journal limited, apt auto-updates disabled, hardware watchdog, unnecessary services disabled.
*   **One-Click Install & Uninstall.**

---

## How It Works

### Sync Protocol

1.  **Boot** → systemd starts `video-sync.service`
2.  **USB check** → Each Pi independently imports `loop.mp4` from USB to SD if present
3.  **SD → RAM** → Video copied to tmpfs for stall-free playback
4.  **Master broadcasts** `stop` → `prepare` → `play` to all slaves
5.  **Synchronized start** → Master and slaves start `cvlc` simultaneously
6.  **Heartbeat** → Master sends periodic heartbeat; slaves revert to black if lost for 5s
7.  **Seamless loop** → `cvlc --input-repeat=65535` handles looping internally (~30ms gap)

### Content Update

1.  Prepare a USB stick (FAT32/exFAT/NTFS) with a file named `loop.mp4`
2.  Insert USB into each Pi one by one — the service restarts automatically via udev
3.  Video is copied USB → SD, USB is ejected, then SD → RAM, playback resumes
4.  USB stick can be removed immediately after the import completes

---

## Prerequisites

*   2 or more **Raspberry Pi 5** devices.
*   **Raspberry Pi OS Lite (Trixie / Debian 13, 64-bit)** on each SD card.
*   A stable, wired (Ethernet) network connection.
*   Each Pi must have a unique, fixed IP address.
*   A USB stick with `loop.mp4` (HEVC/H.265 encoded, matching monitor resolution).

---

## Installation

The installation must be performed on **every** Pi (both Master and Slaves).

#### Step 1: Prepare the System

1.  **Flash** Raspberry Pi OS Lite (Trixie, 64-bit) to your SD card.
2.  **Set a Static IP Address:**
    ```bash
    # Find your connection name
    nmcli connection show

    # Set static IP (adjust values for your network)
    sudo nmcli c mod "Wired connection 1" ipv4.method manual \
    ipv4.addresses 10.0.0.220/24 \
    ipv4.gateway 10.0.0.1 \
    ipv4.dns "8.8.8.8,1.1.1.1"

    # Apply
    sudo nmcli c down "Wired connection 1"; sudo nmcli c up "Wired connection 1"
    ```

#### Step 2: Run the Automated Installer

1.  **Download the Installer Script**
    ```bash
    wget https://github.com/jonaspoeller/synchronized4kplaybackrpi5/releases/latest/download/setup_video_sync.sh
    ```

2.  **Fix line endings and execute**
    ```bash
    sed -i 's/\r$//' setup_video_sync.sh
    chmod +x setup_video_sync.sh
    sudo ./setup_video_sync.sh
    ```

#### Step 3: Follow the Interactive Prompts

The script will ask for:
*   The static IP address and subnet of the current device (e.g., `192.168.1.10/24`).
*   The network port for this sync group (e.g., `5555`).
*   The role of the device (Master or Slave).
*   The IP address of the Master (if configuring a Slave).

The device reboots automatically. After reboot, insert a USB stick with `loop.mp4` to start playback.

---

## Configuration

### Master Configuration

Path: `/opt/video-sync/sync_config.ini`

```ini
[network]
master_ip = 192.168.1.10
broadcast_ip = 192.168.1.255
sync_port = 5555
```

### Slave Configuration

Path: `/opt/video-sync/sync_config.ini`

```ini
[network]
master_ip = 192.168.1.10
sync_port = 5555
```

---

## Usage & Management

| Command | Description |
|---|---|
| `video-sync-start` | Start the service |
| `video-sync-stop` | Stop the service and kill VLC |
| `video-sync-restart` | Restart the service |
| `video-sync-status` | Show service status |
| `video-sync-logs` | Show live journal logs |

---

## Technical Details

### VLC Flags
```bash
cvlc --no-xlib --quiet --fullscreen --no-video-title-show --no-osd \
     --codec=drm_avcodec --vout=drm_vout --drm-vout-display=HDMI-{1|2} \
     --drm-vout-pool-dmabuf --no-audio --file-caching=2000 \
     --input-repeat=65535 \
     /tmp/video-sync/loop.mp4
```

### config.txt
```ini
dtoverlay=vc4-kms-v3d,cma-512
disable_overscan=1
```

### Resolution Handling
The Pi automatically detects the monitor's native resolution via EDID. No `hdmi_group` or `hdmi_mode` is set. VLC `--fullscreen` with `--vout=drm_vout` adapts to the active display resolution. Your video should match the monitor resolution for best results.

### Loop Gap
VLC 3.0 has a known limitation: there is a brief flash (~30ms / 1-3 frames) between loop iterations. For practical purposes this is barely noticeable, especially with videos where the first and last frames share the same background color.

### System Hardening
- **Non-root service** with sudoers only for `mount`/`umount` on `/mnt/usb`
- **Journal limited** to 50MB / 7 days
- **apt auto-updates disabled** — no uncontrolled package changes
- **Hardware watchdog** — auto-reboot on kernel panic (10s) or system hang (15s)
- **Unnecessary services disabled** — bluetooth, ModemManager, avahi, serial-getty
- **Unnecessary timers disabled** — fstrim, e2scrub, man-db, dpkg-db-backup
- **tmpfiles-clean exception** — RAM video copy protected from cleanup
- **WiFi + SSH remain active** for remote management

---

## Uninstallation

```bash
wget -O uninstall_video_sync.sh https://github.com/jonaspoeller/synchronized4kplaybackrpi5/releases/latest/download/uninstall_video_sync.sh
sed -i 's/\r$//' uninstall_video_sync.sh
chmod +x uninstall_video_sync.sh
sudo ./uninstall_video_sync.sh
```

This reverts all system changes (config.txt, cmdline.txt, services, udev rules, hardening, sudoers) and reboots.

---

## Project Structure

```
synchronized4kplaybackrpi5/
├── README.md                  # This file
├── LICENSE                    # MIT License
├── setup_video_sync.sh        # One-click installer (contains Python scripts inline)
├── uninstall_video_sync.sh    # Uninstaller
├── video_sync_master.py       # Reference copy of master script
└── video_sync_slave.py        # Reference copy of slave script

# On the Pi after installation:
/opt/video-sync/
├── video_sync_master.py       # or video_sync_slave.py (depending on role)
├── sync_config.ini            # Network configuration
├── video/loop.mp4             # Persistent video (imported from USB)
├── background.png             # First frame of current video
└── black.png                  # Fallback standby image
```

---

## Project Links

*   **[Source Code and All Files](https://github.com/jonaspoeller/synchronized4kplaybackrpi5)**
*   **[Releases (Downloads)](https://github.com/jonaspoeller/synchronized4kplaybackrpi5/releases)**
*   **[Report an Issue (Issues)](https://github.com/jonaspoeller/synchronized4kplaybackrpi5/issues)**

---

## License

This project is licensed under the MIT License. See the LICENSE file for details.

© 2025 Jonas Pöller
