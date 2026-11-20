# AIO System Dashboard

A web dashboard for the Jetson AIO navigation device. It watches the navigation software (AIO NAV)
and every part it depends on, helps find out why something does not work, and runs the device
without an operator.

| Pages | Port | For |
|-------|------|-----|
| User pages | `:8080` | Everyone, no login: is navigation working, the map, the data to download, the lever arms, Wi-Fi and the environment. English / 繁體中文 |
| Maintenance view | `:8081` | Engineers, with a password: system, ROS 2, GNSS / RTK, camera, network, diagnostics, still detection, AIO NAV's settings, a terminal |

## Documentation

| Guide | For |
|-------|-----|
| [User Guide](docs/USER_GUIDE.md) | People who use the device and collect data |
| [Maintenance Guide](docs/MAINTENANCE.md) | Engineers who find and fix problems |
| [Installation Guide](docs/INSTALL.md) | People who set up devices |
| [Configuration Reference](docs/CONFIG.md) | Every setting |
| [Integration Guide](docs/INTEGRATION.md) | Software that receives the navigation output, and vehicles |
| [System Recorder](docs/SYSMON.md) | The optional black box for freezes and power cuts |

繁體中文: [操作手冊](docs/OPERATION.zh-TW.md) · [設定手冊](docs/CONFIG.zh-TW.md) ·
[系統紀錄器](docs/SYSMON.zh-TW.md) (older editions, to be replaced by translations of the guides
above) · [術語表](docs/TERMINOLOGY.zh-TW.md) (shared with aio-nav-receiver).

For developers, in [`docs/dev/`](docs/dev/): [Architecture](docs/dev/ARCHITECTURE.md) ·
[Flows](docs/dev/FLOWS.md) · [Features and how they relate](docs/dev/FEATURES.md) ·
[Direction](docs/dev/DIRECTION.md) · [Ideas](docs/dev/IDEAS.md) ·
[Documentation plan](docs/dev/DOCS_PLAN.md) · [the original v1 spec](docs/dev/history/v1-spec.md)
(history). The documents in `docs/dev/` are mostly in Traditional Chinese.

## Development (x86, no ROS, no sensors)

```bash
python3 -m pip install --target .devdeps flask pyyaml psutil   # once (no venv needed)
tools/dev.sh start all        # dashboard (fake mode) + fake NAV sender cycling all scenarios
# open http://127.0.0.1:8080 and http://127.0.0.1:8081
tools/dev.sh stop
PYTHONPATH=.devdeps python3 -m unittest discover -s tests -t .
```

- `tools/fake_nav_sender.py --scenario NAME` sends real 0x04 packets. The scenarios are `normal`,
  `align`, `gnss_loss`, `flags`, `pause`, `loss`, `heading_wrap`, `restart` and `all`.
- `dev/fake_state.json` is reloaded while the dashboard runs: edit it to stop services, make devices
  unreachable, mark ROS topics `low_rate` / `stale` / `missing`, or change the `aio_nav` PID to
  simulate a restart.
- `config/dashboard.dev.yaml` is the development settings file. Set `network.sensor_interface` to a
  real network interface of your machine.
- Templates are cached: restart the dashboard after editing them (`tools/dev.sh start`).

Installing on a device: [Installation Guide](docs/INSTALL.md).

## Repository layout

```
aio_system_dashboard/   the dashboard (core, vehicle/ output layer)
aio_dashboard_replay/   optional module: Live / Bag replay and bag player
aio_sysmon/             optional system recorder
config/                 shipped settings (dashboard.yaml, dev and demo variants, sysmon.yaml)
deploy/                 installer, uninstaller, systemd units, the aio-dashboard command
tools/                  development and test tools (fake sender, release build, vehicle gate)
tests/                  unit tests, including checks of the documents against the code
docs/                   the guides; docs/dev/ for developers
wheels/                 the Python packages for offline installs (aarch64, Python 3.10)
```

How the code is built and the rules between its parts: [Architecture](docs/dev/ARCHITECTURE.md).
