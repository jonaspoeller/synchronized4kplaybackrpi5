#!/usr/bin/env python3
"""Test framebuffer fill + VLC restart timing."""
import subprocess, struct, time, os

VIDEO = "/tmp/video-sync/loop.mp4"
SOCK = "/tmp/vlc-sync-slave.sock"

# Extract color
res = subprocess.run(
    ["ffmpeg", "-i", VIDEO, "-vframes", "1", "-vf", "scale=1:1",
     "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
    capture_output=True, timeout=10,
)
r, g, b = (res.stdout[0], res.stdout[1], res.stdout[2]) if len(res.stdout) >= 3 else (0, 0, 0)
print(f"Color: #{r:02x}{g:02x}{b:02x}")

# Read fb params
w, h = map(int, open("/sys/class/graphics/fb0/virtual_size").read().strip().split(","))
bpp = int(open("/sys/class/graphics/fb0/bits_per_pixel").read().strip())
if bpp == 16:
    px = struct.pack("<H", ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3))
else:
    px = struct.pack("BBBB", b, g, r, 255)
fb_data = px * (w * h)

# 1. Fill fb0
t0 = time.time()
with open("/dev/fb0", "wb") as fb:
    fb.write(fb_data)
t1 = time.time()
print(f"fb0 fill: {int((t1-t0)*1000)}ms")

# 2. Kill VLC
subprocess.run(["killall", "-9", "vlc"], capture_output=True)
t2 = time.time()
print(f"kill: {int((t2-t1)*1000)}ms")

# 3. Start new VLC paused
try:
    os.remove(SOCK)
except FileNotFoundError:
    pass
p = subprocess.Popen(
    ["vlc", "-I", "oldrc", f"--rc-unix={SOCK}", "--rc-fake-tty",
     "--no-osd", "--no-video-title", "--fullscreen",
     "--start-paused", VIDEO],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
t3 = time.time()
print(f"vlc spawn: {int((t3-t2)*1000)}ms")

# 4. Wait for RC socket
for i in range(100):
    if os.path.exists(SOCK):
        break
    time.sleep(0.05)
t4 = time.time()
print(f"rc socket ready: {int((t4-t3)*1000)}ms")
print(f"TOTAL kill-to-ready: {int((t4-t1)*1000)}ms")
