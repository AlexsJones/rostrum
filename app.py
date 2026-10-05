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
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tts"))

from ptt import setup_mixer, tx26_port, usb_card  # noqa: E402

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QTextCursor
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QMenu, QPlainTextEdit, QPushButton, QSpinBox, QSystemTrayIcon,
                               QTabWidget, QVBoxLayout, QWidget)

HERE = Path(__file__).resolve().parent
PYTHON = HERE / ".venv" / "bin" / "python"
ICON = HERE / "icons" / "rostrum.svg"
CONFIG = Path.home() / ".config" / "rostrum" / "config.json"
LOG = Path.home() / ".cache" / "rostrum" / "ptt.log"
LOCK = Path.home() / ".cache" / "rostrum" / "lock"          # holds the running ptt.py's pid
LEVEL = Path.home() / ".cache" / "rostrum" / "level"        # ptt.py's live mic level, for the meter
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
# push-to-talk targets, in tab order. Generic and OpenCode both transcribe locally and paste; each keeps
# its own Whisper model and Enter setting (OpenCode sends by default: its prompt is the only place it goes).
TARGETS = {"claude": "Claude Code", "generic": "Generic", "opencode": "OpenCode"}
DEFAULTS = {"backend": "claude", "whisper_model": "base.en", "send_enter": False,
            "opencode_whisper_model": "base.en", "opencode_enter": True, "tts": True, "gain": 22,
            "source": ""}      # "" = auto (ptt.py picks the TX-26, else the USB sound card)
# per transcribing target: (its Whisper model key, its Enter key) in the config
WHISPER_KEYS = {"generic": ("whisper_model", "send_enter"),
                "opencode": ("opencode_whisper_model", "opencode_enter")}

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
    if cfg["source"]:
        args += ["--source", cfg["source"]]
    if cfg["backend"] in WHISPER_KEYS:
        model, enter = WHISPER_KEYS[cfg["backend"]]
        args += ["--transcribe", "--whisper-model", cfg[model]] + (["--enter"] if cfg[enter] else [])
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


def mtime(path):
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def claude_settings():
    try:
        return json.loads(CLAUDE_SETTINGS.read_text())
    except OSError:
        return {}


def write_claude_settings(cfg):
    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS.write_text(json.dumps(cfg, indent=2) + "\n")


def claude_voice():
    """Claude Code's dictation settings (/voice): enabled, mode ('hold' or 'tap'), autoSubmit."""
    try:
        voice = claude_settings().get("voice")
    except ValueError:
        return {}
    return voice if isinstance(voice, dict) else {}


def set_claude_voice(**changes):
    """Change keys of the "voice" object in ~/.claude/settings.json, leaving everything else as it was."""
    cfg = claude_settings()
    if not isinstance(cfg.get("voice"), dict):
        cfg["voice"] = {}
    cfg["voice"].update(changes)
    write_claude_settings(cfg)


def claude_voice_mode():
    mode = claude_voice().get("mode")
    return mode if mode in ("hold", "tap") else "hold"


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
    cfg = claude_settings()
    stops = [g for g in cfg.setdefault("hooks", {}).get("Stop", [])
             if not any("speak.py" in h.get("command", "") for h in g.get("hooks", []))]
    if on:
        stops.append({"hooks": [{"type": "command", "command": SPEAK_HOOK, "async": True}]})
    if stops:
        cfg["hooks"]["Stop"] = stops
    else:
        cfg["hooks"].pop("Stop", None)
    write_claude_settings(cfg)


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
    """What push-to-talk will use: the TX-26 when it's plugged in, else the MicFX's USB sound card."""
    if tx26_port():
        return "TX-26 (its switch is read over USB)"
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    card = next((l.split("\t")[1] for l in out.splitlines() if "usb-C-Media" in l and ".monitor" not in l), None)
    return card and f"MicFX on the USB sound card ({card})"


