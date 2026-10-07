# AIO System Recorder — a black box for the computer

[繁體中文版](SYSMON.zh-TW.md)

When the computer freezes, becomes very slow, or loses power, the evidence normally disappears
with it: the kernel's log lives in memory, and the system log only says what each program
printed. The system recorder (`aio-sysmon`) keeps a small, power-loss-safe record of how the
machine was doing, so afterwards you can answer *what was happening in the last minutes, and
why did it stop?*

It is separate from the dashboard (it keeps recording if the dashboard hangs), uses the Python
standard library only, and has a fixed disk budget so it is never part of the problem.

## 1. What is recorded, and when

Detail is spent only when it is useful.

| Level | What | How often | Disk use |
|-------|------|-----------|----------|
| **Ring** | One light record per second: CPU busy and I/O wait, load, free memory, swap, hottest temperature, tasks stuck on the disk, how late the recorder woke up (lag), time of the last disk write, disk busy %, input voltage, free disk space | every 1 s, flushed to disk every 5 s | **fixed 4 MB** (about 2 hours; it overwrites itself) |
| **Samples** | A full sample: per-core CPU, memory split, swap activity, disk throughput and request time, network per interface, every temperature, power rails (voltage and current), CPU/GPU frequency and load; with the top processes once a minute | every 5 s (every 1 s for a minute after a trigger) | about 15 MB/day, **about 2 MB/day compressed** |
| **Events** | Anomalies, kernel warnings and errors (out of memory, disk timeouts, thermal throttling, GPU errors, blocked tasks), starts and stops, clock jumps, gaps in the recorder itself | when it happens | a few KB/day |
| **Snapshots** | Everything at the moment of an anomaly: the last 2 minutes second by second, the top 12 processes by CPU and memory, tasks stuck on the disk (with their kernel stack), all temperatures and power rails, the last 40 kernel messages | only on a trigger; at most 120/day, 5–25 KB each | capped (see 2) |

**What makes it record detail (triggers).** The limits are *derived from this machine*, not
fixed numbers, and a condition has to hold for a few samples so a blip is ignored:

| Trigger | Rule | On this machine | Derived from |
|---------|------|-----------------|--------------|
| `load` | load average above the limit for 10 s | 12 | 1.5 × number of cores (8) |
| `cpu_saturated` | CPU busy above 92% for 30 s | 92% | |
| `iowait` | more than 25% of CPU time waiting for the disk, 3 s | 25% | |
| `memory_low` | available memory below the limit | 1252 MB | 8% of RAM, at least 512 MB |
| `swap_high` | swap more than 25% used | 25% | only if the machine has swap |
| `swapping` | more than 2000 pages/s swapped out | 2000 | |
| `hot` | hottest temperature above the limit | 85 °C | 10 °C below the first hot trip point (95 °C) |
| `disk_wait_tasks` | 4 or more tasks stuck waiting for the disk, 3 s | 4 | |
| `stall` | the recorder's 100 ms sleep woke up more than 800 ms late | 800 ms | measures lag directly |
| `slow_disk_write` | writing and flushing a sample took more than 1 s | 1000 ms | |
| `disk_space` | free disk space below the limit (once an hour) | 6850 MB | 5% of the disk, at least 2 GB |
| `power_dip` | input voltage 12% below its own 10-minute median | relative | works for any supply voltage |

After a trigger the same kind stays quiet for 2 minutes, so a long problem gives a few snapshots,
not thousands. `stall` is the one to watch for "the computer is very slow": it fires when the
machine could not even run a tiny timer on time.

## 2. Capacity: it limits itself

Measured on this machine (Jetson Orin NX, 8 cores, 15 GB RAM, 134 GB disk):

| | |
|---|---|
| CPU of the recorder | about **0.9% of one core** (0.1% of the machine) |
| Memory | about 15 MB (the service is capped at 96 MB and 15% of a core) |
| Normal day | about 15 MB of samples, about 2 MB after compression |
| Steady state with 30 days kept | roughly **85 MB**: today's file, 29 compressed days, the 4 MB ring |
| Hard cap for everything | **250 MB** (0.3% of the disk, between 60 and 250 MB; smaller disks get a smaller cap) |
| Disk writes | about 250 MB/day at the flash level, negligible for an NVMe drive |

What is given up first when the cap is reached: old snapshots (they may use at most 40% of the
cap), then the oldest daily samples, then the oldest events. Files older than 30 days are deleted.
Yesterday's samples are compressed (about 7×).

**Disk guard.** The recorder watches the free space and degrades before the disk is full:

| Free space | Behaviour |
|------------|-----------|
| above 3% of the disk (here 4.1 GB) | everything |
| below that | no snapshots and no detailed sampling; the ring, samples and events continue |
| below 512 MB | only the ring (it is already allocated, so it needs no space), tiny events and one small status file; samples stop |

**Adaptive detail.** After a trigger it samples every second for a minute, but only while the
disk is in the first level. A message storm from the kernel is capped at 120 lines a minute.

## 3. After a freeze, a very slow period, or a power cut

```bash
aio-sysmon last-crash
```

shows how the previous run ended. If the machine stopped without a clean shutdown, it prints a
verdict, the readings of the last minute, the kernel messages just before the end, and the last
records second by second, for example:

```
verdict:  the machine stopped without a clean shutdown; the input voltage fell in the last minute
last record before the stop: 2026-10-07 02:41:17
last minute: temp_max_c=52.0, load1_max=3.1, mem_avail_min_mb=11800, vin_min_mv=15200, vin_max_mv=19150, ...
```

