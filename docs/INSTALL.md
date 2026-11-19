# AIO Navigation — Installation Guide

This guide is for the people who **set up** AIO navigation devices: install the dashboard, choose
its optional parts, prepare a new device, update and remove it. Every individual setting is in the
[Configuration Reference](CONFIG.md); using and maintaining the device are in the
[User Guide](USER_GUIDE.md) and the [Maintenance Guide](MAINTENANCE.md).

Contents:

1. [What you need](#1-what-you-need)
2. [Installing](#2-installing)
3. [Optional parts](#3-optional-parts)
4. [First-time setup](#4-first-time-setup)
5. [What the installer changes on the system](#5-what-the-installer-changes-on-the-system)
6. [Updating and removing](#6-updating-and-removing)
7. [Security](#7-security)

---

## 1. What you need

| Item | Requirement |
|------|-------------|
| Device | NVIDIA Jetson (Orin NX / AGX), aarch64 |
| System | Ubuntu 22.04 with Python 3.10 |
| ROS 2 | Humble, in `/opt/ros/humble` |
| AIO NAV | aio-nav-ros installed for the service user (the installer finds its `install/` folder) |
| Sensor drivers | The OpenRTK330 / camera drivers as a systemd user unit |
| Optional | `chrony` (system clock from GNSS time); NetworkManager (Wi-Fi); `dnsmasq` and `nft` or `iptables` (hotspot) |

No Internet is needed on the device: the Python packages the dashboard needs come with it
(`wheels/`).

## 2. Installing

There are two ways. Both install into `/opt/aio-dashboard` and start the service
`aio-dashboard.service`.

### 2.1 Release package (recommended for devices)

A release package contains no source code. Install it on the device:

```bash
tar xzf aio-dashboard-*.tar.gz && cd aio-dashboard-*/
sudo ./install.sh --user jetson
cd .. && rm -rf aio-dashboard-*          # nothing with source code is left on the device
```

`--user` is the account that runs ROS and AIO NAV (default: the user running `sudo`).

**Building a release package** (once, on a build machine of the same kind: an Orin with the same
Python version):

```bash
sudo apt install -y gcc python3-dev && python3 -m pip install --user nuitka   # build machine only
tools/build_release.sh                   # -> dist/aio-dashboard-<version>-aarch64-py3.10.tar.gz
tools/build_release.sh --with-replay     # the same, with the replay part
```

The program is compiled into one native module; the web page files stay readable, because browsers
download them anyway. The system recorder (`sysmon`) is not in release packages: install it from a
source checkout.

### 2.2 From a source checkout

```bash
cd ~/web_monitor
sudo deploy/install.sh --user jetson
```

Installing a release over a source install removes the old source files from `/opt/aio-dashboard`.

### 2.3 Installer options

The first run shows a menu of the [optional parts](#3-optional-parts): type their numbers to switch
them on or off, Enter to continue, `q` to quit without changes. Later runs install the same parts
without asking.

| Option | Does |
|--------|------|
| `--user U` | The service user (default: the user running `sudo`) |
| `--choose` | Show the menu of optional parts again |
| `--with-<part>` / `--without-<part>` | Switch a part on or off without the menu (`sysmon`, `replay`, `wifi`, `hotspot`) |
| `--list` | What is installed and chosen (no `sudo` needed) |
| `--dry-run` | What an install would add or remove, then stop (no `sudo` needed) |
| `--yes`, `-y` | Never ask (for scripts) |
| `--reset-config` | Replace the installed settings with the shipped ones (the old file is kept as `.bak`) |
| `--prefix P` | Install folder (default `/opt/aio-dashboard`) |
| `--with-aio-nav PATH` | Also install `aio-nav.service` for AIO NAV (it is not enabled at boot while the dashboard starts AIO NAV itself) |

Without a terminal (a script), the first run keeps the parts already on the machine (none on a fresh
install): use `--with-<part>`.

## 3. Optional parts

Features that not every device needs. They are not installed by default, and the dashboard works
without them.

| Part | Adds | Switched off in the settings by | Release package |
|------|------|---------------------------------|-----------------|
| `replay` | Live / Bag replay switch and the bag player (Maintenance → Replay) | `modules.replay.enabled: false` | Only if built with `--with-replay` |
| `sysmon` | The system recorder: a black box for freezes and power cuts ([SYSMON.md](SYSMON.md)) | — (it is its own service) | Never: install from source |
| `wifi` | The right to change Wi-Fi from the pages (Settings, Maintenance → Network) | `wifi.enabled: false` hides the Wi-Fi panel | Always included |
| `hotspot` | The device's Wi-Fi as a closed network for phones and tablets, with copies of the navigation packets. Needs `wifi`, `dnsmasq`, `nft` or `iptables` | `wifi.hotspot.enabled: false` | Always included |

Without the `wifi` part the Wi-Fi panel is still shown, but changes are refused.

Which parts for which device:

| Device | Suggested parts |
|--------|-----------------|
| Delivered to a customer | `wifi`, `hotspot` (first connection in the field), `sysmon` |
| Test or development device | all |

A part switched off is removed on the next run (the recorder's records in `/var/log/aio-sysmon` are
kept; switching off `hotspot` deletes the hotspot profile).

## 4. First-time setup

The shipped settings work without edits: most values are detected at start-up. On a new device:

1. **Set the maintenance password.** There is no default; until it is set, the Maintenance view
   stays locked.
   ```bash
   aio-dashboard password
   ```
2. **Allow the computers that may open the pages.** `access.allowed_clients` in
   `/opt/aio-dashboard/config/dashboard.yaml` (IPs or networks; the device itself is always
   allowed). Check the address of the camera in `devices.camera.host` too; add a LiDAR if fitted
   ([Configuration Reference, common changes](CONFIG.md#5-common-tasks)). Then:
   ```bash
   aio-dashboard check && aio-dashboard restart
   ```
3. **Open the pages** (`aio-dashboard urls` prints the addresses). The status should go
   **Starting** → **Initializing** by itself.
4. **Check what was detected.** The Environment card of the Settings page (network, RTK, vehicle),
   and on the Maintenance view: Network (the `aio_nav.yaml` found, the interfaces), ROS 2 (the
   topics picked), GNSS / RTK (the RTK corrections).
5. **Measure the lever arms** and enter them on the Setup page ([User Guide](USER_GUIDE.md#44-setup-lever-arms)).
6. **Measure the still limits** on the vehicle (Maintenance → Still detection,
   [Maintenance Guide](MAINTENANCE.md#4-still-detection)).
7. **Set the vehicle address** if a vehicle is connected: `output_udp` on Maintenance → AIO NAV
   config ([Integration Guide](INTEGRATION.md#22-setting-the-destination)).
8. **Do one run**: align, drive, check that the Recording step is green, download a log file.

What the dashboard detects by itself (each can be pinned in the settings):

| Setting | Detected as |
|---------|-------------|
| `nav.aio_nav_config` | The newest `aio_nav.yaml` under `/home/*/aio-nav-ros/install/…` or `~/.local/opt/aio-nav-ros/…` (or `$AIO_NAV_CONFIG`) |
| `data.roots` | The folder of an absolute `fusion_txt_path`, else `output/` next to aio-nav-ros `install/` |
| `network.sensor_interface` | The interface that routes to the sensors, else the default-route interface |
| ROS topics, Live view topics | The first topic of the right message type |
| The environment | Network, RTK and vehicle ([User Guide](USER_GUIDE.md#33-the-environment)) |

Empty optional settings stay quiet: a device without a `host` shows *Not set up* and is never
pinged; a driver unit that does not exist shows *Not installed*.

## 5. What the installer changes on the system

Besides `/opt/aio-dashboard` and its service, the installer makes the device run unattended:

| Change | Where | Why |
|--------|-------|-----|
| The dashboard service | `aio-dashboard.service`, runs as the service user with ROS 2 sourced | Starts at power-on |
| systemd watchdog | `WatchdogSec=60`, `Restart=always` | A dashboard that stops answering for 60 s is restarted; AIO NAV keeps running |
| Survives an out-of-memory kill | `OOMPolicy=continue` | When the kernel kills AIO NAV or DSO, the dashboard brings them back |
| System clock from GNSS time | `/etc/chrony/conf.d/aio-gnss.conf` (only if chrony is installed) | The device keeps no time over a power-off and has no NTP in the field |
| Reboot 10 s after a kernel panic | `/etc/sysctl.d/90-aio-unattended.conf` | Instead of waiting for the hardware watchdog |
| Lingering for the service user | `loginctl enable-linger` | The sensor drivers (a user unit) can start at power-on with nobody logged in |
| Restart permission | `/etc/sudoers.d/aio-dashboard` | Only `systemctl restart` of the units marked `restartable` |
| Wi-Fi / hotspot permission (`wifi`, `hotspot`) | polkit rule `50-aio-dashboard-wifi` | The service user may manage NetworkManager |
| Closed hotspot (`hotspot`) | `/etc/NetworkManager/dispatcher.d/50-aio-hotspot` | Nothing is forwarded to or from the hotspot |
| System journal kept on disk (`sysmon`) | `/etc/systemd/journald.conf.d/aio-persistent.conf` (300 MB) | Kernel messages survive a freeze or a power cut |
| The `aio-dashboard` command | `/usr/local/bin/aio-dashboard` | Day-to-day control ([Maintenance Guide](MAINTENANCE.md#8-the-aio-dashboard-command)) |

If `restartable` or `unit` under `services` changes later, run the installer again: the restart
permission is generated from the installed settings.

## 6. Updating and removing

**Updating.** Install the new release package (or `git pull` and run `deploy/install.sh` again) the
same way. Your settings are kept; the newest shipped settings file is written next to them as
`dashboard.yaml.new` for comparison. `--reset-config` replaces your settings with the shipped ones
(the old file is kept as `dashboard.yaml.bak`).

**Removing.**

```bash
sudo /opt/aio-dashboard/uninstall.sh
```

It first saves `config/` and `state/` to `~/aio-dashboard-backup-<time>` (`--backup-dir` to choose
another folder), then removes the service, the files of [section 5](#5-what-the-installer-changes-on-the-system),
the hotspot profile and `/opt/aio-dashboard`. It leaves the recorder's records, the persistent journal
setting and lingering.

## 7. Security

The device is meant for a **closed sensor network**. Within it:

- **The pages.** The user pages (`:8080`) need no password. They can change only: Start / Stop /
  Resume, the lever arms on Setup, Wi-Fi (only from a computer connected by cable) and the
  environment (saying *No RTK here* needs a confirmation). Everything else is on the Maintenance view
  (`:8081`), which needs the password. Each port serves only its own pages.
- **Who may connect.** Keep `access.allowed_clients` to the computers that need it.
- **The password.** Stored as a salted hash (no default). Sessions end after 15 minutes without use;
  repeated wrong passwords lock out briefly. `maintenance.lock: tools` lets anyone view the
  Maintenance pages and asks for the password only for changes.
- **The terminal** on the Maintenance view is a real shell of the service user. Choose a strong
  password.
- **No commands from the network.** Apart from the terminal, the pages send only fixed action names
  for targets listed in the settings; downloads are limited to the data folders, with no upload,
  delete or rename.
- **Everything is logged**: sign-ins, restarts, saved settings, terminals, environment changes
  (Maintenance → Diagnostics, `logs/events.jsonl`).
- **The link to the vehicle** has no authentication: keep it private
  ([Integration Guide](INTEGRATION.md#8-security-of-the-link)).
