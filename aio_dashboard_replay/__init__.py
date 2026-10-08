"""Optional module "replay": research and post-processing on recorded data.

* Live / Bag replay switch (which aio-nav-ros config file and ROS domain AIO NAV runs with).
  Without this module a machine always runs Live.
* Bag player on the Maintenance page: pick a bag, its topics and any `ros2 bag play` option, save
  presets, pause / resume / change the rate while it plays.

Installed with ``deploy/install.sh --with-replay``; see aio_system_dashboard/modules.py.
"""

NAME = "replay"
TITLE = "Replay"

DEFAULTS = {
    "enabled": True,
    "bag_roots": ["~"],          # folders searched for bags (a folder with metadata.yaml)
    "scan_depth": 2,             # how many folder levels below each root
    "ros_setup": "auto",         # setup.bash to source; auto: aio-nav-ros install/, else /opt/ros/<distro>
    "log_file": "logs/bag_play.log",
    "stop_grace_s": 5.0,         # SIGINT -> SIGTERM -> SIGKILL
}


def register(ctx, app) -> None:
    from .web import register as _register
    _register(ctx, app)
