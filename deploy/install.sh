#!/usr/bin/env bash
# Install the AIO System Dashboard on the Jetson (offline).
# Works from a source checkout (deploy/install.sh) or from a compiled release package
# made by tools/build_release.sh (./install.sh), which contains no Python source.
#
#   sudo deploy/install.sh              install or update, with the optional parts chosen last time
#   sudo deploy/install.sh --choose     pick the optional parts from a menu (also the first time)
#   deploy/install.sh --list            what is installed and chosen (no sudo needed)
#   deploy/install.sh --dry-run         what an install would add or remove, then stop (no sudo)
#
# Optional parts: sysmon (black-box recorder), replay (Live / Bag switch), wifi (Wi-Fi from the
# pages), hotspot (Wi-Fi hotspot on Maintenance > Network; needs wifi). For scripts, instead of the
# menu: --with-<part> / --without-<part>, and --yes (never ask). The choice is kept in
# PREFIX/config/installed-options; a part switched off is removed.
# Other options: [--user nvidia] [--prefix /opt/aio-dashboard] [--with-aio-nav PATH_TO_aio-nav]
#                [--reset-config]
#
# - copies the app to PREFIX (default /opt/aio-dashboard), keeping an existing config
#   (--reset-config replaces it with the default; the old one is saved as .bak)
# - installs bundled wheels (wheels/, aarch64 cp310) into PREFIX/lib, with pip if
#   available, otherwise by unpacking the wheels (no Internet, no venv needed)
# - installs aio-dashboard.service and a sudoers rule that allows ONLY
#   `systemctl restart <unit>` for units marked `restartable: true` in the config
# - installs the `aio-dashboard` command (start/stop/status/logs/config)
# - optionally installs aio-nav.service (--with-aio-nav); not needed for the Overview page,
#   which starts AIO NAV and DSO itself, like the AIO Nav desktop app. When the dashboard starts
#   AIO NAV by itself at boot (autostart.enabled), the unit is installed but NOT enabled, and an
#   already enabled one is disabled: two boot-time managers for one program fight. Otherwise it
#   is enabled as before, with a warning if the Overview page can also start/stop it.
# - optional parts (see above): sysmon installs aio-sysmon.service, the computer's black box
#   (docs/SYSMON.md), and keeps the system journal across reboots; replay installs the replay
#   module (without it the machine always runs Live); wifi lets the dashboard's user manage
#   NetworkManager (polkit); hotspot adds the right to run a Wi-Fi hotspot, and a NetworkManager
#   dispatcher script that keeps it a closed network (nothing forwarded to or from it).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# release package: install.sh sits next to wheels/; source checkout: deploy/install.sh
if [[ -d "$HERE/wheels" ]]; then SRC="$HERE"; else SRC="$(cd "$HERE/.." && pwd)"; fi
PREFIX=/opt/aio-dashboard
RUN_USER="${SUDO_USER:-nvidia}"
AIO_NAV_BIN=""
RESET_CONFIG=0
MODE=install                                     # install | list | dry-run
CHOOSE=0
ASSUME_YES=0
declare -A ASKED=()                              # part -> 1 / 0 from --with-<part> / --without-<part>

PARTS=(sysmon replay wifi hotspot)
declare -A TITLE=(
  [sysmon]="Black-box recorder (aio-sysmon)"
  [replay]="Replay: Live / Bag switch and bag player"
  [wifi]="Wi-Fi from the pages"
  [hotspot]="Wi-Fi hotspot (Maintenance > Network)"
)
PKLA=/etc/polkit-1/localauthority/50-local.d/50-aio-dashboard-wifi.pkla
RULES=/etc/polkit-1/rules.d/50-aio-dashboard-wifi.rules
HOTSPOT_DISPATCHER=/etc/NetworkManager/dispatcher.d/50-aio-hotspot

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) RUN_USER="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --with-aio-nav) AIO_NAV_BIN="$2"; shift 2 ;;
    --reset-config) RESET_CONFIG=1; shift ;;
    --with-sysmon|--with-replay|--with-wifi|--with-hotspot) ASKED[${1#--with-}]=1; shift ;;
    --without-sysmon|--without-replay|--without-wifi|--without-hotspot) ASKED[${1#--without-}]=0; shift ;;
    --no-sysmon) shift ;;                        # old flag: the recorder is off unless chosen
    --choose) CHOOSE=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --list) MODE=list; shift ;;
    --dry-run) MODE=dry-run; shift ;;
    -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 1 ;;
  esac
