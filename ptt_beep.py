"""
Mic push-to-talk: holds a key for exactly as long as the mic's Transmit button is down.

The mic plays a ~700 ms beep (~380 Hz) when Transmit is pressed and makes a sharp
click when it is released. Audio is the only link (headset jack), so we listen for:
  press   -> the beep tone          -> key down
  release -> an isolated click      -> key up
  safety  -> no sound for --timeout -> key up (in case a click is missed)

  .venv/bin/python ptt.py                    # hold SPACE for Claude Code voice mode
  .venv/bin/python ptt.py --dry-run -v       # print events and levels, no key presses
  .venv/bin/python ptt.py --file recording.wav  # run the detector over a recording
  .venv/bin/python ptt.py --key KEY_F13      # hold a different key
"""
import argparse
import signal
import subprocess
import sys
import time
import wave

import numpy as np

RATE = 48000
HOP = 240                       # 5 ms analysis step
TONE_WIN = 2400                 # 50 ms window for tone detection


class Detector:
    def __init__(self, args, floor_db):
        self.a = args
        self.floor = floor_db
        self.held = False
        self.ignore_until_quiet = False
        self.tone_run = 0.0         # seconds of continuous beep seen
        self.quiet_for = 0.0
        self.hist = np.zeros(0)
        self.recent = []            # rms dB of the last few 5 ms hops
        self.pending_click = None   # hops since a candidate click
        freqs = np.fft.rfftfreq(TONE_WIN, 1 / RATE)
        self.band = (freqs >= args.tone - 25) & (freqs <= args.tone + 25)
        self.win = np.hanning(TONE_WIN)

    def tone_ratio(self):
        s = self.hist[-TONE_WIN:]
        if len(s) < TONE_WIN:
            return 0.0
        spec = np.abs(np.fft.rfft(s * self.win)) ** 2
        return spec[self.band].sum() / (spec.sum() + 1e-12)

    def step(self, hop):
        """Feed 5 ms of samples; returns 'press', 'release' or None."""
        dt = HOP / RATE
        hop = hop - hop.mean()
        self.hist = np.concatenate([self.hist[-(TONE_WIN - HOP):], hop])
        rms = 20 * np.log10(hop.std() + 1e-9)
        peak = 20 * np.log10(np.abs(hop).max() + 1e-9)
        self.recent = (self.recent + [rms])[-12:]
        loud = self.floor + self.a.margin

        if not self.held:
            ratio = self.tone_ratio()
            self.tone_run = self.tone_run + dt if (ratio > self.a.tone_ratio and rms > loud) else 0.0
            if self.a.verbose:
                print(f"  {rms:6.1f} dB tone {ratio:4.2f}", flush=True)
            if self.tone_run >= self.a.tone_time:
                self.held, self.quiet_for, self.pending_click = True, 0.0, None
                self.ignore_until_quiet = True      # the rest of the beep is neither speech nor a click
                self.tone_run = 0.0
                return "press"
            return None

        if self.a.verbose:
            print(f"  {rms:6.1f} dB peak {peak:6.1f}", flush=True)
        if self.ignore_until_quiet:
            if rms < loud:
                self.ignore_until_quiet = False
            return None

        self.quiet_for = self.quiet_for + dt if rms < loud else 0.0
        if self.quiet_for >= self.a.timeout:
            self.held = False
            return "release"

        # click: a spike near full scale with quiet just before it and quiet again soon after
        if peak > self.a.click_db and max(self.recent[-8:-3] or [0]) < loud:
            self.pending_click = 0                          # (a newer spike replaces an older one)
        elif self.pending_click is not None:
            self.pending_click += 1
            if self.pending_click >= 8:                     # decide 40 ms after the spike
                self.pending_click = None
                after = np.array(self.recent[-6:])          # 10-40 ms after it, power-averaged
                if 10 * np.log10(np.mean(10 ** (after / 10))) < loud:
                    self.held = False
                    return "release"
        return None


def calibrate(read, seconds):
    levels = []
    for _ in range(int(seconds * RATE / HOP)):
        h = read()
        levels.append(20 * np.log10((h - h.mean()).std() + 1e-9))
    return float(np.median(levels))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default="KEY_SPACE")
    ap.add_argument("--tone", type=float, default=380, help="press-beep frequency (Hz)")
    ap.add_argument("--tone-ratio", type=float, default=0.35, help="share of energy in the tone band")
    ap.add_argument("--tone-time", type=float, default=0.12, help="seconds of beep before pressing")
    ap.add_argument("--click-db", type=float, default=-6.0, help="peak level that counts as a click")
    ap.add_argument("--margin", type=float, default=6.0, help="dB above the floor that counts as sound")
    ap.add_argument("--timeout", type=float, default=5.0, help="release after this long with no sound")
    ap.add_argument("--calibrate", type=float, default=1.5)
    ap.add_argument("--file", help="analyse a 48 kHz mono WAV recording instead of the live mic")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

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

    def send(down):
        if kb:
            kb.write(ecodes.EV_KEY, code, 1 if down else 0)
            kb.syn()

    det = None

    def stop(*_):
        if det and det.held:
            send(False)
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print(f"calibrating for {args.calibrate}s - keep the button released...", flush=True)
    floor = calibrate(read, args.calibrate)
    print(f"noise floor {floor:.1f} dB; listening for the {args.tone:.0f} Hz beep", flush=True)
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
