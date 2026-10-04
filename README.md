# Rostrum

Push-to-talk voice input for coding agents, driven by a physical **Transmit** button on a
hand-held microphone. Hold the button, speak, let go:

- **Claude Code** target: presses Space for Claude Code's built-in voice mode (tap or hold,
  matching your `/voice` setting), and stops Claude talking when you press.
- **Generic** target: transcribes locally with Whisper and pastes the text into whatever
  window is active: OpenCode, Codex, a terminal, a browser.

- **OpenCode** target: the same local transcription, pasted into OpenCode's prompt and sent.

A small control app (**Rostrum** in the app menu) starts/stops it and has a tab for each target
with that target's settings, plus the speech-to-text and text-to-speech models and where
they're wired up.

## Hardware

This is tuned for one specific setup. Other hardware will probably need re-tuning (see
[Tuning](#tuning-for-other-hardware)).

| Part | What we use | Notes |
|---|---|---|
| Microphone | **Teenage Engineering MicFX** | Its Transmit button gates the audio: the line carries nothing unless it's held. |
| Sound card | **Ugreen USB sound card** (3.5 mm in/out) | C-Media chip, USB ID `0d8c:0014`, shows up as *"C-Media Electronics Inc. USB Audio Device"*. The MicFX plugs into its mic input. |
| Computer | Framework Laptop 13 Pro (Intel Core Ultra Series 3) | Fedora Linux 44 Workstation, GNOME 50 on Wayland, PipeWire. |

**Why the USB sound card matters.** Plugged into the laptop's headset jack, the idle line
sat at about −22 dB of hiss and the Transmit button left no reliable trace, so press/release
had to be guessed from speech and timeouts (see `ptt_laptop_jack.py`). Through the Ugreen
card the idle line is digitally silent (about −85 dB), and the button has a clean signature:

- **press**: a sharp click, then your voice
- **release**: a small click, then the sound cuts to silence within 10 ms, even if you've
  already stopped talking. Pauses in speech fade over 60 ms or more, so they're never
  mistaken for a release. Fast speech can cut off just as dead between words (for 30–170 ms),
  so a cut-off only counts once the silence has lasted 0.3 s (`--cut-confirm`).

That gives a press in about 15 ms and a release about 0.3 s after you let go. The USB card's own
*Auto Gain Control* is switched off at startup (it pumps the floor), and its mic gain is set
to 22 of 35 (35 clips speech).

The card also has a HID interface (volume/mute keys); the MicFX button does **not** show up
there, so detection is from the audio.

## Install

### Fedora (RPM)

Download `rostrum-*.x86_64.rpm` from the [releases](https://github.com/AlexsJones/rostrum/releases), then:

```sh
sudo dnf install ./rostrum-*.x86_64.rpm
```

It installs into `/opt/rostrum` with its own Python, adds **Rostrum** to the app menu, and
gives the logged-in user access to `/dev/uinput` (no group changes or `setup.sh` needed).
`rostrum-cli` runs the engine without the window. Voice models download on first use
(Whisper `base.en` ~150 MB, Kokoro ~350 MB).

### From source (Linux)

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/), PipeWire (`pactl`, `parecord`),
`alsa-utils` (`amixer`) and `wl-clipboard` (`wl-copy`).

```sh
git clone <this repo> ~/Code/rostrum && cd ~/Code/rostrum
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
./setup.sh              # once, needs sudo: lets you create a virtual keyboard (/dev/uinput); then log out/in
./install-desktop.sh    # adds "Rostrum" to the app menu
```

To build the RPM yourself: `podman run --rm -v "$PWD":/src:Z -w /src fedora:44 packaging/rpm/build-rpm.sh`
(output in `dist/`). Tagging `v*` builds it on GitHub and attaches it to a release.

## Use

Open **Rostrum** from the app menu and press **Start**. Keep Transmit released for the first
second and a half while it measures the idle line. Push-to-talk keeps running after the window
is closed.

Each target has a tab; the ticked one is the target push-to-talk drives, and **Use … for
push-to-talk** on another tab switches to it (the tray menu does too).

- **Claude Code**: the voice mode (hold or tap) and **Send the prompt when you release
  Transmit** are Claude Code's own settings (`voice.mode` and `voice.autoSubmit` in
  `~/.claude/settings.json`, the same ones `/voice` changes), so open sessions pick them up
  straight away. Claude Code only auto-sends prompts of three words or more; shorter ones wait
  for Enter. Tap mode always sends. **Read replies aloud** adds the speech hook.
- **Generic** and **OpenCode**: each has its own Whisper model and **Press Enter after the
  text**, which OpenCode has on by default.

Or from a terminal:

```sh
.venv/bin/python ptt.py                          # Claude Code (voice mode follows ~/.claude/settings.json)
.venv/bin/python ptt.py --transcribe             # Generic: local Whisper, pasted into the active window
.venv/bin/python ptt.py --transcribe --enter     # ... and press Enter to send it
.venv/bin/python ptt.py --no-tts                 # don't load the text-to-speech voice
.venv/bin/python ptt.py --dry-run                # print presses/releases, touch nothing
.venv/bin/python ptt.py --file usb_button.wav    # replay a recording through the detector
```

Only one copy runs at a time; starting another stops the first.

### Speech to text

| Target | Engine | Where it runs |
|---|---|---|
| Claude Code | Claude Code's built-in dictation | Anthropic's servers |
| Generic | Whisper via faster-whisper, `base.en` by default (~0.7 s a sentence on CPU) | this computer; the model downloads from Hugging Face on first use |

### Text to speech

Kokoro v1.0 (voice *bf_emma*, British English), run locally by `tts/ttsd.py`, which
push-to-talk starts alongside itself. The model downloads to `~/.local/share/rostrum/models`
the first time. **Connect Claude Code** in the app adds a Stop hook to
`~/.claude/settings.json` so Claude's replies are read aloud (`tts/speak.py`). Any agent
can use it by sending `{"cmd": "say", "text": "..."}` to the Unix socket
`~/.cache/rostrum/tts.sock`. Mute with the checkbox in the app, or
`touch ~/.config/rostrum/muted`.

## Troubleshooting

- **Keys fire but Claude Code gets no text.** Claude's voice mode (`/voice tap` or
  `/voice hold`) must match what Rostrum sends. It reads the mode from
  `~/.claude/settings.json` at startup, so restart it after changing `/voice`.
