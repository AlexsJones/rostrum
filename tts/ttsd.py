"""
Speech service: keeps Kokoro loaded so replies start speaking in about a second.

Listens on a Unix socket for one JSON message per connection:
  {"cmd": "say", "text": "..."}   stop whatever is playing, then speak this
  {"cmd": "stop"}                 stop speaking now
  {"cmd": "ping"}                 replies "pong"

Started (and stopped) by mic-ptt; speak.py falls back to speaking on its own when
this isn't running.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback

import numpy as np
import onnxruntime as ort
from kokoro_onnx import Kokoro

import speak

SOCK = speak.SOCK


class Speaker:
    def __init__(self):
        speak.ensure_kokoro()
        so = ort.SessionOptions()
        so.intra_op_num_threads, so.inter_op_num_threads = speak.KOKORO_THREADS, 1
        self.kokoro = Kokoro.from_session(
            ort.InferenceSession(str(speak.KOKORO_MODEL), so, providers=["CPUExecutionProvider"]),
            str(speak.KOKORO_VOICES))
        self.kokoro.create("Ready.", voice=speak.KOKORO_VOICE, lang=speak.KOKORO_LANG)   # warm-up
        self.lock = threading.Lock()
        self.generation = 0         # bumped on every say/stop; a running job quits when it changes
        self.play = None

    def stop(self):
        with self.lock:
            self.generation += 1
            if self.play:
                self.play.kill()
                self.play = None

    def say(self, text):
        self.stop()
        if not text or speak.MUTED.exists():
            return
        with self.lock:
            gen = self.generation
        threading.Thread(target=self._run, args=(text, gen), daemon=True).start()

    def _run(self, text, gen):
        play = None
        try:
            for chunk in speak.sentences(text):
                samples, rate = self.kokoro.create(chunk, voice=speak.KOKORO_VOICE, lang=speak.KOKORO_LANG)
                with self.lock:
                    if gen != self.generation:
                        print(time.strftime("%H:%M:%S"), "cancelled before playing", flush=True)
                        return
                    if play is None:
                        play = self.play = speak.player(rate)
                play.stdin.write((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
                play.stdin.flush()
            if play:
                play.stdin.close()
                play.wait()
        except (BrokenPipeError, OSError):
            pass                    # killed by stop()
        except Exception:
            traceback.print_exc()


def main():
    if os.path.exists(SOCK):
        try:                        # already running? then there's nothing to do
            with socket.socket(socket.AF_UNIX) as s:
                s.connect(str(SOCK))
                sys.exit("ttsd already running")
        except ConnectionRefusedError:
            os.unlink(SOCK)         # stale socket from a crash
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # run the cleanup below
    speaker = Speaker()
    SOCK.parent.mkdir(parents=True, exist_ok=True)
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(SOCK))
    os.chmod(SOCK, 0o600)
    mine = os.stat(SOCK).st_ino
    srv.listen(8)
    print(f"ttsd ready ({speak.KOKORO_VOICE})", flush=True)
    try:
        while True:
            conn, _ = srv.accept()
            with conn:
                try:
                    msg = json.loads(conn.makefile().readline() or "{}")
                except json.JSONDecodeError:
                    continue
                print(time.strftime("%H:%M:%S"), msg.get("cmd"), repr(msg.get("text", ""))[:60], flush=True)
                if msg.get("cmd") == "say":
                    speaker.say(msg.get("text", ""))
                elif msg.get("cmd") == "stop":
                    speaker.stop()
                elif msg.get("cmd") == "ping":
                    conn.sendall(b"pong\n")
    finally:
        speaker.stop()
        srv.close()
        try:                        # only remove the socket if a newer ttsd hasn't replaced it
            if os.stat(SOCK).st_ino == mine:
                os.unlink(SOCK)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
