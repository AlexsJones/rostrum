"""
Mic push-to-talk: holds a key while you're transmitting through the MicFX on the USB sound card.

The MicFX passes no audio at all unless Transmit is held, and the USB card's line is digitally
silent otherwise, so:
  press   -> ~15 ms of sound above the silent floor (the button's press click, or speech)
  release -> the sound cuts off dead within 10 ms (the button opening), or --hang s of silence
             (released during a pause, when there's nothing to cut off)
Natural pauses in speech fade over 60+ ms, so they never look like a release.

  .venv/bin/python ptt.py                    # hold SPACE for Claude Code voice mode
  .venv/bin/python ptt.py --dry-run          # print events only, no key presses
  .venv/bin/python ptt.py --file usb_button.wav   # run the detector over a 48 kHz mono recording
  .venv/bin/python ptt.py --key KEY_F13      # hold a different key
  .venv/bin/python ptt.py --no-tts           # mic only: don't load Claude's voice (e.g. for other agents)
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

RATE = 48000
HOP = 240                       # 5 ms analysis step


class Detector:
    """Sound out of silence -> press. Sound cut off dead, or --hang s of silence -> release."""

    def __init__(self, args, floor_db):
        self.a = args
        self.floor = floor_db
        self.held = False
        self.recent = []            # rms dB of recent 5 ms hops
        self.quiet_for = 0.0

    def step(self, hop):
        """Feed 5 ms of samples; returns 'press', 'release' or None."""
        hop = hop - hop.mean()
        rms = 20 * np.log10(hop.std() + 1e-9)
        self.recent = (self.recent + [rms])[-8:]
        if self.a.verbose:
            print(f"  {rms:6.1f} dB", flush=True)

        if not self.held:
            # 3 of the last 4 hops above the silent floor: the press click or speech
            if sum(r > self.floor + self.a.margin for r in self.recent[-4:]) >= 3:
                self.held, self.quiet_for = True, 0.0
                return "press"
            return None

        silent = rms < self.floor + self.a.quiet_margin
        self.quiet_for = self.quiet_for + HOP / RATE if silent else 0.0
        # the button opening: loud within the last 10 ms, silent now
        if silent and len(self.recent) > 2 and max(self.recent[-3:-1]) > self.floor + self.a.cut_margin:
            self.held = False
            return "release"
        if self.quiet_for >= self.a.hang:
            self.held = False
            return "release"
        return None


def calibrate(read, seconds):
    levels = []
    for _ in range(int(seconds * RATE / HOP)):
        h = read()
        levels.append(20 * np.log10((h - h.mean()).std() + 1e-9))
    return float(np.median(levels))


def usb_source(match):
    """The PipeWire source for the USB sound card, and its ALSA card number."""
    out = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True).stdout
    names = [l.split("\t")[1] for l in out.splitlines() if match in l and ".monitor" not in l]
    if not names:
        sys.exit(f"no input matching '{match}' - is the USB sound card plugged in?")
    card = next((l.split()[0] for l in Path("/proc/asound/cards").read_text().splitlines()
                 if l.strip() and l.split()[0].isdigit() and "USB-Audio" in l), None)
    return names[0], card


def setup_mixer(card, gain):
    """Auto gain pumps the floor and hides the release cut-off; full gain clips speech."""
    if card is None:
        return
    for ctl, val in (("Auto Gain Control", "off"), ("Mic Capture Volume", str(gain))):
        subprocess.run(["amixer", "-q", "-c", card, "cset", f"name={ctl}", val], check=False)


def claude_voice_mode():
    """'tap' or 'hold', as set by /voice in Claude Code."""
    try:
        return json.loads((Path.home() / ".claude" / "settings.json").read_text())["voice"]["mode"]
    except (OSError, ValueError, KeyError, TypeError):
        return "hold"


LOCK = Path.home() / ".cache" / "mic-ptt" / "lock"
TTS_DIR = Path.home() / "Code" / "claude-tts"


def start_ttsd(args):
    """Start the speech service (keeps Claude's voice loaded) alongside push-to-talk."""
    if args.no_tts:
        print("speech service off (--no-tts): Claude's voice model is not loaded", flush=True)
        return None
    if not (TTS_DIR / "ttsd.py").exists():
        return None
    log = open(Path.home() / ".cache" / "claude-tts" / "ttsd.log", "a")
    print("starting the speech service (ttsd)", flush=True)
    return subprocess.Popen([str(TTS_DIR / ".venv" / "bin" / "python"), str(TTS_DIR / "ttsd.py")],
                            cwd=TTS_DIR, stdout=log, stderr=log)


def single_instance():
    """Only one copy may press keys: stop any running copy, then hold the lock until we exit."""
    import fcntl
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    # copies from before the lock existed don't hold it: find them by command line
    me = os.getpid()
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            cmd = (proc / "cmdline").read_bytes().split(b"\0")
            if int(proc.name) != me and any(c.endswith(b"mic-ptt/ptt.py") for c in cmd) \
                    and b"--file" not in cmd and b"--dry-run" not in cmd:
                print(f"stopping the running copy (pid {proc.name})", flush=True)
                os.kill(int(proc.name), signal.SIGTERM)
                os.kill(int(proc.name), signal.SIGCONT)   # a suspended (Ctrl+Z) copy only sees TERM once resumed
        except (OSError, ValueError):
            pass
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    for _ in range(40):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            try:
                old = int(os.pread(fd, 32, 0).decode().strip() or 0)
                if old:
                    print(f"stopping the running copy (pid {old})", flush=True)
                    os.kill(old, signal.SIGTERM)
                    os.kill(old, signal.SIGCONT)
            except (ValueError, ProcessLookupError):
                pass
            time.sleep(0.1)
    else:
        sys.exit("another copy is still running and wouldn't stop")
    os.ftruncate(fd, 0)
    os.pwrite(fd, str(os.getpid()).encode(), 0)
    return fd   # keep open: the lock lasts as long as this process


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default="KEY_SPACE")
    ap.add_argument("--source", default="usb-C-Media",
                    help="record from the PipeWire source whose name contains this")
    ap.add_argument("--gain", type=int, default=22,
                    help="USB card 'Mic Capture Volume' 0-35 (35 = +23 dB clips speech)")
    ap.add_argument("--margin", type=float, default=20.0, help="dB above the silent floor that means transmitting")
    ap.add_argument("--quiet-margin", type=float, default=12.0, help="dB above the floor that still counts as silent")
    ap.add_argument("--cut-margin", type=float, default=25.0,
                    help="release when sound this far above the floor drops to silent within 10 ms")
    ap.add_argument("--hang", type=float, default=3.0, help="release after this long with no sound")
    ap.add_argument("--calibrate", type=float, default=1.5, help="seconds to measure the idle floor at startup")
    ap.add_argument("--file", help="analyse a 48 kHz mono WAV recording instead of the live mic")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tap", action=argparse.BooleanOptionalAction, default=claude_voice_mode() == "tap",
                    help="tap the key on press and on release (Claude Code voice mode 'tap'); "
                         "defaults to the mode in ~/.claude/settings.json")
    ap.add_argument("--no-tts", action="store_true",
                    help="don't start the speech service (skips loading Claude's voice model; use with other agents)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if not args.file and not args.dry_run:
        lock_fd = single_instance()  # noqa: F841
        ttsd = start_ttsd(args)

    if args.file:
        w = wave.open(args.file)
        assert w.getframerate() == RATE and w.getnchannels() == 1, "need 48 kHz mono"
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(float) / 32768
        pos = [0]

        def read():
            h = data[pos[0]:pos[0] + HOP]
            pos[0] += HOP
            if len(h) < HOP:
                raise EOFError
            return h
        clock = lambda: f"{pos[0] / RATE:7.3f}s"
        args.dry_run = True
    else:
        source, card = usb_source(args.source)
        setup_mixer(card, args.gain)
        print(f"recording from {source}", flush=True)
        rec = subprocess.Popen(["parecord", f"--device={source}", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1",
                                "--latency-msec=10"], stdout=subprocess.PIPE)

        def read():
            return np.frombuffer(rec.stdout.read(HOP * 2), dtype=np.int16).astype(float) / 32768
        clock = lambda: time.strftime("%H:%M:%S")

    kb = None
    if not args.dry_run:
        from evdev import UInput, ecodes
        code = ecodes.ecodes[args.key]
        try:
            kb = UInput({ecodes.EV_KEY: [code]}, name="mic-ptt")
        except PermissionError:
            sys.exit("No access to /dev/uinput - run setup.sh once (needs sudo), then log out/in.")

    tts_pid = Path.home() / ".cache" / "claude-tts" / "pid"
    tts_sock = Path.home() / ".cache" / "claude-tts" / "sock"

    def stop_speech():
        import socket
        try:                                    # ttsd: stops mid-word
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(0.2)
                s.connect(str(tts_sock))
                s.sendall(b'{"cmd": "stop"}\n')
        except OSError:
            pass
        try:                                    # speak.py's own fallback player
            os.killpg(int(tts_pid.read_text()), signal.SIGTERM)
        except (OSError, ValueError):
            pass

    def send(down):
        if kb and args.tap:     # tap mode: one tap starts recording, the next stops + sends
            kb.write(ecodes.EV_KEY, code, 1)
            kb.syn()
            kb.write(ecodes.EV_KEY, code, 0)
            kb.syn()
        elif kb:    # hold mode. Key first: any delay here cuts off the start of what you say
            kb.write(ecodes.EV_KEY, code, 1 if down else 0)
            kb.syn()
        if down:    # then stop Claude talking
            stop_speech()

    det = None

    def stop(*_):
        if det and det.held:
            send(False)
        if not (args.file or args.dry_run) and ttsd:
            ttsd.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print(f"calibrating for {args.calibrate}s - keep the button released...", flush=True)
    floor = calibrate(read, args.calibrate)
    if floor > -60:
        print(f"warning: the floor measured {floor:.1f} dB - was Transmit held? restart with it released", flush=True)
    print(f"{'tap' if args.tap else 'hold'} mode (Claude Code voice mode must match: /voice {'tap' if args.tap else 'hold'})",
          flush=True)
    print(f"noise floor {floor:.1f} dB; listening (release on cut-off or {args.hang}s silence)", flush=True)
    det = Detector(args, floor)
    try:
        while True:
            ev = det.step(read())
            if ev:
                send(ev == "press")
                print(f"{clock()} {ev.upper():7s} {args.key}", flush=True)
    except EOFError:
        stop()


if __name__ == "__main__":
    main()
