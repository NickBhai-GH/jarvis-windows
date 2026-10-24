"""Microphone, wake word, end-of-speech detection and speech-to-text."""
import asyncio
import collections
import json
import logging
import os
import re
import subprocess
import time

import numpy as np
import openwakeword
import sounddevice as sd
from faster_whisper import WhisperModel
from openwakeword.model import Model
from openwakeword.vad import VAD

import config

log = logging.getLogger("ears")
RATE = 16000
FRAME = 1280            # 80 ms, the frame size openWakeWord expects
FRAME_S = FRAME / RATE

# Whisper invents these on silence or noise
JUNK = re.compile(r"^\W*(thank you|thanks for watching|you|bye|\.+|okay)?\W*$", re.I)


class Recorder:
    """Collects one utterance frame by frame. feed() returns None while still listening,
    "timeout" if nobody spoke, or the audio once they stop talking."""

    def __init__(self, vad, wait_for_speech):
        self.vad = vad
        self.wait_frames = int(wait_for_speech / FRAME_S)
        self.pre = collections.deque(maxlen=4)
        self.frames = []
        self.speaking = False
        self.silent = 0
        self.waited = 0
        vad.reset_states()

    def feed(self, chunk):
        p = self.vad.predict(chunk, frame_size=640)
        if not self.speaking:
            self.pre.append(chunk)
            self.waited += 1
            if p > 0.5:
                self.speaking = True
                self.frames.extend(self.pre)
            elif self.waited > self.wait_frames:
                return "timeout"
            return None
        self.frames.append(chunk)
        self.silent = self.silent + 1 if p < 0.3 else 0
        if self.silent * FRAME_S >= config.END_SILENCE_S or len(self.frames) * FRAME_S >= config.MAX_UTTERANCE_S:
            return np.concatenate(self.frames)
        return None


class Ears:
    def __init__(self, loop):
        self.loop = loop
        self.q = asyncio.Queue(maxsize=300)
        models_dir = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
        self.oww = Model(wakeword_models=[os.path.join(models_dir, "hey_jarvis_v0.1.onnx")],
                         inference_framework="onnx")
        self.vad = VAD()
        self.last_score = 0.0
        self.mic_name = "?"
        self.whisper = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
        self.fast = WhisperModel("base.en", device="cpu", compute_type="int8")   # rough live text while you talk
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME,
                                     device=config.MIC_DEVICE, callback=self._callback)

    def start(self):
        self.stream.start()
        self.mic_name = str(config.MIC_DEVICE) if config.MIC_DEVICE is not None else self._default_source()
        log.info("mic open: %s", self.mic_name)

    @staticmethod
    def _default_source():
        if config.WINDOWS:
            return sd.query_devices(kind="input")["name"]
        try:
            name = subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True).stdout.strip()
            srcs = json.loads(subprocess.run(["pactl", "--format=json", "list", "sources"],
                                             capture_output=True, text=True).stdout)
            return next((x["description"] for x in srcs if x["name"] == name), name)
        except Exception:
            return "system default"

    def _callback(self, indata, frames, t, status):
        self.loop.call_soon_threadsafe(self._put, indata[:, 0].copy())

    def _put(self, chunk):
        if self.q.full():
            self.q.get_nowait()
        self.q.put_nowait(chunk)

    def heard_wake_word(self, chunk):
        score = max(self.oww.predict(chunk).values())
        self.last_score = score
        if score >= config.WAKE_THRESHOLD:
            log.info("wake word (%.2f)", score)
            self.reset_wake()
            return True
        return False

    def reset_wake(self):
        self.oww.reset()

    def recorder(self, wait_for_speech):
        return Recorder(self.vad, wait_for_speech)

    async def partial(self, audio):
        def run():
            segs, _ = self.fast.transcribe(audio.astype(np.float32) / 32768.0, language="en", beam_size=1,
                                           without_timestamps=True, initial_prompt=config.VOCAB)
            return " ".join(s.text for s in segs).strip()
        text = await self.loop.run_in_executor(None, run)
        return "" if JUNK.match(text) else text

    async def transcribe(self, audio, answer=False):
        """answer: the user is answering a yes/no, where a lone "okay" is a real yes, not whisper junk."""
        def run():
            segs, _ = self.whisper.transcribe(audio.astype(np.float32) / 32768.0, language="en",
                                              beam_size=1, vad_filter=True,
                                              initial_prompt=config.VOCAB)
            return " ".join(s.text for s in segs).strip()
        text = await self.loop.run_in_executor(None, run)
        if answer and re.fullmatch(r"\W*ok(ay)?\W*", text, re.I):
            return text
        return "" if JUNK.match(text) else text


