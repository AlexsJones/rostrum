#!/bin/sh
# Build the RPM inside a Fedora container (it installs into /opt of the container):
#   podman run --rm -v "$PWD":/src:Z -w /src -e VERSION=0.1.0 fedora:44 packaging/rpm/build-rpm.sh
# Output: dist/mic-ptt-<version>-<release>.<dist>.x86_64.rpm
set -eu
VERSION=${VERSION:-0.1.0}
RELEASE=${RELEASE:-1}
PY=3.12
src=$(pwd)

dnf -y -q install rpm-build uv systemd-rpm-macros gcc kernel-headers >/dev/null   # gcc + headers: evdev builds from source

# a self-contained Python + venv at the path they'll be installed to
rm -rf /opt/mic-ptt
mkdir -p /opt/mic-ptt
UV_PYTHON_INSTALL_DIR=/opt/mic-ptt/python uv python install "$PY"
python=$(UV_PYTHON_INSTALL_DIR=/opt/mic-ptt/python uv python find --managed-python "$PY")
uv venv --python "$python" /opt/mic-ptt/.venv
UV_LINK_MODE=copy uv pip install --python /opt/mic-ptt/.venv/bin/python -r requirements.txt

cp app.py ptt.py README.md /opt/mic-ptt/
cp -r tts icons /opt/mic-ptt/
find /opt/mic-ptt -name __pycache__ -prune -exec rm -rf {} +
/opt/mic-ptt/.venv/bin/python -m compileall -q /opt/mic-ptt/app.py /opt/mic-ptt/ptt.py /opt/mic-ptt/tts || true

top=$(mktemp -d)
mkdir -p "$top/SOURCES"
cp -r packaging/linux icons "$top/SOURCES/"
rpmbuild -bb packaging/rpm/mic-ptt.spec --define "_topdir $top" \
    --define "version $VERSION" --define "release $RELEASE"
mkdir -p "$src/dist"
cp "$top"/RPMS/*/*.rpm "$src/dist/"
ls -lh "$src"/dist/*.rpm
