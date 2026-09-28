#!/bin/sh
# Build the RPM inside a Fedora container (it installs into /opt of the container):
#   podman run --rm -v "$PWD":/src:Z -w /src -e VERSION=0.1.0 fedora:44 packaging/rpm/build-rpm.sh
# Output: dist/rostrum-<version>-<release>.<dist>.x86_64.rpm
set -eu
VERSION=${VERSION:-0.1.0}
RELEASE=${RELEASE:-1}
PY=3.12
src=$(pwd)

dnf -y -q install rpm-build uv systemd-rpm-macros gcc kernel-headers >/dev/null   # gcc + headers: evdev builds from source

# a self-contained Python + venv at the path they'll be installed to
rm -rf /opt/rostrum
mkdir -p /opt/rostrum
UV_PYTHON_INSTALL_DIR=/opt/rostrum/python uv python install "$PY"
python=$(UV_PYTHON_INSTALL_DIR=/opt/rostrum/python uv python find --managed-python "$PY")
uv venv --python "$python" /opt/rostrum/.venv
UV_LINK_MODE=copy uv pip install --python /opt/rostrum/.venv/bin/python -r requirements.txt

cp app.py ptt.py README.md /opt/rostrum/
cp -r tts icons /opt/rostrum/
find /opt/rostrum -name __pycache__ -prune -exec rm -rf {} +
/opt/rostrum/.venv/bin/python -m compileall -q /opt/rostrum/app.py /opt/rostrum/ptt.py /opt/rostrum/tts || true

top=$(mktemp -d)
mkdir -p "$top/SOURCES"
cp -r packaging/linux icons "$top/SOURCES/"
rpmbuild -bb packaging/rpm/rostrum.spec --define "_topdir $top" \
    --define "version $VERSION" --define "release $RELEASE"
mkdir -p "$src/dist"
cp "$top"/RPMS/*/*.rpm "$src/dist/"
ls -lh "$src"/dist/*.rpm
