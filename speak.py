"""
Claude Code Stop hook: read the last reply aloud (Kokoro, British 'Emma'; Piper as fallback).

Reads the hook JSON on stdin. Speaks only prose: code blocks, tables, inline code,
URLs and markdown symbols are dropped, and long replies are cut to the first few
sentences. A new reply (or mic-ptt's press) stops the previous one.

  touch ~/.config/claude-tts/muted   # mute      rm it to unmute
  ~/.config/claude-tts/sink          # preferred output name prefixes, one per line (falls back to default)
  speak.py --stop                    # stop talking now
  ttsd.py                            # keeps the voice loaded (mic-ptt starts it)
  echo "hello" | speak.py --text     # speak arbitrary text
"""
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VOICE = HERE / "voices" / "en_US-lessac-medium.onnx"
PIPER = HERE / ".venv" / "bin" / "piper"
PIDFILE = Path.home() / ".cache" / "claude-tts" / "pid"
MUTED = Path.home() / ".config" / "claude-tts" / "muted"
SOCK = Path.home() / ".cache" / "claude-tts" / "sock"          # ttsd, when running
SINK_PREF = Path.home() / ".config" / "claude-tts" / "sink"     # sink-name prefix, e.g. bluez_output.F0_D3
MAX_CHARS = 600


def send(msg):
    """Hand a command to ttsd. False if it isn't running."""
    import socket
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(1)
            s.connect(str(SOCK))
            s.sendall((json.dumps(msg) + "\n").encode())
        return True
    except OSError:
        return False


def stop_current():
    send({"cmd": "stop"})
    try:
        pgid = int(PIDFILE.read_text())
        os.killpg(pgid, signal.SIGTERM)
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        pass


def last_reply(hook):
    if hook.get("last_assistant_message"):
        return hook["last_assistant_message"]
    path = hook.get("transcript_path")
    if not path or not Path(path).exists():
        return ""
    text = ""
    for line in Path(path).read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = entry.get("message") or {}
        if entry.get("type") == "assistant" and msg.get("role") == "assistant":
            parts = [c.get("text", "") for c in msg.get("content", []) if isinstance(c, dict) and c.get("type") == "text"]
            if any(p.strip() for p in parts):
                text = "\n".join(parts)
    return text


def speakable(md):
    md = re.sub(r"```.*?```", " ", md, flags=re.S)               # code blocks
    md = "\n".join(l for l in md.splitlines() if not l.lstrip().startswith("|"))   # tables
    md = re.sub(r"`[^`]*`", "", md)                                # inline code
    md = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", md)               # links -> text
    md = re.sub(r"https?://\S+", "", md)
    md = re.sub(r"^\s*#+\s*", "", md, flags=re.M)                  # headings
    md = re.sub(r"^\s*[-*]\s+", "", md, flags=re.M)                # bullets
    md = re.sub(r"[*_>#]", "", md)
    md = re.sub(r"\s+", " ", md).strip()
    if len(md) > MAX_CHARS:
        cut = md[:MAX_CHARS]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        md = cut[:end + 1] if end > 200 else cut
    return md


def pick_sink():
    """First output whose name starts with the configured prefix, if it's connected right now."""
    try:
        prefixes = [l.strip() for l in SINK_PREF.read_text().splitlines() if l.strip()]
        sinks = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True).stdout
    except (FileNotFoundError, OSError):
        return None
    names = [l.split("\t")[1] for l in sinks.splitlines() if "\t" in l]
    for pre in prefixes:
        for n in names:
            if n.startswith(pre):
                return n
    return None


KOKORO_MODEL = HERE / "kokoro" / "kokoro-v1.0.onnx"
KOKORO_VOICES = HERE / "kokoro" / "voices-v1.0.bin"
KOKORO_VOICE, KOKORO_LANG, KOKORO_THREADS = "bf_emma", "en-gb", 8


def player(rate):
    sink = pick_sink()
    return subprocess.Popen(["paplay", "--raw", f"--rate={rate}", "--channels=1", "--format=s16le"]
                            + ([f"--device={sink}"] if sink else []), stdin=subprocess.PIPE)


def sentences(text, min_len=40):
    """Split into sentences, merging short ones so each chunk is worth a synthesis call."""
    out = []
    for part in re.split(r"(?<=[.!?])\s+", text):
        if len(out) > 1 and len(out[-1]) < min_len:     # never grow the first chunk: it sets the start delay
            out[-1] += " " + part
        else:
            out.append(part)
    return [p for p in out if p.strip()]


def speak_kokoro(text):
    import numpy as np
    import onnxruntime as ort
    from kokoro_onnx import Kokoro
    so = ort.SessionOptions()
    so.intra_op_num_threads, so.inter_op_num_threads = KOKORO_THREADS, 1
    kokoro = Kokoro.from_session(
        ort.InferenceSession(str(KOKORO_MODEL), so, providers=["CPUExecutionProvider"]), str(KOKORO_VOICES))
    play = None
    for chunk in sentences(text):   # synthesise the next sentence while the last one plays
        samples, rate = kokoro.create(chunk, voice=KOKORO_VOICE, speed=1.0, lang=KOKORO_LANG)
        play = play or player(rate)
        play.stdin.write((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
        play.stdin.flush()
    if play:
        play.stdin.close()
        play.wait()


def speak_piper(text):
    with open(VOICE.with_suffix(".onnx.json")) as f:
        rate = json.load(f)["audio"]["sample_rate"]
    piper = subprocess.Popen([str(PIPER), "-m", str(VOICE), "--output-raw"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    play = player(rate)
    piper.stdin.write(text.encode())
    piper.stdin.close()
    for block in iter(lambda: piper.stdout.read(4096), b""):
        play.stdin.write(block)
    play.stdin.close()
    play.wait()


def say(text):
    """Speak in a separate process group so a new reply or a mic-ptt press can stop it."""
    if send({"cmd": "say", "text": text}):     # ttsd has the voice loaded already
        return
    stop_current()
    if not text or MUTED.exists():
        return
    PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.Popen([sys.executable, __file__, "--worker"], stdin=subprocess.PIPE, start_new_session=True)
    PIDFILE.write_text(str(p.pid))
    p.stdin.write(text.encode())
    p.stdin.close()
    p.wait()


def worker(text):
    try:
        speak_kokoro(text)
    except Exception:           # missing model, bad input... still say something
        speak_piper(text)


if __name__ == "__main__":
    if "--worker" in sys.argv:
        worker(sys.stdin.read())
    elif "--stop" in sys.argv:
        stop_current()
    elif "--text" in sys.argv:
        say(speakable(sys.stdin.read()))
    else:
        try:
            hook = json.load(sys.stdin)
        except json.JSONDecodeError:
            hook = {}
        say(speakable(last_reply(hook)))
