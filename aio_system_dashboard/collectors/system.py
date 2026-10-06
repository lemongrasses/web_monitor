"""Host metrics: CPU, GPU (Jetson sysfs), RAM, disk, temperatures, uptime."""

import os
import time
from pathlib import Path
from typing import Dict, List, Optional

import psutil

from .base import PeriodicCollector
from ..config import resolve_path

# Jetson GPU load is exposed in per-mille under one of these paths.
GPU_LOAD_PATHS = (
    "/sys/devices/gpu.0/load",
    "/sys/devices/platform/gpu.0/load",
    "/sys/devices/platform/bus@0/17000000.gpu/load",
    "/sys/devices/platform/17000000.ga10b/load",
    "/sys/devices/platform/17000000.gv11b/load",
)


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def read_gpu_load() -> Optional[float]:
    for p in GPU_LOAD_PATHS:
        v = _read_text(p)
        if v is not None and v.isdigit():
            return int(v) / 10.0
    return None


def read_thermal_zones() -> List[Dict]:
    zones = []
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        t = _read_text(str(zone / "temp"))
        name = _read_text(str(zone / "type")) or zone.name
        try:
            temp_c = int(t) / 1000.0
        except (TypeError, ValueError):
            continue
        if -40.0 < temp_c < 150.0:
            zones.append({"name": name, "temp_c": round(temp_c, 1)})
    return zones


class SystemCollector(PeriodicCollector):
    name = "system"
    section = "system"

    def __init__(self, store, cfg):
        super().__init__(store, cfg["system"]["interval_s"])
        paths = list(cfg["system"]["disk_paths"])
        for root in cfg["data"]["roots"]:
            paths.append(str(resolve_path(root["path"])))
        self.disk_paths = paths
        self.started = time.time()
        psutil.cpu_percent(interval=None)  # prime the counter

    def poll(self) -> Dict:
        vm = psutil.virtual_memory()
        disks = []
        seen = set()
        for p in self.disk_paths:
            if not os.path.exists(p):
                continue
            try:
                usage = psutil.disk_usage(p)
            except OSError:
                continue
            key = (usage.total, usage.used)
            if key in seen:
                continue
            seen.add(key)
            disks.append({"path": p, "total": usage.total, "used": usage.used,
                          "free": usage.free, "percent": usage.percent})
        temps = read_thermal_zones()
        return {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "cpu_count": psutil.cpu_count(),
            "gpu_percent": read_gpu_load(),
            "mem_total": vm.total,
            "mem_used": vm.total - vm.available,
            "mem_percent": vm.percent,
            "disks": disks,
            "temps": temps,
            "max_temp_c": max((z["temp_c"] for z in temps), default=None),
            "uptime_s": time.time() - psutil.boot_time(),
            "dashboard_uptime_s": time.time() - self.started,
            "load_avg": list(os.getloadavg()),
        }
