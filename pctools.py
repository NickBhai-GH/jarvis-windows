"""Jarvis's hands on the desktop: apps, media, volume, screenshots, keyboard."""
import asyncio
import base64
import configparser
import glob
import io
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import time

from PIL import Image
from claude_agent_sdk import create_sdk_mcp_server, tool

import config
import events

if config.WINDOWS:
    import winctl
    kwin = winctl
else:
    import kwin

YDOTOOL_SOCKET = os.path.join(config.RUNTIME_DIR, "jarvis-ydotool.sock")

KEYCODES = {
    "esc": 1, "escape": 1, "minus": 12, "equal": 13, "backspace": 14, "tab": 15, "enter": 28, "return": 28,
    "ctrl": 29, "control": 29, "shift": 42, "alt": 56, "space": 57, "capslock": 58,
    "super": 125, "meta": 125, "win": 125, "home": 102, "up": 103, "pageup": 104, "left": 105,
    "right": 106, "end": 107, "down": 108, "pagedown": 109, "insert": 110, "delete": 111, "del": 111,
    "print": 99, "comma": 51, "dot": 52, "period": 52, "slash": 53, "semicolon": 39, "grave": 41,
}
KEYCODES.update({str(d): d + 1 for d in range(1, 10)} | {"0": 11})
for row, start in (("qwertyuiop", 16), ("asdfghjkl", 30), ("zxcvbnm", 44)):
    KEYCODES.update({ch: start + i for i, ch in enumerate(row)})
KEYCODES.update({f"f{i}": 58 + i for i in range(1, 11)} | {"f11": 87, "f12": 88})


def text(s):
    return {"content": [{"type": "text", "text": s}]}


def run(*cmd, env=None, timeout=15):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    return (p.stdout + p.stderr).strip()


# ---------- apps ----------

def desktop_entries():
    if config.WINDOWS:
        return winctl.start_apps()
    dirs =os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    dirs += [os.path.join(config.HOME, ".local/share"), os.path.join(config.HOME, ".local/share/flatpak/exports/share"),
             "/var/lib/flatpak/exports/share"]
    seen = {}
    for d in dirs:
        for path in glob.glob(os.path.join(d, "applications", "*.desktop")):
            app_id = os.path.basename(path)[:-8]
            if app_id in seen:
                continue
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(path, encoding="utf-8")
                e = cp["Desktop Entry"]
            except Exception:
                continue
            if e.get("NoDisplay", "false").lower() == "true" or e.get("Type") != "Application":
                continue
            seen[app_id] = {"id": app_id, "name": e.get("Name", app_id), "token": app_id.split(".")[-1],
                            "keywords": (e.get("GenericName", "") + " " + e.get("Keywords", "")).lower()}
    return list(seen.values())


ALIASES = ({"files": "file explorer", "file manager": "file explorer", "explorer": "file explorer",
            "browser": "google chrome", "chrome": "google chrome", "text editor": "notepad"} if config.WINDOWS else
           {"files": "dolphin", "file manager": "dolphin", "terminal": "konsole", "browser": "google chrome",
            "chrome": "google chrome", "settings": "system settings", "text editor": "kate", "calculator": "kcalc"})


def find_app(query):
    q = query.lower().strip()
    q = ALIASES.get(q, q)
    best, best_score = None, 0
    for a in desktop_entries():
        name = a["name"].lower()
        score = (100 if name == q else 80 if name.startswith(q) else 60 if q in name
                 else 40 if q in a["id"].lower() else 20 if q in a["keywords"] else 0)
        if score > best_score or (score == best_score and best and len(name) < len(best["name"])):
            best, best_score = a, score
    return best


@tool("open_app", "Open a desktop application by name and bring it to the front, e.g. 'spotify', 'discord', "
      "'obs', 'steam', 'files'. If it is already running, its window is focused instead.", {"name": str})
