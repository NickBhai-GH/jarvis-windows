"""Jarvis desktop widget: a glowing "emitter line" at the bottom centre of a monitor, always on top and click-through.

At rest it draws nothing (if your wallpaper has a line there, line it up with LINE_W / LINE_UP below and it looks like
the wallpaper's own line lighting up). When something happens the line lights up, one colour per state
(design.COLORS), never mixed, and the words float above it with a soft dark glow on the glyphs only (no box, no blur):
listening, white, rippling with your voice; thinking (or working by himself), cyan, a spark sweeping to and fro;
speaking, blue, rippling with Jarvis's voice; waiting for yes/no, orange, breathing; an update held until your call
goes quiet, green, breathing slower ("Update ready"); Jarvis not running, a dim red line. Background work at rest: a
faint spark drifting along the line. It all fades away a couple of seconds after Jarvis finishes.
The text: what you're saying (live), then what he's saying, sentence by sentence. Look: design.py.

Run: systemctl --user start jarvis-widget   (starts and stops with Jarvis)
"""
import http.client
import json
import math
import os
import sys
import threading
import time

SYSTEM_PY = "/usr/bin/python3"
if __name__ == "__main__" and sys.platform != "win32" and sys.executable != SYSTEM_PY:   # layer-shell needs the system Qt, not the venv's
    os.execv(SYSTEM_PY, [SYSTEM_PY, os.path.abspath(__file__)])  # (see overlay.py); before any Qt loads

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
                           QPixmap, QRadialGradient)
from PySide6.QtWidgets import QApplication, QWidget

import config
import design as d
import overlay

TITLE = "Jarvis Widget"
LINE_W, LINE_UP = 660, 19            # the line's length, and its height above the screen's bottom edge (in pixels)
GLOW = 40                            # room either side for the line's glow
W, H = LINE_W + 2 * GLOW, 160        # the window, bottom centre of the screen: the line plus the words above it
LINE_X0, LINE_X1, LY = GLOW, GLOW + LINE_W, H - LINE_UP + 0.5     # the line, in window pixels (LY: its centre row)
TEXT_X0, TEXT_X1, TEXT_BOTTOM = LINE_X0 + 30, LINE_X1 - 30, LY - 11.5   # the words sit just above it
PORT = 8765
ACTIVE = ("listening", "thinking", "speaking", "waiting", "ready")   # lit while these last
LINGER_S = 2.5                                # stays up this long after he finishes, then fades away
NOTE_S = 4.0                                  # background-work / offline notes show this long

LABELS = {"idle": "Idle", "listening": "Listening", "thinking": "Thinking", "speaking": "Jarvis",
          "waiting": "Say yes or no", "ready": "Update ready", "offline": "Jarvis isn't running"}
TOOL_NAMES = {"screenshot": "looking at the screen", "click_at": "clicking", "move_mouse": "moving the mouse",
              "scroll": "scrolling", "type_text": "typing", "press_keys": "pressing keys", "open_app": "opening an app",
              "focus_window": "switching windows", "list_windows": "checking windows", "media": "media controls",
              "volume": "volume", "Bash": "running a command", "Read": "reading a file", "Write": "writing a file",
              "Edit": "editing a file", "Grep": "searching files", "Glob": "finding files", "WebSearch": "searching the web",
              "WebFetch": "reading a web page", "ToolSearch": "loading tools", "Agent": "sub-agent working",
              "navigate": "opening a page in Chrome", "computer": "working in Chrome", "read_page": "reading the page",
              "get_page_text": "reading the page", "find": "searching the page", "form_input": "filling a form",
              "annotate": "pointing on screen"}


def short_dur(sec):
    sec = int(max(0, sec))
    return f"{sec}s" if sec < 60 else f"{sec // 60}m {sec % 60:02d}s" if sec < 3600 else f"{sec // 3600}h {sec % 3600 // 60:02d}m"


