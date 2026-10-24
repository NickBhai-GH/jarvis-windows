"""Jarvis's "show me where" overlay: rings, arrows, boxes and callouts drawn over both monitors.

One see-through window per monitor, made a KDE layer-shell surface on the overlay layer, so it sits above
normal and fullscreen windows, takes no keyboard focus and lets every click through. pctools.annotate starts
it with the shapes (already in desktop pixels) as JSON; it fades out by itself after `duration` seconds, or
when terminated (clear_annotations / a newer drawing).

Look: design.py (iOS dark mode, one colour: COLORS['annotate']). Shapes scale and fade in, staggered; rings pulse gently.
A label is "Title" or "Title\\ndetail" and becomes a frosted pill with a short pointer at its shape.

Runs on the system python (/usr/bin/python3): its PySide6 uses the system Qt, which KDE's layer-shell-qt
library is built for. The venv's pip PySide6 bundles its own Qt and can't load it.

Try it: /usr/bin/python3 overlay.py '{"duration": 5, "shapes": [{"type": "ring", "x": 960, "y": 540, "radius": 60,
         "label": "Here", "step": 1}]}'
"""
import ctypes
import json
import math
import signal
import sys
import time

import shiboken6
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QRegion
from PySide6.QtWidgets import QApplication, QWidget

import design as d

TOP, BOTTOM, LEFT, RIGHT = 1, 2, 4, 8          # layer-shell anchors


def _ptr(obj):
    return shiboken6.getCppPointer(obj)[0]


if sys.platform == "win32":
    # Windows: the window flags already make it topmost, click-through and focus-free; callers place it themselves.
    def layer_surface(widget, screen, anchors=None, margins=None):
        widget.setScreen(screen)

    def _set_zone(ls, zone):
        pass

    def _blur(window, on, region):              # no compositor blur hook here: the pills stay plain dark glass
        pass
else:
    # KDE's layer-shell-qt has no Python binding, so call its four setters directly (C++ names, plain pointer args).
    _ls = ctypes.CDLL("libLayerShellQtInterface.so.6")
    _get = _ls._ZN12LayerShellQt6Window3getEP7QWindow
    _get.restype, _get.argtypes = ctypes.c_void_p, [ctypes.c_void_p]
    _set_layer = _ls._ZN12LayerShellQt6Window8setLayerENS0_5LayerE
    _set_keyboard = _ls._ZN12LayerShellQt6Window24setKeyboardInteractivityENS0_21KeyboardInteractivityE
    _set_zone = _ls._ZN12LayerShellQt6Window16setExclusiveZoneEi
    _set_screen = _ls._ZN12LayerShellQt6Window9setScreenEP7QScreen
    _set_anchors = _ls._ZN12LayerShellQt6Window10setAnchorsE6QFlagsINS0_6AnchorEE
    _set_margins = _ls._ZN12LayerShellQt6Window10setMarginsERK8QMargins
    for f in (_set_layer, _set_keyboard, _set_zone, _set_anchors):
        f.argtypes = [ctypes.c_void_p, ctypes.c_int]
    for f in (_set_screen, _set_margins):
        f.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    LAYER_OVERLAY, KEYBOARD_NONE = 3, 0

    def layer_surface(widget, screen, anchors=TOP | BOTTOM | LEFT | RIGHT, margins=None):
        """Make a QWidget a layer-shell surface on the overlay layer of `screen`: above fullscreen windows, and it can
        never take keyboard focus. Call before show(). Anchors pin it to screen edges; margins are QMargins from them."""
        widget.winId()                              # create the native window so layer-shell can claim it
        ls = _get(_ptr(widget.windowHandle()))
        _set_layer(ls, LAYER_OVERLAY)
        _set_keyboard(ls, KEYBOARD_NONE)
        _set_screen(ls, _ptr(screen))
        _set_anchors(ls, anchors)
        if margins is not None:
            _set_margins(ls, _ptr(margins))
        return ls
    # KWin's blur behind the pills (real frosted glass), same trick: KWindowEffects::enableBlurBehind.
    _blur = ctypes.CDLL("libKF6WindowSystem.so.6")._ZN14KWindowEffects16enableBlurBehindEP7QWindowbRK7QRegion
    _blur.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_void_p]

