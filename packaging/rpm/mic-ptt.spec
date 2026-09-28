# Packages a self-contained /opt/mic-ptt (own Python + venv) that build-rpm.sh has already built.
%global debug_package %{nil}
%global __os_install_post %{nil}
%global _build_id_links none
%global _binary_payload w19T0.zstdio

Name:           mic-ptt
Version:        %{?version}%{!?version:0.1.0}
Release:        %{?release}%{!?release:1}%{?dist}
Summary:        Push-to-talk voice input for Claude Code and other apps
License:        Proprietary
URL:            https://github.com/AlexsJones/mic-ptt
ExclusiveArch:  x86_64
AutoReqProv:    no

Requires:       pulseaudio-utils
Requires:       alsa-utils
Requires:       wl-clipboard
# the Qt window (PySide6 bundles Qt, not these system libraries); X11 ones for non-Wayland sessions
Requires:       fontconfig
Requires:       libglvnd-glx
Requires:       mesa-libEGL
Requires:       libxkbcommon
Requires:       libxkbcommon-x11
Requires:       libwayland-cursor
Requires:       libwayland-egl
Requires:       libwayland-server
Requires:       xcb-util
Requires:       xcb-util-cursor
Requires:       xcb-util-image
Requires:       xcb-util-keysyms
Requires:       xcb-util-renderutil
Requires:       xcb-util-wm

%description
Hold a microphone's Transmit button to talk to a coding agent. Drives Claude Code's
voice mode, or transcribes locally with Whisper and pastes into the active window.
Replies can be read aloud with Kokoro. Includes the Mic PTT control app.

%install
mkdir -p %{buildroot}/opt
cp -a /opt/mic-ptt %{buildroot}/opt/
install -Dm755 %{_sourcedir}/linux/mic-ptt %{buildroot}%{_bindir}/mic-ptt
install -Dm755 %{_sourcedir}/linux/mic-ptt-cli %{buildroot}%{_bindir}/mic-ptt-cli
install -Dm644 %{_sourcedir}/linux/mic-ptt.desktop %{buildroot}%{_datadir}/applications/mic-ptt.desktop
install -Dm644 %{_sourcedir}/icons/mic-ptt.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/mic-ptt.svg
install -Dm644 %{_sourcedir}/linux/70-mic-ptt-uinput.rules %{buildroot}%{_udevrulesdir}/70-mic-ptt-uinput.rules
install -Dm644 %{_sourcedir}/linux/mic-ptt-uinput.conf %{buildroot}%{_modulesloaddir}/mic-ptt-uinput.conf

%post
/usr/sbin/modprobe uinput >/dev/null 2>&1 || :
/usr/bin/udevadm control --reload-rules >/dev/null 2>&1 || :
/usr/bin/udevadm trigger --name-match=uinput >/dev/null 2>&1 || :

%files
/opt/mic-ptt
%{_bindir}/mic-ptt
%{_bindir}/mic-ptt-cli
%{_datadir}/applications/mic-ptt.desktop
%{_datadir}/icons/hicolor/scalable/apps/mic-ptt.svg
%{_udevrulesdir}/70-mic-ptt-uinput.rules
%{_modulesloaddir}/mic-ptt-uinput.conf
