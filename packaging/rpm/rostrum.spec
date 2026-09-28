# Packages a self-contained /opt/rostrum (own Python + venv) that build-rpm.sh has already built.
%global debug_package %{nil}
%global __os_install_post %{nil}
%global _build_id_links none
%global _binary_payload w19T0.zstdio

Name:           rostrum
Version:        %{?version}%{!?version:0.1.0}
Release:        %{?release}%{!?release:1}%{?dist}
Summary:        Push-to-talk voice input for Claude Code and other apps
License:        Proprietary
URL:            https://github.com/AlexsJones/rostrum
ExclusiveArch:  x86_64
AutoReqProv:    no
# renamed from mic-ptt: upgrading replaces the old package
Obsoletes:      mic-ptt < 0.2.0
Provides:       mic-ptt = %{version}-%{release}

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
Replies can be read aloud with Kokoro. Includes the Rostrum control app.

%install
mkdir -p %{buildroot}/opt
cp -a /opt/rostrum %{buildroot}/opt/
install -Dm755 %{_sourcedir}/linux/rostrum %{buildroot}%{_bindir}/rostrum
install -Dm755 %{_sourcedir}/linux/rostrum-cli %{buildroot}%{_bindir}/rostrum-cli
install -Dm644 %{_sourcedir}/linux/rostrum.desktop %{buildroot}%{_datadir}/applications/rostrum.desktop
install -Dm644 %{_sourcedir}/icons/rostrum.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/rostrum.svg
install -Dm644 %{_sourcedir}/linux/70-rostrum-uinput.rules %{buildroot}%{_udevrulesdir}/70-rostrum-uinput.rules
install -Dm644 %{_sourcedir}/linux/rostrum-uinput.conf %{buildroot}%{_modulesloaddir}/rostrum-uinput.conf

%post
/usr/sbin/modprobe uinput >/dev/null 2>&1 || :
/usr/bin/udevadm control --reload-rules >/dev/null 2>&1 || :
/usr/bin/udevadm trigger --name-match=uinput >/dev/null 2>&1 || :

%files
/opt/rostrum
%{_bindir}/rostrum
%{_bindir}/rostrum-cli
%{_datadir}/applications/rostrum.desktop
%{_datadir}/icons/hicolor/scalable/apps/rostrum.svg
%{_udevrulesdir}/70-rostrum-uinput.rules
%{_modulesloaddir}/rostrum-uinput.conf
