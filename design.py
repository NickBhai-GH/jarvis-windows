"""Jarvis's look, shared by the widget and the screen overlay: one colour per state, colours, type, radii, shadows, motion.
The overlay is iOS dark mode; the widget is a glowing line with HUD-style type (HUD / MONO)."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPen

# One colour per state, never mixed: no multicolour gradients anywhere; glows vary only in brightness.
# Swap any of these freely.
COLORS = {
    "listening": "#FFFFFF",     # white: you are talking
    "thinking": "#62D6FF",      # cyan
    "speaking": "#0A84FF",      # blue: Jarvis is talking
    "waiting": "#FF9F0A",       # orange: waiting for your yes or no
    "ready": "#30D158",         # green: an update is waiting for a quiet moment on your call
    "background": "#E8F9FF",    # background-work notes (a pale cyan)
    "idle": "#8E8E93",
    "offline": "#FF453A",       # red: Jarvis isn't running
    "annotate": "#0A84FF",      # the overlay's rings, arrows, boxes and step badges
}
INK = QColor(12, 12, 14)                     # text on light fills


def color(name):
    return QColor(COLORS.get(name, COLORS["idle"]))


def on(c):
    """Readable text colour on a fill of colour c."""
    return INK if c.lightnessF() > 0.7 else LABEL


LABEL = QColor(255, 255, 255)
SECONDARY = QColor(235, 235, 245, 153)       # iOS secondaryLabel (60%)
GLASS = QColor(28, 28, 30, 217)              # callout pills
HAIRLINE = QColor(255, 255, 255, 34)

RADIUS_PILL = 12
SHADOW = (16, 6, 70)                         # blur, y offset, peak alpha

# Motion (seconds)
FADE_IN = 0.22
FADE_OUT = 0.20
STAGGER = 0.12

FAMILIES = ["Inter", "SF Pro Text", "Noto Sans"]   # first one installed wins
HUD = ["Rajdhani", *FAMILIES]                       # HUD type for the widget, if installed (else the fallbacks)
MONO = ["Share Tech Mono", "monospace"]


def font(px, weight=QFont.Normal, families=FAMILIES):
    f = QFont()
    f.setFamilies(families)
    f.setPixelSize(px)
    f.setWeight(weight)
    f.setHintingPreference(QFont.PreferNoHinting)   # smooth, Apple-like glyphs
    return f


def alpha(color, a):
    """The colour with its alpha multiplied by a (0-1)."""
    c = QColor(color)
    c.setAlphaF(max(0.0, min(1.0, c.alphaF() * a)))
    return c


def clamp01(t):
    return max(0.0, min(1.0, t))


def ease_out(t):
    return 1 - (1 - clamp01(t)) ** 3


def ease_back(t):
    """Ease out with a small overshoot, for things that pop in."""
    t = clamp01(t) - 1
    return 1 + 2.2 * t ** 3 + 1.2 * t ** 2


def shadow(p, path, opacity=1.0, blur=SHADOW[0], dy=SHADOW[1], peak=SHADOW[2]):
    """Soft drop shadow under a filled path: stacked translucent outlines approximate a gaussian blur."""
    p.save()
    p.translate(0, dy)
    p.setBrush(Qt.NoBrush)
    steps = 8
    for i in range(steps, 0, -1):
        p.setPen(QPen(QColor(0, 0, 0, round(peak * opacity / steps * 0.9)), blur * i / steps,
                      Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)
    p.fillPath(path, QColor(0, 0, 0, round(peak * opacity * 0.5)))
    p.restore()

