"""sensor_msgs/PointCloud2 -> compact float32 [x, y, z, intensity] array for the browser."""

import numpy as np

# sensor_msgs/PointField datatypes
_PF_TYPES = {1: "i1", 2: "u1", 3: "i2", 4: "u2", 5: "i4", 6: "u4", 7: "f4", 8: "f8"}
INTENSITY_FIELDS = ("intensity", "reflectivity", "i", "signal")


class PointCloudError(Exception):
    pass


def pointcloud2_to_xyzi(msg, max_points: int = 30000, seed: int = 0) -> np.ndarray:
    """Return an (N, 4) float32 array, finite points only, randomly subsampled to max_points."""
    fields = {f.name: f for f in msg.fields}
    missing = [n for n in ("x", "y", "z") if n not in fields]
    if missing:
        raise PointCloudError(f"point cloud has no {', '.join(missing)} field")
    inten = next((n for n in INTENSITY_FIELDS if n in fields), None)
    order = ">" if msg.is_bigendian else "<"
    names = ["x", "y", "z"] + ([inten] if inten else [])
    try:
        dtype = np.dtype({
            "names": names,
            "formats": [order + _PF_TYPES[fields[n].datatype] for n in names],
            "offsets": [fields[n].offset for n in names],
            "itemsize": int(msg.point_step),
        })
    except KeyError as e:
        raise PointCloudError(f"unsupported point field datatype {e}") from None

    width, height = int(msg.width), int(msg.height)
    row_step, point_step = int(msg.row_step), int(msg.point_step)
    raw = np.frombuffer(msg.data, dtype=np.uint8)
    if width == 0 or height == 0:
        return np.zeros((0, 4), dtype=np.float32)
    if raw.size < height * row_step:
        raise PointCloudError("truncated point cloud data")
    rows = raw[: height * row_step].reshape(height, row_step)[:, : width * point_step]
    pts = np.ascontiguousarray(rows).view(dtype).reshape(-1)

    out = np.empty((pts.size, 4), dtype=np.float32)
    out[:, 0], out[:, 1], out[:, 2] = pts["x"], pts["y"], pts["z"]
    out[:, 3] = pts[inten] if inten else 0.0
    keep = np.isfinite(out[:, :3]).all(axis=1) & (np.abs(out[:, :3]).sum(axis=1) > 1e-3)
    out = out[keep]
    if len(out) > max_points:
        idx = np.random.default_rng(seed).choice(len(out), max_points, replace=False)
        out = out[np.sort(idx)]
    return out
