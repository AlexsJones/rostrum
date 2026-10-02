"""
Regression tests: replay the recorded MicFX sessions through ptt.py exactly as it runs live.

  .venv/bin/python -m unittest discover -s tests -v
  ROSTRUM_SKIP_WHISPER=1 ...     # skip the transcription test (it downloads Whisper base.en once)

usb_live.wav   hold+speak+release while talking (4.2-7.6 s); hold+speak, stop talking, then
               release (9.7-16.9 s); three quick taps (19.3-20.3 s)
usb_button.wav hold+speak (9.55-14.88 s); a long hold (17.6-31.7 s); four quick taps
Times below are where the sound starts / cuts off in the recordings.
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
CUT_CONFIRM = 0.3       # ptt.py --cut-confirm default: a release lands this long after the cut-off
TOL = 0.03


def run(wav, *args):
    """ptt.py over a recording: [(seconds, 'PRESS'|'RELEASE')] and the full output."""
    out = subprocess.run([PY, str(ROOT / "ptt.py"), "--file", str(wav), *args],
                         capture_output=True, text=True, cwd=ROOT, timeout=300).stdout
    return [(float(m[1]), m[2]) for m in re.finditer(r"^\s*([\d.]+)s (PRESS|RELEASE)", out, re.M)], out


def load(name):
    with wave.open(str(ROOT / name)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).copy()


def save(samples, path):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(samples.astype(np.int16).tobytes())


def between(events, start, end):
    return [e for e in events if start <= e[0] <= end]


class Detector(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live, _ = run(ROOT / "usb_live.wav")
        cls.button, _ = run(ROOT / "usb_button.wav")

    def assertAt(self, event, kind, t):
        self.assertEqual(event[1], kind)
        self.assertAlmostEqual(event[0], t, delta=TOL, msg=f"{kind} at {event[0]}, expected {t}")

    def test_press_on_the_click_release_on_the_cut_off(self):
        a, b = between(self.live, 4, 8)
        self.assertAt(a, "PRESS", 4.225)
        self.assertAt(b, "RELEASE", 7.56 + CUT_CONFIRM)

    def test_release_after_you_stopped_talking(self):
        # speech ends ~13.95 s; the button's own release click + cut-off comes at 16.86 s
        a, b = between(self.live, 9, 18)
        self.assertAt(a, "PRESS", 9.74)
        self.assertAt(b, "RELEASE", 16.86 + CUT_CONFIRM)

    def test_long_hold_with_pauses_stays_held(self):
        a, b = between(self.button, 9, 16)
        self.assertAt(a, "PRESS", 9.565)
        self.assertAt(b, "RELEASE", 14.875 + CUT_CONFIRM)
        press = between(self.button, 16, 32)
        self.assertEqual([e[1] for e in press], ["PRESS"], "the 17.6-31.7 s hold was split")

    def test_quick_taps_end_released(self):
        taps = between(self.live, 19, 21)
        self.assertGreaterEqual(len(taps), 2)
        self.assertEqual(taps[0][1], "PRESS")
        self.assertEqual(taps[-1][1], "RELEASE")
        self.assertEqual(self.button[-1][1], "RELEASE")

    def test_every_press_has_a_release(self):
        for events in (self.live, self.button):
            self.assertEqual([e[1] for e in events], ["PRESS", "RELEASE"] * (len(events) // 2))

    def test_fast_speech_gaps_are_not_releases(self):
        # talking fast, the MicFX goes dead silent between words for 30-170 ms (seen live: the
        # engine released and re-pressed mid-sentence, which ended Claude's dictation)
        d = load("usb_live.wav")
        for start, ms in [(11.0, 30), (12.5, 45), (14.2, 170)]:
            d[int(start * 48000):int(start * 48000) + ms * 48] = 0
        with tempfile.TemporaryDirectory() as tmp:
            save(d, Path(tmp) / "fast.wav")
            events, _ = run(Path(tmp) / "fast.wav")
        self.assertEqual([e[1] for e in between(events, 9, 18)], ["PRESS", "RELEASE"])

    def test_silence_never_presses(self):
        with tempfile.TemporaryDirectory() as tmp:
            save(load("usb_button.wav")[:9 * 48000], Path(tmp) / "idle.wav")
            events, out = run(Path(tmp) / "idle.wav")
        self.assertEqual(events, [])
        self.assertIn("noise floor", out)

    def test_recording_too_short_to_calibrate(self):
        with tempfile.TemporaryDirectory() as tmp:
            save(load("usb_live.wav")[:48000], Path(tmp) / "short.wav")
            r = subprocess.run([PY, str(ROOT / "ptt.py"), "--file", str(Path(tmp) / "short.wav")],
                               capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 1)
        self.assertIn("shorter than", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


@unittest.skipIf(os.environ.get("ROSTRUM_SKIP_WHISPER"), "ROSTRUM_SKIP_WHISPER set")
class Transcribe(unittest.TestCase):
    def test_sentences_word_for_word_and_taps_skipped(self):
        _, out = run(ROOT / "usb_live.wav", "--transcribe")
        texts = re.findall(r"\(\d+\.\ds\) (['\"])(.*)\1$", out, re.M)
        self.assertEqual([t[1].lower().rstrip(".") for t in texts], [
            "this is me saying a sentence and letting go",
            "this is me saying a sentence and then waiting a second. now i'm going to let go",
        ])


class TX26(unittest.TestCase):
    """The TX-26's switch arrives over USB serial; a pseudo-terminal stands in for the Teensy."""

    def test_switch_lines_become_press_and_release(self):
        import pty
        import select
        import time
        master, slave = pty.openpty()
        proc = subprocess.Popen([PY, str(ROOT / "ptt.py"), "--tx26-port", os.ttyname(slave), "--dry-run", "--no-tap"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT)
        try:
            sent = b""
            while b"\n" not in sent and select.select([master], [], [], 10)[0]:
                sent += os.read(master, 64)
            self.assertEqual(sent.strip(), b"GAIN 44")
            for line in (b"PTT 1", b"PTT 0", b"PTT 1", b"PTT 0"):    # flickers: a few ms each, dropped
                os.write(master, line + b"\r\n")
                time.sleep(0.005)
            for line in (b"LEVEL 0.010", b"PTT 1", b"PTT 1", b"LEVEL 0.300", b"PTT 0"):
                os.write(master, line + b"\r\n")
                time.sleep(0.2)
        finally:
            os.close(master)            # the TX-26 unplugged: the engine should stop by itself
            out = proc.communicate(timeout=20)[0]
            os.close(slave)
        events = re.findall(r"(PRESS|RELEASE)", out)
        self.assertEqual(events, ["PRESS", "RELEASE"], out)
        self.assertIn("unplugged", out)


class App(unittest.TestCase):
    """The settings window must never cut off dictation (seen live: scrolling over Gain restarted it)."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        sys.path.insert(0, str(ROOT))
        import app
        cls.app = app
        from PySide6.QtWidgets import QApplication
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.patched = {n: getattr(self.app, n) for n in ("CONFIG", "LOG", "setup_mixer", "engine_pid", "start_engine")}
        self.mixer, self.starts = [], []
        self.app.CONFIG, self.app.LOG = tmp / "config.json", tmp / "ptt.log"
        self.app.setup_mixer = lambda card, gain: self.mixer.append(gain)
        self.app.engine_pid = lambda: 1234
        self.app.start_engine = lambda cfg: self.starts.append(cfg["backend"])
        self.win = self.app.Window(self.app.QIcon())

    def tearDown(self):
        self.win.timer.stop()
        self.win.restart_timer.stop()
        self.win.deleteLater()
        for n, v in self.patched.items():
            setattr(self.app, n, v)
        self.tmp.cleanup()

    def wheel(self, widget):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtWidgets import QApplication
        QApplication.sendEvent(widget, QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, 120),
                                                   Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))

    def test_scrolling_past_the_settings_changes_nothing(self):
        gain, model = self.win.gain.value(), self.win.model.currentIndex()
        self.wheel(self.win.gain)
        self.wheel(self.win.model)
        self.assertEqual((self.win.gain.value(), self.win.model.currentIndex()), (gain, model))

    def test_gain_goes_to_the_sound_card_without_a_restart(self):
        self.win.gain.setValue(self.win.gain.value() - 1)
        self.assertEqual(self.mixer, [self.win.gain.value()])
        self.assertFalse(self.win.restart_timer.isActive())

    def test_restart_waits_for_transmit_to_be_released(self):
        self.app.LOG.write_text("10:00:00.000 PRESS   KEY_SPACE\n")
        self.win.set_backend("generic" if self.win.cfg["backend"] == "claude" else "claude")
        self.win.restart_timer.stop()
        self.win.restart()
        self.assertEqual(self.starts, [])
        self.assertTrue(self.win.restart_timer.isActive())
        self.app.LOG.write_text("10:00:00.000 PRESS   KEY_SPACE\n10:00:04.000 RELEASE KEY_SPACE\n")
        self.win.restart()
        self.assertEqual(len(self.starts), 1)


if __name__ == "__main__":
    unittest.main()