def describe_step(ev):
    """'Command: Check the weather' rather than just 'running a command'."""
    short = ev["name"].split("__")[-1]
    name = TOOL_NAMES.get(short, short.replace("_", " "))
    try:
        inp = json.loads(ev.get("input") or "{}")
    except ValueError:
        inp = {}
    detail = inp.get("description") or inp.get("query") or inp.get("url") or inp.get("name") or ""
    if short in ("Read", "Write", "Edit") and inp.get("file_path"):
        detail = os.path.basename(inp["file_path"])
    if short in ("ToolSearch", "screenshot", "click_at", "move_mouse", "scroll", "annotate"):
        detail = ""
    name = name[0].upper() + name[1:]
    return f"{name}: {detail}" if detail else name


class Feed(QObject):
    """Asks Jarvis what's happening several times a second (a tiny local request, about 2 ms)."""
    event = Signal(dict)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    @staticmethod
    def _get(path):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=3)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{PORT}"})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def _run(self):
        use_live = True
        while True:
            busy = False
            try:
                if use_live:
                    status, body = self._get("/api/live")
                    if status == 404:
                        use_live = False       # an older Jarvis without /api/live: use the full status instead
                        continue
                    view = json.loads(body)
                else:
                    status, body = self._get("/api/snapshot")
                    view = from_snapshot(json.loads(body))
                self.event.emit({"kind": "live", **view})
                busy = view["activity"] != "idle" or bool(view["tasks"])
            except Exception:
                self.event.emit({"kind": "live", "activity": "offline", "mic": 0, "out": 0, "tasks": [],
                                 "question": "", "you": "", "said": "", "step": "", "self_started": False})
                use_live = True
                time.sleep(2)
                continue
            time.sleep((1 / 15 if use_live else 0.7) if busy else 0.5)


def from_snapshot(snap):
    """Build the widget's view from the dashboard snapshot (fallback for an older running Jarvis)."""
    hist = snap.get("history", [])
    last_turn = max((i for i, e in enumerate(hist) if e["kind"] == "turn_start"), default=-1)
    said = step = ""
    for e in hist[last_turn + 1:]:
        if e["kind"] == "say":
            said = e["text"]
        elif e["kind"] == "tool_use" and not e.get("sub"):
            step = describe_step(e)
    heard = [e for e in hist if e["kind"] == "heard" and e.get("text")]
    turn = snap.get("turn") or {}
    return {"activity": snap.get("activity", "idle"), "mic": 0, "out": 0, "question": snap.get("question") or "",
            "tasks": snap.get("tasks", []), "you": (heard[-1]["text"] if heard else "") if snap.get("question")
            else turn.get("text") or (heard[-1]["text"] if heard else ""), "said": said, "step": step,
            "self_started": turn.get("source") == "self"}


def pick_screen():
    """The monitor from config."""
    screens = sorted(QApplication.screens(), key=lambda s: s.geometry().x())
    return {"left": screens[0], "right": screens[-1]}.get(config.WIDGET_SCREEN) or next(
        (s for s in screens if s.name() == config.WIDGET_SCREEN), screens[0])


