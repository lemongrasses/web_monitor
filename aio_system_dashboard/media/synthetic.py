"""Synthetic camera frame and LiDAR scan for fake mode (UI development / demos)."""

import math
import time

import numpy as np


def fake_camera_frame(width: int = 640, height: int = 400) -> np.ndarray:
    """RGB road scene: sky, horizon, lane lines and a moving block."""
    t = time.time()
    y = np.arange(height)[:, None]
    x = np.arange(width)[None, :]
    img = np.zeros((height, width, 3), dtype=np.uint8)
    horizon = int(height * 0.45)
    sky = (y < horizon)
    img[..., 0] = np.where(sky, 120 + 60 * y / horizon, 70)
    img[..., 1] = np.where(sky, 170 + 40 * y / horizon, 72)
    img[..., 2] = np.where(sky, 230, 78)
    # perspective lane lines converging on the horizon
    ground = ~sky
    depth = np.clip((y - horizon) / (height - horizon), 1e-3, 1)
    cx = width / 2
    for lane in (-1.0, 0.0, 1.0):
        lx = cx + lane * depth * width * 0.55
        dashed = ((1 / depth + t * 4) % 2) < 1 if lane == 0 else True
        line = ground & (np.abs(x - lx) < 2 + 6 * depth) & dashed
        img[line] = (235, 235, 210)
    # a vehicle ahead that drifts left/right
    bx = int(cx + 80 * math.sin(t * 0.7))
    by = horizon + 40
    img[by: by + 45, bx - 40: bx + 40] = (180, 40, 40)
    img[by + 5: by + 20, bx - 30: bx + 30] = (90, 130, 170)
    # moving scan bar so it is obvious the image is live
    bar = int((t * 120) % width)
    img[:, max(0, bar - 2): bar + 2] = (255, 210, 0)
    return img


def fake_lidar_scan(rings: int = 32, azimuths: int = 1024) -> np.ndarray:
    """(N, 4) float32 xyz+intensity: ground, a 40x24 m room, two cars and a walking person."""
    t = time.time()
    elev = np.radians(np.linspace(-22.5, 22.5, rings))[:, None]
    az = np.linspace(-math.pi, math.pi, azimuths, endpoint=False)[None, :]
    sensor_h = 1.8
    dx, dy = np.cos(az), np.sin(az)

    # distance to the walls of an axis-aligned room around the sensor (horizontal range)
    with np.errstate(divide="ignore"):
        tx = np.where(dx > 0, 25.0 / dx, np.where(dx < 0, -15.0 / dx, np.inf))
        ty = np.where(dy > 0, 12.0 / dy, np.where(dy < 0, -12.0 / dy, np.inf))
    wall_r = np.minimum(tx, ty)
    rng = np.broadcast_to(wall_r, (rings, azimuths)).copy()
    # ground hit for downward beams
    with np.errstate(divide="ignore"):
        ground_r = np.where(elev < 0, sensor_h / np.tan(-elev), np.inf)
    rng = np.minimum(rng, np.broadcast_to(ground_r, rng.shape))
    # obstacles as vertical cylinders (x, y, radius, height)
    person = (8 * math.cos(t * 0.4), 6 * math.sin(t * 0.4), 0.35, 1.8)
    for ox, oy, rad, top in ((10.0, -4.0, 2.2, 1.5), (-7.0, 5.0, 2.2, 1.5), person):
        b = ox * dx + oy * dy
        c = ox * ox + oy * oy - rad * rad
        disc = b * b - c
        hit = np.where(disc > 0, b - np.sqrt(np.maximum(disc, 0)), np.inf)
        hit = np.where(hit > 0, hit, np.inf)
        z_at = hit * np.tan(elev) + sensor_h  # height above ground at the hit
        rng = np.where((z_at >= 0) & (z_at <= top) & (hit < rng), hit, rng)

    rng = np.minimum(rng, 60.0)
    x = rng * dx
    y = rng * dy
    z = rng * np.tan(elev)  # sensor frame (sensor at z=0)
    noise = np.random.default_rng(int(t * 10)).normal(0, 0.02, rng.shape)
    inten = np.clip(80 - rng + 40 * (z > -sensor_h + 0.1), 5, 255)
    pts = np.stack([x + noise, y + noise, z, inten], axis=-1).reshape(-1, 4)
    return pts[np.isfinite(pts).all(axis=1)].astype(np.float32)
