"""
Mic push-to-talk: holds a key while you're transmitting through the MicFX on the USB sound card.

The MicFX passes no audio at all unless Transmit is held, and the USB card's line is digitally
silent otherwise, so:
  press   -> ~15 ms of sound above the silent floor (the button's press click, or speech)
  release -> the sound cuts off dead within 10 ms (the button opening) and stays off for 0.3 s,
             or --hang s of silence (released during a pause, when there's nothing to cut off)
Natural pauses in speech fade over 60+ ms; fast speech can cut off dead between words, but
sound comes back within the 0.3 s, so neither looks like a release.

  .venv/bin/python ptt.py                    # hold SPACE for Claude Code voice mode
  .venv/bin/python ptt.py --dry-run          # print events only, no key presses
  .venv/bin/python ptt.py --file usb_button.wav   # run the detector over a 48 kHz mono recording
  .venv/bin/python ptt.py --key KEY_F13      # hold a different key
  .venv/bin/python ptt.py --no-tts           # mic only: don't load Claude's voice (e.g. for other agents)
  .venv/bin/python ptt.py --transcribe       # any app (OpenCode, Codex...): local Whisper, pasted on release
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
    """Sound out of silence -> press. Sound cut off dead and staying off, or --hang s of silence -> release."""

    def __init__(self, args, floor_db):
        self.a = args
        self.floor = floor_db
        self.held = False
        self.recent = []            # rms dB of recent 5 ms hops
        self.quiet_for = 0.0
        self.cut = False            # sound cut off dead: a release, unless it comes back (a gap between words)

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
                self.held, self.quiet_for, self.cut = True, 0.0, False
                return "press"
            return None

        silent = rms < self.floor + self.a.quiet_margin
        self.quiet_for = self.quiet_for + HOP / RATE if silent else 0.0
        # the button opening: loud within the last 10 ms, silent now. Fast speech can cut off just
        # as dead between words, so it only counts once the silence has lasted --cut-confirm.
        if silent and len(self.recent) > 2 and max(self.recent[-3:-1]) > self.floor + self.a.cut_margin:
            self.cut = True
        elif not silent:
            self.cut = False
        if self.cut and self.quiet_for >= self.a.cut_confirm:
            self.held = self.cut = False
            return "release"
        if self.quiet_for >= self.a.hang:
            self.held = False
            return "release"
        return None


def voiced_clip(hops, loud_db, min_sound):
    """The held audio minus its trailing silence, or None if it holds under min_sound seconds of sound."""
    loud = [20 * np.log10((h - h.mean()).std() + 1e-9) > loud_db for h in hops]
    if sum(loud) * HOP / RATE < min_sound:
        return None
    last = len(loud) - 1 - loud[::-1].index(True)
    return np.concatenate(hops[:last + 1 + int(0.1 * RATE / HOP)])     # keep 100 ms after the last sound


TX26_NAMES = ("TX-26", "Teensy MIDI/Audio")   # its USB product name (before the rename, the Teensy default)
TX26_LOUD_DB = -42.0                          # a hop louder than this is speech (the mic's hiss sits near -48)


def tx26_port():
    """The TX-26's serial port (/dev/ttyACM*), found by its USB product name, or None."""
    for tty in sorted(Path("/sys/class/tty").glob("ttyACM*")):
        try:
            product = (tty / "device" / ".." / "product").resolve().read_text().strip()
        except OSError:
            continue
        if product in TX26_NAMES:
            return f"/dev/{tty.name}"
    return None


def tx26_source():
    """The TX-26 microphone's PipeWire source, or None."""
    out = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True).stdout
    return next((l.split("\t")[1] for l in out.splitlines() if ".monitor" not in l and
                 any(n.replace(" ", "_").replace("/", "_") in l for n in TX26_NAMES)), None)


