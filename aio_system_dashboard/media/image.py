"""Turn ROS image messages into something a browser can show.

sensor_msgs/CompressedImage is passed through unchanged. sensor_msgs/Image is
downscaled by an integer stride and encoded as JPEG (OpenCV, if installed) or
PNG (numpy + zlib, always available).
"""

import struct
import zlib
from typing import Tuple

import numpy as np

try:  # optional, only for faster/smaller JPEG output
    import cv2  # type: ignore
except ImportError:  # pragma: no cover - depends on the host
    cv2 = None


class ImageConvertError(Exception):
    pass


def encode_png(img: np.ndarray) -> bytes:
    """Minimal PNG encoder for uint8 gray (H, W) or RGB (H, W, 3) arrays."""
    img = np.ascontiguousarray(img, dtype=np.uint8)
    h, w = img.shape[:2]
    color_type = 2 if img.ndim == 3 else 0
    rows = np.zeros((h, 1 + img[0].size), dtype=np.uint8)  # filter byte 0 per row
    rows[:, 1:] = img.reshape(h, -1)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows.tobytes(), 6))
            + chunk(b"IEND", b""))


def encode_rgb(img: np.ndarray) -> Tuple[bytes, str]:
    """Encode uint8 gray/RGB as JPEG when OpenCV is present, else PNG."""
    if cv2 is not None:
        bgr = img[..., ::-1] if img.ndim == 3 else img
        ok, buf = cv2.imencode(".jpg", np.ascontiguousarray(bgr), [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            return buf.tobytes(), "image/jpeg"
    return encode_png(img), "image/png"


def _to_u8(gray16: np.ndarray) -> np.ndarray:
    """Contrast-stretch a 16-bit image (e.g. mono16 / depth) to 8 bits."""
    valid = gray16[gray16 > 0]
    if valid.size == 0:
        return np.zeros(gray16.shape, dtype=np.uint8)
    lo, hi = np.percentile(valid, (1, 99))
    hi = max(hi, lo + 1)
    return (np.clip((gray16.astype(np.float32) - lo) / (hi - lo), 0, 1) * 255).astype(np.uint8)


def image_msg_to_array(msg, max_width: int = 960) -> np.ndarray:
    """sensor_msgs/Image -> uint8 gray (H, W) or RGB (H, W, 3), downscaled."""
    h, w, step = int(msg.height), int(msg.width), int(msg.step)
    enc = str(msg.encoding).lower()
    if h == 0 or w == 0:
        raise ImageConvertError("empty image")
    stride = max(1, -(-w // max_width))  # ceil
    buf = np.frombuffer(msg.data, dtype=np.uint8)
    if buf.size < h * step:
        raise ImageConvertError(f"truncated image data ({buf.size} < {h * step})")
    rows = buf[: h * step].reshape(h, step)[::stride]

    if enc in ("rgb8", "bgr8", "8uc3"):
        img = rows[:, : w * 3].reshape(-1, w, 3)[:, ::stride]
        return img[..., ::-1] if enc == "bgr8" else img
    if enc in ("rgba8", "bgra8", "8uc4"):
        img = rows[:, : w * 4].reshape(-1, w, 4)[:, ::stride, :3]
        return img[..., ::-1] if enc == "bgra8" else img
    if enc in ("mono8", "8uc1") or enc.startswith("bayer_") and enc.endswith("8"):
        return rows[:, :w][:, ::stride]  # Bayer shown as raw gray: enough to see the scene
    if enc in ("mono16", "16uc1") or enc.startswith("bayer_") and enc.endswith("16"):
        dt = ">u2" if getattr(msg, "is_bigendian", 0) else "<u2"
        return _to_u8(rows[:, : w * 2].copy().view(dt)[:, ::stride])
    if enc in ("yuv422", "uyvy"):
        return rows[:, 1 : w * 2 : 2][:, ::stride]  # luminance only
    if enc in ("yuv422_yuy2", "yuyv"):
        return rows[:, 0 : w * 2 : 2][:, ::stride]
    raise ImageConvertError(f"unsupported image encoding {msg.encoding!r}")


def compressed_mime(fmt: str) -> str:
    return "image/png" if "png" in (fmt or "").lower() else "image/jpeg"
