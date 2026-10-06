#!/usr/bin/env bash
# Remove the AIO System Dashboard from this device (the reverse of install.sh).
#
#   sudo deploy/uninstall.sh [--prefix /opt/aio-dashboard] [--backup-dir DIR]
#
# Removes aio-dashboard.service, aio-nav.service (the optional unit install.sh can add), the
# sudoers rule, the `aio-dashboard` command and PREFIX. The installed config is copied to
# DIR (default: ~/aio-dashboard-backup-<time>) first.
# The recorder's records (/var/log/aio-sysmon) and the persistent-journal setting are kept: they
# are the evidence about past freezes. Delete them yourself if you want them gone.
# Not touched: network services, sensor drivers, aio-nav-ros, and AIO NAV / DSO processes that
# were started from the dashboard (they run in their own sessions; stop them from the Overview page).
set -euo pipefail

PREFIX=/opt/aio-dashboard
RUN_USER="${SUDO_USER:-root}"
BACKUP_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --backup-dir) BACKUP_DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
HOME_DIR="$(getent passwd "$RUN_USER" | cut -d: -f6)"
[[ -n "$BACKUP_DIR" ]] || BACKUP_DIR="$HOME_DIR/aio-dashboard-backup-$(date +%Y%m%d-%H%M%S)"

if [[ -d "$PREFIX/config" ]]; then
  mkdir -p "$BACKUP_DIR"
  cp -a "$PREFIX/config/." "$BACKUP_DIR/"
  [[ -d "$PREFIX/state" ]] && cp -a "$PREFIX/state" "$BACKUP_DIR/state"
  chown -R "$RUN_USER": "$BACKUP_DIR"
  echo "==> config saved to $BACKUP_DIR"
fi

for unit in aio-dashboard.service aio-nav.service aio-sysmon.service; do
  if [[ -f "/etc/systemd/system/$unit" ]]; then
    echo "==> removing $unit"
    systemctl stop "$unit" 2>/dev/null || true
    systemctl disable "$unit" 2>/dev/null || true
    rm -f "/etc/systemd/system/$unit"
  fi
done
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

rm -f /etc/sudoers.d/aio-dashboard
[[ -L /usr/local/bin/aio-dashboard ]] && rm -f /usr/local/bin/aio-dashboard
[[ -L /usr/local/bin/aio-sysmon ]] && rm -f /usr/local/bin/aio-sysmon
if [[ -d "$PREFIX" ]]; then
  echo "==> removing $PREFIX"
  rm -rf "$PREFIX"
fi
echo "Done. Install again with: sudo deploy/install.sh --user $RUN_USER"
