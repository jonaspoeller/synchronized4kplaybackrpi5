#!/usr/bin/env python3
"""Fill framebuffer with a solid color extracted from first video frame."""
import struct, subprocess, sys

video = sys.argv[1] if len(sys.argv) > 1 else "/tmp/video-sync/loop.mp4"

# Extract average color from first frame
result = subprocess.run(
    ["ffmpeg", "-i", video, "-vframes", "1", "-vf", "scale=1:1",
     "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
    capture_output=True, timeout=10,
)
if len(result.stdout) >= 3:
    r, g, b = result.stdout[0], result.stdout[1], result.stdout[2]
else:
    r, g, b = 0, 0, 0

# Read framebuffer params
w, h = map(int, open("/sys/class/graphics/fb0/virtual_size").read().strip().split(","))
bpp = int(open("/sys/class/graphics/fb0/bits_per_pixel").read().strip())

if bpp == 16:
    px = struct.pack("<H", ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3))
else:
    px = struct.pack("BBBB", b, g, r, 255)

with open("/dev/fb0", "wb") as fb:
    fb.write(px * (w * h))

print(f"FB filled {w}x{h} {bpp}bpp with #{r:02x}{g:02x}{b:02x}")
