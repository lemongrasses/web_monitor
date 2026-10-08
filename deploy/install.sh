#!/usr/bin/env bash
# Install the AIO System Dashboard on the Jetson (offline).
# Works from a source checkout (deploy/install.sh) or from a compiled release package
# made by tools/build_release.sh (./install.sh), which contains no Python source.
#
#   sudo deploy/install.sh [--user nvidia] [--prefix /opt/aio-dashboard] [--with-aio-nav PATH_TO_aio-nav]
#                          [--reset-config] [--with-sysmon]
#
# - copies the app to PREFIX (default /opt/aio-dashboard), keeping an existing config
#   (--reset-config replaces it with the default; the old one is saved as .bak)
# - installs bundled wheels (wheels/, aarch64 cp310) into PREFIX/lib, with pip if
#   available, otherwise by unpacking the wheels (no Internet, no venv needed)
# - installs aio-dashboard.service and a sudoers rule that allows ONLY
#   `systemctl restart <unit>` for units marked `restartable: true` in the config
# - installs the `aio-dashboard` command (start/stop/status/logs/config)
# - optionally installs aio-nav.service (--with-aio-nav); not needed for the Overview page,
#   which starts AIO NAV and DSO itself, like the AIO Nav desktop app
# - optional, off by default: --with-sysmon installs aio-sysmon.service, the computer's black box
#   (docs/SYSMON.md), and keeps the system journal across reboots. If it is already installed,
#   re-running the installer updates it even without the flag.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# release package: install.sh sits next to wheels/; source checkout: deploy/install.sh
if [[ -d "$HERE/wheels" ]]; then SRC="$HERE"; else SRC="$(cd "$HERE/.." && pwd)"; fi
PREFIX=/opt/aio-dashboard
RUN_USER="${SUDO_USER:-nvidia}"
AIO_NAV_BIN=""
RESET_CONFIG=0
WITH_SYSMON=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) RUN_USER="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --with-aio-nav) AIO_NAV_BIN="$2"; shift 2 ;;
    --reset-config) RESET_CONFIG=1; shift ;;
    --with-sysmon) WITH_SYSMON=1; shift ;;
    --no-sysmon) shift ;;                        # old flag: the recorder is off by default now
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
if [[ $WITH_SYSMON -eq 0 && -f /etc/systemd/system/aio-sysmon.service && -d "$SRC/aio_sysmon" ]]; then
  echo "==> the system recorder is already installed: updating it (use deploy/uninstall.sh to remove)"
  WITH_SYSMON=1
fi
id "$RUN_USER" >/dev/null 2>&1 || { echo "user $RUN_USER does not exist" >&2; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3, 8)' || { echo "python3 >= 3.8 required" >&2; exit 1; }