def _pactl(what):
    return json.loads(subprocess.run(["pactl", "--format=json", "list", what], capture_output=True, text=True,
                                     timeout=3).stdout)


class CallWatch:
    """Is the user on a call, and are the other people talking? apps: other apps capturing a real mic (Discord, Zoom,
    Chrome for Meet), not Jarvis and not a monitor. last_heard: when one of those apps' playback streams was last
    louder than POLITE_CALL_LEVEL (a parec monitor per stream, so music and Jarvis's own voice don't count).
    The monitors run while Jarvis keeps calling update() and close themselves 10 s after the last call."""

    def __init__(self):
        self.apps, self.last_heard, self.since, self.asked = [], 0.0, 0.0, 0.0
        self.readers = {}                            # sink-input index -> task following its loudness

    @staticmethod
    def scan(pa=_pactl):
        """(names of other apps holding a mic, indexes of their playback streams). Any failure = not on a call."""
        if config.WINDOWS:
            return [], []        # ponytail: PulseAudio only; Windows could read pycaw capture sessions + session peak meters
        try:
            clients = {str(c["index"]): c["properties"] for c in pa("clients")}

            def who(o):                              # Jarvis's own streams only carry the pid on their client
                p = o["properties"] if "application.process.id" in o["properties"] else clients.get(str(o["client"]), {})
                return p.get("application.process.binary") or p.get("application.name", "?"), p.get("application.process.id")
            mics = {s["index"] for s in pa("sources") if not s.get("monitor_source")}
            takers = [o for o in pa("source-outputs") if o["source"] in mics and not o.get("corked")
                      and who(o)[1] != str(os.getpid())]
            callers = {who(o) for o in takers}       # (binary, pid): flatpak pids alone can repeat across apps
            streams = [o["index"] for o in pa("sink-inputs") if who(o) in callers and not o.get("corked")]
            return sorted({name for name, _ in callers}), streams
        except Exception:
            log.exception("call check failed")
            return [], []

    async def update(self):
        self.asked = time.time()
        self.apps, streams = await asyncio.get_running_loop().run_in_executor(None, self.scan)
        self.readers = {i: t for i, t in self.readers.items() if not t.done()}
        for i in streams:
            if i not in self.readers:
                if not self.readers:
                    self.since = time.time()         # fresh ears on the call: hear QUIET_S of it before trusting it
                self.readers[i] = asyncio.create_task(self._follow(i))

    async def _follow(self, index):
        proc = await asyncio.create_subprocess_exec(
            "parec", f"--monitor-stream={index}", "--format=s16le", "--rate=8000", "--channels=1",
            "--latency-msec=100", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while time.time() - self.asked < 10:
                try:
                    data = await asyncio.wait_for(proc.stdout.readexactly(1600), 1)   # 100 ms of audio
                except asyncio.TimeoutError:
                    continue                         # a paused stream sends nothing
                except asyncio.IncompleteReadError:
                    break                            # the stream ended
                a = np.frombuffer(data, np.int16).astype(np.float32)
                if np.sqrt(np.mean(a * a)) > config.POLITE_CALL_LEVEL:
                    self.last_heard = time.time()
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