def soft_glow(pm, spread=4):
    """A dark, soft copy of what's on pm (its glyphs' silhouette, blurred by scaling down and back up)."""
    img = pm.toImage().convertToFormat(QImage.Format_ARGB32_Premultiplied)
    q = QPainter(img)
    q.setCompositionMode(QPainter.CompositionMode_SourceIn)
    q.fillRect(img.rect(), QColor(2, 8, 10))
    q.end()
    w, h = img.width(), img.height()
    small = img.scaled(w // spread, h // spread, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    out = QPixmap.fromImage(small.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
    out.setDevicePixelRatio(pm.devicePixelRatio())
    return out


class Widget(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(TITLE)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                            | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(W, H)
        self.activity = "offline"
        self.hue = "offline"              # the line's colour: the last state that lit it (so it fades out in it)
        self.mic = self.out = 0.0         # smoothed levels
        self.mic_raw = self.out_raw = 0.0
        self.you = ""                     # what you said (live, then final)
        self.said = ""                    # sentence he's speaking now
        self.step = ""
        self.question = ""
        self.tasks = {}                   # background work: task_id -> {description, started, last_tool}
        self.self_started = False
        self.t0 = self.last_tick = time.time()
        self.linger_until = 0.0           # stays up this long after he finishes
        self.note_until = 0.0             # a background-work or offline note shows until then
        self.task_ids = frozenset()
        self.font_label = d.font(13, families=d.MONO)
        self.font_label.setLetterSpacing(QFont.AbsoluteSpacing, 3)
        self.font_text = d.font(21, QFont.Medium, d.HUD)
        self.font_small = d.font(18, QFont.Medium, d.HUD)
        self.presence = 0.0               # the words: 0 = gone, 1 = up (eases both ways)
        self.lit = 0.0                    # the line: 0 = not drawn, 1 = lit
        self.content = None               # what's on show: (label, text, dim, footer, lines)
        self.content_key = None
        self.content_t0 = 0.0             # when the current kind of content appeared (for its fade-in)
        self.last_key = None
        self.words_key = self.words_pm = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)

    # ---------- events ----------

    def on_event(self, ev):
        if ev.get("kind") != "live":
            return
        if ev["activity"] != self.activity:
            if ev["activity"] == "offline":
                self.note_until = time.time() + NOTE_S
            self.activity = ev["activity"]
        self.mic_raw, self.out_raw = ev.get("mic", 0), ev.get("out", 0)
        self.question = ev.get("question", "")
        self.you, self.said, self.step = ev.get("you", ""), ev.get("said", ""), ev.get("step", "")
        self.self_started = ev.get("self_started", False)
        self.tasks = {t.get("task_id", str(i)): t for i, t in enumerate(ev.get("tasks", []))}
        ids = frozenset(self.tasks)
        if ids != self.task_ids:
            self.task_ids = ids
            if ids:                        # a job started or finished and others are still going: say so briefly
                self.note_until = time.time() + NOTE_S

    def compose(self, now):
        """What to show: (label, text, dim, footer, task_lines). Same wording as always."""
        a = self.activity
        label, text, dim, footer, lines = LABELS.get(a, a), "", False, "", None
        if self.tasks and a != "idle" and now < self.note_until:
            footer = f"+{len(self.tasks)} in the background"
        if a == "listening":
            label, text = "You", self.you or "..."
        elif a == "thinking":
            label = "Working by himself" if self.self_started else "Thinking"
            if self.step:
                text = self.step
            else:
                text, dim = ("Checking on a background job" if self.self_started else self.you), True
        elif a == "speaking":
            text = self.said or "..."
        elif a == "waiting":
            text = self.question
        elif a == "idle" and self.said and now < self.linger_until:
            label, text, dim = "Jarvis", self.said, True
        elif a == "idle" and self.tasks:
            label = f"Working in the background ({len(self.tasks)})"
            lines = [(short_dur(now - t["started"]), t["description"] or "Background job")
                     for t in sorted(self.tasks.values(), key=lambda t: t["started"])[:3]]
        elif a == "idle":
            text, dim = "Say “Hey Jarvis”", True
        elif a == "offline":
            text, dim = "Jarvis isn't running", True
        elif a == "ready":                    # an update held for a quiet moment on your call
            text = "Update ready"
        if a in ("idle", "offline", "ready") and not lines and label != "Jarvis":
            label, text = text, ""            # a short note: just the one line, in the label's style
        return label, text, dim, footer, lines

    def tick(self):
        now = time.time()
        dt, self.last_tick = min(0.05, now - self.last_tick), now
        # attack fast, release slow, so the glow feels alive rather than jittery
        for name in ("mic", "out"):
            raw, cur = getattr(self, name + "_raw"), getattr(self, name)
            setattr(self, name, cur + (raw - cur) * (0.5 if raw > cur else 0.12))
        if self.activity in ACTIVE:
            self.linger_until = now + LINGER_S
        if self.activity != "idle":
            self.hue = self.activity
        show = self.activity in ACTIVE or now < self.linger_until or now < self.note_until
        lit = self.activity in ACTIVE or self.activity == "offline"
        for name, target in (("presence", 1.0 if show else 0.0), ("lit", 1.0 if lit else 0.0)):
            v = getattr(self, name)
            v += (target - v) * min(1.0, dt * 9)                   # eases in or out over about a third of a second
            setattr(self, name, target if abs(target - v) < 0.01 else v)
        if show or self.content is None:   # while fading away, keep showing what was there
            self.content = self.compose(now)
        if self.content[0] != self.content_key:
            self.content_key, self.content_t0 = self.content[0], now
        # Full 60 fps while it fades or follows a voice or a sweep; breathing and the drifting background spark repaint
        # just the line at 30 fps; otherwise only when something changes (saves CPU). At rest it draws nothing at all.
        fading = 0 < self.presence < 1 or 0 < self.lit < 1 or now - self.content_t0 < 0.3
        fast = fading or (self.lit and self.activity in ("listening", "speaking", "thinking"))
        slow = not fast and ((self.lit and self.activity in ("waiting", "ready")) or (self.tasks and not self.lit))
        key = (self.content if self.presence else None, round(self.presence, 2), round(self.lit, 2))
        if fast or key != self.last_key:
            self.update()
        elif slow:
            self.update(0, int(LY) - 24, W, H - int(LY) + 24)
        self.last_key = key
        self.timer.setInterval(16 if fast else 33 if slow else 100)

    # ---------- drawing ----------

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        self.draw_line(p)
        if self.content and self.presence:
            p.setOpacity(self.presence * d.ease_out((time.time() - self.content_t0) / 0.28))
            p.drawPixmap(0, 0, self.words())

    def draw_line(self, p):
        """The emitter line in the state's colour: rippling with a voice, a spark sweeping to and fro while thinking,
        breathing while it waits. At rest only a faint spark drifts along it while background work runs."""
        a, t = self.activity, time.time() - self.t0
        x0, half = LINE_X0, LINE_W / 2
        cx = x0 + half
        if self.tasks and self.lit < 1:                           # background work, under everything else
            k = 0.55 * (1 - self.lit)
            self.draw_spark(p, cx + half * 0.8 * math.sin(t * 0.45), 46, d.color("background"), k, LY)
        if not self.lit:
            return
        c, k = d.color(self.hue), self.lit
        level = self.mic if self.hue == "listening" else self.out if self.hue == "speaking" else 0.0
        if self.hue in ("waiting", "ready"):
            k *= 0.6 + 0.4 * math.sin(t * (3.0 if self.hue == "waiting" else 1.5))
        elif self.hue == "offline":
            k *= 0.45
        # the line itself: pinned at both ends, rippling in the middle with the voice
        amp = (1.2 + 14 * level) if self.hue in ("listening", "speaking") else 0.0
        path = QPainterPath(QPointF(x0, LY))
        for i in range(1, 121):
            u = i / 60 - 1
            wave = 0.6 * math.sin(u * 13 + t * 9) + 0.4 * math.sin(u * 29 - t * 14)
            path.lineTo(cx + u * half, LY + amp * (1 - u * u) ** 2 * wave)
        g = QLinearGradient(x0, 0, x0 + 2 * half, 0)
        g.setColorAt(0, d.alpha(c, 0))
        g.setColorAt(0.5, c)
        g.setColorAt(1, d.alpha(c, 0))
        p.setBrush(Qt.NoBrush)
        for width, a_ in ((12, 0.07), (6, 0.16), (2.6, 0.45), (1.2, 1.0)):      # wide and faint to thin and bright
            p.setOpacity(k * a_)
            p.setPen(QPen(QBrush(g), width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)
        p.setOpacity(1)
        # the spark: a bright core on the line, wider with the voice; it sweeps while he thinks
        sx = cx + half * 0.82 * math.sin(t * 1.7) if self.hue == "thinking" else cx
        self.draw_spark(p, sx, 130 * (1 + 1.4 * level), c, k * (0.75 + 0.25 * level), LY)

    @staticmethod
    def draw_spark(p, x, length, c, k, y):
        """A short bright dash with a pool of light spilling under it."""
        p.save()
        p.translate(x, y + 2)
        p.scale(1, 0.07)                                          # a flat ellipse of light
        pool = QRadialGradient(QPointF(0, 0), length * 1.4)
        pool.setColorAt(0, d.alpha(c, 0.30 * k))
        pool.setColorAt(1, d.alpha(c, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(pool)
        p.drawEllipse(QPointF(0, 0), length * 1.4, length * 1.4)
        p.restore()
        hot = QColor(c).lighter(130)
        g = QLinearGradient(x - length / 2, 0, x + length / 2, 0)
        g.setColorAt(0, d.alpha(hot, 0))
        g.setColorAt(0.5, d.alpha(QColor(255, 255, 255), k))
        g.setColorAt(1, d.alpha(hot, 0))
        for width, a_ in ((7, 0.25), (2, 1.0)):
            p.setPen(QPen(QBrush(g), width, Qt.SolidLine, Qt.RoundCap))
            p.setOpacity(a_)
            p.drawLine(QPointF(x - length / 2, y), QPointF(x + length / 2, y))
        p.setOpacity(1)

    def words(self):
        """The words with a soft dark glow behind the glyphs only (no plate), redrawn only when they change."""
        key = (self.content, self.activity)
        if key != self.words_key:
            dpr = self.devicePixelRatioF()
            pm = QPixmap(round(W * dpr), round(H * dpr))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.transparent)
            q = QPainter(pm)
            q.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
            self.draw_words(q, *self.content)
            q.end()
            out = QPixmap(pm.size())
            out.setDevicePixelRatio(dpr)
            out.fill(Qt.transparent)
            q = QPainter(out)
            for spread in (8, 4, 4):                              # a wide soft halo, then a tighter one
                q.drawPixmap(0, 0, soft_glow(pm, spread))
            q.drawPixmap(0, 0, pm)
            q.end()
            self.words_key, self.words_pm = key, out
        return self.words_pm

    def draw_words(self, p, label, text, dim, footer, lines):
        """Centred over the line, bottom up: footer, then the text (or the task lines), then the label."""
        a = self.activity
        left, right, bottom = TEXT_X0, TEXT_X1, TEXT_BOTTOM
        width = right - left
        if footer:
            p.setFont(self.font_label)
            p.setPen(d.alpha(d.color("background"), 0.75))
            p.drawText(QRectF(left, bottom - 16, width, 16), Qt.AlignCenter, footer.upper())
            bottom -= 20
        if lines:
            p.setFont(self.font_small)
            fm = p.fontMetrics()
            for i, (dur, desc) in enumerate(reversed(lines)):
                row = QRectF(left, bottom - 22 * (i + 1), width, 22)
                p.setPen(d.LABEL)
                p.drawText(row, Qt.AlignCenter, fm.elidedText(f"{dur}   {desc}", Qt.ElideRight, int(width)))
            bottom -= 22 * len(lines) + 2
        elif text:
            p.setFont(self.font_text)
            flags = Qt.AlignHCenter | Qt.AlignBottom | Qt.TextWordWrap
            box = QRectF(left, bottom - 3 * p.fontMetrics().lineSpacing(), width, 3 * p.fontMetrics().lineSpacing())
            shown = self.fit(p, text, box)
            p.setPen(d.SECONDARY if dim else d.LABEL)
            p.drawText(box, flags, shown)
            bottom -= p.boundingRect(box, flags, shown).height() + 2
        if label:
            p.setFont(self.font_label)
            p.setPen(d.alpha(d.LABEL, 0.6) if label == "Jarvis" and a == "idle"
                     else d.color("background" if a == "idle" and lines else a).lighter(130))   # small type: a touch brighter
            p.drawText(QRectF(left, bottom - 18, width, 18), Qt.AlignCenter,
                       p.fontMetrics().elidedText(label.upper(), Qt.ElideRight, int(width)))

    @staticmethod
    def fit(p, text, box):
        """Show the END of long text (the newest words), trimmed from the front."""
        flags = Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap
        if p.boundingRect(box, flags, text).height() <= box.height():
            return text
        words = text.split()
        while len(words) > 1:
            words.pop(0)
            if p.boundingRect(box, flags, "... " + " ".join(words)).height() <= box.height():
                break
        return "... " + " ".join(words)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("jarvis-widget")
    app.setDesktopFileName("jarvis-widget")
    w = Widget()
    screen = pick_screen()
    ls = overlay.layer_surface(w, screen, overlay.BOTTOM)   # bottom centre, pinned, never takes focus
    overlay._set_zone(ls, -1)                                 # ignore any panel: measured from the screen's very bottom
    if sys.platform == "win32":                               # no layer shell: sit just above the taskbar ourselves
        g = screen.availableGeometry()
        w.move(g.x() + (g.width() - W) // 2, g.y() + g.height() - H)
    feed = Feed()
    feed.event.connect(w.on_event)
    w.show()
    feed.start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
