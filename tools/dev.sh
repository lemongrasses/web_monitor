#!/usr/bin/env bash
# Development helper (x86, no ROS): run the dashboard in fake mode plus the fake NAV sender.
#   tools/dev.sh start [scenario]   start dashboard + fake sender (default scenario: all)
#   tools/dev.sh stop               stop both
#   tools/dev.sh status
# Logs and PID files go to logs/. Flask is taken from .devdeps/ if present
# (python3 -m pip install --target .devdeps flask pyyaml psutil).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
export PYTHONPATH="${PWD}/.devdeps${PYTHONPATH:+:$PYTHONPATH}"

stop_pid() {
  local f="logs/$1.pid"
  if [[ -f "$f" ]] && kill -0 "$(cat "$f")" 2>/dev/null; then
    local pid; pid="$(cat "$f")"
    kill "$pid"
    for _ in $(seq 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done  # release ports
    echo "stopped $1"
  fi
  rm -f "$f"
}

case "${1:-}" in
  start)
    "$0" stop >/dev/null
    nohup python3 -m aio_system_dashboard --config "${CONFIG:-config/dashboard.dev.yaml}" >logs/dev_server.log 2>&1 &
    echo $! >logs/dashboard.pid
    nohup python3 tools/fake_nav_sender.py --scenario "${2:-all}" >logs/fake_sender.log 2>&1 &
    echo $! >logs/sender.pid
    sleep 2
    echo "Product UI      http://127.0.0.1:8080"
    echo "Maintenance UI  http://127.0.0.1:8081"
    echo "Fake state      dev/fake_state.json (edit live)"
    ;;
  stop)
    stop_pid sender
    stop_pid dashboard
    ;;
  status)
    for p in dashboard sender; do
      if [[ -f logs/$p.pid ]] && kill -0 "$(cat logs/$p.pid)" 2>/dev/null; then echo "$p running"; else echo "$p stopped"; fi
    done
    ;;
  *)
    sed -n '2,8p' "$0"; exit 1 ;;
esac
