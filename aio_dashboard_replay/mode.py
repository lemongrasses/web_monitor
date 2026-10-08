"""Switch between live and bag replay (two aio-nav-ros config files) from the maintenance page.

The dashboard's own ROS connection and the AIO NAV / DSO processes must all use the same
domain (the one in the chosen config file), so a switch (1) stops AIO NAV and DSO, (2) stores the choice in state/ros_mode.json,
(3) restarts the dashboard, which reads the choice at start-up. The camera and IMU drivers
are started elsewhere and are not touched. Starting AIO NAV again is left to the operator.
"""

from typing import Callable, Dict


def apply_mode(cfg, mode: str, stop_nav: Callable[[], Dict], schedule_restart: Callable[[], None],
               events=None) -> Dict:
    modes = cfg["ros"]["modes"]
    if mode not in modes:
        return {"success": False, "status": 400, "summary": f"unknown mode {mode!r}"}
    label = modes[mode]["label"]
    if not cfg.mode_config_path(mode):
        return {"success": False, "status": 400,
                "summary": f"config file for {label} not found ({modes[mode].get('config')})"}
    if mode == cfg.ros_mode():
        return {"success": True, "changed": False, "summary": f"already in {label} mode"}

    stopped = stop_nav()
    if not stopped.get("success"):
        return {"success": False, "status": 500,
                "summary": "could not stop AIO NAV / DSO: " + stopped.get("summary", "")}
    try:
        cfg.save_ros_mode(mode)
    except OSError as e:
        return {"success": False, "status": 500, "summary": f"cannot save the setting: {e}"}

    ros = {m["name"]: m for m in cfg.ros_mode_info()["modes"]}[mode]
    detail = (f"{modes[mode].get('config')}: ROS_DOMAIN_ID={ros['domain_id']}, "
              f"localhost only {'on' if ros['localhost_only'] else 'off'}")
    if events is not None:
        events.add("warning", "action", f"ROS environment set to {label}", detail + "; dashboard restarting")
    schedule_restart()
    return {"success": True, "changed": True, "restarting": True,
            "summary": f"{label} ({detail}). Dashboard restarting; start AIO NAV again afterwards."}