- **"the floor measured … was Transmit held?"** Restart with the button released.
- **"no input matching 'usb-C-Media'"**: the USB sound card isn't plugged in, or it's a
  different card (use `--source`).

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

They replay the recorded sessions (`usb_live.wav`, `usb_button.wav`) through `ptt.py` exactly as
it runs live: where each press and release lands, releasing after you've stopped talking, fast
speech not counting as a release, silence never pressing, word-for-word transcription, and the
settings window never cutting off dictation. They run on every push and pull request.
`ROSTRUM_SKIP_WHISPER=1` skips the transcription test.

## Tuning for other hardware

Record yourself holding, talking, releasing and tapping, then replay it through the detector:

```sh
parecord --file-format=wav --format=s16le --rate=48000 --channels=1 test.wav   # Ctrl+C to stop
.venv/bin/python ptt.py --file test.wav -v
```

The thresholds are all relative to the measured idle floor: `--margin` (press),
`--quiet-margin` (what counts as silent), `--cut-margin` (release cut-off), `--cut-confirm`
(how long a cut-off must stay silent), `--hang`
(fallback release after silence).

## Platform support

Linux (PipeWire, Wayland) only for now. macOS is planned; the engine needs porting for audio
capture (`parecord`), mixer control (`amixer`), key presses (`/dev/uinput`) and the clipboard
(`wl-copy`). The control app (Qt) is already cross-platform.
