"""
Rostrum: a small control window (and tray icon, where the desktop has one) for ptt.py.

ptt.py runs as its own background process, so push-to-talk keeps working when this
window is closed; the app finds it again through ptt.py's lock file and reads its log.

  .venv/bin/python app.py
  ./install-desktop.sh        # add "Rostrum" to the app menu
"""
import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tts"))

from ptt import setup_mixer, usb_card  # noqa: E402

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QIcon, QTextCursor
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QMenu, QPlainTextEdit, QPushButton, QRadioButton,
                               QSpinBox, QSystemTrayIcon, QVBoxLayout, QWidget)

HERE = Path(__file__).resolve().parent
PYTHON = HERE / ".venv" / "bin" / "python"
ICON = HERE / "icons" / "rostrum.svg"
CONFIG = Path.home() / ".config" / "rostrum" / "config.json"
LOG = Path.home() / ".cache" / "rostrum" / "ptt.log"
LOCK = Path.home() / ".cache" / "rostrum" / "lock"          # holds the running ptt.py's pid
TTS_DIR = HERE / "tts"
TTS_SOCK = Path.home() / ".cache" / "rostrum" / "tts.sock"
TTS_MUTED = Path.home() / ".config" / "rostrum" / "muted"
HF_CACHE = Path.home() / ".cache" / "huggingface" / "hub"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"

WHISPER_MODELS = {
    "tiny.en": "fastest, least accurate",
    "base.en": "~0.7 s a sentence, accurate (default)",
    "small.en": "~2 s a sentence, more accurate",
    "medium.en": "slow on CPU, most accurate English",
}
DEFAULTS = {"backend": "claude", "whisper_model": "base.en", "send_enter": False, "tts": True, "gain": 22}

# where the text-to-speech hook can be wired up: (agent, config file)
TTS_HOOKS = [("Claude Code", CLAUDE_SETTINGS),
             ("Codex", Path.home() / ".codex" / "config.toml"),
             ("OpenCode", Path.home() / ".config" / "opencode" / "opencode.json")]


