"""Jarvis settings. Change these (or override them in config_local.py), then restart Jarvis."""
import os
import sys
import tempfile

WINDOWS = sys.platform == "win32"
HOME = os.path.expanduser("~")
JARVIS_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
SOCKET_PATH = os.path.join(RUNTIME_DIR, "jarvis.sock")
CTL_PORT = 8766            # Windows has no unix socket: jarvisctl uses this localhost port plus a token file
CTL_TOKEN_FILE = os.path.join(RUNTIME_DIR, "jarvis-ctl.token")
STATE_FILE = os.path.join(JARVIS_DIR, "state.json")
LOG_FILE = os.path.join(JARVIS_DIR, "logs", "jarvis.log")

# You
USER_NAME = "Tony"         # what Jarvis calls you when it talks about you
HONORIFIC = "sir"          # how Jarvis addresses you: "sir", "ma'am", "boss"...
CITY = "London"            # for the weather line in the morning greeting (wttr.in; use + for spaces, e.g. "New+York")

# Folders Jarvis may write to without asking (plus your home folder)
WRITE_OK_DIRS = []

# Brain
MODEL = "claude-opus-5-5"
JUDGE_MODEL = "claude-sonnet-5"   # decides workers' permission requests before any reach you
EFFORT = "medium"         # low / medium / high: higher = smarter but slower to answer
SESSION_IDLE_RESET_MIN = 30   # after this long with nothing going on, the next request gets a fresh session
NOTES_FILE = os.path.join(JARVIS_DIR, "notes.md")   # running summary carried from session to session
PERSONA_FILE = os.path.join(JARVIS_DIR, "persona.md")

# Ears
MIC_DEVICE = None          # None = system default input
WAKE_THRESHOLD = 0.5       # raise if it wakes by itself, lower if it misses you
WAIT_FOR_SPEECH_S = 6.0    # after "hey jarvis", how long to wait for you to start talking
END_SILENCE_S = 1.6        # this much silence = you've finished talking (lower values cut people off mid-thought)
MAX_UTTERANCE_S = 45.0
FOLLOW_UP_S = 5.0          # after Jarvis answers, listen this long without needing the wake word
CONFIRM_WAIT_S = 8.0       # how long to wait for "yes" on a confirmation
BARGE_IN_BY_VOICE = True   # talk over him to cut him off (needs headphones; set False if he interrupts himself)
WHISPER_MODEL = "small.en"
# Names and jargon whisper should expect. Add your own names, places and apps it keeps mishearing.
VOCAB = "Jarvis, Spotify, Discord, OBS, Steam, Konsole, Dolphin, Kate, Chrome, Thunderbird."

# Mouth
VOICE_ENGINE = "kokoro"    # "kokoro" (works out of the box), or "piper" for your own trained Piper voice (PIPER_MODEL)
PIPER_MODEL = ""           # path to a Piper .onnx voice; its .onnx.json must sit next to it
VOICE = "bm_lewis"         # Kokoro voice: try bm_george, bm_daniel, bm_fable
SPEED = 1.05
KOKORO_MODEL = os.path.join(JARVIS_DIR, "models", "kokoro-v1.0.onnx")
KOKORO_VOICES = os.path.join(JARVIS_DIR, "models", "voices-v1.0.bin")

# Don't talk over your calls: unprompted speech (worker updates, timers) waits while you're on a call (another app
# has a mic) and anyone on it is talking; the widget shows "Update ready" meanwhile. "Hey Jarvis" delivers it now.
POLITE = True
POLITE_QUIET_S = 1.5       # nobody on the call (you or them) has talked for this long = go ahead
POLITE_CALL_LEVEL = 200    # the call app's playback louder than this (16-bit RMS) = the other people are talking
POLITE_MAX_WAIT_S = 60     # held this long: also a desktop notification (still waits for quiet or "Hey Jarvis")

# Desktop widget
WIDGET_SCREEN = "right"        # monitor by connector name (e.g. "HDMI-A-1", "DP-1"), or "left"/"right"
                               # (it sits at the bottom centre; its size and height: LINE_W / LINE_UP in widget.py)

# Your own settings go in config_local.py (gitignored), e.g. USER_NAME = "Pepper"
try:
    from config_local import *  # noqa: F401,F403
except ImportError:
    pass