echo "==> installing to $PREFIX (service user: $RUN_USER)"
mkdir -p "$PREFIX"/{config,logs,lib,dev,bin,docs,state}
# Replace the app completely, so switching from source to a compiled release leaves no .py behind.
rm -rf "$PREFIX/aio_system_dashboard" "$PREFIX"/aio_system_dashboard*.so
shopt -s nullglob
COMPILED=("$SRC"/aio_system_dashboard*.so)
shopt -u nullglob
if (( ${#COMPILED[@]} )); then
  echo "    compiled release (no Python source)"
  cp "${COMPILED[@]}" "$PREFIX/"
  mkdir -p "$PREFIX/aio_system_dashboard"
  cp -r "$SRC/aio_system_dashboard/templates" "$SRC/aio_system_dashboard/static" "$PREFIX/aio_system_dashboard/"
else
  echo "    from source checkout"
  cp -r "$SRC/aio_system_dashboard" "$PREFIX/"
  find "$PREFIX/aio_system_dashboard" -name __pycache__ -type d -prune -exec rm -rf {} +
fi
if [[ -f "$SRC/dev/fake_state.json" ]]; then cp "$SRC/dev/fake_state.json" "$PREFIX/dev/"; fi
if [[ -f "$SRC/VERSION" ]]; then
  cp "$SRC/VERSION" "$PREFIX/VERSION"
else
  git -c safe.directory="$SRC" -C "$SRC" describe --tags --always --dirty 2>/dev/null > "$PREFIX/VERSION" \
    || echo "source" > "$PREFIX/VERSION"
fi
cp "$SRC"/docs/CONFIG*.md "$SRC"/docs/OPERATION*.md "$PREFIX/docs/" 2>/dev/null || true
install -m 0755 "$SRC/deploy/aio-dashboard" "$PREFIX/bin/aio-dashboard"
ln -sf "$PREFIX/bin/aio-dashboard" /usr/local/bin/aio-dashboard
if [[ -f "$PREFIX/config/dashboard.yaml" && $RESET_CONFIG -eq 1 ]]; then
  cp "$PREFIX/config/dashboard.yaml" "$PREFIX/config/dashboard.yaml.bak"
  cp "$SRC/config/dashboard.yaml" "$PREFIX/config/dashboard.yaml"
  echo "    replaced config (previous one saved as dashboard.yaml.bak)"
elif [[ -f "$PREFIX/config/dashboard.yaml" ]]; then
  echo "    keeping existing $PREFIX/config/dashboard.yaml (new default: dashboard.yaml.new)"
  cp "$SRC/config/dashboard.yaml" "$PREFIX/config/dashboard.yaml.new"
else
  cp "$SRC/config/dashboard.yaml" "$PREFIX/config/dashboard.yaml"
fi

echo "==> installing Python wheels into $PREFIX/lib"
if python3 -m pip --version >/dev/null 2>&1; then
  # Packages go into PREFIX/lib only. --no-warn-conflicts hides pip's complaints about
  # unrelated system packages (e.g. ultralytics wanting torch); the root warning is expected here.
  PIP_ROOT_USER_ACTION=ignore python3 -m pip install --quiet --no-warn-conflicts --no-index \
    --find-links "$SRC/wheels" --target "$PREFIX/lib" --upgrade flask pyyaml psutil
else
  echo "    pip not found; unpacking wheels directly"
  python3 - "$SRC/wheels" "$PREFIX/lib" <<'EOF'
import sys, zipfile, pathlib
src, dst = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
for whl in sorted(src.glob("*.whl")):
    zipfile.ZipFile(whl).extractall(dst)
    print("    unpacked", whl.name)
EOF
fi
PYTHONPATH="$PREFIX/lib" python3 -c 'import flask, yaml, psutil' \
  || { echo "dependency check failed (wheels must match this Python/arch)" >&2; exit 1; }

chown -R "$RUN_USER": "$PREFIX/logs" "$PREFIX/dev" "$PREFIX/state"

echo "==> systemd unit aio-dashboard.service"
sed -e "s|@USER@|$RUN_USER|g" -e "s|@PREFIX@|$PREFIX|g" "$SRC/deploy/aio-dashboard.service" \
  > /etc/systemd/system/aio-dashboard.service

echo "==> sudoers rule for whitelisted driver restarts"
UNITS=$(PYTHONPATH="$PREFIX/lib:$PREFIX" python3 - "$PREFIX/config/dashboard.yaml" <<'EOF'
import re, sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
for svc in (cfg.get("services") or {}).values():
    unit = (svc or {}).get("unit", "")
    if svc.get("restartable") and re.fullmatch(r"[A-Za-z0-9@._-]+\.service", unit):
        print(unit)
EOF
)
SUDOERS=/etc/sudoers.d/aio-dashboard
if [[ -n "$UNITS" ]]; then
  {
    echo "# Generated by aio-dashboard install.sh from $PREFIX/config/dashboard.yaml"
    for u in $UNITS; do echo "$RUN_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart $u"; done
  } > "$SUDOERS.tmp"
  visudo -cf "$SUDOERS.tmp" >/dev/null && install -m 0440 "$SUDOERS.tmp" "$SUDOERS"
  rm -f "$SUDOERS.tmp"
  echo "    allowed: $(echo $UNITS)"
else
  rm -f "$SUDOERS"
  echo "    no restartable units configured"
fi

if [[ -n "$AIO_NAV_BIN" ]]; then
  [[ -x "$AIO_NAV_BIN" ]] || { echo "aio-nav wrapper not executable: $AIO_NAV_BIN" >&2; exit 1; }
  echo "==> systemd unit aio-nav.service ($AIO_NAV_BIN)"
  sed -e "s|@USER@|$RUN_USER|g" -e "s|@AIO_NAV_BIN@|$AIO_NAV_BIN|g" "$SRC/deploy/aio-nav.service" \
    > /etc/systemd/system/aio-nav.service
  systemctl daemon-reload
  systemctl enable aio-nav.service
fi

if [[ $WITH_SYSMON -eq 1 && -d "$SRC/aio_sysmon" ]]; then
  echo "==> system recorder (aio-sysmon): a black box for the computer"
  SYSMON_DIR=/var/log/aio-sysmon
  rm -rf "$PREFIX/aio_sysmon"
  cp -r "$SRC/aio_sysmon" "$PREFIX/"
  find "$PREFIX/aio_sysmon" -name __pycache__ -type d -prune -exec rm -rf {} +
  install -d -m 0755 "$SYSMON_DIR"
  if [[ ! -f "$PREFIX/config/sysmon.yaml" || $RESET_CONFIG -eq 1 ]]; then
    install -m 0644 "$SRC/config/sysmon.yaml" "$PREFIX/config/sysmon.yaml"
  fi
  install -m 0755 "$SRC/deploy/aio-sysmon" "$PREFIX/bin/aio-sysmon"
  ln -sf "$PREFIX/bin/aio-sysmon" /usr/local/bin/aio-sysmon
  cp "$SRC"/docs/SYSMON*.md "$PREFIX/docs/" 2>/dev/null || true
  sed -e "s|@PREFIX@|$PREFIX|g" -e "s|@LOGDIR@|$SYSMON_DIR|g" "$SRC/deploy/aio-sysmon.service" \
    > /etc/systemd/system/aio-sysmon.service
  # Keep the system journal on disk: after a hang or a power cut, the kernel's own messages are
  # otherwise gone with the memory they were in.
  install -d -m 2755 /var/log/journal
  install -d /etc/systemd/journald.conf.d
  printf '[Journal]\nStorage=persistent\nSystemMaxUse=300M\n' > /etc/systemd/journald.conf.d/aio-persistent.conf
  systemctl restart systemd-journald 2>/dev/null || true
  journalctl --flush 2>/dev/null || true
fi

systemctl daemon-reload
systemctl enable aio-dashboard.service
systemctl restart aio-dashboard.service
if [[ $WITH_SYSMON -eq 1 && -d "$SRC/aio_sysmon" ]]; then
  systemctl enable aio-sysmon.service
  systemctl restart aio-sysmon.service
fi

echo
echo "Done ($(cat "$PREFIX/VERSION")). Settings are detected automatically."
echo "  aio-dashboard status    service state and web addresses"
echo "  aio-dashboard config    edit settings ($PREFIX/config/dashboard.yaml) and restart"
echo "  aio-dashboard logs      follow the log"
if [[ $WITH_SYSMON -eq 1 && -d "$SRC/aio_sysmon" ]]; then
  echo "  aio-sysmon status       the computer's black box (after a freeze: aio-sysmon last-crash)"
fi
"$PREFIX/bin/aio-dashboard" urls
echo "Re-run install.sh after changing restartable units so the sudoers rule is regenerated."