STROKE, BADGE_R, GAP, POINTER = 3.0, 10.0, 10.0, 7.0
ACCENT = d.color("annotate")


def bezier(a, c, b, t):
    u = 1 - t
    return QPointF(u * u * a.x() + 2 * u * t * c.x() + t * t * b.x(), u * u * a.y() + 2 * u * t * c.y() + t * t * b.y())


class Shape:
    """One annotation, laid out once in desktop pixels."""
    def __init__(self, s, i, fonts):
        self.kind, self.delay = s.get("type", "ring"), i * d.STAGGER
        self.title, _, self.detail = str(s.get("label") or "").partition("\n")
        self.title, self.detail = self.title.strip(), self.detail.strip()
        self.step = str(s["step"]) if s.get("step") else ""
        self.fonts = fonts
        x, y = float(s["x"]), float(s["y"])
        if self.kind == "arrow":
            self.a, self.b = QPointF(float(s["from_x"]), float(s["from_y"])), QPointF(x, y)
            v = self.b - self.a
            self.c = (self.a + self.b) / 2 + QPointF(v.y(), -v.x()) * 0.18     # a gentle bow
            self.bounds = QRectF(self.a, self.b).normalized()
            anchor, below = QRectF(self.a, self.a), None                   # the pill sits on the tail
        elif self.kind == "rect":
            self.bounds = QRectF(x, y, float(s["w"]), float(s["h"]))
            anchor, below = self.bounds, True
        else:
            r = float(s.get("radius", 40))
            self.bounds = QRectF(x - r, y - r, 2 * r, 2 * r)
            anchor, below = self.bounds, True
        self.pill = self.layout_pill(anchor, below) if self.title else None
        self.badge = None
        if self.step and not self.pill:
            self.badge = (self.a if self.kind == "arrow" else self.bounds.topLeft() if self.kind == "rect"
                          else self.bounds.center() + QPointF(-0.707, -0.707) * self.bounds.width() / 2)
        self.extent = self.bounds.adjusted(-40, -40, 40, 40)
        if self.pill:
            self.extent = self.extent.united(self.pill[0].adjusted(-30, -30, 30, 40))

    def layout_pill(self, anchor, below):
        tf, df, _ = self.fonts
        tw = tf.horizontalAdvance(self.title)
        dw = df.horizontalAdvance(self.detail) if self.detail else 0
        lead = 2 * BADGE_R + 8 if self.step else 0
        w = 28 + lead + max(tw, dw)
        h = 18 + tf.height() + (df.height() + 1 if self.detail else 0)
        box = QRectF(0, 0, w, max(h, 2 * BADGE_R + 18))
        scr = QApplication.screenAt(anchor.center().toPoint()) or QApplication.primaryScreen()
        g = QRectF(scr.geometry()).adjusted(12, 12, -12, -12)              # stay on the shape's monitor
        if below is None:                                                  # centred on an arrow's tail
            box.moveCenter(anchor.center())
        else:
            room_below = anchor.bottom() + GAP + POINTER + box.height() <= g.bottom()
            room_above = anchor.top() - GAP - POINTER - box.height() >= g.top()
            below = room_below or not room_above
            box.moveCenter(QPointF(anchor.center().x(), 0))
            if below:
                box.moveTop(anchor.bottom() + GAP + POINTER)
            else:
                box.moveBottom(anchor.top() - GAP - POINTER)
        box.moveLeft(max(g.left(), min(box.left(), g.right() - box.width())))
        box.moveTop(max(g.top(), min(box.top(), g.bottom() - box.height())))
        path = QPainterPath()
        path.addRoundedRect(box, d.RADIUS_PILL, d.RADIUS_PILL)
        if below is not None:                                              # a short pointer at the shape
            px = max(box.left() + 18, min(anchor.center().x(), box.right() - 18))
            edge, tip = (box.top(), box.top() - POINTER) if below else (box.bottom(), box.bottom() + POINTER)
            tri = QPainterPath()
            tri.moveTo(px - 8, edge)
            tri.quadTo(px - 2, tip + (1 if below else -1), px, tip)
            tri.quadTo(px + 2, tip + (1 if below else -1), px + 8, edge)
            tri.closeSubpath()
            path = path.united(tri)
        return box, path, (1 if below else -1) if below is not None else 0

    # ---------- drawing ----------

    def paint(self, p, t):
        k = (t - self.delay) / d.FADE_IN
        if k <= 0:
            return
        op = d.clamp01(k)
        p.save()
        p.setBrush(Qt.NoBrush)                    # outlines only; never inherit a fill from whatever drew last
        if self.kind == "arrow":
            self.paint_arrow(p, t, op)
        else:
            self.paint_outline(p, t, op)
        if self.pill:
            self.paint_pill(p, t)
        if self.badge:
            self.paint_badge(p, self.badge, d.ease_back((t - self.delay - 0.1) / 0.3), shadow=True)
        p.restore()

    def outline(self, grow=0.0, scale=1.0):
        b, path = self.bounds, QPainterPath()
        c = b.center()
        w, h = b.width() * scale + 2 * grow, b.height() * scale + 2 * grow
        r = QRectF(c.x() - w / 2, c.y() - h / 2, w, h)
        if self.kind == "rect":
            path.addRoundedRect(r, 10 + grow, 10 + grow)
        else:
            path.addEllipse(r)
        return path

    def paint_outline(self, p, t, op):
        path = self.outline(scale=0.88 + 0.12 * d.ease_back((t - self.delay) / 0.32))
        p.fillPath(path, d.alpha(ACCENT, 0.10 * op))
        for width, a in ((14, 0.05), (9, 0.10), (5.5, 0.18)):             # soft outer glow
            p.setPen(QPen(d.alpha(ACCENT, a * op), width))
            p.drawPath(path)
        p.save()
        p.translate(0, 1)
        p.setPen(QPen(d.alpha(QColor(0, 0, 0), 0.28 * op), STROKE + 1.5))  # lifts it off busy backgrounds
        p.drawPath(path)
        p.restore()
        p.setPen(QPen(d.alpha(ACCENT, op), STROKE))
        p.drawPath(path)
        phase = (t - self.delay - 0.45) % 2.2 / 1.3                        # a gentle ripple every 2.2 s
        if t - self.delay > 0.45 and phase < 1:
            p.setPen(QPen(d.alpha(ACCENT, 0.5 * (1 - phase) * op), 2))
            p.drawPath(self.outline(grow=18 * d.ease_out(phase)))

    def paint_arrow(self, p, t, op):
        pr = d.ease_out((t - self.delay) / 0.4)
        pts = [bezier(self.a, self.c, self.b, pr * i / 24) for i in range(25)]
        path = QPainterPath(pts[0])
        for q in pts[1:]:
            path.lineTo(q)
        tip, back = pts[-1], pts[-3]
        ang = math.atan2(tip.y() - back.y(), tip.x() - back.x())
        for side in (-0.5, 0.5):
            path.moveTo(tip)
            path.lineTo(tip - QPointF(math.cos(ang + side), math.sin(ang + side)) * 11)
        for dy, color, width in ((1, d.alpha(QColor(0, 0, 0), 0.28 * op), STROKE + 1.5), (0, d.alpha(ACCENT, op), STROKE)):
            p.save()
            p.translate(0, dy)
            p.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
            p.restore()

    def paint_pill(self, p, t):
        box, path, side = self.pill
        k = d.clamp01((t - self.delay - 0.08) / d.FADE_IN)
        if k <= 0:
            return
        p.save()
        p.setOpacity(p.opacity() * k)
        p.translate(0, -side * 6 * (1 - d.ease_out(k)))                    # slides out from its shape
        d.shadow(p, path)
        p.fillPath(path, d.GLASS)
        p.setPen(QPen(d.HAIRLINE, 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        tf, df, fonts = self.fonts
        x = box.left() + 14
        if self.step:
            self.paint_badge(p, QPointF(x + BADGE_R, box.center().y()), 1.0)
            x += 2 * BADGE_R + 8
        text_h = tf.height() + (df.height() + 1 if self.detail else 0)
        y = box.center().y() - text_h / 2
        p.setFont(fonts[0])
        p.setPen(d.LABEL)
        p.drawText(QRectF(x, y, box.right() - x, tf.height()), Qt.AlignLeft | Qt.AlignVCenter, self.title)
        if self.detail:
            p.setFont(fonts[1])
            p.setPen(d.SECONDARY)
            p.drawText(QRectF(x, y + tf.height() + 1, box.right() - x, df.height()), Qt.AlignLeft | Qt.AlignVCenter,
                       self.detail)
        p.restore()

    def paint_badge(self, p, at, scale, shadow=False):
        if scale <= 0:
            return
        r = BADGE_R * scale
        if shadow:
            ring = QPainterPath()
            ring.addEllipse(at, r + 1.5, r + 1.5)
            d.shadow(p, ring, blur=8, dy=2, peak=60)
            p.setPen(QPen(d.LABEL, 1.5))
        else:
            p.setPen(Qt.NoPen)
        p.setBrush(ACCENT)
        p.drawEllipse(at, r, r)
        p.setFont(self.fonts[2][2])
        p.setPen(d.on(ACCENT))
        p.drawText(QRectF(at.x() - r, at.y() - r, 2 * r, 2 * r), Qt.AlignCenter, self.step)


class Overlay(QWidget):
    def __init__(self, screen, shapes, clock):
        super().__init__()
        self.shapes, self.clock, self.origin = shapes, clock, screen.geometry().topLeft()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                            | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        _set_zone(layer_surface(self, screen), -1)        # whole monitor, over the taskbar too
        mine = QRectF(screen.geometry())
        self.dirty = [s.extent.translated(-self.origin.x(), -self.origin.y()).toAlignedRect()
                      for s in shapes if s.extent.intersects(mine)]
        self.glass = QRegion()
        for s in shapes:
            if s.pill and s.pill[0].intersects(mine):
                self.glass += QRegion(s.pill[1].translated(-self.origin.x(), -self.origin.y()).toFillPolygon().toPolygon())
        self.blurred = False

    def blur(self, on):
        """Blur what's behind the pills once they're in; drop it as they fade out."""
        if on != self.blurred and not self.glass.isEmpty():
            self.blurred = on
            _blur(_ptr(self.windowHandle()), on, _ptr(self.glass))

    def animate(self):
        for r in self.dirty:                       # repaint only around the shapes, not the whole monitor
            self.update(r)

    def paintEvent(self, _):
        t, fade = self.clock()
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        p.translate(-self.origin.x(), -self.origin.y())       # shapes are in desktop pixels
        p.setOpacity(fade)
        for s in self.shapes:
            s.paint(p, t)


def main():
    spec = json.loads(sys.argv[1])
    if sys.platform == "win32":                    # shapes arrive in real pixels: keep Qt from rescaling them for DPI
        import os
        os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
    app = QApplication(sys.argv[:1])
    app.setApplicationName("jarvis-overlay")
    fonts = (d.font(15, QFont.DemiBold), d.font(13), d.font(12, QFont.Bold))
    metrics = (QFontMetricsF(fonts[0]), QFontMetricsF(fonts[1]), fonts)
    shapes = [Shape(s, i, metrics) for i, s in enumerate(spec["shapes"])]
    start = time.monotonic()
    state = {"out": max(0.5, float(spec.get("duration", 8))) - d.FADE_OUT}

    def clock():
        t = time.monotonic() - start
        return t, 1 - d.ease_out((t - state["out"]) / d.FADE_OUT)

    def fade_now(*_):
        state["out"] = min(state["out"], time.monotonic() - start)

    signal.signal(signal.SIGTERM, fade_now)       # clear_annotations: fade out rather than vanish
    windows = [Overlay(s, shapes, clock) for s in app.screens()]
    for w in windows:
        w.show()

    settled = max(s.delay for s in shapes) + 0.08 + d.FADE_IN

    def tick():
        t = clock()[0]
        if t > state["out"] + d.FADE_OUT + 0.05:
            app.quit()
        for w in windows:
            w.blur(settled <= t < state["out"])
            w.animate()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(16)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
