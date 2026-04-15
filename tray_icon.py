"""
tray_icon.py
------------
Generates the 64x64 system-tray icon image used by the widget.

Public API
----------
make_tray_icon(status) -> PIL.Image.Image
    status: 'ok' (green dot), 'loading' (yellow), 'error' (red)
"""

from PIL import Image, ImageDraw

from logging_setup import log


def make_tray_icon(status: str = "ok") -> Image.Image:
    """Generate a 64x64 RGBA tray icon reflecting the current fetch status."""
    log.debug("Starting make_tray_icon status=%s", status)
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background rounded square
    draw.rounded_rectangle([2, 2, 62, 62], radius=12, fill=(30, 30, 35))

    # "C" lettermark
    draw.text((14, 10), "C", fill=(255, 255, 255))

    # Status dot
    dot_color = {
        "ok":      (80, 220, 120),
        "loading": (255, 200, 60),
        "error":   (220, 80, 80),
    }.get(status, (150, 150, 150))
    draw.ellipse([44, 44, 58, 58], fill=dot_color)

    log.debug("Finished make_tray_icon")
    return img