done
OPTIONS_FILE="$PREFIX/config/installed-options"

# ------------------------------------------------------------------ optional parts
chosen_words() {                                 # the parts named in the options file (not its comments)
  [[ -f "$OPTIONS_FILE" ]] || return 0
  local w
  for w in $(sed 's/#.*//' "$OPTIONS_FILE"); do printf ' %s ' "$w"; done
}
checkable() {                                    # the polkit folder is readable by root only
  [[ $1 != wifi ]] || [[ -r "$(dirname "$PKLA")" ]]
}
installed_now() {                                # is the part on this machine now?
  case "$1" in
    sysmon) [[ -f /etc/systemd/system/aio-sysmon.service ]] ;;
    replay) [[ -d "$PREFIX/aio_dashboard_replay" ]] || compgen -G "$PREFIX/aio_dashboard_replay*.so" >/dev/null ;;
    wifi) [[ -f "$PKLA" ]] ;;
    hotspot) [[ "$(chosen_words)" == *" hotspot "* ]] ;;
  esac
}
needs() {                                        # what this system lacks for the part ("" = nothing)
  local out=()
  if [[ $1 == wifi || $1 == hotspot ]] && ! command -v nmcli >/dev/null 2>&1; then out+=("NetworkManager (nmcli)"); fi
  # NetworkManager runs dnsmasq to give the devices on the hotspot their addresses
  if [[ $1 == hotspot ]] && ! command -v dnsmasq >/dev/null 2>&1; then out+=("dnsmasq (package dnsmasq-base)"); fi
  if [[ $1 == hotspot ]] && ! command -v nft >/dev/null 2>&1 && ! command -v iptables >/dev/null 2>&1; then
    out+=("nft or iptables (keeps the hotspot a closed network)")
  fi
  if (( ${#out[@]} )); then (IFS=,; echo "${out[*]}") | sed 's/,/, /g'; fi
}
available() {                                    # can this package install the part on this system?
  case "$1" in
    sysmon) [[ -d "$SRC/aio_sysmon" ]] ;;
    replay) [[ -d "$SRC/aio_dashboard_replay" ]] || compgen -G "$SRC/aio_dashboard_replay*.so" >/dev/null ;;
    wifi|hotspot) [[ -z "$(needs "$1")" ]] ;;
  esac
}
declare -A WANT=()
if [[ -f "$OPTIONS_FILE" ]]; then                # the choice made last time
  CHOSEN_BEFORE=1
  BEFORE="$(chosen_words)"
  for p in "${PARTS[@]}"; do WANT[$p]=0; [[ "$BEFORE" == *" $p "* ]] && WANT[$p]=1; done
else                                             # first time with this installer: what is there
  CHOSEN_BEFORE=0
  for p in "${PARTS[@]}"; do WANT[$p]=0; installed_now "$p" && WANT[$p]=1; done
fi
for p in "${!ASKED[@]}"; do WANT[$p]=${ASKED[$p]}; done
settle() {                                       # the hotspot needs the Wi-Fi permission
  local why
  if [[ ${WANT[hotspot]} -eq 1 && ${WANT[wifi]} -eq 0 ]]; then
    if [[ ${ASKED[wifi]:-} == 0 ]]; then WANT[hotspot]=0; else WANT[wifi]=1; fi
  fi
  for p in "${PARTS[@]}"; do
    if [[ ${WANT[$p]} -eq 1 ]] && ! available "$p"; then
      why="$(needs "$p")"
      echo "    note: $p (${TITLE[$p]}) left out: ${why:+this system has no }${why:-not in this package}" >&2
      WANT[$p]=0
    fi
  done
}
settle

