"""
Mic push-to-talk: holds a key while you're transmitting through the headset-jack mic.

The Transmit button leaves no reliable trace on the line, so this keys off sound:
  press   -> ~50 ms of sustained sound (speech, or the mic's press beep)
  release -> the mic's release click (a lone spike after a pause), or --hang s of quiet

  .venv/bin/python ptt.py                    # hold SPACE for Claude Code voice mode
  .venv/bin/python ptt.py --dry-run          # print events only, no key presses
  .venv/bin/python ptt.py --file recording.wav    # run the detector over a 48 kHz mono recording
  .venv/bin/python ptt.py --key KEY_F13      # hold a different key
"""
import argparse
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
TONE_WIN = 2400                 # 50 ms window for tone detection


class Detector:
    """Sustained sound -> press. Lone click after a pause, or --hang s of quiet -> release."""

    def __init__(self, args, floor_db):
        self.a = args
        self.floor = floor_db
        self.held = False
        self.recent = []            # rms dB of recent 5 ms hops
        self.quiet_for = 0.0
        self.pending_click = None

    def step(self, hop):
        """Feed 5 ms of samples; returns 'press', 'release' or None."""
        dt = HOP / RATE
        hop = hop - hop.mean()
        rms = 20 * np.log10(hop.std() + 1e-9)
        peak = 20 * np.log10(np.abs(hop).max() + 1e-9)
        self.recent = (self.recent + [rms])[-40:]
        loud = self.floor + self.a.margin
        if self.a.verbose:
            print(f"  {rms:6.1f} dB peak {peak:6.1f}", flush=True)

        if not self.held:
            # 10 of the last 16 hops (50 of 80 ms) loud: speech or the beep, not a lone click
            if sum(r > loud for r in self.recent[-16:]) >= 10:
                self.held, self.quiet_for, self.pending_click = True, 0.0, None
                return "press"
            return None

        self.quiet_for = self.quiet_for + dt if rms < loud else 0.0

        # release click: a spike after >= 150 ms of quiet, quiet again 40 ms later
        if self.pending_click is not None:
            self.pending_click += 1
            if self.pending_click >= 8:
                self.pending_click = None
                after = np.array(self.recent[-6:])
                if 10 * np.log10(np.mean(10 ** (after / 10))) < loud:
                    self.held = False
                    return "release"
        elif peak > self.floor + self.a.click_margin and len(self.recent) > 31 and max(self.recent[-31:-1]) < loud:
            self.pending_click = 0

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


LOCK = Path.home() / ".cache" / "rostrum" / "lock"
TTS_DIR = Path(__file__).resolve().parent / "tts"


def start_ttsd(args):
    """Start the speech service (keeps Claude's voice loaded) alongside push-to-talk."""
    if args.no_tts or not (TTS_DIR / "ttsd.py").exists():
        return None
    (Path.home() / ".cache" / "rostrum").mkdir(parents=True, exist_ok=True)
    log = open(Path.home() / ".cache" / "rostrum" / "ttsd.log", "a")
    print("starting the speech service (ttsd)", flush=True)
    return subprocess.Popen([sys.executable, str(TTS_DIR / "ttsd.py")],
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
            if int(proc.name) != me and any(c.endswith(b"ptt.py") and not c.endswith(b"_ptt.py") for c in cmd) \
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
    ap.add_argument("--click-margin", type=float, default=17.0,
                    help="a click peaks this many dB above the idle hiss level")
    ap.add_argument("--margin", type=float, default=6.0, help="dB above the floor that counts as sound")
    ap.add_argument("--hang", type=float, default=3.0, help="release after this long with no sound")
    ap.add_argument("--calibrate", type=float, default=1.5)
    ap.add_argument("--file", help="analyse a 48 kHz mono WAV recording instead of the live mic")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tap", action="store_true",
                    help="tap the key on press and on release (Claude Code voice mode 'tap')")
    ap.add_argument("--no-tts", action="store_true", help="don't start the speech service")
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
        rec = subprocess.Popen(["parecord", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1",
                                "--latency-msec=10"], stdout=subprocess.PIPE)

        def read():
            return np.frombuffer(rec.stdout.read(HOP * 2), dtype=np.int16).astype(float) / 32768
        clock = lambda: time.strftime("%H:%M:%S")

    kb = None
    if not args.dry_run:
        from evdev import UInput, ecodes
        code = ecodes.ecodes[args.key]
        try:
            kb = UInput({ecodes.EV_KEY: [code]}, name="rostrum")
        except PermissionError:
            sys.exit("No access to /dev/uinput - run setup.sh once (needs sudo), then log out/in.")

    tts_pid = Path.home() / ".cache" / "rostrum" / "tts.pid"
    tts_sock = Path.home() / ".cache" / "rostrum" / "tts.sock"

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
    print(f"noise floor {floor:.1f} dB; listening (release on click or {args.hang}s quiet)", flush=True)
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
