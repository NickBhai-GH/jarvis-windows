"""JARVIS: say "hey jarvis", talk, get an answer out loud.

Run:        systemctl --user start jarvis   (or ~/jarvis/.venv/bin/python ~/jarvis/jarvis.py)
Poke:       ~/jarvis/jarvisctl listen | stop | say "open spotify" | status
Dashboard:  http://127.0.0.1:8765
"""
import asyncio
import collections
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta

import numpy as np

import config
import dashboard
import events
import pctools
from pctools import kwin
import worker
from brain import Brain, read_notes, read_state, summarize, write_state
from ears import CallWatch, Ears
from mouth import Mouth

IDLE, LISTEN, BUSY = "idle", "listening", "busy"
# a yes only counts at the start of the answer, so "but yes, that's right" said on a call doesn't approve
YES = re.compile(r"^\W*((um+|uh+|erm|hmm+|oh|ah)\W+)*(yes|yeah|yep|yup|sure|go ahead|do it|confirm(ed)?|affirmative|ok|okay|please do|proceed|send it)\b", re.I)
NEW_SESSION = re.compile(r"^\W*(please\W+)?(start\W+)?(a\W+)?(new|fresh)\W+(session|chat|conversation|start)\W*(please)?\W*$"
                         r"|^\W*fresh start\W*$", re.I)
NO = re.compile(r"\b(no|nope|don'?t|stop|cancel|wait|hold on|never ?mind)\b", re.I)
GO_AHEAD = re.compile(r"^\W*((ok(ay)?|yes|yeah|sure|go on|go ahead|tell me|what is it|what'?s up|let'?s hear it|shoot)\W*)+"
                      r"(then|please|sir)?\W*$", re.I)


def time_tag():
    """[Mon 28 Sep, 07:42, first today] so the brain can greet, welcome back and notice late nights."""
    now, st = datetime.now(), read_state()
    day = (now - timedelta(hours=4)).date().isoformat()     # a late night still counts as the day before
    gap = time.time() - st.get("last_heard", time.time())
    extra = ", first today" if st.get("last_heard_day") != day else (
        f", back after about {round(gap / 3600)} hours" if gap > 3 * 3600 else "")
    write_state(last_heard_day=day, last_heard=time.time())
    return f"[{now:%a %d %b, %H:%M}{extra}]"


log = logging.getLogger("jarvis")


STEP_WORDS = {"screenshot": "Looking at the screen", "click_at": "Clicking", "move_mouse": "Moving the mouse",
              "scroll": "Scrolling", "type_text": "Typing", "press_keys": "Pressing keys", "open_app": "Opening an app",
              "focus_window": "Switching windows", "list_windows": "Checking windows", "media": "Media controls",
              "volume": "Volume", "Bash": "Command", "Read": "Reading", "Write": "Writing", "Edit": "Editing",
              "Grep": "Searching files", "Glob": "Finding files", "WebSearch": "Searching the web",
              "WebFetch": "Reading a web page", "ToolSearch": "Loading tools", "Agent": "Sub-agent",
              "navigate": "Opening a page in Chrome", "computer": "Working in Chrome", "read_page": "Reading the page",
              "get_page_text": "Reading the page", "find": "Searching the page", "form_input": "Filling a form"}


def step_words(ev):
    """'Command: Check the weather' rather than 'Bash'."""
    short = ev["name"].split("__")[-1]
    name = STEP_WORDS.get(short, short.replace("_", " ").capitalize())
    try:
        inp = json.loads(ev.get("input") or "{}")
    except ValueError:
        inp = {}
    detail = inp.get("description") or inp.get("query") or inp.get("url") or inp.get("name") or ""
    if short in ("Read", "Write", "Edit") and inp.get("file_path"):
        detail = os.path.basename(inp["file_path"])
    if short in ("ToolSearch", "screenshot", "click_at", "move_mouse", "scroll"):
        detail = ""
    return f"{name}: {detail}" if detail else name


