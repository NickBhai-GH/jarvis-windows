"""Text-to-speech (Kokoro, or a Piper voice) with a playback buffer that can be cut off instantly."""
import asyncio
import logging
import re
import threading

import numpy as np
import sounddevice as sd
from kokoro_onnx import Kokoro
from scipy.signal import resample_poly

import config

log = logging.getLogger("mouth")
RATE = 24000


def _tone(freqs, dur=0.09, vol=0.18):
    out = []
    for f in freqs:
        t = np.linspace(0, dur, int(RATE * dur), endpoint=False)
        env = np.minimum(1, np.minimum(t, dur - t) * 60)
        out.append(np.sin(2 * np.pi * f * t) * env * vol)
    return np.concatenate(out).astype(np.float32)


CHIMES = {
    "wake": _tone([660, 990]),
    "done": _tone([880, 587]),
    "error": _tone([300, 220], dur=0.15),
}


def speakable(text):
    """Strip things that sound awful read aloud."""
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"[*_#>|]+", " ", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip()


class SentenceSplitter:
    """Turns a stream of text deltas into whole sentences, skipping code blocks."""

    def __init__(self):
        self.buf = ""
        self.in_code = False

    def feed(self, delta):
        self.buf += delta
        out = []
        while True:
            if "```" in self.buf:
                before, _, after = self.buf.partition("```")
                if not self.in_code:
                    out += self._sentences(before, final=True)
                self.in_code = not self.in_code
                self.buf = after
                continue
            if self.in_code:
                return out
            m = re.search(r"(?<=[.!?])\s+|\n+", self.buf)
            if not m:
                return out
            if self.buf[:m.start()].strip():
                out.append(self.buf[:m.start()])
            self.buf = self.buf[m.end():]

    def flush(self):
        rest, self.buf = self.buf, ""
        return [] if self.in_code else self._sentences(rest, final=True)

    @staticmethod
    def _sentences(text, final):
        return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


class Mouth:
    def __init__(self, loop):
        self.loop = loop
        if config.VOICE_ENGINE == "piper":
            from piper import PiperVoice
            self.piper = PiperVoice.load(config.PIPER_MODEL)
        else:
            self.kokoro = Kokoro(config.KOKORO_MODEL, config.KOKORO_VOICES)
        self.lock = threading.Lock()
        self.chunks = []          # audio waiting to play
        self.pos = 0
        self.textq = asyncio.Queue()
        self.generation = 0       # bumped by stop() so stale speech is dropped
        self.synthesizing = False
        self.out_level = 0.0
        self.on_sentence_start = None     # called (in the event loop) as each sentence starts playing
        self.hold = None                  # async (token, text): unprompted speech waits on this before it plays
        self.polite = False               # the sentence in hand is unprompted
        self.stream = sd.OutputStream(samplerate=RATE, channels=1, dtype="float32",
                                      blocksize=1024, callback=self._callback)

    def start(self):
        self.stream.start()
        self.loop.create_task(self._worker())

    def _callback(self, out, frames, t, status):
        out.fill(0)
        filled = 0
        with self.lock:
            while filled < frames and self.chunks:
                cur, text = self.chunks[0]
                if self.pos == 0 and text and self.on_sentence_start:
                    self.loop.call_soon_threadsafe(self.on_sentence_start, text)
                n = min(frames - filled, len(cur) - self.pos)
                out[filled:filled + n, 0] = cur[self.pos:self.pos + n]
                filled += n
                self.pos += n
                if self.pos >= len(cur):
                    self.chunks.pop(0)
                    self.pos = 0
        self.out_level = float(np.sqrt(np.mean(out[:, 0] ** 2)))

    def _enqueue_audio(self, audio, text=None):
        with self.lock:
            self.chunks.append((audio, text))

    def chime(self, name):
        self._enqueue_audio(CHIMES[name])

    def say(self, text, polite=None):
        """polite: a token for unprompted speech (one per update), so it waits on self.hold first."""
        text = speakable(text)
        if text:
            log.info("say: %s", text)
            self.textq.put_nowait((self.generation, text, polite))

    def stop(self):
        self.generation += 1
        while not self.textq.empty():
            self.textq.get_nowait()
        with self.lock:
            self.chunks.clear()
            self.pos = 0

    @property
    def playing(self):
        with self.lock:
            return bool(self.chunks)

    @property
    def busy(self):
        return self.playing or self.synthesizing or not self.textq.empty()

    async def wait_done(self):
        while self.busy:
            await asyncio.sleep(0.05)

    def _synth(self, text):
        if config.VOICE_ENGINE != "piper":
            return self.kokoro.create(text, voice=config.VOICE, speed=config.SPEED, lang="en-gb")[0]
        audio = np.concatenate([c.audio_float_array for c in self.piper.synthesize(text)])
        return resample_poly(audio, RATE, self.piper.config.sample_rate)     # Piper is 22050 Hz, the stream 24000

    async def _worker(self):
        while True:
            gen, text, polite = await self.textq.get()
            if gen != self.generation:
                continue
            self.synthesizing, self.polite = True, polite is not None
            try:
                audio = await self.loop.run_in_executor(None, self._synth, text)
                if self.polite and self.hold:
                    await self.hold(polite, text)
                if gen == self.generation:
                    self._enqueue_audio(audio.astype(np.float32), text)
            except Exception:
                log.exception("tts failed for %r", text)
            finally:
                self.synthesizing = False