async def open_app(args):
    app = find_app(args["name"])
    for q in filter(None, [args["name"], app and app["name"], app and app["token"]]):
        w = await kwin.focus(q)
        if w:
            await seen_window(w)
            return text(f"{w['title']} was already open; brought it to the front.")
    if not app:
        return text(f"No installed app matches '{args['name']}'. Try list_apps.")
    if config.WINDOWS:
        winctl.launch(app["id"])
    else:
        subprocess.Popen(["gtk-launch", app["id"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    for _ in range(30):
        await asyncio.sleep(0.5)
        for q in (app["name"], app["token"]):
            w = await kwin.focus(q)
            if w:
                await seen_window(w)
                return text(f"Launched {app['name']}; its window '{w['title']}' is in front.")
    return text(f"Launched {app['name']} ({app['id']}) but no window showed up within 15s; it may still be loading.")


@tool("list_apps", "List installed desktop apps whose name contains the query (empty query = all).", {"query": str})
async def list_apps(args):
    q = args.get("query", "").lower()
    names = sorted(a["name"] for a in desktop_entries() if q in a["name"].lower() or q in a["keywords"])
    return text(", ".join(names) or "none")


@tool("open_path", "Open a file, folder or URL with its default app (xdg-open). For web work prefer the Chrome tools.",
      {"target": str})
async def open_path(args):
    if config.WINDOWS:
        os.startfile(os.path.expanduser(args["target"]))
        return text(f"Opened {args['target']}.")
    subprocess.Popen(["xdg-open", os.path.expanduser(args["target"])], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return text(f"Opened {args['target']}.")


# ---------- media and volume ----------

def mpris_players():
    return [s.strip() for s in run("qdbus").splitlines() if s.strip().startswith("org.mpris.MediaPlayer2.")]


def player_prop(svc, prop):
    out = run("busctl", "--user", "--json=short", "get-property", svc, "/org/mpris/MediaPlayer2",
              "org.mpris.MediaPlayer2.Player", prop)
    try:
        return json.loads(out)["data"]
    except Exception:
        return {} if prop == "Metadata" else "Unknown"


@tool("media", "Control whatever is playing music/video (Spotify, YouTube in Chrome, etc). "
      "action: status, play_pause, play, pause, next, previous, stop.", {"action": str})
async def media(args):
    if config.WINDOWS:                    # the media keys: whatever Windows' media overlay is controlling
        action = args["action"].lower()
        if action not in winctl.MEDIA:
            return text("On Windows I can send play_pause, next, previous and stop; for what's playing, take a screenshot.")
        winctl.media(action)
        return text(f"Sent {action}.")
    players = mpris_players()
    if not players:
        return text("Nothing is exposing media controls right now.")
    playing = [p for p in players if player_prop(p, "PlaybackStatus") == "Playing"]
    svc = (playing or players)[0]
    action = args["action"].lower()
    if action == "status":
        lines = []
        for p in players:
            meta = player_prop(p, "Metadata")
            title = meta.get("xesam:title", {}).get("data", "unknown")
            artist = ", ".join(meta.get("xesam:artist", {}).get("data", []))
            name = p.split(".")[3]
            lines.append(f"{name}: {player_prop(p, 'PlaybackStatus')} - {title}{' by ' + artist if artist else ''}")
        return text("\n".join(lines))
    method = {"play_pause": "PlayPause", "play": "Play", "pause": "Pause", "next": "Next",
              "previous": "Previous", "stop": "Stop"}.get(action)
    if not method:
        return text(f"Unknown action {action}.")
    run("qdbus", svc, "/org/mpris/MediaPlayer2", f"org.mpris.MediaPlayer2.Player.{method}")
    return text(f"{method} sent to {svc}.")


@tool("volume", "System output volume. action: get, set (level 0-100), up, down, mute, unmute. "
      "Pass level 0 when not setting.", {"action": str, "level": int})
async def volume(args):
    sink, action = "@DEFAULT_AUDIO_SINK@", args["action"].lower()
    if config.WINDOWS:
        return text(winctl.volume(action, args.get("level", 50)))
    if action == "set":
        run("wpctl", "set-volume", "-l", "1.0", sink, f"{max(0, min(100, args.get('level', 50)))}%")
    elif action in ("up", "down"):
        run("wpctl", "set-volume", "-l", "1.0", sink, "10%+" if action == "up" else "10%-")
    elif action in ("mute", "unmute"):
        run("wpctl", "set-mute", sink, "1" if action == "mute" else "0")
    return text(run("wpctl", "get-volume", sink))


# ---------- windows, screen, mouse, keyboard ----------

@tool("list_windows", "List open windows: title, app, position/size in desktop pixels, which one is active.", {})
async def list_windows(args):
    ws = await kwin.windows()
    return text("\n".join(f"{'* ' if w['active'] else ''}{w['title']} [{w['app']}] at {w['x']},{w['y']} "
                          f"{w['w']}x{w['h']}{' (minimised)' if w['minimized'] else ''}" for w in ws))


@tool("focus_window", "Bring a window to the front and give it keyboard focus. query matches the window title "
      "or app name, e.g. 'discord', 'thunderbird', 'Konsole'.", {"query": str})
async def focus_window(args):
    w = await kwin.focus(args["query"])
    if w:
        await seen_window(w)
    return text(f"Focused '{w['title']}' at {w['x']},{w['y']} {w['w']}x{w['h']}." if w
                else f"No open window matches '{args['query']}'. Try list_windows.")


LAST_SHOT = {"x": 0, "y": 0, "scale": 1.0, "ts": 0.0, "win": None}   # win: the window in front at the last look
SHOT_MAX_AGE_S = 60


async def seen_window(w=None):
    """Remember which window is in front: after a screenshot, or after Jarvis himself moved focus."""
    w = w or await kwin.active_window()
    LAST_SHOT["win"] = w and w.get("id")


async def focus_changed(check_age=False):
    """A refusal message if another window took focus (a game, a popup) since Jarvis last looked, or the screenshot is
    too old to click from; None if it's safe. Typing or clicking blind is how text lands in the wrong app."""
    w = await kwin.active_window()
    if LAST_SHOT["win"] and w and w.get("id") != LAST_SHOT["win"]:
        return text(f"Not done: '{w.get('title')}' took focus since your last screenshot. Take a fresh screenshot first.")
    if check_age and time.time() - LAST_SHOT["ts"] > SHOT_MAX_AGE_S:
        return text("Not done: the last screenshot is over a minute old. Take a fresh one before clicking.")


async def grab_desktop():
    """The whole desktop as one RGB image, or None."""
    if config.WINDOWS:
        from PIL import ImageGrab
        return await asyncio.get_running_loop().run_in_executor(None, lambda: ImageGrab.grab(all_screens=True).convert("RGB"))
    path = tempfile.mktemp(suffix=".png", dir=config.RUNTIME_DIR)
    proc = await asyncio.create_subprocess_exec("spectacle", "-b", "-n", "-f", "-o", path,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await asyncio.wait_for(proc.wait(), 20)
    img = None
    for _ in range(50):             # spectacle can return before the file is fully written
        await asyncio.sleep(0.1)
        try:
            img = Image.open(path).convert("RGB")
            break
        except (OSError, FileNotFoundError):
            continue
    if os.path.exists(path):
        os.remove(path)
    return img


@tool("screenshot","Look at the screen. target: 'window' (the active window, sharpest), 'left' or 'right' "
      "(one monitor, full detail) or 'both' (both monitors, half resolution). click_at, move_mouse and scroll "
      "take pixel coordinates inside the most recent screenshot image.", {"target": str})
async def screenshot(args):
    target = args.get("target", "both")
    img, (ox, oy) = (await grab_desktop()), (winctl.virtual_origin() if config.WINDOWS else (0, 0))
    if img is None:
        return text("Screenshot failed.")
    # box in desktop pixels; (ox, oy) is where the image's top-left sits on the desktop (negative if a monitor is left of the main one)
    box, label = (ox, oy, ox + img.width, oy + img.height), "both monitors"
    if target in ("left", "right"):
        screens = sorted(await kwin.screens(), key=lambda s: s["x"])
        s = screens[0] if target == "left" else screens[-1]
        box, label = (s["x"], s["y"], s["x"] + s["w"], s["y"] + s["h"]), f"the {target} monitor"
    elif target == "window":
        w = await kwin.active_window()
        if w:
            box = (max(ox, int(w["x"])), max(oy, int(w["y"])), min(ox + img.width, int(w["x"] + w["w"])),
                   min(oy + img.height, int(w["y"] + w["h"])))
            label = f"the active window '{w['title']}'"
    img = img.crop((box[0] - ox, box[1] - oy, box[2] - ox, box[3] - oy))
    scale = max(1.0, img.width / 1920, img.height / 1080)
    if scale > 1:
        img = img.resize((round(img.width / scale), round(img.height / scale)))
    LAST_SHOT.update(x=box[0], y=box[1], scale=scale, ts=time.time())
    await seen_window()
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    save_shot(buf.getvalue(), label)
    note = f"Screenshot of {label}, {img.width}x{img.height}. Use these image pixel coordinates with click_at."
    return {"content": [{"type": "text", "text": note},
                        {"type": "image", "data": base64.b64encode(buf.getvalue()).decode(), "mimeType": "image/jpeg"}]}


SHOTS_DIR = os.path.join(config.JARVIS_DIR, "logs", "shots")


def save_shot(jpeg, label):
    """Keep what Jarvis saw, for the dashboard. Only the newest 300 are kept."""
    os.makedirs(SHOTS_DIR, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}.jpg"
    with open(os.path.join(SHOTS_DIR, name), "wb") as f:
        f.write(jpeg)
    events.emit("screenshot", file=name, what=label)
    for old in sorted(os.listdir(SHOTS_DIR))[:-300]:
        os.remove(os.path.join(SHOTS_DIR, old))


def ydotoold_running():
    return subprocess.run(["pgrep", "-f", f"ydotoold -p {YDOTOOL_SOCKET}"], capture_output=True).returncode == 0


def flatten_virtual_mouse():
    """Mouse acceleration makes relative moves overshoot; turn it off for ydotool's device only."""
    tree = run("busctl", "--user", "tree", "org.kde.KWin")
    for dev in re.findall(r"/org/kde/KWin/InputDevice/event\d+", tree):
        if "ydotoold" in run("busctl", "--user", "get-property", "org.kde.KWin", dev, "org.kde.KWin.InputDevice", "name"):
            run("busctl", "--user", "set-property", "org.kde.KWin", dev, "org.kde.KWin.InputDevice",
                "pointerAccelerationProfileFlat", "b", "true")
            run("busctl", "--user", "set-property", "org.kde.KWin", dev, "org.kde.KWin.InputDevice",
                "pointerAcceleration", "d", "0")


def ensure_ydotoold():
    if ydotoold_running() and os.path.exists(YDOTOOL_SOCKET):
        return
    if os.path.exists(YDOTOOL_SOCKET):
        os.remove(YDOTOOL_SOCKET)
    subprocess.Popen(["ydotoold", "-p", YDOTOOL_SOCKET, "-P", "0600"], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(30):
        if os.path.exists(YDOTOOL_SOCKET):
            break
        time.sleep(0.1)
    time.sleep(0.3)
    flatten_virtual_mouse()


_flattened = False


def ydotool(*args):
    ensure_ydotoold()
    return run("ydotool", *args, env={**os.environ, "YDOTOOL_SOCKET": YDOTOOL_SOCKET})


async def move_to(x, y):
    """Relative moves corrected against KWin's real cursor position until it's spot on."""
    global _flattened
    if config.WINDOWS:
        winctl.move(x, y)
        c = await kwin.cursor()
        return abs(c["x"] - x) <= 1 and abs(c["y"] - y) <= 1
    if not _flattened:                   # at login KWin may not have been ready the first time
        flatten_virtual_mouse()
        _flattened = True
    for _ in range(6):
        c = await kwin.cursor()
        dx, dy = round(x - c["x"]), round(y - c["y"])
        if abs(dx) <= 1 and abs(dy) <= 1:
            return True
        ydotool("mousemove", "-x", str(dx), "-y", str(dy))
        await asyncio.sleep(0.03)
    return False


def to_desktop(x, y):
    return LAST_SHOT["x"] + x * LAST_SHOT["scale"], LAST_SHOT["y"] + y * LAST_SHOT["scale"]


BUTTONS = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}


@tool("click_at", "Click at a point in the most recent screenshot image (pixel coordinates in that image). "
      "button: left, right or middle. clicks: 1 or 2 (double-click).", {"x": int, "y": int, "button": str, "clicks": int})
async def click_at(args):
    refusal = await focus_changed(check_age=True)
    if refusal:
        return refusal
    x, y = to_desktop(args["x"], args["y"])
    if not await move_to(x, y):
        return text("Couldn't get the mouse to that spot.")
    button = BUTTONS.get(args.get("button", "left"), "0xC0")
    clicks = max(1, min(3, args.get("clicks", 1)))
    if config.WINDOWS:
        winctl.click(args.get("button", "left"), clicks)
    else:
        ydotool("click", "--repeat", str(clicks), "--next-delay", "80", button)
    await asyncio.sleep(0.15)
    await seen_window()                       # a click may bring another window forward: that one is Jarvis's doing
    return text(f"Clicked {args.get('button', 'left')} x{clicks} at desktop {round(x)},{round(y)}.")


@tool("move_mouse", "Move the mouse to a point in the most recent screenshot image (e.g. to hover).", {"x": int, "y": int})
async def move_mouse(args):
    ok = await move_to(*to_desktop(args["x"], args["y"]))
    return text("Moved." if ok else "Couldn't get the mouse to that spot.")


@tool("scroll", "Scroll the mouse wheel over a point in the most recent screenshot image. direction: up or down. "
      "amount: notches, 1-20.", {"x": int, "y": int, "direction": str, "amount": int})
async def scroll(args):
    await move_to(*to_desktop(args["x"], args["y"]))
    n = max(1, min(20, args.get("amount", 3)))
    if config.WINDOWS:
        winctl.wheel(-n if args["direction"] == "down" else n)
        return text(f"Scrolled {args['direction']} {n}.")
    ydotool("mousemove", "--wheel", "-x", "0", "-y", str(-n if args["direction"] == "down" else n))
    return text(f"Scrolled {args['direction']} {n}.")


@tool("type_text", "Type text into whatever window has focus, as if on the keyboard. Newlines are NOT allowed; "
      "use press_keys 'enter' separately.", {"text": str})
async def type_text(args):
    refusal = await focus_changed()
    if refusal:
        return refusal
    t = args["text"].replace("\n", " ")
    if config.WINDOWS:
        winctl.type_text(t)
    else:
        ydotool("type", "--key-delay", "4", "--", t)
    return text("Typed.")


@tool("press_keys", "Press a key combination in the focused window, e.g. 'ctrl+t', 'alt+tab', 'enter', "
      "'super', 'ctrl+shift+esc'.", {"keys": str})
async def press_keys(args):
    names = [k.strip().lower() for k in args["keys"].split("+") if k.strip()]
    missing = [n for n in names if n not in KEYCODES]
    if missing:
        return text(f"Unknown key(s): {', '.join(missing)}.")
    refusal = await focus_changed()
    if refusal:
        return refusal
    if config.WINDOWS:
        winctl.press(names)
        return text(f"Pressed {args['keys']}.")
    codes = [KEYCODES[n] for n in names]
    seq = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
    ydotool("key", *seq)
    return text(f"Pressed {args['keys']}.")


OVERLAY = {"proc": None}
SHAPE = {"type": "object", "required": ["type", "x", "y"], "properties": {
    "type": {"type": "string", "enum": ["ring", "arrow", "rect"]},
    "x": {"type": "number"}, "y": {"type": "number"}, "radius": {"type": "number"},
    "from_x": {"type": "number"}, "from_y": {"type": "number"}, "w": {"type": "number"}, "h": {"type": "number"},
    "label": {"type": "string"}, "step": {"type": "integer"}}}


def shapes_to_desktop(shapes):
    """Screenshot-image coordinates -> desktop pixels, the same mapping click_at uses; sizes scale with it."""
    out = []
    for s in shapes:
        s = dict(s)
        for kx, ky in (("x", "y"), ("from_x", "from_y")):
            if kx in s and ky in s:
                s[kx], s[ky] = to_desktop(float(s[kx]), float(s[ky]))
        for k in ("radius", "w", "h"):
            if k in s:
                s[k] = float(s[k]) * LAST_SHOT["scale"]
        out.append(s)
    return out


def stop_overlay():
    if OVERLAY["proc"] and OVERLAY["proc"].poll() is None:
        OVERLAY["proc"].terminate()


@tool("annotate", "Draw on the user's screen to SHOW them where something is (bright yellow, click-through, clears itself). "
      "Coordinates are pixels in the most recent screenshot image, exactly like click_at. Each shape: "
      "type 'ring' (x, y = centre, radius), 'arrow' (from_x, from_y -> x, y; the head is at x, y, the target) or "
      "'rect' (x, y = top-left, w, h); optional label (2-4 words; add '\n' and a short detail line if useful) and step (1, 2, 3 for a sequence). "
      "duration: seconds before it clears, default 8. A new annotate replaces the old drawing.",
      {"type": "object", "required": ["shapes"], "properties": {
          "shapes": {"type": "array", "items": SHAPE}, "duration": {"type": "number"}}})
async def annotate(args):
    shapes = args.get("shapes") or []
    if not shapes:
        return text("Nothing to draw.")
    try:
        desktop = shapes_to_desktop(shapes)
    except (KeyError, TypeError, ValueError) as e:
        return text(f"Bad shape: {e}.")
    duration = max(1.0, min(120.0, float(args.get("duration") or 8)))
    stop_overlay()
    log = open(os.path.join(config.JARVIS_DIR, "logs", "overlay.log"), "w")
    OVERLAY["proc"] = subprocess.Popen([sys.executable if config.WINDOWS else "/usr/bin/python3",
                                        os.path.join(config.JARVIS_DIR, "overlay.py"),
                                        json.dumps({"shapes": desktop, "duration": duration})],
                                       stdout=log, stderr=log, start_new_session=True,
                                       creationflags=winctl.NO_WINDOW if config.WINDOWS else 0)
    log.close()
    await asyncio.sleep(0.6)                 # a bad shape or a broken Qt shows up as an instant exit
    if OVERLAY["proc"].poll() not in (None, 0):
        with open(os.path.join(config.JARVIS_DIR, "logs", "overlay.log")) as f:
            return text("The overlay failed: " + f.read()[-400:])
    return text(f"Drawing {len(shapes)} shape(s) on screen for {duration:g}s.")


@tool("clear_annotations", "Remove whatever annotate drew on the screen, right now.", {})
async def clear_annotations(args):
    stop_overlay()
    return text("Cleared.")


@tool("notify", "Show a desktop notification (for things worth seeing, like a link or a number to copy).",
      {"title": str, "body": str})
async def notify(args):
    if config.WINDOWS:
        winctl.notify(args["title"], args["body"])
        return text("Shown.")
    run("notify-send", "-a", "Jarvis", args["title"], args["body"])
    return text("Shown.")


TOOLS = [open_app, list_apps, open_path, media, volume, list_windows, focus_window, screenshot, click_at,
         move_mouse, scroll, type_text, press_keys, annotate, clear_annotations, notify]


def server(extra=()):
    return create_sdk_mcp_server(name="jarvis", version="1.0.0", tools=TOOLS + list(extra))


def cleanup():
    stop_overlay()
    if os.path.exists(YDOTOOL_SOCKET) and shutil.which("pkill"):
        subprocess.run(["pkill", "-f", f"ydotoold -p {YDOTOOL_SOCKET}"])
