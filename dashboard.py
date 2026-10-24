"""The dashboard web server: http://127.0.0.1:8765 (this PC only).

GET  /                 the page
GET  /api/snapshot     everything about right now, plus recent history
GET  /api/events       live stream (server-sent events)
POST /api/cmd          listen / stop / say / yes / no (needs the page's token)
"""
import asyncio
import json
import os
import re
import secrets
import time

from aiohttp import web

import config
import events

PORT = 8765
TOKEN = secrets.token_urlsafe(24)
SHOTS_DIR = os.path.join(config.JARVIS_DIR, "logs", "shots")
PAGE = os.path.join(config.JARVIS_DIR, "dashboard.html")
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
CLK = 100 if config.WINDOWS else os.sysconf("SC_CLK_TCK")


# ---------- processes in the jarvis service ----------

def _boot_time():
    if config.WINDOWS:
        return 0
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("btime"):
                return int(line.split()[1])
    return 0


BOOT = _boot_time()
_cpu_prev = {}


def _cgroup_pids():
    with open("/proc/self/cgroup") as f:
        cg = f.read().strip().split("::", 1)[-1]
    try:
        with open(f"/sys/fs/cgroup{cg}/cgroup.procs") as f:
            return [int(p) for p in f.read().split()]
    except OSError:
        return [os.getpid()]


def _describe(cmd):
    """Turn a raw command line into something a person can read."""
    joined = " ".join(cmd)
    if "jarvis.py" in joined:
        return "Jarvis (ears, voice, safety gate, dashboard)", "core"
    if "_bundled" in joined and "claude" in joined or re.search(r"(^|[/\\])claude(\.exe)?( |$)", joined, re.I):
        return "Claude session (the brain)", "core"
    if cmd and cmd[0].endswith("ydotoold"):
        return "Virtual keyboard and mouse", "core"
    if cmd and os.path.basename(cmd[0]) == "rg":
        return "Claude indexing your files (for file search)", "core"
    m = re.search(r"eval '(.*)' < /dev/null", joined, re.S)
    if m:
        return m.group(1).replace("'\\''", "'"), "job"
    return joined[:300], "other"


def _win_processes():
    """Jarvis and everything it started, from psutil (Windows has no /proc or cgroups)."""
    import psutil
    me, out = psutil.Process(), []
    for p in [me] + me.children(recursive=True):
        p = _cpu_prev.setdefault(p.pid, p)       # the same object each time, so cpu_percent has a baseline
        try:
            with p.oneshot():
                cmd, started, mem = p.cmdline(), p.create_time(), p.memory_info().rss / 1e6
                cpu = p.cpu_percent(None)        # since the last call for this process, like the /proc version
        except psutil.Error:
            continue
        if not cmd or p.name().lower() == "conhost.exe":
            continue
        label, role = _describe(cmd)
        out.append({"pid": p.pid, "label": label, "role": role, "started": started, "cpu": round(cpu, 1),
                    "mem_mb": round(mem), "ends": None})
    return out


def processes():
    if config.WINDOWS:
        return _win_processes()
    out, now = [], time.time()
    for pid in _cgroup_pids():
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = [c.decode(errors="replace") for c in f.read().split(b"\0") if c]
            with open(f"/proc/{pid}/stat") as f:
                st = f.read().rsplit(")", 1)[1].split()
            with open(f"/proc/{pid}/statm") as f:
                rss_mb = int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
        except (OSError, IndexError):
            continue
        if not cmd:
            continue
        ticks = int(st[11]) + int(st[12])
        started = BOOT + int(st[19]) / CLK
        prev = _cpu_prev.get(pid)
        cpu = (ticks - prev[0]) / CLK / (now - prev[1]) * 100 if prev and now > prev[1] else 0.0
        _cpu_prev[pid] = (ticks, now)
        label, role = _describe(cmd)
        # hide the helper shells that only wrap a job we already show
        if role == "other" and cmd[0] in ("sleep", "/usr/bin/sleep"):
            continue
        sleep = re.match(r"\s*sleep (\d+)", label) if role == "job" else None
        out.append({"pid": pid, "label": label, "role": role, "started": started, "cpu": round(cpu, 1),
                    "mem_mb": round(rss_mb), "ends": started + int(sleep.group(1)) if sleep else None})
    return out


# ---------- web ----------

def _check(request):
    if request.host not in ALLOWED_HOSTS:
        raise web.HTTPForbidden(text="bad host")


async def page(request):
    _check(request)
    with open(PAGE, encoding="utf-8") as f:
        html = f.read().replace("__TOKEN__", TOKEN)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


async def snapshot(request):
    _check(request)
    j = request.app["jarvis"]
    if request.query.get("lite"):
        return web.json_response(j.snapshot(), dumps=lambda o: json.dumps(o, default=str))
    return web.json_response(j.snapshot() | {"history": list(events.history)[-1500:], "procs": processes()},
                             dumps=lambda o: json.dumps(o, default=str))


async def stream(request):
    _check(request)
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"})
    await resp.prepare(request)
    q = events.subscribe()
    try:
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), 15)
                await resp.write(f"data: {json.dumps(ev, default=str)}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        events.unsubscribe(q)
    return resp


async def live(request):
    _check(request)
    return web.json_response(request.app["jarvis"].live_view())


async def command(request):
    _check(request)
    if request.headers.get("X-Jarvis-Token") != TOKEN:
        raise web.HTTPForbidden(text="bad token")
    body = await request.json()
    reply = await request.app["jarvis"].command(body.get("cmd"), body.get("text", ""))
    return web.json_response({"reply": reply})


async def shot(request):
    _check(request)
    name = os.path.basename(request.match_info["name"])
    path = os.path.join(SHOTS_DIR, name)
    if not os.path.exists(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def ticker(jarvis):
    """Live-only updates, only while a dashboard tab is open."""
    n = 0
    while True:
        await asyncio.sleep(0.25)
        if not events.watchers():
            continue
        events.emit("meter", **jarvis.meter())
        n += 1
        if n % 8 == 0:
            events.emit("procs", procs=processes())


async def start(jarvis):
    os.makedirs(SHOTS_DIR, exist_ok=True)
    app = web.Application()
    app["jarvis"] = jarvis
    app.router.add_get("/", page)
    app.router.add_get("/api/snapshot", snapshot)
    app.router.add_get("/api/events", stream)
    app.router.add_get("/api/live", live)
    app.router.add_post("/api/cmd", command)
    app.router.add_get("/shots/{name}", shot)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    asyncio.create_task(ticker(jarvis))
    return runner