show_parts() {
  local i=1 p mark now
  for p in "${PARTS[@]}"; do
    mark=" "; [[ ${WANT[$p]} -eq 1 ]] && mark="x"
    now=""; installed_now "$p" && now="  * installed now"
    checkable "$p" || { now="  ? (run with sudo to check)"; [[ $MODE == list && $CHOSEN_BEFORE -eq 0 ]] && mark="?"; }
    printf '  %d [%s] %-42s%s\n' "$i" "$mark" "${TITLE[$p]}" "$now"
    i=$((i + 1))
  done
}
menu() {
  local line n p
  echo "AIO System Dashboard installer: optional parts"
  while true; do
    echo
    show_parts
    printf 'Numbers to switch on/off (e.g. 4 or 2 4), Enter to continue, q to quit: '
    IFS= read -r line || line=""
    case "$line" in
      "") return 0 ;;
      q|Q) echo "nothing changed"; exit 0 ;;
    esac
    for n in $line; do
      if [[ "$n" =~ ^[0-9]+$ && $n -ge 1 && $n -le ${#PARTS[@]} ]]; then
        p=${PARTS[$((n - 1))]}
        WANT[$p]=$((1 - WANT[$p]))
        [[ $p == hotspot && ${WANT[hotspot]} -eq 1 ]] && WANT[wifi]=1     # needs the Wi-Fi permission
        [[ $p == wifi && ${WANT[wifi]} -eq 0 ]] && WANT[hotspot]=0
      else
        echo "  (no part $n)"
      fi
    done
    settle
  done
}
chosen_text() {
  local p out=()
  for p in "${PARTS[@]}"; do [[ ${WANT[$p]} -eq 1 ]] && out+=("$p"); done
  if (( ${#out[@]} )); then echo "${out[*]}"; else echo "none"; fi
}

if [[ $MODE == list ]]; then
  echo "Optional parts in $PREFIX ($([[ $CHOSEN_BEFORE -eq 1 ]] && echo "chosen: $OPTIONS_FILE" || echo "not chosen yet: shown as found"))"
  show_parts
  echo "Change them: sudo $0 --choose"
  exit 0
fi
if [[ $CHOOSE -eq 1 || ( $CHOSEN_BEFORE -eq 0 && ${#ASKED[@]} -eq 0 && $ASSUME_YES -eq 0 ) ]] && [[ -t 0 ]]; then
  menu
fi
WITH_SYSMON=${WANT[sysmon]}
WITH_REPLAY=${WANT[replay]}
WITH_WIFI=${WANT[wifi]}
WITH_HOTSPOT=${WANT[hotspot]}
CHANGES=()
for p in "${PARTS[@]}"; do
  if ! checkable "$p"; then CHANGES+=("$p: run with sudo to check"); continue; fi
  if [[ ${WANT[$p]} -eq 1 ]] && ! installed_now "$p"; then CHANGES+=("add $p"); fi
  if [[ ${WANT[$p]} -eq 0 ]] && installed_now "$p"; then CHANGES+=("remove $p"); fi
done
echo "==> optional parts: $(chosen_text)$( (( ${#CHANGES[@]} )) && printf ' (%s)' "$(IFS='|'; echo "${CHANGES[*]}" | sed 's/|/, /g')" )"
if [[ $MODE == dry-run ]]; then
  echo "    dry run: nothing installed"
  exit 0
fi

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
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

# Optional module "replay" (a package next to the app; see aio_system_dashboard/modules.py).
if [[ $WITH_REPLAY -eq 0 ]] && installed_now replay; then
  echo "==> removing the replay module (not chosen)"
fi
rm -rf "$PREFIX/aio_dashboard_replay" "$PREFIX"/aio_dashboard_replay*.so
if [[ $WITH_REPLAY -eq 1 ]]; then
  shopt -s nullglob
  RCOMPILED=("$SRC"/aio_dashboard_replay*.so)
  shopt -u nullglob
  if (( ${#RCOMPILED[@]} )); then
    echo "==> optional module: replay (compiled)"
    cp "${RCOMPILED[@]}" "$PREFIX/"
    mkdir -p "$PREFIX/aio_dashboard_replay"
    cp -r "$SRC/aio_dashboard_replay/templates" "$SRC/aio_dashboard_replay/static" "$PREFIX/aio_dashboard_replay/"
  elif [[ -d "$SRC/aio_dashboard_replay" ]]; then
    echo "==> optional module: replay (Live / Bag replay switch, bag player)"
    cp -r "$SRC/aio_dashboard_replay" "$PREFIX/"
    find "$PREFIX/aio_dashboard_replay" -name __pycache__ -type d -prune -exec rm -rf {} +
  else
    echo "    warning: --with-replay, but this package has no replay module" >&2
    WITH_REPLAY=0
  fi
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

echo "==> unattended operation: reboot 10 s after a kernel panic"
# The device runs without an operator: a kernel panic must not leave it hanging (the L4T hardware
# watchdog would reset it only after 120 s). Removed by deploy/uninstall.sh.
cat > /etc/sysctl.d/90-aio-unattended.conf <<'EOF_SYSCTL'
# AIO dashboard (deploy/install.sh): reboot 10 s after a kernel panic instead of hanging.
kernel.panic = 10
EOF_SYSCTL
sysctl -q -p /etc/sysctl.d/90-aio-unattended.conf || true

if command -v chronyd >/dev/null 2>&1 && [[ -d /etc/chrony ]]; then
  echo "==> GNSS time for the system clock (chrony time source; used when there is no NTP)"
  # The dashboard writes the GNSS time of the IMU stamps to this shared-memory segment
  # (collectors/gnss_time.py); chrony uses it, prefers NTP when it can, steps a clock that is far
  # off and keeps the hardware clock (rtcsync). perm=0666: the dashboard user may write it.
  mkdir -p /etc/chrony/conf.d
  cat > /etc/chrony/conf.d/aio-gnss.conf <<'EOF_CHRONY'
# AIO dashboard (deploy/install.sh): GNSS time from the IMU stamps, SHM segment 2.
refclock SHM 2:perm=0666 refid GNSS poll 4 precision 1e-2
EOF_CHRONY
  systemctl restart chrony 2>/dev/null || systemctl restart chronyd 2>/dev/null || true
else
  echo "    (no chrony: the system clock is not set from GNSS time on this machine)"
fi

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

# Who starts AIO NAV at boot? "autostart": the dashboard does (then systemd must not); "control":
# the Overview page can start/stop it, nothing automatic; "no": the dashboard only watches.
NAV_OWNER=$(PYTHONPATH="$PREFIX/lib" python3 - "$PREFIX/config/dashboard.yaml" <<'EOF2'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
control = bool((cfg.get("nav") or {}).get("allow_control"))
auto = bool((cfg.get("autostart") or {}).get("enabled", True))   # on by default
print("autostart" if control and auto else "control" if control else "no")
EOF2
)
if [[ -n "$AIO_NAV_BIN" ]]; then
  [[ -x "$AIO_NAV_BIN" ]] || { echo "aio-nav wrapper not executable: $AIO_NAV_BIN" >&2; exit 1; }
  echo "==> systemd unit aio-nav.service ($AIO_NAV_BIN)"
  sed -e "s|@USER@|$RUN_USER|g" -e "s|@AIO_NAV_BIN@|$AIO_NAV_BIN|g" "$SRC/deploy/aio-nav.service" \
    > /etc/systemd/system/aio-nav.service
  systemctl daemon-reload
  if [[ "$NAV_OWNER" == autostart ]]; then
    echo "    not enabled at boot: the dashboard starts AIO NAV itself (autostart.enabled)"
  else
    systemctl enable aio-nav.service
  fi
fi
# Automatic start of a systemd *user* unit (the sensor drivers) at power-on needs the user's
# service manager to exist with nobody logged in: lingering. One-time, root.
AUTOSTART_USER_UNITS=$(PYTHONPATH="$PREFIX/lib" python3 - "$PREFIX/config/dashboard.yaml" <<'EOF2'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
auto = cfg.get("autostart") or {}
services = cfg.get("services") or {}
if auto.get("enabled", True) and (cfg.get("nav") or {}).get("allow_control"):   # on by default
    print(" ".join((services.get(k) or {}).get("user_unit", "") for k in auto.get("start_services") or []).strip())
EOF2
)
if [[ -n "$AUTOSTART_USER_UNITS" ]]; then
  echo "==> lingering for $RUN_USER (user units start at power-on: $AUTOSTART_USER_UNITS)"
  loginctl enable-linger "$RUN_USER" || echo "    warning: could not enable lingering; the drivers may not start before someone logs in" >&2
fi

# Wi-Fi from the pages without root: allow the dashboard's user (only) to manage NetworkManager;
# the hotspot adds the right to share a Wi-Fi connection (an access point that hands out
# addresses). polkit 0.105 (Ubuntu 22.04) reads .pkla files; newer polkit reads .rules.
if [[ $WITH_WIFI -eq 1 ]]; then
  echo "==> Wi-Fi permission for $RUN_USER (NetworkManager, via polkit)$([[ $WITH_HOTSPOT -eq 1 ]] && echo ", with the hotspot")"
  NM=org.freedesktop.NetworkManager
  ACTIONS=("$NM.settings.modify.system" "$NM.network-control" "$NM.wifi.scan" "$NM.enable-disable-wifi")
  if [[ $WITH_HOTSPOT -eq 1 ]]; then ACTIONS+=("$NM.wifi.share.protected" "$NM.wifi.share.open"); fi
  install -d -m 0755 "$(dirname "$PKLA")"
  cat > "$PKLA" <<EOF2
# Installed by aio-dashboard install.sh (optional part wifi$([[ $WITH_HOTSPOT -eq 1 ]] && echo " + hotspot")); removed by uninstall.sh.
[aio-dashboard Wi-Fi]
Identity=unix-user:$RUN_USER
Action=$(IFS=';'; echo "${ACTIONS[*]}")
ResultAny=yes
ResultInactive=yes
ResultActive=yes
EOF2
  chmod 0644 "$PKLA"
  if [[ -d "$(dirname "$RULES")" ]]; then
    cat > "$RULES" <<EOF2
// Installed by aio-dashboard install.sh (optional part wifi); removed by uninstall.sh.
polkit.addRule(function(action, subject) {
  if (subject.user == "$RUN_USER" && [
$(printf '      "%s",\n' "${ACTIONS[@]}" | sed '$ s/,$//')].indexOf(action.id) >= 0) {
    return polkit.Result.YES;
  }
});
EOF2
    chmod 0644 "$RULES"
  fi
  echo "    note: this also lets $RUN_USER change wired NetworkManager profiles; the dashboard itself"
  echo "    only ever changes Wi-Fi profiles"
elif [[ -f "$PKLA" || -f "$RULES" ]]; then
  echo "==> removing the Wi-Fi permission (not chosen)"
  rm -f "$PKLA" "$RULES"
fi
hotspot_dev() {                                  # the interface the hotspot runs on now, if it does
  nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | sed -n 's/^aio-hotspot://p' | head -n 1
}
if [[ $WITH_HOTSPOT -eq 1 ]]; then
  echo "==> hotspot: a closed network (its devices reach this computer only, nothing is forwarded)"
  install -d -m 0755 "$(dirname "$HOTSPOT_DISPATCHER")"
  install -m 0755 "$SRC/deploy/aio-hotspot-dispatcher" "$HOTSPOT_DISPATCHER"
  dev="$(hotspot_dev)"
  if [[ -n "$dev" ]]; then
    CONNECTION_ID=aio-hotspot "$HOTSPOT_DISPATCHER" "$dev" up ||
      echo "    warning: could not close the running hotspot; it is closed at its next start" >&2
  fi
else
  if [[ -f "$HOTSPOT_DISPATCHER" ]]; then
    CONNECTION_ID=aio-hotspot "$HOTSPOT_DISPATCHER" "$(hotspot_dev)" down || true
    rm -f "$HOTSPOT_DISPATCHER"
  fi
  if installed_now hotspot; then
    echo "==> removing the hotspot (not chosen): its NetworkManager profile goes, the Wi-Fi joins a saved network"
    nmcli connection delete aio-hotspot >/dev/null 2>&1 || true
  fi
fi

if systemctl is-enabled --quiet aio-nav.service 2>/dev/null; then
  if [[ "$NAV_OWNER" == autostart ]]; then
    echo "==> aio-nav.service was enabled at boot; disabling it (the dashboard starts AIO NAV itself)"
    systemctl disable aio-nav.service
  elif [[ "$NAV_OWNER" == control ]]; then
    echo "    note: aio-nav.service starts AIO NAV at boot and the Overview page can also start/stop it;"
    echo "    keep one manager (the dashboard Maintenance page shows this as a warning)"
  fi
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
elif installed_now sysmon; then
  # Its records (/var/log/aio-sysmon) and the persistent journal stay: they are the evidence about
  # past freezes (as deploy/uninstall.sh).
  echo "==> removing the system recorder (not chosen); its records in /var/log/aio-sysmon are kept"
  systemctl disable --now aio-sysmon.service 2>/dev/null || true
  rm -f /etc/systemd/system/aio-sysmon.service /usr/local/bin/aio-sysmon "$PREFIX/bin/aio-sysmon"
  rm -rf "$PREFIX/aio_sysmon"
fi

systemctl daemon-reload
systemctl enable aio-dashboard.service
systemctl restart aio-dashboard.service
if [[ $WITH_SYSMON -eq 1 && -d "$SRC/aio_sysmon" ]]; then
  systemctl enable aio-sysmon.service
  systemctl restart aio-sysmon.service
fi

# The choice, for the next run (and the dashboard reads it: the hotspot is shown when installed).
{
  echo "# Optional parts chosen in deploy/install.sh; the next run installs the same ones."
  echo "# Change them with: sudo deploy/install.sh --choose"
  chosen_text | sed 's/^none$//'
} > "$OPTIONS_FILE"
chmod 0644 "$OPTIONS_FILE"

echo
echo "Done ($(cat "$PREFIX/VERSION")). Settings are detected automatically."
echo "  optional parts: $(chosen_text)   (the next run installs the same; change: sudo $0 --choose)"
echo "  aio-dashboard status    service state and web addresses"
echo "  aio-dashboard config    edit settings ($PREFIX/config/dashboard.yaml) and restart"
echo "  aio-dashboard logs      follow the log"
if [[ $WITH_SYSMON -eq 1 && -d "$SRC/aio_sysmon" ]]; then
  echo "  aio-sysmon status       the computer's black box (after a freeze: aio-sysmon last-crash)"
fi
if [[ $WITH_REPLAY -eq 1 ]]; then
  echo "  Maintenance > Replay    Live / Bag replay switch and the bag player (replay module)"
fi
if [[ $WITH_HOTSPOT -eq 1 ]]; then
  echo "  Maintenance > Network   Wi-Fi hotspot for phones and tablets"
fi
"$PREFIX/bin/aio-dashboard" urls
if [[ ! -s "$PREFIX/config/maint_password" ]]; then
  echo "The maintenance view stays locked until a password is set:  aio-dashboard password"
fi
echo "Re-run install.sh after changing restartable units so the sudoers rule is regenerated."