The possible hints, in the order they are checked: the **input voltage fell** (power supply,
cable, brown-out), **overheating**, **memory running out**, **the disk stalling**, **the system
stalling just before the end**, and otherwise *readings were normal right up to the last record*,
which means a sudden power loss, a hard reset, or a hang that gave no warning (the kernel messages
and snapshots are then the place to look). A hang that the hardware watchdog turned into a reset
(2 minutes on this machine) shows up the same way.

For a slow, not frozen, computer:

```bash
aio-sysmon report --hours 3      # hour by hour, plus the notable events
aio-sysmon events --kind anomaly # when it was triggered and why
aio-sysmon snapshots             # list the detailed snapshots
aio-sysmon snapshots stall       # open the newest one with "stall" in its name
```

A snapshot answers *who was using the machine at that moment*: the top processes by CPU and
memory, tasks stuck on the disk, temperatures, the power rails and the kernel messages.

All commands:

| Command | Shows |
|---------|-------|
| `aio-sysmon status` | whether it is running, space used, the latest reading |
| `aio-sysmon last-crash` | the verdict for the last unclean stop and the last records before it |
| `aio-sysmon report [--hours N] [--since "YYYY-MM-DD HH:MM"]` | per-hour summary and notable events |
| `aio-sysmon ring [--rows N]` | the newest second-by-second records |
| `aio-sysmon events [--hours N] [--kind KIND]` | events (`anomaly`, `kernel`, `unclean_stop`, `tick_gap`, `clock_step`, `start`, `stop`) |
| `aio-sysmon snapshots [NAME]` | list snapshots, or print one |

The files are plain text and gzip in `/var/log/aio-sysmon/`: `ring.bin` (fixed ring),
`samples/` (daily JSON lines), `events.jsonl`, `snapshots/`, `last_known.json`, `run.json`.

## 3b. Staying alive when the machine struggles

Normally the recorder is a quiet ordinary process (slightly favoured: nice -5, hard to OOM-kill,
its memory pinned in RAM so memory pressure cannot swap it out; about 11 MB).
Only when a trigger shows the machine has **stopped responding** (`stall`, `iowait`,
`disk_wait_tasks`, `memory_low`, `swapping`, `swap_high`, `hot`, `slow_disk_write`) does it raise
itself to a low real-time priority for 60 s, so its detailed sampling is not starved, and then it drops back (event
`priority`). A machine that is merely busy (`load`, `cpu_saturated`) does not trigger this. It
never pauses or touches other programs. Guards: the priority is far below the kernel's own threads;
a watchdog thread drops it if the loop stops progressing; the service sets `LimitRTTIME`, so the
kernel itself kills a real-time task that computes 1 s without sleeping (systemd restarts it).
If the machine is too stuck to run the recorder at all, the ring and `last-crash` are what remain.

## 4. Settings

`/opt/aio-dashboard/config/sysmon.yaml` (restart with `sudo systemctl restart aio-sysmon`).
Everything is optional. The most useful keys:

| Key | Default | Meaning |
|-----|---------|---------|
| `interval_s` | `5` | a full sample every this many seconds |
| `ring_mb` | `4` | size of the second-by-second ring (about 2 hours per 4 MB) |
| `max_total_mb` | derived | hard cap for the whole directory |
| `retention_days` | `30` | how long compressed days are kept |
| `min_free_mb`, `critical_free_mb` | derived, `512` | the disk guard levels above |
| `burst_s`, `burst_interval_s` | `60`, `1` | detailed sampling after a trigger |
| `triggers` | `{}` | override any limit, e.g. `{iowait_pct: 15, temp_c: 80}` |
| `lock_memory` | `true` | keep the recorder in RAM |
| `escalate`, `escalate_s`, `escalate_priority` | `true`, `60`, `10` | the temporary real-time raise above |
| `kmsg` | `true` | copy kernel warnings and errors (needs root) |

## 5. What it cannot see

- **Per-process disk use.** This kernel does not provide it. Disk problems are shown by the whole
  disk's busy %, request time, the time of the recorder's own disk write, and the tasks stuck in
  disk wait.
- **The last few seconds before a power cut.** The ring is flushed every 5 s, so up to 5 s of
  records can be lost; the samples are flushed on every write.
- **A freeze with healthy numbers.** If the readings were normal until the last record and the
  kernel logged nothing, the verdict says so honestly: it points to power or a hardware or kernel
  fault, which the recorder cannot tell apart.
- **Clock problems.** If the machine has no battery-backed clock, its time can be wrong until it
  is set. Every record also carries the uptime, and a jump of the clock is logged as `clock_step`.
- **Release packages.** The compiled release made by `tools/build_release.sh` does not include the
  recorder yet; it is installed from a source checkout.

## 6. Install, permissions and removal

`sudo deploy/install.sh` installs it with the dashboard (`--no-sysmon` skips it). It also keeps the
system journal on disk (`/etc/systemd/journald.conf.d/aio-persistent.conf`, 300 MB), so the
kernel's own log survives a reboot too.

The service runs as root to read kernel messages and the stacks of stuck tasks, but it is
sandboxed: the system is read-only to it, home directories are hidden, and only
`/var/log/aio-sysmon` is writable. Snapshots contain command lines of processes, so they are
readable by root and the `adm` group only.

`sudo deploy/uninstall.sh` removes the service but keeps the records (they are the evidence).
Delete `/var/log/aio-sysmon` yourself to remove them.
