#!/usr/bin/env bash
# Build a release package that contains no Python source.
#
#   tools/build_release.sh
#
# Run it on the TARGET architecture (an Orin for Jetson deployments): the backend is
# compiled with Nuitka into one native module (.so) for this CPU and Python version.
# Output: dist/aio-dashboard-<version>-<arch>-py<ver>.tar.gz
#
# Deploy on any device with the same architecture and Python version:
#   tar xzf aio-dashboard-*.tar.gz && cd aio-dashboard-*/ && sudo ./install.sh --user jetson
#   cd .. && rm -rf aio-dashboard-*        # nothing with source code is left behind
#
# Build requirements (build machine only, not the devices you deploy to):
#   sudo apt install -y gcc python3-dev
#   python3 -m pip install --user nuitka
set -euo pipefail
cd "$(dirname "$0")/.."

ARCH="$(uname -m)"
PYV="$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
VERSION="$(git describe --tags --always --dirty 2>/dev/null || date +%Y%m%d)"
NAME="aio-dashboard-${VERSION}-${ARCH}-py${PYV}"
BUILD="build/release"
STAGE="$BUILD/$NAME"

need() { echo "missing: $1" >&2; echo "install with: $2" >&2; exit 1; }
command -v gcc >/dev/null || need gcc "sudo apt install -y gcc"
python3 -c 'import sysconfig, os; assert os.path.exists(os.path.join(sysconfig.get_paths()["include"], "Python.h"))' \
  2>/dev/null || need "Python headers" "sudo apt install -y python3-dev"
python3 -m nuitka --version >/dev/null 2>&1 || need nuitka "python3 -m pip install --user nuitka"
if [[ "$VERSION" == *-dirty ]]; then
  echo "warning: building from uncommitted changes ($VERSION)" >&2
fi

echo "==> compiling backend with Nuitka ($ARCH, Python $PYV)"
rm -rf "$BUILD"
mkdir -p "$BUILD/compile" "$STAGE/aio_system_dashboard" dist
# Nuitka >= 2.x: --mode=module; older releases only know --module.
if python3 -m nuitka --help 2>/dev/null | grep -q -- "--mode="; then MODE=(--mode=module); else MODE=(--module); fi
python3 -m nuitka "${MODE[@]}" aio_system_dashboard \
  --include-package=aio_system_dashboard \
  --python-flag=no_docstrings \
  --no-pyi-file --remove-output --quiet --assume-yes-for-downloads \
  --output-dir="$BUILD/compile"

echo "==> staging $NAME"
cp "$BUILD"/compile/aio_system_dashboard*.so "$STAGE/"
cp -r aio_system_dashboard/templates aio_system_dashboard/static "$STAGE/aio_system_dashboard/"
mkdir -p "$STAGE/config" "$STAGE/deploy" "$STAGE/docs"
cp config/dashboard.yaml "$STAGE/config/"
cp -r wheels "$STAGE/"
cp deploy/aio-dashboard deploy/aio-dashboard.service deploy/aio-nav.service "$STAGE/deploy/"
cp deploy/install.sh "$STAGE/install.sh"
cp docs/CONFIG.md docs/CONFIG.zh-TW.md docs/OPERATION.md docs/OPERATION.zh-TW.md "$STAGE/docs/"
mkdir -p "$STAGE/dev" && cp dev/fake_state.json "$STAGE/dev/"   # data for --fake demos, not code
echo "$VERSION" > "$STAGE/VERSION"
cat > "$STAGE/README.txt" <<EOF
AIO System Dashboard $VERSION ($ARCH, Python $PYV) — compiled release, no source code.

Install:   sudo ./install.sh --user <ros-user>      (add --reset-config to replace settings)
Then:      aio-dashboard status | config | logs | restart
Operation: docs/OPERATION.md  /  docs/OPERATION.zh-TW.md
Settings:  docs/CONFIG.md  /  docs/CONFIG.zh-TW.md
After installing, this folder can be deleted.
EOF

# No Python source may ship in a release.
if find "$STAGE" -name '*.py' -o -name '*.pyc' -o -name '*.pyi' | grep -q .; then
  echo "error: Python source found in release:" >&2
  find "$STAGE" -name '*.py' -o -name '*.pyc' -o -name '*.pyi' >&2
  exit 1
fi

echo "==> smoke test: the compiled module imports"
SMOKE_LIB="$BUILD/smoke-lib"
if python3 -m pip install --quiet --no-warn-conflicts --no-index --find-links wheels \
     --target "$SMOKE_LIB" flask pyyaml psutil >/dev/null 2>&1; then
  (cd / && PYTHONPATH="$STAGE:$SMOKE_LIB" python3 -c \
    'import aio_system_dashboard.__main__ as m, aio_system_dashboard.web.common as c; assert (c.PKG_DIR / "templates").is_dir(); print("    ok")')
else
  echo "    skipped: bundled wheels do not match this machine ($ARCH)"
fi

tar -czf "dist/$NAME.tar.gz" -C "$BUILD" "$NAME"
rm -rf "$BUILD"
echo
echo "Release: dist/$NAME.tar.gz ($(du -h "dist/$NAME.tar.gz" | cut -f1))"
echo "Deploy:  copy it to the device, then"
echo "         tar xzf $NAME.tar.gz && cd $NAME && sudo ./install.sh --user <ros-user>"