def load_config():
    try:
        return {**DEFAULTS, **json.loads(CONFIG.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_config(cfg):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")


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


def engine_pid():
    """pid of the running ptt.py, or None. Never touches the lock itself: ptt.py starting up would
    take a held lock to mean another copy is running, and kill the pid in it."""
    try:
        pid = int(LOCK.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if runs_engine(pid, HERE / "ptt.py") else None


def engine_holding():
    """True while the engine has Transmit down (its last logged event is a press)."""
    try:
        lines = LOG.read_text(errors="replace").splitlines()
    except OSError:
        return False
    return next((" PRESS " in l for l in reversed(lines) if " PRESS " in l or " RELEASE " in l), False)


def engine_args(cfg):
    args = [str(PYTHON), str(HERE / "ptt.py"), "--gain", str(cfg["gain"])]
    if cfg["backend"] == "generic":
        args += ["--transcribe", "--whisper-model", cfg["whisper_model"]] + (["--enter"] if cfg["send_enter"] else [])
    if not cfg["tts"]:
        args.append("--no-tts")
    return args


def start_engine(cfg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "w") as log:     # ptt.py stops any running copy itself
        subprocess.Popen(engine_args(cfg), cwd=HERE, stdout=log, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)


def stop_engine():
    pid = engine_pid()
    if pid and pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def claude_voice_mode():
    try:
        return json.loads(CLAUDE_SETTINGS.read_text())["voice"]["mode"]
    except (OSError, ValueError, KeyError, TypeError):
        return "hold"


def whisper_downloaded(name):
    return (HF_CACHE / f"models--Systran--faster-whisper-{name}").exists()


def tts_info():
    """Model, voice and wiring of the Kokoro speech service (tts/speak.py)."""
    import speak
    model = speak.KOKORO_MODEL
    return {"model": model, "size": model.stat().st_size if model.exists() else None,
            "voice": speak.KOKORO_VOICE, "lang": speak.KOKORO_LANG,
            "wired": [name for name, cfg in TTS_HOOKS if cfg.exists() and "speak.py" in cfg.read_text()]}


SPEAK_HOOK = f'"{PYTHON}" "{TTS_DIR / "speak.py"}" 2>/dev/null || true'


def claude_hook():
    """The command of Claude Code's Stop hook that reads replies aloud, or None."""
    try:
        stops = json.loads(CLAUDE_SETTINGS.read_text()).get("hooks", {}).get("Stop", [])
    except (OSError, ValueError):
        return None
    return next((h["command"] for g in stops for h in g.get("hooks", []) if "speak.py" in h.get("command", "")), None)


def set_claude_hook(on):
    """Add (or remove) the Stop hook in ~/.claude/settings.json; any older speak.py hook is replaced."""
    try:
        cfg = json.loads(CLAUDE_SETTINGS.read_text())
    except OSError:
        cfg = {}
    stops = [g for g in cfg.setdefault("hooks", {}).get("Stop", [])
             if not any("speak.py" in h.get("command", "") for h in g.get("hooks", []))]
    if on:
        stops.append({"hooks": [{"type": "command", "command": SPEAK_HOOK, "async": True}]})
    if stops:
        cfg["hooks"]["Stop"] = stops
    else:
        cfg["hooks"].pop("Stop", None)
    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS.write_text(json.dumps(cfg, indent=2) + "\n")


def tts_running():
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(0.2)
            s.connect(str(TTS_SOCK))
            s.sendall(b'{"cmd": "ping"}\n')
            return s.recv(16).startswith(b"pong")
    except OSError:
        return False


def usb_mic():
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return next((l.split("\t")[1] for l in out.splitlines() if "usb-C-Media" in l and ".monitor" not in l), None)


class NoWheel:
    """Scrolling the window over a spin box or combo box must not change it: only once clicked into."""

    def __init__(self, *a):
        super().__init__(*a)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, ev):
        if self.hasFocus():
            super().wheelEvent(ev)
        else:
            ev.ignore()


class SpinBox(NoWheel, QSpinBox):
    pass


class ComboBox(NoWheel, QComboBox):
    pass


def note(text):
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet("color: palette(placeholder-text);")
    return lbl


class Window(QWidget):
    def __init__(self, icon):
        super().__init__()
        self.cfg = load_config()
        self.setWindowTitle("Rostrum")
        self.setWindowIcon(icon)
        self.setMinimumWidth(520)
        root = QVBoxLayout(self)

        # status + start/stop
        top = QHBoxLayout()
        self.status = QLabel()
        self.status.setTextFormat(Qt.RichText)
        self.toggle = QPushButton()
        self.toggle.setMinimumWidth(90)
        self.toggle.clicked.connect(self.on_toggle)
        top.addWidget(self.status, 1)
        top.addWidget(self.toggle)
        root.addLayout(top)
        root.addWidget(note("Push-to-talk keeps running when this window is closed."))

        # target
        box = QGroupBox("Target")
        lay = QVBoxLayout(box)
        self.rb_claude = QRadioButton("Claude Code — drives its voice mode with the Space key")
        self.rb_generic = QRadioButton("Generic — types what you say into the active window (terminal, browser…)")
        self.backend = QButtonGroup(self)
        for rb in (self.rb_claude, self.rb_generic):
            self.backend.addButton(rb)
            lay.addWidget(rb)
        (self.rb_generic if self.cfg["backend"] == "generic" else self.rb_claude).setChecked(True)
        self.backend.buttonToggled.connect(lambda *_: self.changed())
        root.addWidget(box)

        # speech to text
        box = QGroupBox("Speech to text")
        form = QFormLayout(box)
        self.stt_engine = QLabel()
        self.stt_engine.setWordWrap(True)
        form.addRow("Engine:", self.stt_engine)
        self.model = ComboBox()
        for name, desc in WHISPER_MODELS.items():
            self.model.addItem(f"{name} — {desc}", name)
        self.model.setCurrentIndex(max(0, self.model.findData(self.cfg["whisper_model"])))
        self.model.currentIndexChanged.connect(lambda *_: self.changed())
        self.model_row = QLabel("Whisper model:")
        form.addRow(self.model_row, self.model)
        self.model_state = note("")
        form.addRow("", self.model_state)
        self.enter = QCheckBox("Press Enter after the text (send it)")
        self.enter.setChecked(self.cfg["send_enter"])
        self.enter.toggled.connect(lambda *_: self.changed())
        form.addRow("", self.enter)
        root.addWidget(box)

        # text to speech
        box = QGroupBox("Text to speech")
        form = QFormLayout(box)
        self.tts_engine = QLabel()
        self.tts_engine.setWordWrap(True)
        self.tts_engine.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("Engine:", self.tts_engine)
        wired = QHBoxLayout()
        self.tts_wired = QLabel()
        self.tts_wired.setWordWrap(True)
        self.hook_btn = QPushButton()
        self.hook_btn.clicked.connect(self.on_hook)
        wired.addWidget(self.tts_wired, 1)
        wired.addWidget(self.hook_btn)
        form.addRow("Wired to:", wired)
        self.tts_state = QLabel()
        form.addRow("Service:", self.tts_state)
        self.tts = QCheckBox("Keep the voice loaded while push-to-talk runs (replies start in ~1 s)")
        self.tts.setChecked(self.cfg["tts"])
        self.tts.toggled.connect(lambda *_: self.changed())
        form.addRow("", self.tts)
        self.mute = QCheckBox("Mute spoken replies")
        self.mute.setChecked(TTS_MUTED.exists())
        self.mute.toggled.connect(self.on_mute)
        form.addRow("", self.mute)
        root.addWidget(box)

        # microphone
        box = QGroupBox("Microphone")
        form = QFormLayout(box)
        self.mic = QLabel()
        self.mic.setWordWrap(True)
        form.addRow("Input:", self.mic)
        self.gain = SpinBox()
        self.gain.setRange(0, 35)
        self.gain.setValue(self.cfg["gain"])
        self.gain.setSuffix("   (35 clips speech)")
        self.gain.valueChanged.connect(lambda *_: self.changed())
        form.addRow("Gain:", self.gain)
        root.addWidget(box)

        # activity
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(300)
        self.log.setMinimumHeight(140)
        self.log.setStyleSheet("font-family: monospace;")
        root.addWidget(QLabel("Activity"))
        root.addWidget(self.log, 1)
        self.log_pos = 0

        self.restart_timer = QTimer(self, singleShot=True, interval=600, timeout=self.restart)
        self.timer = QTimer(self, interval=500, timeout=self.refresh)
        self.timer.start()
        self.refresh_static()
        self.refresh()

    # --- actions

    def changed(self):
        """Save the settings. Gain goes straight to the sound card; anything else restarts a running
        engine (after a pause, so clicking through a spin box doesn't thrash)."""
        before = dict(self.cfg)
        self.cfg.update(backend="generic" if self.rb_generic.isChecked() else "claude",
                        whisper_model=self.model.currentData(), send_enter=self.enter.isChecked(),
                        tts=self.tts.isChecked(), gain=self.gain.value())
        save_config(self.cfg)
        self.refresh_static()
        if self.cfg["gain"] != before["gain"]:
            setup_mixer(usb_card(), self.cfg["gain"])
        if engine_pid() and any(self.cfg[k] != before[k] for k in self.cfg if k != "gain"):
            self.restart_timer.start()

    def restart(self):
        """Never mid-sentence: stopping the engine while Transmit is down would cut off the dictation."""
        if engine_holding():
            self.restart_timer.start(1000)
            return
        self.log.appendPlainText("— restarting with new settings (keep Transmit released) —")
        self.start()

    def start(self):
        self.log_pos = 0
        self.log.clear()
        start_engine(self.cfg)
        QTimer.singleShot(300, self.refresh)

    def on_toggle(self):
        if engine_pid():
            stop_engine()
        else:
            self.start()
        QTimer.singleShot(300, self.refresh)

    def set_backend(self, name):
        (self.rb_generic if name == "generic" else self.rb_claude).setChecked(True)

    def on_hook(self):
        set_claude_hook(claude_hook() != SPEAK_HOOK)
        self.refresh_static()

    def on_mute(self, on):
        TTS_MUTED.parent.mkdir(parents=True, exist_ok=True)
        TTS_MUTED.touch() if on else TTS_MUTED.unlink(missing_ok=True)

    # --- display

    def refresh_static(self):
        generic = self.cfg["backend"] == "generic"
        if generic:
            self.stt_engine.setText("Whisper (faster-whisper), on this computer. "
                                    "Pasted into the focused window with Ctrl+Shift+V when you release Transmit.")
        else:
            self.stt_engine.setText(f"Claude Code's built-in dictation (audio streamed to Anthropic). "
                                    f"Voice mode: {claude_voice_mode()} — push-to-talk follows it.")
        for w in (self.model_row, self.model, self.model_state, self.enter):
            w.setVisible(generic)
        name = self.cfg["whisper_model"]
        self.model_state.setText(f"{name}: downloaded" if whisper_downloaded(name)
                                 else f"{name}: downloads from Hugging Face on first start")

        t = tts_info()
        size = f"{t['size'] / 1e6:.0f} MB" if t["size"] else "downloads (~350 MB) the first time the voice starts"
        self.tts_engine.setText(f"Kokoro v1.0, on this computer — voice {t['voice']} ({t['lang']})\n"
                                f"{t['model']} — {size}")
        hook = claude_hook()
        wired = [w for w in t["wired"] if w != "Claude Code"]
        if hook == SPEAK_HOOK:
            wired.insert(0, "Claude Code (reply hook)")
            self.hook_btn.setText("Disconnect Claude Code")
        elif hook:
            wired.insert(0, "Claude Code (reply hook, via an older install)")
            self.hook_btn.setText("Update Claude Code hook")
        else:
            self.hook_btn.setText("Connect Claude Code")
        self.tts_wired.setText(", ".join(wired) or "nothing — replies aren't read aloud")
        mic = usb_mic()
        self.mic.setText(mic or "USB sound card not found — plug it in and restart")

    def refresh(self):
        pid = engine_pid()
        if pid:
            label = "Claude Code" if self.cfg["backend"] == "claude" else f"Generic ({self.cfg['whisper_model']})"
            self.status.setText(f"<b style='color:#2e9d4b'>●</b> <b>Running</b> — {label}")
            self.toggle.setText("Stop")
        else:
            self.status.setText("<b style='color:#999'>●</b> <b>Stopped</b>")
            self.toggle.setText("Start")
        self.tts_state.setText("running" if tts_running() else "not running")
        try:
            with open(LOG) as f:
                f.seek(self.log_pos)
                new = f.read()
                self.log_pos = f.tell()
        except OSError:
            new = ""
        if new:
            self.log.moveCursor(QTextCursor.End)
            self.log.insertPlainText(new)
            self.log.moveCursor(QTextCursor.End)
        tray = getattr(self, "tray", None)
        if tray:
            tray.update(bool(pid), self.cfg["backend"])

    def closeEvent(self, ev):
        if getattr(self, "tray", None):     # with a tray icon, closing just hides the window
            ev.ignore()
            self.hide()


class Tray(QSystemTrayIcon):
    def __init__(self, icon, win):
        super().__init__(icon)
        self.win = win
        menu = QMenu()
        self.state = menu.addAction("")
        self.state.setEnabled(False)
        self.toggle = menu.addAction("", win.on_toggle)
        menu.addSeparator()
        self.claude = QAction("Target: Claude Code", menu, checkable=True)
        self.generic = QAction("Target: Generic", menu, checkable=True)
        self.claude.triggered.connect(lambda: win.set_backend("claude"))
        self.generic.triggered.connect(lambda: win.set_backend("generic"))
        menu.addAction(self.claude)
        menu.addAction(self.generic)
        menu.addSeparator()
        menu.addAction("Settings…", self.show_win)
        menu.addAction("Quit (push-to-talk keeps running)", QApplication.quit)
        self.setContextMenu(menu)
        self.activated.connect(lambda r: self.show_win() if r == QSystemTrayIcon.Trigger else None)
        self.menu = menu

    def show_win(self):
        self.win.show()
        self.win.raise_()
        self.win.activateWindow()

    def update(self, running, backend):
        self.state.setText("Running" if running else "Stopped")
        self.toggle.setText("Stop" if running else "Start")
        self.claude.setChecked(backend == "claude")
        self.generic.setChecked(backend == "generic")
        self.setToolTip(f"Rostrum — {'running' if running else 'stopped'}")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Rostrum")
    app.setDesktopFileName("rostrum")       # Wayland app id: matches rostrum.desktop
    icon = QIcon(str(ICON)) if ICON.exists() else QIcon.fromTheme("audio-input-microphone")
    app.setWindowIcon(icon)
    win = Window(icon)
    if QSystemTrayIcon.isSystemTrayAvailable():
        win.tray = Tray(icon, win)
        win.tray.show()
        app.setQuitOnLastWindowClosed(False)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
