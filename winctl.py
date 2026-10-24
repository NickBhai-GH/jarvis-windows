"""Windows stand-in for kwin.py + ydotool: windows, focus, cursor, monitors, mouse, keyboard, media, volume,
toasts and Start-menu apps. Same async window API as kwin.py, so pctools/brain/jarvis use it as `kwin`."""
import ctypes
import json
import os
import subprocess
import time
from ctypes import wintypes as wt

import psutil
from pynput.keyboard import Controller as _Keys, Key, KeyCode
from pynput.mouse import Button, Controller as _Mouse

u32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # per-monitor v2: every coordinate is a real pixel
NO_WINDOW = 0x08000000                                   # CREATE_NO_WINDOW for helper processes
_mouse, _keys = _Mouse(), _Keys()


# ---------- windows (same shape as kwin.py) ----------

def _rect(hwnd):
    r = wt.RECT()
    if dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)):   # 9: frame without invisible borders
        u32.GetWindowRect(hwnd, ctypes.byref(r))
    return r


def _cloaked(hwnd):
    v = ctypes.c_int(0)
    dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(v), ctypes.sizeof(v))       # 14: DWMWA_CLOAKED
    return v.value != 0


def _app(hwnd):
    pid = wt.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        return os.path.splitext(psutil.Process(pid.value).name())[0]
    except psutil.Error:
        return "?"


def _title(hwnd):
    n = u32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    u32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _info(hwnd, active):
    r = _rect(hwnd)
    return {"id": str(hwnd), "title": _title(hwnd), "app": _app(hwnd), "x": r.left, "y": r.top,
            "w": r.right - r.left, "h": r.bottom - r.top, "active": hwnd == active, "minimized": bool(u32.IsIconic(hwnd))}


def _normal(hwnd):
    """A window you'd see in Alt+Tab: visible, titled, not a tool window, not owned, not a hidden UWP frame."""
    return (u32.IsWindowVisible(hwnd) and u32.GetWindowTextLengthW(hwnd) and not u32.GetWindow(hwnd, 4)
            and not u32.GetWindowLongW(hwnd, -20) & 0x80 and not _cloaked(hwnd))


def _hwnds():
    out = []
    cb = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(lambda h, _: out.append(h) or True)
    u32.EnumWindows(cb, 0)
    return [h for h in out if _normal(h)]


async def start():
    pass


async def windows():
    active = u32.GetForegroundWindow()
    return [_info(h, active) for h in _hwnds()]


async def active_window():
    h = u32.GetForegroundWindow()
    return _info(h, h) if h else None


async def focus(query):
    q, ws = query.lower(), await windows()
    hit = (next((w for w in ws if w["app"].lower() == q), None) or next((w for w in ws if q in w["title"].lower()), None)
           or next((w for w in ws if q in w["app"].lower()), None))
    if not hit:
        return None
    h = int(hit["id"])
    if u32.IsIconic(h):
        u32.ShowWindow(h, 9)                       # SW_RESTORE
    u32.keybd_event(0x12, 0, 0, 0)                 # a tap of Alt lets a background process set the foreground
    u32.keybd_event(0x12, 0, 2, 0)
    u32.SetForegroundWindow(h)
    time.sleep(0.05)
    return _info(h, u32.GetForegroundWindow())


async def cursor():
    p = wt.POINT()
    u32.GetCursorPos(ctypes.byref(p))
    return {"x": p.x, "y": p.y}


class _MONITORINFOEX(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD),
                ("szDevice", wt.WCHAR * 32)]


async def screens():
    out = []

    def each(hmon, _hdc, _rc, _):
        mi = _MONITORINFOEX(cbSize=ctypes.sizeof(_MONITORINFOEX))
        u32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r = mi.rcMonitor
        out.append({"name": mi.szDevice, "x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top})
        return True
    u32.EnumDisplayMonitors(None, None, ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT),
                                                           wt.LPARAM)(each), 0)
    return out


def virtual_origin():
    """Top-left of the whole desktop (negative when a monitor sits left of or above the main one)."""
    return u32.GetSystemMetrics(76), u32.GetSystemMetrics(77)


# ---------- mouse and keyboard ----------