def list_sources():
    """Recordable PipeWire sources (not monitors), as (description, name) pairs for the Source picker."""
    try:
        out = subprocess.run(["pactl", "list", "sources"], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    sources, name, desc = [], None, None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Name:"):
            name, desc = line.split(":", 1)[1].strip(), None
        elif line.startswith("Description:"):
            desc = line.split(":", 1)[1].strip()
        elif line.startswith('device.description = "'):
            desc = desc or line.split('"', 2)[1]
        if name and desc is not None and not name.endswith(".monitor"):
            sources.append((desc, name))
            name = desc = None     # one entry per source; wait for the next Name:
    return sources


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


class LevelMeter(QWidget):
    """A VU-style bar of the mic's recent peak: green while there's headroom, amber loud, red near
    clipping. The noise floor is a tick, so you can raise the gain until speech sits well above it
    without the peaks going red."""

    MIN_DB, MAX_DB = -54.0, 0.0

    def __init__(self):
        super().__init__()
        self.setMinimumSize(200, 16)
        self.db = self.floor = None     # None: no signal (engine stopped, or Claude records its own audio)

    def show_level(self, db, floor):
        if (db, floor) != (self.db, self.floor):
            self.db, self.floor = db, floor
            self.update()

    def _x(self, r, db):
        frac = max(0.0, min(1.0, (db - self.MIN_DB) / (self.MAX_DB - self.MIN_DB)))
        return r.x() + int(r.width() * frac)

    def paintEvent(self, _):
        p = QPainter(self)
        r = self.rect().adjusted(0, 0, -1, -1)
        p.fillRect(r, self.palette().base())
        if self.db is None:
            p.setPen(self.palette().placeholderText().color())
            p.drawText(r, Qt.AlignCenter, "no signal")
        else:
            col = QColor("#e5484d") if self.db >= -4 else QColor("#e2a336") if self.db >= -12 else QColor("#30a46c")
            p.fillRect(r.x(), r.y(), self._x(r, self.db) - r.x(), r.height(), col)
            if self.floor is not None and self.floor > self.MIN_DB:
                fx = self._x(r, self.floor)
                p.setPen(self.palette().text().color())
                p.drawLine(fx, r.y(), fx, r.y() + r.height())
        p.setPen(self.palette().mid().color())
        p.drawRect(r)


def note(text):
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setAlignment(Qt.AlignLeft | Qt.AlignTop)     # spare height goes below the text, not above it
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

        # one tab per target; the one push-to-talk drives is ticked, and opens first
        self.tabs = QTabWidget()
        self.use = {}
        builders = {"claude": self.claude_tab, "generic": lambda: self.whisper_tab("generic"),
                    "opencode": lambda: self.whisper_tab("opencode")}
        for name, label in TARGETS.items():
            self.tabs.addTab(builders[name](), label)
        self.tabs.setCurrentIndex(list(TARGETS).index(self.cfg["backend"]))
        root.addWidget(self.tabs)

        # text to speech: one service, whichever agent it reads for
        box = QGroupBox("Text to speech")
        form = QFormLayout(box)
        self.tts_engine = QLabel()
        self.tts_engine.setWordWrap(True)
        self.tts_engine.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("Engine:", self.tts_engine)
        self.tts_wired = QLabel()
        self.tts_wired.setWordWrap(True)
        form.addRow("Wired to:", self.tts_wired)
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
        self.source = ComboBox()
        self.source.currentIndexChanged.connect(lambda *_: self.changed())
        form.addRow("Source:", self.source)
        self.meter = LevelMeter()
        form.addRow("Level:", self.meter)
        self.gain = SpinBox()
        self.gain.setRange(0, 35)
        self.gain.setValue(self.cfg["gain"])
        self.gain.setSuffix("   (35 clips speech)")
        self.gain.valueChanged.connect(lambda *_: self.changed())
        form.addRow("Gain:", self.gain)
        self.gain_note = note("")
        form.addRow("", self.gain_note)
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
        self.meter_timer = QTimer(self, interval=100, timeout=self.update_meter)
        self.meter_timer.start()
        self.inputs_timer = QTimer(self, interval=2000, timeout=self.refresh_inputs)
        self.inputs_timer.start()
        self.refresh_static()
        self.refresh()

    # --- target tabs

    def use_button(self, name):
        """The tab's 'drive this target' button; disabled (and ticked) on the active target's tab."""
        btn = QPushButton()
        btn.clicked.connect(lambda: self.set_backend(name))
        self.use[name] = btn
        return btn

    def claude_tab(self):
        page = QWidget()
        form = self.claude_form = QFormLayout(page)
        form.addRow(self.use_button("claude"))
        form.addRow(note("Transmit holds Space, Claude Code's push-to-talk key; Claude Code does the "
                         "transcribing (audio streamed to Anthropic)."))
        self.voice_mode = ComboBox()
        self.voice_mode.addItem("hold: talk while Transmit is held", "hold")
        self.voice_mode.addItem("tap: press to start, press again to send", "tap")
        self.voice_mode.currentIndexChanged.connect(self.on_voice_mode)
        mine = "Claude Code's own setting, in ~/.claude/settings.json (the one /voice changes). Open sessions " \
               "pick changes up straight away."
        self.voice_mode.setToolTip(mine)
        form.addRow("Voice mode:", self.voice_mode)
        self.autosubmit = QCheckBox("Send the prompt when you release Transmit")
        self.autosubmit.toggled.connect(self.on_autosubmit)
        self.autosubmit.setToolTip(mine)
        form.addRow("", self.autosubmit)
        self.autosubmit_note = note("")
        form.addRow("", self.autosubmit_note)
        self.voice_off = QPushButton("Voice dictation is off in Claude Code: turn it on")
        self.voice_off.clicked.connect(lambda: (set_claude_voice(enabled=True, mode=self.voice_mode.currentData()),
                                                self.refresh_static()))
        form.addRow("", self.voice_off)
        hook = QHBoxLayout()
        self.hook_state = QLabel()
        self.hook_state.setWordWrap(True)
        self.hook_btn = QPushButton()
        self.hook_btn.clicked.connect(self.on_hook)
        hook.addWidget(self.hook_state, 1)
        hook.addWidget(self.hook_btn)
        form.addRow("Replies:", hook)
        return page

    def whisper_tab(self, name):
        """Generic and OpenCode: local Whisper, pasted with Ctrl+Shift+V into the focused window."""
        model_key, enter_key = WHISPER_KEYS[name]
        page = QWidget()
        form = QFormLayout(page)
        form.addRow(self.use_button(name))
        if name == "opencode":
            form.addRow(note("OpenCode has no voice input of its own: Rostrum transcribes what you say with "
                             "Whisper on this computer and pastes it into OpenCode's prompt. Keep its terminal "
                             "focused while you talk."))
        else:
            form.addRow(note("Transcribed with Whisper on this computer and pasted into whatever window is "
                             "focused when you release Transmit: Codex, a terminal, a browser."))
        model = ComboBox()
        for m, desc in WHISPER_MODELS.items():
            model.addItem(f"{m} — {desc}", m)
        model.setCurrentIndex(max(0, model.findData(self.cfg[model_key])))
        model.currentIndexChanged.connect(lambda *_: self.changed())
        form.addRow("Whisper model:", model)
        state = note("")
        form.addRow("", state)
        enter = QCheckBox("Press Enter after the text (send it)")
        enter.setChecked(self.cfg[enter_key])
        enter.toggled.connect(lambda *_: self.changed())
        form.addRow("", enter)
        if name == "opencode":
            form.addRow("Replies:", note("not read aloud: OpenCode isn't wired to the voice yet"))
            self.oc_model, self.oc_model_state, self.oc_enter = model, state, enter
        else:
            self.model, self.model_state, self.enter = model, state, enter
        return page

    # --- actions

    def changed(self):
        """Save the settings. Gain goes straight to the sound card; anything else restarts a running
        engine (after a pause, so clicking through a spin box doesn't thrash)."""
        before = dict(self.cfg)
        self.cfg.update(whisper_model=self.model.currentData(), send_enter=self.enter.isChecked(),
                        opencode_whisper_model=self.oc_model.currentData(),
                        opencode_enter=self.oc_enter.isChecked(),
                        tts=self.tts.isChecked(), gain=self.gain.value(),
                        source=self.source.currentData() or "")
        save_config(self.cfg)
        self.refresh_static()
        if self.cfg["gain"] != before["gain"]:
            setup_mixer(usb_card(), self.cfg["gain"])
        if engine_pid() and self.engine_settings(before) != self.engine_settings(self.cfg):
            self.restart_timer.start()

    @staticmethod
    def engine_settings(cfg):
        """What the running engine was started with: only these need a restart (an inactive tab's don't)."""
        keys = ["backend", "tts", "source"] + list(WHISPER_KEYS.get(cfg["backend"], ()))
        return {k: cfg[k] for k in keys}

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
        """Make `name` the target push-to-talk drives (its tab's button, or the tray menu)."""
        if name == self.cfg["backend"]:
            return
        self.cfg["backend"] = name
        save_config(self.cfg)
        self.tabs.setCurrentIndex(list(TARGETS).index(name))
        self.refresh_static()
        if engine_pid():
            self.restart_timer.start()

    def on_voice_mode(self, *_):
        mode = self.voice_mode.currentData()
        if mode == claude_voice_mode() and claude_voice().get("enabled", False):
            return
        set_claude_voice(mode=mode, enabled=True)
        self.refresh_static()
        if engine_pid() and self.cfg["backend"] == "claude":   # the engine reads the mode when it starts
            self.restart_timer.start()

    def on_autosubmit(self, on):
        if on != bool(claude_voice().get("autoSubmit")):
            set_claude_voice(autoSubmit=on)     # Claude Code reloads it live; no restart needed
        self.refresh_static()

    def on_hook(self):
        set_claude_hook(claude_hook() != SPEAK_HOOK)
        self.refresh_static()

    def on_mute(self, on):
        TTS_MUTED.parent.mkdir(parents=True, exist_ok=True)
        TTS_MUTED.touch() if on else TTS_MUTED.unlink(missing_ok=True)

    # --- display

    def refresh_static(self):
        active = self.cfg["backend"]
        for i, (name, label) in enumerate(TARGETS.items()):
            self.tabs.setTabText(i, f"✓ {label}" if name == active else label)
            self.use[name].setText("✓ Push-to-talk drives this target" if name == active
                                   else f"Use {label} for push-to-talk")
            self.use[name].setEnabled(name != active)
        self.fit_tabs()
        for name, combo, state in (("generic", self.model, self.model_state),
                                   ("opencode", self.oc_model, self.oc_model_state)):
            m = combo.currentData()
            state.setText(f"{m}: downloaded" if whisper_downloaded(m)
                          else f"{m}: downloads from Hugging Face on first start")
        self.sync_claude()

        t = tts_info()
        size = f"{t['size'] / 1e6:.0f} MB" if t["size"] else "downloads (~350 MB) the first time the voice starts"
        self.tts_engine.setText(f"Kokoro v1.0, on this computer — voice {t['voice']} ({t['lang']})\n"
                                f"{t['model']} — {size}")
        hook = claude_hook()
        wired = [w for w in t["wired"] if w != "Claude Code"]
        if hook == SPEAK_HOOK:
            wired.insert(0, "Claude Code (reply hook)")
            self.hook_state.setText("read aloud when Claude finishes")
            self.hook_btn.setText("Disconnect")
        elif hook:
            wired.insert(0, "Claude Code (reply hook, via an older install)")
            self.hook_state.setText("read aloud, through an older install's hook")
            self.hook_btn.setText("Update hook")
        else:
            self.hook_state.setText("not read aloud")
            self.hook_btn.setText("Read replies aloud")
        self.tts_wired.setText(", ".join(wired) or "nothing — replies aren't read aloud")
        self.refresh_inputs()

    def refresh_inputs(self):
        """The input label and the Source list, refreshed on a timer too so a mic plugged in or
        unplugged while the window is open is picked up without a restart."""
        mic = usb_mic()
        self.mic.setText(mic or "no TX-26 or USB sound card found — plug one in and restart")
        self.populate_sources()
        self.update_gain_enabled()

    def update_gain_enabled(self):
        """Gain sets the C-Media sound card's mic volume, so it only bites when that card is the one in
        use. Grey it out for other mics (a Yeti, a display, the TX-26), which use their own gain."""
        src = self.source.currentData() or ""
        micfx = "C-Media" in src if src else (usb_card() is not None and not tx26_port())
        self.gain.setEnabled(micfx)
        self.gain_note.setText("" if micfx else "Only the MicFX's USB sound card has this control — "
                                                "other mics use their own gain.")
        self.gain_note.setVisible(not micfx)

    def populate_sources(self):
        """List the recordable inputs, keeping the saved choice selected (and shown even if unplugged).
        Only rebuilt when the set of inputs changes, so it doesn't fight the user mid-selection."""
        items = [("Auto — TX-26, else the USB sound card", "")]
        items += [(desc, name) for desc, name in list_sources()]
        chosen = self.cfg["source"]
        if chosen and chosen not in [name for _, name in items]:
            items.append((f"{chosen}  (not connected)", chosen))
        if items == [(self.source.itemText(i), self.source.itemData(i)) for i in range(self.source.count())]:
            return
        self.source.blockSignals(True)
        self.source.clear()
        for desc, name in items:
            self.source.addItem(desc, name)
        self.source.setCurrentIndex(max(0, self.source.findData(chosen)))
        self.source.blockSignals(False)

    def sync_claude(self):
        """Show Claude Code's voice settings as they are now: /voice in Claude changes them too."""
        voice = claude_voice()
        self.claude_mtime = mtime(CLAUDE_SETTINGS)
        for w in (self.voice_mode, self.autosubmit):
            w.blockSignals(True)
        self.voice_mode.setCurrentIndex(max(0, self.voice_mode.findData(claude_voice_mode())))
        tap = claude_voice_mode() == "tap"
        self.autosubmit.setChecked(tap or bool(voice.get("autoSubmit")))
        self.autosubmit.setEnabled(not tap)
        for w in (self.voice_mode, self.autosubmit):
            w.blockSignals(False)
        self.autosubmit_note.setText(
            "Tap mode always sends prompts of three words or more." if tap else
            "Only prompts of three words or more: shorter ones wait for Enter (Claude Code's limit).")
        self.claude_form.setRowVisible(self.voice_off, not voice.get("enabled"))   # no gap when hidden

    def update_meter(self):
        """Read ptt.py's level file; a reading older than 1.5 s means it's not recording."""
        try:
            if time.time() - LEVEL.stat().st_mtime > 1.5:
                raise ValueError
            db, floor = LEVEL.read_text().split()
            self.meter.show_level(float(db), float(floor))
        except (OSError, ValueError):
            self.meter.show_level(None, None)

    def refresh(self):
        if mtime(CLAUDE_SETTINGS) != self.claude_mtime:
            self.refresh_static()
        pid = engine_pid()
        if pid:
            backend = self.cfg["backend"]
            label = TARGETS[backend]
            if backend in WHISPER_KEYS:
                label += f" ({self.cfg[WHISPER_KEYS[backend][0]]})"
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

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.fit_tabs()

    def showEvent(self, ev):
        super().showEvent(ev)
        QTimer.singleShot(0, self.fit_tabs)     # once laid out: before that the pages have no width

    def fit_tabs(self):
        """Tabs don't grow for wrapped text (Qt ignores height-for-width there), so the notes would be cut
        off: measure every page at the width it has now and keep the tabs at least that tall. A short
        window then squeezes the activity log instead."""
        frame = self.tabs.style().pixelMetric(self.tabs.style().PixelMetric.PM_DefaultFrameWidth)
        page = self.tabs.currentWidget()
        width = page.width() if page.width() > 100 else self.tabs.width() - 2 * frame
        if width <= 100:
            return                              # not laid out yet; showEvent measures again
        tallest = max(self.tabs.widget(i).layout().totalHeightForWidth(width) for i in range(self.tabs.count()))
        self.tabs.setMinimumHeight(tallest + self.tabs.tabBar().sizeHint().height() + 2 * frame)

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
        self.targets = {}
        for name, label in TARGETS.items():
            act = QAction(f"Target: {label}", menu, checkable=True)
            act.triggered.connect(lambda _=False, n=name: win.set_backend(n))
            menu.addAction(act)
            self.targets[name] = act
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
        for name, act in self.targets.items():
            act.setChecked(name == backend)
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
