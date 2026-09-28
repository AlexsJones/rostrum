#!/bin/sh
# One-time: let members of the "input" group create virtual keyboards via /dev/uinput.
set -e
echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' |
  sudo tee /etc/udev/rules.d/99-uinput.rules >/dev/null
echo uinput | sudo tee /etc/modules-load.d/uinput.conf >/dev/null
sudo usermod -aG input "$USER"
sudo udevadm control --reload-rules
sudo udevadm trigger --name-match=uinput
ls -l /dev/uinput
echo "Done. Log out and back in so the new 'input' group membership applies."