KEYS = {"esc": Key.esc, "escape": Key.esc, "backspace": Key.backspace, "tab": Key.tab, "enter": Key.enter,
        "return": Key.enter, "ctrl": Key.ctrl, "control": Key.ctrl, "shift": Key.shift, "alt": Key.alt,
        "space": Key.space, "capslock": Key.caps_lock, "super": Key.cmd, "meta": Key.cmd, "win": Key.cmd,
        "home": Key.home, "up": Key.up, "pageup": Key.page_up, "left": Key.left, "right": Key.right, "end": Key.end,
        "down": Key.down, "pagedown": Key.page_down, "insert": Key.insert, "delete": Key.delete, "del": Key.delete,
        "print": Key.print_screen, "minus": KeyCode.from_vk(0xBD), "equal": KeyCode.from_vk(0xBB),
        "comma": KeyCode.from_vk(0xBC), "dot": KeyCode.from_vk(0xBE), "period": KeyCode.from_vk(0xBE),
        "slash": KeyCode.from_vk(0xBF), "semicolon": KeyCode.from_vk(0xBA), "grave": KeyCode.from_vk(0xC0)}
KEYS.update({c: KeyCode.from_vk(ord(c.upper())) for c in "abcdefghijklmnopqrstuvwxyz0123456789"})   # real keys, so ctrl+t works
KEYS.update({f"f{i}": getattr(Key, f"f{i}") for i in range(1, 13)})


def move(x, y):
    u32.SetCursorPos(round(x), round(y))           # absolute and exact: no acceleration to correct for


def click(button="left", clicks=1):
    _mouse.click({"right": Button.right, "middle": Button.middle}.get(button, Button.left), clicks)


def wheel(notches):
    _mouse.scroll(0, notches)                      # positive = up


def type_text(t):
    _keys.type(t)


def press(names):
    keys = [KEYS[n] for n in names]
    for k in keys:
        _keys.press(k)
    for k in reversed(keys):
        _keys.release(k)


# ---------- media, volume, notifications, apps ----------

MEDIA = {"play_pause": Key.media_play_pause, "play": Key.media_play_pause, "pause": Key.media_play_pause,
         "next": Key.media_next, "previous": Key.media_previous, "stop": KeyCode.from_vk(0xB2)}


def media(action):
    _keys.tap(MEDIA[action])


def _endpoint():
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().EndpointVolume


def volume(action, level=50):
    ep = _endpoint()
    now = ep.GetMasterVolumeLevelScalar()
    if action == "set":
        ep.SetMasterVolumeLevelScalar(max(0, min(100, level)) / 100, None)
    elif action in ("up", "down"):
        ep.SetMasterVolumeLevelScalar(max(0.0, min(1.0, now + (0.1 if action == "up" else -0.1))), None)
    elif action in ("mute", "unmute"):
        ep.SetMute(action == "mute", None)
    return f"Volume {round(ep.GetMasterVolumeLevelScalar() * 100)}%{' (muted)' if ep.GetMute() else ''}"


_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1)
$x = $t.GetElementsByTagName('text')
$x.Item(0).AppendChild($t.CreateTextNode($env:JT)) > $null
$x.Item(1).AppendChild($t.CreateTextNode($env:JB)) > $null
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""


def notify(title, body):
    """A Windows toast. Title and body go in through the environment, never into the script text."""
    subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", _TOAST], creationflags=NO_WINDOW,
                     env={**os.environ, "JT": title, "JB": body})


_apps = {"t": 0, "list": []}


def start_apps():
    """Everything in the Start menu: [{id (AppUserModelID), name, keywords, token}]. Cached for five minutes."""
    if time.time() - _apps["t"] > 300:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              "Get-StartApps | ConvertTo-Json -Compress"], capture_output=True, text=True,
                             timeout=20, creationflags=NO_WINDOW).stdout
        items = json.loads(out or "[]")
        _apps["list"] = [{"id": a["AppID"], "name": a["Name"], "keywords": "",
                          "token": os.path.splitext(os.path.basename(a["AppID"].split("!")[0]))[0]}
                         for a in (items if isinstance(items, list) else [items])]
        _apps["t"] = time.time()
    return _apps["list"]


def launch(app_id):
    subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + app_id], creationflags=NO_WINDOW)