class Switch:
    """The TX-26's push-to-talk switch, read from its USB serial port: stands in for Detector.
    step() ignores the audio and returns 'press' / 'release' as the TX-26 reports them."""

    def __init__(self, port, gain):
        import queue
        import termios
        import threading
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
        mode = termios.tcgetattr(self.fd)
        mode[3] &= ~(termios.ICANON | termios.ECHO)               # raw lines, no echo back
        termios.tcsetattr(self.fd, termios.TCSANOW, mode)
        os.write(self.fd, f"GAIN {gain}\n".encode())
        self.held, self.events = False, queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = b""
        while True:
            try:
                chunk = os.read(self.fd, 256)
            except OSError:
                chunk = b""
            if not chunk:
                self.events.put("gone")
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip() in (b"PTT 1", b"PTT 0"):
                    self.events.put(line.strip())

    def step(self, hop):
        import queue
        try:
            ev = self.events.get_nowait()
        except queue.Empty:
            return None
        if ev == "gone":
            raise EOFError("the TX-26 was unplugged")
        down = ev == b"PTT 1"
        if down == self.held:
            return None
        self.held = down
        return "press" if down else "release"


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
    return names[0], usb_card()


def usb_card():
    """ALSA card number of the USB sound card, or None."""
    try:
        cards = Path("/proc/asound/cards").read_text().splitlines()
    except OSError:
        return None
    return next((l.split()[0] for l in cards if l.strip() and l.split()[0].isdigit() and "USB-Audio" in l), None)


def setup_mixer(card, gain):
    """Auto gain pumps the floor and hides the release cut-off; full gain clips speech."""
    if card is None:
        return
    for ctl, val in (("Auto Gain Control", "off"), ("Mic Capture Volume", str(gain))):
        subprocess.run(["amixer", "-q", "-c", card, "cset", f"name={ctl}", val], check=False)


class Transcriber:
    """Local Whisper: each clip is transcribed in the background and pasted into the focused window."""

    def __init__(self, args, kb):
        import queue
        import threading
        from faster_whisper import WhisperModel
        print(f"loading Whisper '{args.whisper_model}' (downloaded on first use)...", flush=True)
        self.model = WhisperModel(args.whisper_model, device="cpu", compute_type="int8",
                                  cpu_threads=min(8, os.cpu_count() or 1))
        self.a, self.kb, self.q = args, kb, queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def add(self, clip):
        if self.a.file:             # replaying a recording: transcribe in step, so none are lost at the end
            self.handle(clip)
        else:
            self.q.put(clip)

    def _worker(self):
        while True:
            self.handle(self.q.get())

    def handle(self, clip):
        from scipy.signal import resample_poly
        if clip is None:            # a tap with no speech: Whisper would invent words
            return
        t = time.monotonic()
        audio = resample_poly(clip, 1, 3).astype(np.float32)   # 48 kHz -> 16 kHz
        segs, _ = self.model.transcribe(audio, language="en", beam_size=1, without_timestamps=True)
        text = " ".join(s.text.strip() for s in segs).strip()
        print(f"{time.strftime('%H:%M:%S')} ({time.monotonic() - t:.1f}s) {text!r}", flush=True)
        if text and self.kb:
            self.paste(text + " ")

    def paste(self, text):
        """Clipboard + Ctrl+Shift+V: the terminal paste, which works for any text and keyboard layout."""
        from evdev import ecodes as e
        subprocess.run(["wl-copy", "--", text], check=False)
        time.sleep(0.05)
        combo = [e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT, e.KEY_V]
        for k in combo:
            self.kb.write(e.EV_KEY, k, 1)
        self.kb.syn()
        for k in reversed(combo):
            self.kb.write(e.EV_KEY, k, 0)
        self.kb.syn()
        if self.a.enter:
            time.sleep(0.05)
            self.kb.write(e.EV_KEY, e.KEY_ENTER, 1)
            self.kb.write(e.EV_KEY, e.KEY_ENTER, 0)
            self.kb.syn()


def claude_voice_mode():
    """'tap' or 'hold', as set by /voice in Claude Code."""
    try:
        return json.loads((Path.home() / ".claude" / "settings.json").read_text())["voice"]["mode"]
    except (OSError, ValueError, KeyError, TypeError):
        return "hold"


