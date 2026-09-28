#!/bin/sh
# Add "Mic PTT" to the app menu (per user, no sudo). Run again after moving this folder.
set -e
here=$(cd "$(dirname "$0")" && pwd)
dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$dir"
cat > "$dir/mic-ptt.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Mic PTT
Comment=Push-to-talk voice input for Claude Code and other apps
Exec=$here/.venv/bin/python $here/app.py
Icon=$here/icons/mic-ptt.svg
Terminal=false
Categories=Utility;
Keywords=voice;microphone;dictation;claude;speech;
StartupWMClass=mic-ptt
DESKTOP
update-desktop-database "$dir" 2>/dev/null || true
echo "Installed $dir/mic-ptt.desktop"
