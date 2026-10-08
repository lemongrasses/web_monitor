#!/bin/sh
# Target survey: what this machine offers to the dashboard and the recorder, so we can decide how to
# build for it (Python or C/C++, static or not, which sensors exist).
#
#   sh target_survey.sh            (no root needed; with sudo a few more items can be read)
#
# Read-only, offline (no network access), plain POSIX sh so it also runs on BusyBox/Yocto systems.
# It writes the report next to itself as target-<model>-<date>.txt and prints it. It does NOT record
# the host name, IP or MAC addresses, serial numbers or user names.
LC_ALL=C; export LC_ALL
VERSION=1

have() { command -v "$1" >/dev/null 2>&1; }
first() { "$@" 2>&1 | head -n 1; }
sec() { printf '\n## %s\n' "$1"; }
kv() { v=$(printf '%s' "$2" | tr '\n' ' ' | sed 's/ *$//'); printf '%-22s %s\n' "$1:" "${v:--}"; }
yesno() { if [ -e "$1" ]; then echo yes; else echo no; fi; }
readable() { if [ -r "$1" ] && head -c 1 "$1" >/dev/null 2>&1; then echo yes; elif [ -e "$1" ]; then echo "exists, not readable"; else echo no; fi; }

model=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null)
[ -n "$model" ] || model=$(grep -m1 -i -E '^(model name|Hardware)' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^ *//')
[ -n "$model" ] || model=unknown
slug=$(printf '%s' "$model" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9' '-' | sed 's/--*/-/g; s/^-//; s/-$//' | cut -c1-40)
out="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/target-${slug:-unknown}-$(date +%Y%m%d-%H%M).txt"
[ -w "$(dirname "$out")" ] || out="./target-${slug:-unknown}-$(date +%Y%m%d-%H%M).txt"

survey() {
echo "# Target survey v$VERSION  $(date '+%Y-%m-%d %H:%M')  (run as: $( [ "$(id -u)" = 0 ] && echo root || echo normal user))"

sec "Board"
kv model "$model"
kv compatible "$(tr '\0' ' ' < /proc/device-tree/compatible 2>/dev/null)"
[ -f /etc/nv_tegra_release ] && kv "L4T (Jetson)" "$(head -n1 /etc/nv_tegra_release)"
have dpkg-query && kv "JetPack package" "$(dpkg-query -W -f='${Version}' nvidia-jetpack 2>/dev/null)"
kv "SoC / CPU part" "$(grep -m1 -E 'CPU part' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^ *//')"
kv cores "$(grep -c ^processor /proc/cpuinfo 2>/dev/null)"
kv memory "$(awk '/^MemTotal/{printf "%.0f MB", $2/1024}' /proc/meminfo)"
kv swap "$(awk '/^SwapTotal/{printf "%.0f MB", $2/1024}' /proc/meminfo)"
kv "root disk" "$(df -h / 2>/dev/null | awk 'NR==2{print $2" total, "$4" free"}')  fs=$(awk '$2=="/"{print $3}' /proc/mounts | tail -n1)"

sec "Operating system"
if [ -r /etc/os-release ]; then . /etc/os-release; kv os "${PRETTY_NAME:-$NAME $VERSION_ID}"; kv "os id" "${ID:-?} ${VERSION_ID:-?}"; fi
[ -r /etc/version ] && kv "yocto /etc/version" "$(head -n1 /etc/version)"
kv architecture "$(uname -m)  ($(getconf LONG_BIT 2>/dev/null)-bit userland)"
kv kernel "$(uname -r)"
kv "kernel build" "$(uname -v)"
kv "page size" "$(getconf PAGESIZE 2>/dev/null)"

sec "C library and C++ runtime (decides how C/C++ binaries must be built)"
if ls /lib/ld-musl-* >/dev/null 2>&1; then kv libc "musl ($(ls /lib/ld-musl-* | head -n1))"
else kv libc "$(getconf GNU_LIBC_VERSION 2>/dev/null || first ldd --version)"; fi
kv "dynamic loader" "$(ls /lib/ld-linux-aarch64.so.1 /lib64/ld-linux-x86-64.so.2 /lib/ld-linux-armhf.so.3 2>/dev/null | head -n1)"
cxx=$(ls /usr/lib/aarch64-linux-gnu/libstdc++.so.6 /usr/lib/libstdc++.so.6 /lib/libstdc++.so.6 /usr/lib64/libstdc++.so.6 2>/dev/null | head -n1)
if [ -n "$cxx" ]; then
  kv libstdc++ "$(readlink -f "$cxx")"
  kv "newest GLIBCXX" "$(grep -ao 'GLIBCXX_[0-9][0-9.]*' "$cxx" 2>/dev/null | sort -u -t. -k1,1 -k2,2n -k3,3n -k4,4n | tail -n1)"
else kv libstdc++ "not found"; fi
kv "static libc (libc.a)" "$(ls /usr/lib/aarch64-linux-gnu/libc.a /usr/lib/libc.a 2>/dev/null | head -n1 || true)"
kv "ulimit -l (mlock)" "$(ulimit -l 2>/dev/null)"

sec "Build tools on this machine"
for t in gcc g++ cc c++ clang make cmake ninja pkg-config strip; do
  if have "$t"; then kv "$t" "$(first "$t" --version)"; else kv "$t" "-"; fi
done

sec "Init and service manager"
kv "pid 1" "$(cat /proc/1/comm 2>/dev/null)"
have systemctl && kv systemd "$(first systemctl --version)"
for t in systemd-run journalctl chrt taskset ionice; do kv "$t" "$(have "$t" && echo yes || echo -)"; done
kv busybox "$(have busybox && first busybox | cut -c1-60 || echo -)"
kv "persistent journal" "$(yesno /var/log/journal)"

sec "Python"
for p in python3 python3.8 python3.10 python3.12 python; do
  if have "$p"; then kv "$p" "$(first "$p" --version) at $(command -v "$p")"; fi
done
if have python3; then
  for m in yaml flask werkzeug jinja2 numpy ssl ctypes sqlite3 rclpy; do
    r=$(python3 -c "import $m; print(getattr($m,'__version__','ok'))" 2>/dev/null | tail -n1)
    kv "  module $m" "${r:--}"
  done
  for d in /opt/aio-dashboard/lib; do [ -d "$d/flask" ] && kv "  dashboard bundle" "flask etc. bundled in $d"; done
  kv "pip" "$(python3 -m pip --version 2>/dev/null | cut -d' ' -f1-2 || echo -)"
else kv python3 "not installed"; fi

sec "ROS"
kv "/opt/ros" "$(ls /opt/ros 2>/dev/null | tr '\n' ' ')"
kv ros2 "$(have ros2 && echo "on PATH" || echo "not on PATH (ROS not sourced in this shell)")"
kv "ROS_DISTRO (env)" "${ROS_DISTRO:--}"
for d in /opt/ros/*; do
  [ -d "$d/lib" ] || continue
  kv "  $(basename "$d") rclpy" "$(ls -d "$d"/lib/python3*/site-packages/rclpy "$d"/local/lib/python3*/dist-packages/rclpy 2>/dev/null | head -n1)"
  kv "  $(basename "$d") rclcpp" "$(ls "$d"/lib/librclcpp.so 2>/dev/null)"
  kv "  $(basename "$d") middleware" "$(ls "$d"/lib 2>/dev/null | grep -E '^librmw_(fastrtps|cyclonedds|connextdds)_cpp\.so$' | tr '\n' ' ')"
done

sec "What the recorder can read"
kv "/proc/stat" "$(readable /proc/stat)"
kv "/proc/diskstats" "$(readable /proc/diskstats)"
kv "/proc/vmstat" "$(readable /proc/vmstat)"
kv "/proc/pressure (PSI)" "$(readable /proc/pressure/cpu)"
kv "/proc/self/smaps_rollup" "$(readable /proc/self/smaps_rollup)"
kv "/dev/kmsg" "$(readable /dev/kmsg)"
kv "/proc/config.gz" "$(readable /proc/config.gz)"
if [ -r /proc/config.gz ] && have zcat; then
  for o in CONFIG_PSI CONFIG_RT_GROUP_SCHED CONFIG_PREEMPT CONFIG_PREEMPT_RT CONFIG_CGROUP_SCHED CONFIG_TASK_IO_ACCOUNTING CONFIG_MAGIC_SYSRQ; do
    v=$(zcat /proc/config.gz | grep -E "^$o=|^# $o is not set" | head -n1)
    kv "  $o" "${v:-(not in this kernel)}"
  done
fi
kv "rt runtime" "$(cat /proc/sys/kernel/sched_rt_runtime_us 2>/dev/null) us per $(cat /proc/sys/kernel/sched_rt_period_us 2>/dev/null) us"
kv "cgroup" "$(stat -fc %T /sys/fs/cgroup 2>/dev/null) ($(grep -c . /proc/self/cgroup 2>/dev/null) line(s) in /proc/self/cgroup)"
kv "hardware watchdog" "$(ls /dev/watchdog* 2>/dev/null | tr '\n' ' ')$(cat /sys/class/watchdog/watchdog0/timeout 2>/dev/null | sed 's/$/ s timeout/')"
echo "thermal zones:"
for z in /sys/class/thermal/thermal_zone*; do
  [ -d "$z" ] || continue
  t=$(cat "$z/temp" 2>/dev/null); trips=""
  for tp in "$z"/trip_point_*_temp; do [ -r "$tp" ] && trips="$trips $(( $(cat "$tp") / 1000 ))"; done
  printf '  %-14s %-18s now=%s C  trips:%s\n' "$(basename "$z")" "$(cat "$z/type" 2>/dev/null)" "$([ -n "$t" ] && echo $((t/1000)) || echo ?)" "$trips"
done
echo "hwmon sensors (power rails, voltages):"
for h in /sys/class/hwmon/hwmon*; do
  [ -d "$h" ] || continue
  printf '  %-8s %-16s %s\n' "$(basename "$h")" "$(cat "$h/name" 2>/dev/null)" "$(ls "$h" 2>/dev/null | grep -E '^(in|curr|power|temp)[0-9]+_(input|label)$' | tr '\n' ' ' | cut -c1-90)"
done
kv "cpufreq governor" "$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)"
kv "devfreq (GPU etc.)" "$(ls /sys/class/devfreq 2>/dev/null | tr '\n' ' ')"
kv "Jetson GPU load" "$(ls /sys/devices/platform/gpu.0/load /sys/devices/gpu.0/load 2>/dev/null | head -n1)"
echo "block devices (I/O scheduler):"
for b in /sys/block/*; do
  n=$(basename "$b"); case "$n" in loop*|ram*|zram*) continue;; esac
  printf '  %-12s %8s  %s\n' "$n" "$(awk '{printf "%.0f GB", $1*512/1e9}' "$b/size" 2>/dev/null)" "$(cat "$b/queue/scheduler" 2>/dev/null)"
done

sec "Network (interface names only)"
kv interfaces "$(ls /sys/class/net 2>/dev/null | tr '\n' ' ')"
for t in ip ss netstat curl wget; do kv "$t" "$(have "$t" && echo yes || echo -)"; done

sec "Archive tools (offline install)"
for t in tar gzip xz unzip dpkg rpm opkg; do kv "$t" "$(have "$t" && echo yes || echo -)"; done
}

survey 2>/dev/null | tee "$out"
echo
echo "saved: $out"