LOCK = Path.home() / ".cache" / "rostrum" / "lock"
ENGINE = Path(__file__).resolve()
TTS_DIR = ENGINE.parent / "tts"


def start_ttsd(args):
    """Start the speech service (keeps Claude's voice loaded) alongside push-to-talk."""
    if args.no_tts:
        print("speech service off (--no-tts): Claude's voice model is not loaded", flush=True)
        return None
    if not (TTS_DIR / "ttsd.py").exists():
        return None
    logfile = Path.home() / ".cache" / "rostrum" / "ttsd.log"
    logfile.parent.mkdir(parents=True, exist_ok=True)
    log = open(logfile, "a")
    print("starting the speech service (ttsd)", flush=True)
    return subprocess.Popen([sys.executable, str(TTS_DIR / "ttsd.py")],
                            cwd=TTS_DIR, stdout=log, stderr=log)


def runs_engine(pid, engine):
    """True if process pid is running this install's ptt.py (a relative path resolves against its cwd)."""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    for c in cmd:
        if not c.endswith(b"ptt.py"):
            continue
        path = Path(os.fsdecode(c))
        if not path.is_absolute():
            try:                        # reading another process's cwd can be refused
                path = Path(os.readlink(f"/proc/{pid}/cwd")) / path
            except OSError:
                continue
        if path.resolve() == engine:
            return True
    return False