class Jarvis:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.ears = Ears(self.loop)
        self.mouth = Mouth(self.loop)
        self.brain = Brain(confirm=self.confirm, on_sentence=self.speak, on_auto_turn=self.on_auto_turn,
                           on_crash=self.on_brain_crash)
        self._state = IDLE
        self.recorder = None
        self.listen_target = None     # None = a command for the brain, a Future = a yes/no answer
        self.pending_confirm = None
        self.pending_question = None
        self.processing = False
        self.inbox = asyncio.Queue()
        self.turn = None              # what's being worked on right now
        self.started = time.time()
        self.level = 0.0
        self.wake_score = 0.0
        self.brain_lock = asyncio.Lock()   # held while asking, and while swapping sessions
        self.last_activity = time.time()
        self.resetting = False
        self.listen_id = 0
        self.talk_frames = collections.deque(maxlen=15)
        self.talk_run = 0
        self.partial_busy = False
        self.mouth.on_sentence_start = lambda text: events.emit("speaking_now", text=text)
        self.live = {"you": "", "said": "", "step": "", "self_started": False}
        events.listen(self.track_live)
        self.confirm_lock = asyncio.Lock()     # one spoken yes/no at a time (Jarvis and workers share it)
        worker.confirm = lambda question: self.confirm(question, unprompted=True)
        self.call = CallWatch()
        self.last_voice = 0.0         # the user last spoke into Jarvis's mic (VAD), for not talking over them
        self.held = None              # the unprompted update waiting for a quiet moment (its token)
        self.deliver = None           # the update he asked for with "Hey Jarvis": plays once he's said his piece
        self.engaged_listen = None    # that listen: a bare "go ahead" in it isn't a command
        self.mouth.hold = self.hold_for_quiet
        worker.notify = lambda message: self.inbox.put_nowait(("worker", message))

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value):
        if value != self._state:
            self._state = value
            events.emit("state", state=value)

    async def run(self):
        events.load()
        await kwin.start()
        if not config.WINDOWS:
            await self.loop.run_in_executor(None, pctools.ensure_ydotoold)
        await self.catch_up_notes()
        await self.brain.start()
        self.mouth.start()
        self.ears.start()
        if config.WINDOWS:          # localhost port; jarvisctl proves it's you with the token file only you can read
            self.ctl_token = secrets.token_urlsafe(24)
            with open(config.CTL_TOKEN_FILE, "w") as f:
                f.write(self.ctl_token)
            server = await asyncio.start_server(self.handle_ctl, "127.0.0.1", config.CTL_PORT)
        else:
            self.ctl_token = None
            server = await asyncio.start_unix_server(self.handle_ctl, path=config.SOCKET_PATH)
            os.chmod(config.SOCKET_PATH, 0o600)
        await dashboard.start(self)
        events.emit("started", mic=self.ears.mic_name, voice=config.VOICE, whisper=config.WHISPER_MODEL,
                    model=config.MODEL, dashboard=f"http://127.0.0.1:{dashboard.PORT}")
        self.mouth.chime("wake")
        self.mouth.say(f"Online and ready, {config.HONORIFIC}.")
        log.info("ready, dashboard on http://127.0.0.1:%d", dashboard.PORT)
        async with server:
            await asyncio.gather(self.audio_loop(), self.inbox_loop(), self.idle_watch())

    # ---------- sessions and notes ----------

    async def catch_up_notes(self):
        """If the last session never got summarised (restart, crash), do it now."""
        st = read_state()
        if st.get("session_id") and not st.get("summarized"):
            try:
                await asyncio.wait_for(summarize(st["session_id"]), 240)
            except Exception as e:
                log.exception("notes catch-up failed")
                events.emit("error", where="notes", error=str(e))

    async def new_session(self, reason):
        async with self.brain_lock:
            self.resetting = True
            events.emit("session_reset", reason=reason, turns=self.brain.session_turns)
            try:
                old, turns = self.brain.session_id, self.brain.session_turns
                await self.brain.stop()
                if old and turns:
                    try:
                        await asyncio.wait_for(summarize(old), 240)
                    except Exception as e:
                        log.exception("summary failed")
                        events.emit("error", where="notes", error=str(e))
                await self.brain.start()
            finally:
                self.resetting = False
        self.last_activity = time.time()

    def background_jobs(self):
        return bool(self.brain.tasks) or any(p["role"] == "job" for p in dashboard.processes())

    async def idle_watch(self):
        while True:
            await asyncio.sleep(60)
            idle_for = time.time() - self.last_activity
            if (self.brain.session_turns and idle_for > config.SESSION_IDLE_RESET_MIN * 60 and self.state == IDLE
                    and not self.processing and not self.mouth.busy and self.inbox.empty()
                    and not self.background_jobs()):
                log.info("idle %.0f min, starting a fresh session", idle_for / 60)
                await self.new_session("idle")

    # ---------- listening ----------

    def begin_listen(self, target, wait, prefix=None):
        self.listen_id += 1
        self.recorder = self.ears.recorder(wait)
        if prefix:                                 # he'd already started talking
            self.recorder.speaking = True
            self.recorder.frames = list(prefix)
        self.listen_target = target
        self.state = LISTEN
        log.info("listening%s", " for yes/no" if target else "")

    async def wake(self, how="wake word"):
        events.emit("wake", how=how, score=round(self.wake_score, 2))
        held = self.held
        if held:                                   # he's come for the held update: no barge-in, it plays after
            self.deliver = held
        else:
            if self.pending_confirm and not self.pending_confirm.done():
                self.pending_confirm.set_result("")
            if self.processing or self.mouth.busy:
                log.info("barge-in")
                events.emit("barge_in")
                self.mouth.stop()
                if self.processing:
                    await self.brain.interrupt()
        self.mouth.chime("wake")
        self.begin_listen(None, config.WAIT_FOR_SPEECH_S)
        self.engaged_listen = self.listen_id if held else None

    async def audio_loop(self):
        while True:
            chunk = await self.ears.q.get()
            self.level = max(self.level * 0.6, float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2))) / 32768)
            if self.state != LISTEN:
                if self.ears.heard_wake_word(chunk):
                    await self.wake()
                    continue
                self.wake_score = self.ears.last_score
                p = self.ears.vad.predict(chunk, frame_size=640)
                if p > 0.5:
                    self.last_voice = time.time()
                # on a call, his talking is for them: an unprompted update finishes its sentence, then waits
                if config.BARGE_IN_BY_VOICE and self.mouth.busy and not (self.mouth.polite and self.call.apps):
                    await self.check_talk_over(chunk, p)
                else:
                    self.talk_frames.clear()
                    self.talk_run = 0
                continue
            result = self.recorder.feed(chunk)
            if result is None:
                if self.recorder.speaking and not self.partial_busy and len(self.recorder.frames) % 8 == 0:
                    self.loop.create_task(self.live_partial(self.listen_id, list(self.recorder.frames)))
                continue
            target, self.listen_target = self.listen_target, None
            self.ears.reset_wake()
            self.state = BUSY if (self.processing or target) else IDLE
            if isinstance(result, str):          # nobody spoke
                log.info("heard nothing")
                events.emit("heard", text="", confirm=bool(target))
                if target and not target.done():
                    target.set_result("")
                continue
            self.state = BUSY
            self.loop.create_task(self.handle_audio(result, target))

    async def live_partial(self, listen_id, frames):
        self.partial_busy = True
        try:
            text = await self.ears.partial(np.concatenate(frames))
            if text and listen_id == self.listen_id and self.state == LISTEN:
                events.emit("partial", text=text, listen_id=listen_id)
        finally:
            self.partial_busy = False

    async def check_talk_over(self, chunk, p):
        """The user talking while Jarvis is speaking: stop talking and listen, keeping what they've said so far."""
        self.talk_frames.append(chunk)
        self.talk_run = self.talk_run + 1 if p > 0.6 else 0
        if self.talk_run < 4:                      # about a third of a second of real speech
            return
        prefix = list(self.talk_frames)
        self.talk_frames.clear()
        self.talk_run = 0
        answering = self.pending_confirm is not None and not self.pending_confirm.done()
        log.info("talked over (%s)", "answering the question" if answering else "barge-in")
        events.emit("barge_in", how="voice")
        self.mouth.stop()
        if answering:                              # he answered before the question finished
            self.begin_listen(self.pending_confirm, config.CONFIRM_WAIT_S, prefix)
            return
        if self.processing:
            await self.brain.interrupt()
        self.begin_listen(None, config.WAIT_FOR_SPEECH_S, prefix)

    async def handle_audio(self, audio, target):
        t = time.time()
        text = await self.ears.transcribe(audio, answer=bool(target))
        log.info("heard: %s", text or "(nothing)")
        events.emit("heard", text=text, confirm=bool(target), listen_id=self.listen_id, seconds=round(len(audio) / 16000, 1),
                    stt_s=round(time.time() - t, 2))
        if target:
            if not target.done():
                target.set_result(text)
        elif text and self.engaged_listen == self.listen_id and GO_AHEAD.match(text):
            log.info("go ahead: delivering the held update")
            self.state = BUSY if self.processing else IDLE
        elif text:
            self.inbox.put_nowait(("voice", text))
        else:
            self.mouth.chime("error")
            self.state = BUSY if self.processing else IDLE

    # ---------- thinking and talking ----------

    async def inbox_loop(self):
        while True:
            source, text = await self.inbox.get()
            self.last_activity = time.time()
            if NEW_SESSION.match(text):
                self.processing, self.state = True, BUSY
                self.speak(f"Very good, {config.HONORIFIC}. Filing my notes and starting a fresh session.")
                await self.new_session("asked")
                self.speak("Fresh session ready. Awaiting instructions.")
                await self.mouth.wait_done()
                self.processing, self.state = False, IDLE
                continue
            self.processing, self.state = True, BUSY
            self.turn = {"source": source, "text": text, "started": time.time(), "first_speech": None}
            events.emit("turn_start", source=source, text=text)
            prompt = text if source == "worker" else time_tag() + " " + (text if source == "voice" else f"[typed] {text}")
            print(f"\nYou: {text}", flush=True)
            try:
                async with self.brain_lock:
                    await self.brain.ask(prompt)
            except Exception as e:
                log.exception("brain failed")
                events.emit("error", where="brain", error=str(e))
                self.mouth.say(f"I'm afraid something went wrong on my end, {config.HONORIFIC}.")
            await self.mouth.wait_done()
            self.processing, self.turn = False, None
            self.last_activity = time.time()
            if self.state == BUSY:               # not interrupted by a new "hey jarvis"
                if source == "voice" and self.inbox.empty():
                    self.begin_listen(None, config.FOLLOW_UP_S)
                else:
                    self.state = IDLE

    async def on_auto_turn(self, started):
        """The session started talking by itself (a timer or background job finished)."""
        if started:
            self.processing = True
            self.turn = {"source": "self", "text": "", "started": time.time(), "first_speech": None}
            if self.state != LISTEN:
                self.state = BUSY
            return

        async def finish():
            await self.mouth.wait_done()
            if not self.brain.in_turn:
                self.processing, self.turn = False, None
                if self.state == BUSY:
                    self.state = IDLE
        self.loop.create_task(finish())

    async def on_brain_crash(self):
        self.mouth.say(f"I appear to have lost my connection, {config.HONORIFIC}. Reconnecting.")
        await asyncio.sleep(3)
        if self.resetting:
            return                             # we stopped it on purpose
        try:
            await self.brain.stop()
        except Exception:
            pass
        await self.catch_up_notes()
        try:
            await self.brain.start()
        except Exception as e:
            events.emit("error", where="reconnect", error=str(e))

    def speak(self, sentence, unprompted=False):
        print(f"Jarvis: {sentence}", flush=True)
        if self.turn and self.turn["first_speech"] is None:
            self.turn["first_speech"] = time.time()
            events.emit("first_speech", after_s=round(time.time() - self.turn["started"], 2))
        events.emit("say", text=sentence)
        unprompted = unprompted or (self.turn or {}).get("source") in ("worker", "self")
        self.mouth.say(sentence, polite=(self.turn or object()) if unprompted else None)   # one token per update

    async def hold_for_quiet(self, token, text):
        """Before an unprompted sentence plays: if the user is on a call (another app has a mic), wait until nobody on it
        (him on his mic, them on the call's playback) has talked for POLITE_QUIET_S, letting the sentence before
        finish first. "Hey Jarvis" delivers it once he's said his piece. After POLITE_MAX_WAIT_S a desktop
        notification too, and it keeps waiting. Not on a call: no wait, same as before."""
        if not config.POLITE:
            return
        gen, start, checked, notified, why = self.mouth.generation, time.time(), 0.0, False, "cancelled"
        try:
            while gen == self.mouth.generation:
                now = time.time()
                if now - checked >= 1:
                    checked = now
                    await self.call.update()
                    if not self.call.apps:
                        why = "not on a call"
                        break
                    now = time.time()
                if self.mouth.playing or self.state == LISTEN:
                    pass                           # the sentence before is finishing, or he's talking to Jarvis
                elif token is self.deliver:
                    why = "he asked"
                    break
                elif now - max(self.last_voice, self.call.last_heard, self.call.since) >= config.POLITE_QUIET_S:
                    why = "quiet"
                    break
                elif not self.held:
                    self.held = token
                    log.info("holding unprompted speech, on a call (%s): %s", ", ".join(self.call.apps), text)
                    events.emit("polite_hold", apps=self.call.apps, text=text)
                if not notified and now - start > config.POLITE_MAX_WAIT_S:
                    notified = True
                    log.info("held %.0fs: desktop notification, still waiting for quiet", now - start)
                    events.emit("polite_notify", text=text)
                    try:
                        if config.WINDOWS:
                            pctools.winctl.notify("Jarvis has an update", text)
                        else:
                            subprocess.Popen(["notify-send", "-a", "Jarvis", "Jarvis has an update", text])
                    except OSError:
                        log.exception("notify-send failed")
                await asyncio.sleep(0.1)
        finally:
            if self.held is token:
                self.held = None
                log.info("released unprompted speech after %.1fs (%s)", time.time() - start, why)
                events.emit("polite_release", waited_s=round(time.time() - start, 1), why=why)

    async def confirm(self, question, unprompted=False):
        async with self.confirm_lock:
            return await self._confirm(question, unprompted)

    async def _confirm(self, question, unprompted=False):
        await self.mouth.wait_done()
        # the future exists before he hears the question, so a "yes" said over it still counts
        self.pending_confirm = self.loop.create_future()
        self.pending_question = question
        self.speak(question, unprompted)
        events.emit("confirm_ask", question=question)
        await self.mouth.wait_done()
        already = self.state == LISTEN and self.listen_target is self.pending_confirm
        if not self.pending_confirm.done() and not already:
            self.mouth.chime("wake")
            self.begin_listen(self.pending_confirm, config.CONFIRM_WAIT_S)
        try:
            answer = await asyncio.wait_for(self.pending_confirm, config.CONFIRM_WAIT_S + 30)
        except asyncio.TimeoutError:
            answer = ""
        self.pending_confirm = self.pending_question = None
        if self.state == BUSY and not self.processing:
            self.state = IDLE                 # the question came from a background job
        ok = bool(YES.match(answer)) and not NO.search(answer)
        log.info("confirm %r -> %s", answer, ok)
        events.emit("confirm_answer", question=question, answer=answer, approved=ok)
        return ok

    # ---------- commands (jarvisctl and the dashboard) ----------

    async def command(self, cmd, text=""):
        if cmd == "say":
            if not text.strip():
                return "nothing to send"
            self.inbox.put_nowait(("typed", text.strip()))
        elif cmd == "listen":
            await self.wake(how="button")
        elif cmd == "stop":
            events.emit("stopped_by_user")
            self.mouth.stop()
            if self.pending_confirm and not self.pending_confirm.done():
                self.pending_confirm.set_result("no")
            if self.processing:
                await self.brain.interrupt()
            if self.state == LISTEN and not self.listen_target:
                self.state = BUSY if self.processing else IDLE
        elif cmd in ("yes", "no"):
            if not (self.pending_confirm and not self.pending_confirm.done()):
                return "nothing is waiting for a yes/no"
            self.pending_confirm.set_result(cmd)
            if self.state == LISTEN and self.listen_target is self.pending_confirm:
                self.listen_target, self.state = None, BUSY
                self.ears.reset_wake()
        elif cmd == "new":
            if self.processing:
                return "busy right now; try again when he's finished"
            self.inbox.put_nowait(("typed", "new session"))
        elif cmd == "speak":
            self.mouth.say(text)
        elif cmd == "status":
            return json.dumps({"state": self.state, "processing": self.processing, "speaking": self.mouth.busy,
                               "queued": self.inbox.qsize(), "waiting_for_yes_no": bool(self.pending_confirm)})
        else:
            return f"unknown command {cmd}"
        return "ok"

    def activity(self):
        if self.held and self.state != LISTEN:
            return "ready"
        if self.pending_question:
            return "waiting"
        if self.state == LISTEN:
            return "listening"
        if self.mouth.busy:
            return "speaking"
        if self.processing:
            return "thinking"
        return "idle"

    def meter(self):
        return {"level": round(min(1.0, self.level * 8), 3), "wake": round(self.wake_score, 2),
                "activity": self.activity()}

    def track_live(self, ev):
        """Keep the few things the widget shows, updated from the event stream."""
        k, live = ev["kind"], self.live
        if k == "wake":
            live.update(you="", step="")
        elif k == "partial":
            live["you"] = ev["text"]
        elif k == "heard":
            live["you"] = ev["text"] or "(didn't catch that)"
        elif k == "turn_start":
            live.update(said="", step="", self_started=ev.get("source") in ("self", "worker"))
            if ev.get("source") not in ("self", "worker"):
                live["you"] = ev.get("text", "")
        elif k == "tool_use" and not ev.get("sub"):
            live["step"] = step_words(ev)
        elif k == "speaking_now":
            live["said"] = ev["text"]
        elif k == "confirm_answer":
            live["you"] = ev.get("answer") or "(no answer)"
        elif k in ("barge_in", "stopped_by_user"):
            live["said"] = ""

    def live_view(self):
        return {"activity": self.activity(), "mic": round(min(1.0, self.level * 8), 3),
                "out": round(min(1.0, self.mouth.out_level * 5), 3), "question": self.pending_question or "",
                "tasks": [{"task_id": t["task_id"], "description": t.get("description", ""), "started": t["started"]}
                          for t in list(self.brain.tasks.values()) + worker.as_tasks()], "now": time.time(), **self.live}

    def pulse(self):
        return {"kind": "pulse", "activity": self.activity(), "mic": round(min(1.0, self.level * 8), 3),
                "out": round(min(1.0, self.mouth.out_level * 5), 3)}

    def snapshot(self):
        return {"activity": self.activity(), "state": self.state, "turn": self.turn,
                "question": self.pending_question, "queued": self.inbox.qsize(),
                "tasks": list(self.brain.tasks.values()) + worker.as_tasks(), "brain": self.brain.info, "context": {
                    k: v for k, v in self.brain.context.items() if k != "categories"},
                "session_id": self.brain.session_id, "started": self.started, "now": time.time(),
                "session": {"started": self.brain.session_started, "turns": self.brain.session_turns,
                            "reset_after_min": config.SESSION_IDLE_RESET_MIN, "last_activity": self.last_activity,
                            "resetting": self.resetting},
                "notes": {"text": read_notes(), "updated": os.path.getmtime(config.NOTES_FILE)
                          if os.path.exists(config.NOTES_FILE) else None},
                "config": {"model": config.MODEL, "effort": config.EFFORT, "voice": config.VOICE,
                           "whisper": config.WHISPER_MODEL, "wake_threshold": config.WAKE_THRESHOLD,
                           "mic": self.ears.mic_name}}

    async def handle_ctl(self, reader, writer):
        try:
            req = json.loads(await reader.readline())
            if self.ctl_token and not secrets.compare_digest(str(req.get("token", "")), self.ctl_token):
                raise PermissionError("bad token")
            reply = await self.command(req.get("cmd"), req.get("text", ""))
            writer.write((reply + "\n").encode())
            await writer.drain()
        except Exception as e:
            log.exception("ctl")
            writer.write(f"error: {e}\n".encode())
        finally:
            writer.close()


async def main():
    os.makedirs(os.path.dirname(config.LOG_FILE), exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(config.LOG_FILE, encoding="utf-8")]
                        + ([logging.StreamHandler(sys.stderr)] if sys.stderr else []))
    if os.path.exists(config.SOCKET_PATH):
        os.remove(config.SOCKET_PATH)
    jarvis = Jarvis()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        if config.WINDOWS:          # no loop signal handlers on Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
        else:
            loop.add_signal_handler(sig, stop.set)
    task = asyncio.create_task(jarvis.run())
    await asyncio.wait([task, asyncio.create_task(stop.wait())], return_when=asyncio.FIRST_COMPLETED)
    if task.done() and task.exception():
        log.error("crashed", exc_info=task.exception())
    events.emit("stopping")
    task.cancel()
    jarvis.ears.stream.close()
    jarvis.mouth.stream.close()
    await jarvis.brain.stop()
    await worker.stop_all()
    pctools.cleanup()
    for path in (config.SOCKET_PATH, config.CTL_TOKEN_FILE):
        if os.path.exists(path):
            os.remove(path)


if __name__ == "__main__":
    asyncio.run(main())
