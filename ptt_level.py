"""
Voice-activated push-to-talk: holds a key while the mic is transmitting.

The mic only passes audio while its Transmit button is held, so audio level is
the trigger. Claude Code's voice mode sees an ordinary held space bar.

  .venv/bin/python ptt.py                 # hold SPACE while you talk
  .venv/bin/python ptt.py --dry-run       # just print what it would do
  .venv/bin/python ptt.py --key KEY_F13   # hold a different key (e.g. for Codex)

Options: --threshold DB (absolute trigger level; default = noise floor + --margin),
--margin DB (default 8), --hang SECONDS of quiet before release (default 1.5),
--release-margin DB above the floor that still counts as talking (default 3),
--attack N windows above threshold before pressing (default 2, 20 ms each).
"""
import argparse
import array
import math
import signal
import statistics
import subprocess
import sys
import time

RATE, WIN = 16000, 320          # 20 ms windows


def level_db(buf):
    a = array.array("h", buf)
    if not a:
        return -120.0
    m = sum(a) / len(a)
    rms = math.sqrt(sum((x - m) ** 2 for x in a) / len(a))
    return 20 * math.log10(max(rms, 1) / 32768)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default="KEY_SPACE")
    ap.add_argument("--threshold", type=float)
    ap.add_argument("--margin", type=float, default=8.0)
    ap.add_argument("--hang", type=float, default=1.5)
    ap.add_argument("--release-margin", type=float, default=3.0,
                    help="stay held until the level drops below floor + this (dB)")
    ap.add_argument("--attack", type=int, default=2)
    ap.add_argument("--calibrate", type=float, default=2.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true", help="print the level of every 20 ms window")
    args = ap.parse_args()

    from evdev import UInput, ecodes
    code = ecodes.ecodes[args.key]
    kb = None
    if not args.dry_run:
        try:
            kb = UInput({ecodes.EV_KEY: [code]}, name="mic-ptt")
        except PermissionError:
            sys.exit("No access to /dev/uinput - run setup.sh once (needs sudo), then log out/in.")

    rec = subprocess.Popen(["parecord", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1",
                            "--latency-msec=20"], stdout=subprocess.PIPE)
    read = lambda: rec.stdout.read(WIN * 2)

    held = False

    def press(down):
        nonlocal held
        held = down
        if kb:
            kb.write(ecodes.EV_KEY, code, 1 if down else 0)
            kb.syn()
        print(f"{time.strftime('%H:%M:%S')} {'PRESS  ' if down else 'release'} {args.key}", flush=True)

    def stop(*_):
        if held:
            press(False)
        rec.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    if args.threshold is None:
        print(f"calibrating noise floor for {args.calibrate:.0f}s - keep the button released...", flush=True)
        floor = statistics.median(level_db(read()) for _ in range(int(args.calibrate * RATE / WIN)))
        threshold = floor + args.margin
    else:
        threshold = args.threshold
        floor = threshold - args.margin
    release_at = floor + args.release_margin
    print(f"noise floor {floor:.1f} dB -> press above {threshold:.1f} dB, "
          f"release after {args.hang}s below {release_at:.1f} dB", flush=True)

    above, quiet_since = 0, None
    smooth = 10 ** (floor / 10)            # power, ~250 ms moving average for the release decision
    alpha = 1 - math.exp(-WIN / RATE / 0.25)
    while True:
        db = level_db(read())
        now = time.monotonic()
        smooth += alpha * (10 ** (db / 10) - smooth)
        sdb = 10 * math.log10(smooth)
        if args.verbose:
            print(f"{db:7.1f} dB  smoothed {sdb:6.1f} {'#' * max(0, int((sdb + 60) / 2))}", flush=True)
        above = above + 1 if db >= threshold else 0
        if not held and above >= args.attack:
            press(True)
        if sdb >= release_at:
            quiet_since = None
        else:
            if held:
                quiet_since = quiet_since or now
                if now - quiet_since >= args.hang:
                    press(False)


if __name__ == "__main__":
    main()