def single_instance():
    """Only one copy may press keys: stop any running copy, then hold the lock until we exit."""
    import fcntl
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    # copies from before the lock existed don't hold it: find them by command line
    me = os.getpid()
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            cmd = (proc / "cmdline").read_bytes().split(b"\0")
            if int(proc.name) != me and runs_engine(int(proc.name), ENGINE) \
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
    ap.add_argument("--input", choices=("auto", "tx26", "micfx"), default="auto",
                    help="tx26: the TX-26 USB mic, its switch read over USB serial; micfx: the MicFX on the USB "
                         "sound card, its button heard in the audio; auto: the TX-26 when it's plugged in")
    ap.add_argument("--tx26-port", help="the TX-26's serial port (default: found by its USB name)")
    ap.add_argument("--mic-gain", type=int, default=44, help="TX-26 mic preamp, 0-63 dB")
    ap.add_argument("--source", default="usb-C-Media",
                    help="record from the PipeWire source whose name contains this")
    ap.add_argument("--gain", type=int, default=22,
                    help="USB card 'Mic Capture Volume' 0-35 (35 = +23 dB clips speech)")
    ap.add_argument("--margin", type=float, default=20.0, help="dB above the silent floor that means transmitting")
    ap.add_argument("--quiet-margin", type=float, default=12.0, help="dB above the floor that still counts as silent")
    ap.add_argument("--cut-margin", type=float, default=25.0,
                    help="release when sound this far above the floor drops to silent within 10 ms")
    ap.add_argument("--cut-confirm", type=float, default=0.3,
                    help="a cut-off only counts as a release once the silence lasts this long (s); "
                         "shorter gaps are fast speech between words")
    ap.add_argument("--hang", type=float, default=3.0, help="release after this long with no sound")
    ap.add_argument("--calibrate", type=float, default=1.5, help="seconds to measure the idle floor at startup")
    ap.add_argument("--file", help="analyse a 48 kHz mono WAV recording instead of the live mic")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tap", action=argparse.BooleanOptionalAction, default=claude_voice_mode() == "tap",
                    help="tap the key on press and on release (Claude Code voice mode 'tap'); "
                         "defaults to the mode in ~/.claude/settings.json")
    ap.add_argument("--no-tts", action="store_true",
                    help="don't start the speech service (skips loading Claude's voice model; use with other agents)")
    ap.add_argument("--transcribe", action="store_true",
                    help="transcribe locally with Whisper and paste into the focused window, instead of "
                         "driving Claude Code's voice mode (for OpenCode, Codex, ...)")
    ap.add_argument("--whisper-model", default="base.en", help="e.g. tiny.en, base.en, small.en (slower, more accurate)")
    ap.add_argument("--enter", action="store_true", help="--transcribe: press Enter after pasting (send the prompt)")
    ap.add_argument("--min-clip", type=float, default=0.5,
                    help="--transcribe: ignore holds with less than this much sound (s), e.g. taps")
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
        port = args.tx26_port or (tx26_port() if args.input != "micfx" else None)
        if args.input == "tx26" and not port:
            sys.exit("no TX-26 found - is it plugged in?")
        switch = Switch(port, args.mic_gain) if port else None
        source = None
        if switch:
            print(f"TX-26 on {port}: its switch is read over USB (mic gain {args.mic_gain} dB)", flush=True)
            source = tx26_source()
            if source and not args.dry_run and not args.transcribe:
                subprocess.run(["pactl", "set-default-source", source], check=False)
                print(f"made the TX-26 the default mic, for Claude Code's voice mode: {source}", flush=True)
        else:
            source, card = usb_source(args.source)
            setup_mixer(card, args.gain)
        rec = None
        if not switch or args.transcribe:
            print(f"recording from {source}", flush=True)
            rec = subprocess.Popen(["parecord", f"--device={source}", "--raw", "--format=s16le", f"--rate={RATE}",
                                    "--channels=1", "--latency-msec=10"], stdout=subprocess.PIPE)

        def read():
            if rec is None:         # the TX-26 in Claude mode: only its switch matters, Claude records itself
                time.sleep(HOP / RATE)
                return np.zeros(HOP)
            return np.frombuffer(rec.stdout.read(HOP * 2), dtype=np.int16).astype(float) / 32768
        clock = lambda: time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"

    kb = None
    if not args.dry_run:
        from evdev import UInput, ecodes
        code = ecodes.ecodes[args.key]
        keys = [ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTSHIFT, ecodes.KEY_V, ecodes.KEY_ENTER] if args.transcribe else [code]
        try:
            kb = UInput({ecodes.EV_KEY: keys}, name="rostrum")
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
        if args.transcribe:
            pass
        elif kb and args.tap:     # tap mode: one tap starts recording, the next stops + sends
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
        if not args.file and rec:
            rec.terminate()     # else parecord outlives us and writes errors into the (next) log
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    if args.file or not switch:
        print(f"calibrating for {args.calibrate}s - keep the button released...", flush=True)
        try:
            floor = calibrate(read, args.calibrate)
        except EOFError:
            sys.exit(f"the recording is shorter than the {args.calibrate}s calibration")
        if floor > -60:
            print(f"warning: the floor measured {floor:.1f} dB - was Transmit held? restart with it released", flush=True)
    if args.transcribe:
        print("transcribe mode: speech is pasted into the focused window" + (" and sent" if args.enter else ""), flush=True)
    else:
        print(f"{'tap' if args.tap else 'hold'} mode (Claude Code voice mode must match: /voice {'tap' if args.tap else 'hold'})",
              flush=True)
    if args.file or not switch:
        print(f"noise floor {floor:.1f} dB; listening (release on cut-off or {args.hang}s silence)", flush=True)
        det, loud = Detector(args, floor), floor + args.margin
    else:
        print("listening to the TX-26's switch", flush=True)
        det, loud = switch, TX26_LOUD_DB
    stt = Transcriber(args, kb) if args.transcribe else None
    preroll = int(0.1 * RATE / HOP)             # keep 100 ms before the press
    hops = []
    try:
        while True:
            hop = read()
            ev = det.step(hop)
            hops = (hops + [hop]) if det.held or ev == "release" else (hops + [hop])[-preroll:]
            if ev:
                send(ev == "press")
                print(f"{clock()} {ev.upper():7s} {'' if stt else args.key}", flush=True)
                if ev == "release" and stt:
                    stt.add(voiced_clip(hops, loud, args.min_clip))
                    hops = []
    except EOFError as e:
        if str(e):
            print(e, flush=True)
        stop()


if __name__ == "__main__":
    main()
