#!/bin/bash
# ============================================================================
# Synchronized Video Playback - Uninstaller (Pi 5)
# ============================================================================
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

if [ "$EUID" -ne 0 ]; then
    error "This script must be run as root (sudo)."
    exit 1
fi

info "=== Synchronized Video Playback Uninstaller (Pi 5) ==="

# --- Stop and disable service ---
info "Stopping and disabling service..."
systemctl stop video-sync.service 2>/dev/null || true
systemctl disable video-sync.service 2>/dev/null || true
rm -f /etc/systemd/system/video-sync.service
systemctl daemon-reload

# --- Kill any remaining VLC processes ---
pkill -f "cvlc.*drm_vout" 2>/dev/null || true

# --- Remove udev rules ---
info "Removing udev rules..."
rm -f /etc/udev/rules.d/99-video-sync-usb.rules
rm -f /etc/udev/rules.d/99-video-sync-hdmi.rules
udevadm control --reload-rules

# --- Remove sudoers rule ---
info "Removing sudoers rule..."
rm -f /etc/sudoers.d/video-sync

# --- Remove system hardening configs ---
info "Removing system hardening configs..."
rm -f /etc/systemd/journald.conf.d/video-sync.conf
rm -f /etc/tmpfiles.d/video-sync.conf
rm -f /etc/sysctl.d/99-video-sync-watchdog.conf
rm -f /etc/systemd/system.conf.d/watchdog.conf

# Re-enable services that were disabled
for svc in bluetooth.service ModemManager.service avahi-daemon.service; do
    systemctl unmask "$svc" 2>/dev/null || true
    systemctl enable "$svc" 2>/dev/null || true
done
systemctl enable serial-getty@ttyAMA10.service 2>/dev/null || true

# Re-enable timers
for tmr in fstrim.timer e2scrub_all.timer man-db.timer dpkg-db-backup.timer; do
    systemctl enable "$tmr" 2>/dev/null || true
done

# Unmask apt services
systemctl unmask apt-daily.service apt-daily-upgrade.service 2>/dev/null || true
systemctl enable apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true

# Reset kernel panic settings
sysctl -w kernel.panic=0 kernel.panic_on_oops=0 2>/dev/null || true

# --- Remove application ---
info "Removing application files..."
rm -rf /opt/video-sync

# --- Clean up RAM copy ---
rm -rf /tmp/video-sync

# --- Unmount and remove mount point ---
umount /mnt/usb 2>/dev/null || true
rmdir /mnt/usb 2>/dev/null || true

# --- Remove helper scripts ---
info "Removing helper scripts..."
rm -f /usr/local/bin/video-sync-start
rm -f /usr/local/bin/video-sync-stop
rm -f /usr/local/bin/video-sync-status
rm -f /usr/local/bin/video-sync-logs
rm -f /usr/local/bin/video-sync-restart

# --- Remove ZeroTier ---
if command -v zerotier-cli &>/dev/null; then
    info "Leaving ZeroTier networks and removing..."
    for nw in $(zerotier-cli listnetworks -j 2>/dev/null | grep -oP '"nwid"\s*:\s*"\K[^"]+'); do
        zerotier-cli leave "$nw" 2>/dev/null || true
    done
    systemctl stop zerotier-one.service 2>/dev/null || true
    systemctl disable zerotier-one.service 2>/dev/null || true
    apt-get purge -y zerotier-one 2>/dev/null || true
    rm -rf /var/lib/zerotier-one
    info "ZeroTier removed."
else
    info "ZeroTier not installed, skipping."
fi

# --- Restore Ethernet to DHCP ---
info "Restoring Ethernet to DHCP..."
ETH_CON=$(nmcli -t -f NAME,TYPE connection show | grep ':.*ethernet' | head -n1 | cut -d: -f1)
if [ -n "$ETH_CON" ]; then
    nmcli connection modify "${ETH_CON}" ipv4.method auto ipv4.addresses "" ipv4.gateway "" ipv4.dns "" 2>/dev/null || true
    info "Ethernet '${ETH_CON}' restored to DHCP. Will apply after reboot."
else
    warn "No ethernet connection found. Skipping DHCP restore."
fi

# --- Restore config.txt ---
info "Restoring config.txt..."
CONFIG_FILE="/boot/firmware/config.txt"
if [ -f "$CONFIG_FILE" ]; then
    sed -i '/# --- Video Sync Setup ---/,/# --- End Video Sync Setup ---/d' "$CONFIG_FILE"
    info "Removed Video Sync settings from config.txt."
fi

# --- Restore cmdline.txt ---
info "Restoring cmdline.txt..."
CMDLINE_FILE="/boot/firmware/cmdline.txt"
if [ -f "$CMDLINE_FILE" ]; then
    sed -i 's/ quiet//g' "$CMDLINE_FILE"
    sed -i 's/ loglevel=[0-9]*//g' "$CMDLINE_FILE"
    sed -i 's/ consoleblank=[0-9]*//g' "$CMDLINE_FILE"
    sed -i 's/ vt.global_cursor_default=[0-9]*//g' "$CMDLINE_FILE"
    sed -i 's/ logo.nologo//g' "$CMDLINE_FILE"
    sed -i 's/console=tty3/console=tty1/g' "$CMDLINE_FILE"
    info "Restored cmdline.txt."
fi

# --- Re-enable getty and graphical target ---
info "Restoring boot defaults..."
systemctl enable getty@tty1.service 2>/dev/null || true
systemctl set-default graphical.target 2>/dev/null || true

# --- Done ---
echo ""
info "============================================"
info "  Uninstall complete!"
info "============================================"
echo ""
warn "Rebooting in 10 seconds to apply changes... (Ctrl+C to cancel)"
sleep 10
reboot
