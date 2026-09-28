#!/bin/sh
# Add "Rostrum" to the app menu (per user, no sudo). Run again after moving this folder.
set -e
here=$(cd "$(dirname "$0")" && pwd)
dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$dir"
cat > "$dir/rostrum.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Rostrum
Comment=Push-to-talk voice input for Claude Code and other apps
Exec=$here/.venv/bin/python $here/app.py
Icon=$here/icons/rostrum.svg
Terminal=false
Categories=Utility;
Keywords=voice;microphone;dictation;claude;speech;
StartupWMClass=rostrum
DESKTOP
update-desktop-database "$dir" 2>/dev/null || true
echo "Installed $dir/rostrum.desktop"
