#!/usr/bin/python3
"""JARVIS OS: a live dashboard that fills a 1920x1080 monitor and sits under every normal window, like a wallpaper.

One Qt WebEngine window (system PySide6, no extra packages) shows index.html. Python fetches each feed on its own
timer in a thread and pushes the result into the page with feed(name, data). Every feed is READ-ONLY: the Slack,
Asana, Steam and Twitch helpers only ever issue GETs, and the optional Discord listener (off unless you add a user
token, see README) only receives gateway events (it never sends a message, reaction, typing, status or any REST write).
Credentials live in ~/.config/jarvis-os, one file each; any that is missing shows faded sample data instead.
"""
import asyncio
import datetime as dt
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.expanduser("~/.config/jarvis-os")          # credentials live here, chmod 600 (see README)
JARVIS = "http://127.0.0.1:8765"
SCHEMES = ("http", "https", "slack", "discord", "steam", "spotify")
STALE = 600                    # a failing feed keeps showing its last good data this long, then says unavailable
# A normal desktop-client user agent, so the gateway sees an ordinary session rather than anything odd.
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
DISCORD_EPOCH = 1420070400000   # Discord snowflake ids count milliseconds from 2015-01-01
WINDOWS = sys.platform == "win32"
# The PC's own time zone name (e.g. "Europe/Berlin"), from /etc/localtime; UTC if it can't be read.
if WINDOWS:                    # Windows names its zones differently ("India Standard Time"); tzlocal maps them
    from tzlocal import get_localzone_name
    LOCAL_TZ = get_localzone_name()
else:
    _lt = os.path.realpath("/etc/localtime")
    LOCAL_TZ = _lt.split("zoneinfo/", 1)[1] if "zoneinfo/" in _lt else "UTC"
SCHEDULE_TZ = ZoneInfo(LOCAL_TZ)   # work mode Mon-Fri WORK_HOURS in this zone, game mode the rest
WORK_HOURS = (9, 19)
MODE_TZ = {"game": LOCAL_TZ, "work": LOCAL_TZ}   # every time and "today" shown follows the mode; set either to any zone
shown_tz = SCHEDULE_TZ                           # MODE_TZ of the current mode, set by Hub.check_mode
WORK_FEEDS = ("asana", "slack")                            # not fetched in game mode (calendar is personal too)
log = logging.getLogger("jarvis-os")


def secret(name):
    try:
        with open(os.path.join(CONF, name)) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "jarvis-os", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def get_json(url, headers=None):
    return json.loads(get(url, headers))


def hm(ts):
    return dt.datetime.fromtimestamp(float(ts), shown_tz).strftime("%H:%M")


def dur(sec):
    m = int(sec // 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


# ---------------------------------------------------------------- Work / game mode

def scheduled_mode(t):
    if WINDOWS:                # the Windows install is the games side: work lives on Linux
        return "game"
    d = dt.datetime.fromtimestamp(t, SCHEDULE_TZ)
    return "work" if d.weekday() < 5 and WORK_HOURS[0] <= d.hour < WORK_HOURS[1] else "game"


def next_switch(t):
    """The next scheduled switch after t: the start or end of WORK_HOURS on a weekday."""
    d = dt.datetime.fromtimestamp(t, SCHEDULE_TZ)
    return min(b for i in range(8) for h in WORK_HOURS
               if (b := dt.datetime.combine(d.date() + dt.timedelta(i), dt.time(h), SCHEDULE_TZ)) > d and b.weekday() < 5).timestamp()


def mode_now(text, now):
    """The `mode` file ("game 1759480000", written by ./mode) -> "work" or "game". A manual game or work holds until
    the next scheduled switch; auto, no file or anything else follows the schedule."""
    want, _, at = (text or "").partition(" ")
    if want in ("work", "game") and at.isdigit() and now < next_switch(int(at)):
        return want
    return scheduled_mode(now)


# ---------------------------------------------------------------- Asana (my tasks)

STATUS_ORDER = ["Triage", "In progress", "Blocked"]  # a "Status" custom field's options, if your tasks have one


def day_label(d, today):
    gap = (d - today).days
    return "Today" if gap == 0 else "Tomorrow" if gap == 1 else d.strftime("%A") if gap < 7 else f"{d:%a} {d.day} {d:%b}"


def shape_tasks(tasks, today):
    """Asana tasks (incomplete + completed since today) -> the day ring counts and the TODAY groups: Overdue, Today,
    Tomorrow, the weekday names, dates, then No due date; inside a group by status (STATUS_ORDER), then name."""
    done, keyed = 0, []
    for t in tasks:
        if t.get("completed"):
            at = t.get("completed_at")
            done += bool(at) and dt.datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(shown_tz).date() == today
            continue
        cf = {c.get("name"): c.get("display_value") for c in t.get("custom_fields") or [] if c.get("display_value")}
        m = re.match(r"\s*\[#?(\d+)\]\s*", t["name"])               # an optional "[#123]" reference at the front
        project = next((p["name"] for p in t.get("projects") or []), "")
        due = t.get("due_on") and dt.date.fromisoformat(t["due_on"])
        status = (cf.get("Status") or "").capitalize()
        group = "No due date" if not due else "Overdue" if due < today else day_label(due, today)
        key = (due or dt.date.max, STATUS_ORDER.index(status) if status in STATUS_ORDER else 9, t["name"])
        keyed.append((key, {"group": group, "id": f"#{m.group(1)}" if m else "", "client": project,
                            "task": t["name"][m.end():] if m else t["name"], "status": status,
                            "due": "" if not due else "Due today" if due == today else f"Due {due.day} {due:%b}",
                            "late": bool(due and due < today), "href": t.get("permalink_url", "")}))
    keyed.sort(key=lambda kr: kr[0])
    groups = []
    for _, r in keyed:
        if not groups or groups[-1]["label"] != r["group"]:
            groups.append({"label": r["group"], "tasks": []})
        groups[-1]["tasks"].append(r)
    count = lambda label: sum(len(g["tasks"]) for g in groups if g["label"] == label)
    return {"done": done, "overdue": count("Overdue"), "open": count("Today"), "groups": groups}


_asana_home = None


def asana():
    """Your Asana tasks, read with a personal access token (asana-token); GET only. The workspace is asana-workspace
    (its gid) or, without that file, your first workspace."""
    global _asana_home
    tok = secret("asana-token")
    if not tok:
        return SAMPLE["asana"]
    run = lambda q: get_json("https://app.asana.com/api/1.0" + q, {"Authorization": "Bearer " + tok})
    ws = secret("asana-workspace") or run("/users/me?opt_fields=workspaces")["data"]["workspaces"][0]["gid"]
    today = dt.datetime.now(shown_tz).date()
    since = urllib.parse.quote(dt.datetime.combine(today, dt.time(), shown_tz).isoformat())
    fields = "name,due_on,completed,completed_at,permalink_url,custom_fields.name,custom_fields.display_value,projects.name"
    tasks, offset = [], ""
    while True:
        d = run(f"/tasks?assignee=me&workspace={ws}&completed_since={since}&limit=100&opt_fields={fields}{offset}")
        tasks += d["data"]
        nxt = (d.get("next_page") or {}).get("offset")
        if not nxt:
            break
        offset = "&offset=" + nxt
    if not _asana_home:
        _asana_home = f"https://app.asana.com/0/{run(f'/users/me/user_task_list?workspace={ws}')['data']['gid']}/list"
    return shape_tasks(tasks, today) | {"home": _asana_home}


# ---------------------------------------------------------------- Calendar (iCal secret address)

def _ics_time(prop, val, tz):
    """Aware datetime in the event's own zone (so a weekly London call keeps its London time across DST)."""
    params = dict(p.split("=", 1) for p in prop.split(";")[1:] if "=" in p)
    if len(val) == 8:                                                       # all-day
        return None
    t = dt.datetime.strptime(val.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    return t.replace(tzinfo=dt.timezone.utc if val.endswith("Z") else ZoneInfo(params["TZID"]) if "TZID" in params else tz)


def _recurs_on(start, rule, day, exdates):
    """Daily/weekly RRULE check for one day. ponytail: ignores COUNT, MONTHLY/YEARLY and BYSETPOS; good enough for call
    series, upgrade to python-dateutil's rrulestr if a monthly call goes missing."""
    r = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    if day < start.date() or day in exdates or r.get("FREQ") not in ("DAILY", "WEEKLY"):
        return False
    if "UNTIL" in r and day > dt.date(int(r["UNTIL"][:4]), int(r["UNTIL"][4:6]), int(r["UNTIL"][6:8])):
        return False
    step = int(r.get("INTERVAL", 1))
    if r["FREQ"] == "DAILY":
        return (day - start.date()).days % step == 0
    days = r.get("BYDAY", "MO,TU,WE,TH,FR,SA,SU"[start.weekday() * 3:start.weekday() * 3 + 2]).split(",")
    weeks = ((day - day.weekday() * dt.timedelta(1)) - (start.date() - start.weekday() * dt.timedelta(1))).days // 7
    return "MO,TU,WE,TH,FR,SA,SU".split(",")[day.weekday()] in [d[-2:] for d in days] and weeks % step == 0


def ics_today(text, day, tz=None):
    """Today's timed events from an iCal feed, sorted: [{start, end, title, href}] with epoch seconds."""
    tz = tz or dt.datetime.now().astimezone().tzinfo
    text = re.sub(r"\r?\n[ \t]", "", text)
    events, moved = [], set()
    for block in re.findall(r"BEGIN:VEVENT\r?\n(.*?)END:VEVENT", text, re.S):
        ev, ex = {}, set()
        for line in block.splitlines():
            if ":" in line:
                prop, val = line.split(":", 1)
                ev.setdefault(prop.split(";")[0], (prop, val))
                if prop.startswith("EXDATE"):
                    ex |= {t.date() for x in val.split(",") if (t := _ics_time(prop, x, tz))}
        if "DTSTART" not in ev or ev.get("STATUS", ("", ""))[1] == "CANCELLED":
            continue
        start = _ics_time(*ev["DTSTART"], tz)
        if not start:
            continue
        end = _ics_time(*ev["DTEND"], tz) if "DTEND" in ev else start + dt.timedelta(minutes=30)
        uid = ev.get("UID", ("", ""))[1]
        if "RECURRENCE-ID" in ev and (orig := _ics_time(*ev["RECURRENCE-ID"], tz)):
            moved.add((uid, orig.date()))                                   # a moved/edited instance of a series
        series = None
        if "RRULE" in ev:
            if not _recurs_on(start, ev["RRULE"][1], day, ex):
                continue
            # ponytail: takes `day` in the event's own zone; a series whose local day differs from yours can slip a day
            moved_start = dt.datetime.combine(day, start.time(), start.tzinfo)
            start, end, series = moved_start, moved_start + (end - start), uid
        start, end = start.astimezone(tz), end.astimezone(tz)
        if start.date() != day:
            continue
        blob = " ".join(v for _, v in (ev.get(k, ("", "")) for k in ("LOCATION", "DESCRIPTION", "X-GOOGLE-CONFERENCE")))
        link = re.search(r"https://(?:meet\.google\.com|[\w.]*zoom\.us|teams\.microsoft\.com)/[^\s\\,<>\"]+", blob)
        title = ev.get("SUMMARY", ("", "Busy"))[1].replace("\\,", ",").replace("\\;", ";").replace("\\n", " ")
        events.append({"start": start.timestamp(), "end": end.timestamp(), "title": title, "series": series,
                       "href": link.group(0) if link else "https://calendar.google.com/calendar/r/day"})
    events = [e for e in events if not ((s := e.pop("series")) and (s, day) in moved)]
    return sorted(events, key=lambda e: e["start"])


def calendar():
    url = secret("calendar-ics-url")
    if not url:
        return {"linked": False, "events": []}
    return {"linked": True, "events": ics_today(get(url), dt.datetime.now(shown_tz).date(), shown_tz)}


# ---------------------------------------------------------------- Slack (user token, read scopes only)

SLACK_READS = {"auth.test", "users.conversations", "conversations.info", "conversations.history", "users.info"}
_slack_users = {}


def slack_text(s, users):
    s = re.sub(r"<@(\w+)(?:\|[^>]*)?>", lambda m: "@" + users(m.group(1)), s)
    s = re.sub(r"<#\w+\|([^>]*)>", r"#\1", s)
    s = re.sub(r"<!(here|channel|everyone)[^>]*>", r"@\1", s)
    s = re.sub(r"<([^|>]+)\|([^>]+)>", r"\2", s)
    s = re.sub(r"<([^>]+)>", r"\1", s)
    return " ".join(s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").split())


def slack():
    tok = secret("slack-token")
    if not tok:
        return SAMPLE["slack"]

    def api(method, **q):
        assert method in SLACK_READS, method                                # read-only, always
        d = get_json(f"https://slack.com/api/{method}?{urllib.parse.urlencode(q)}", {"Authorization": "Bearer " + tok})
        if not d.get("ok"):
            raise RuntimeError(f"slack {method}: {d.get('error')}")
        return d

    def name(uid):
        if uid not in _slack_users:
            u = api("users.info", user=uid)["user"]
            _slack_users[uid] = u.get("profile", {}).get("display_name") or u.get("real_name") or u.get("name", uid)
        return _slack_users[uid]

    me = api("auth.test")
    team, uid = me["team_id"], me["user_id"]
    convs, cursor = [], ""
    while True:
        d = api("users.conversations", types="public_channel,private_channel,mpim,im", exclude_archived="true",
                limit=200, cursor=cursor)
        convs += d["channels"]
        cursor = d.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    # ponytail: two calls per conversation every round (fine for ~100 conversations at a 3 min cadence);
    # if Slack starts answering 429, raise the interval or only re-check conversations whose `latest` moved.
    # DMs and group DMs (external ones too), plus only the internal channels named in slack-channels
    keep = set(secret("slack-channels").split())
    convs = [c for c in convs if c.get("is_im") or c.get("is_mpim")
             or (c.get("name") in keep and not (c.get("is_ext_shared") or c.get("is_shared")))]
    rows, unread = [], 0
    for c in convs:
        if c.get("is_user_deleted"):
            continue
        try:
            info = api("conversations.info", channel=c["id"])["channel"]
        except RuntimeError:                              # e.g. channel_not_found on some Slack Connect DMs: skip it
            continue
        dm = bool(c.get("is_im") or c.get("is_mpim"))
        last_read = float(info.get("last_read") or 0)
        if not dm and not last_read:                      # never-opened channel; "0000000000.000000" is rejected as oldest
            continue
        q = {"limit": 1} if dm else {"oldest": info["last_read"], "limit": 10}   # DMs: latest message, read or not
        msgs = [m for m in api("conversations.history", channel=c["id"], **q)["messages"]
                if m.get("subtype") in (None, "bot_message", "thread_broadcast") and (dm or m.get("user") != uid)]
        if not msgs:
            continue
        m = msgs[0]
        new = float(m["ts"]) > last_read and m.get("user") != uid
        if new:
            unread += info.get("unread_count_display") or (1 if dm else len(msgs))
        mention = f"<@{uid}>" in m.get("text", "")
        where = ("with " + name(c["user"])) if c.get("is_im") else \
            ("with " + ", ".join(x for x in c.get("name", "").removeprefix("mpdm-").rsplit("-", 1)[0].split("--")
                                 if x != me.get("user"))) if c.get("is_mpim") \
            else ("mentioned you in #" if mention else "#") + c.get("name", "")
        who = "You" if m.get("user") == uid else name(m["user"]) if m.get("user") else m.get("username", "Bot")
        rows.append({"who": who, "where": where, "text": slack_text(m.get("text", ""), name), "time": hm(m["ts"]),
                     "ts": float(m["ts"]), "dm": dm, "wait": bool(new and (dm or mention)),
                     "href": f"slack://channel?team={team}&id={c['id']}"})
    rows.sort(key=lambda r: -r["ts"])
    rows = [r for r in rows if r["dm"]][:3] + [r for r in rows if not r["dm"]]   # newest 3 DMs, then channel unreads
    return {"unread": unread, "waiting": sum(r["wait"] for r in rows), "items": rows[:8]}


# ---------------------------------------------------------------- Steam (Web API key)

STEAM_STATES = {1: "Online", 2: "Busy", 3: "Away", 4: "Snooze", 5: "Looking to trade", 6: "Looking to play"}
_steam_seen = {}                        # (steamid, what) -> first time we saw it; durations count from there


def steam_id():
    vdf = open(r"C:\Program Files (x86)\Steam\config\loginusers.vdf" if WINDOWS
               else os.path.expanduser("~/.steam/steam/config/loginusers.vdf"), encoding="utf-8").read()
    users = re.findall(r'"(7656\d{13})"\s*\{(.*?)\}', vdf, re.S)
    return next((i for i, body in users if re.search(r'"MostRecent"\s*"1"', body)), users[0][0])


def steam():
    key = secret("steam-key")
    if not key:
        return SAMPLE["steam"]
    api = "https://api.steampowered.com/ISteamUser"
    favs = secret("steam-favourites").split()                              # steamids pinned on top, shown even offline
    ids = [f["steamid"] for f in get_json(f"{api}/GetFriendList/v1/?key={key}&steamid={steam_id()}&relationship=friend")
           ["friendslist"]["friends"]]
    ids = favs + [i for i in ids if i not in favs]
    players = [p for n in range(0, len(ids), 100)                          # summaries take 100 ids per call
               for p in get_json(f"{api}/GetPlayerSummaries/v2/?key={key}&steamids={','.join(ids[n:n + 100])}")["response"]["players"]]
    now, rows = time.time(), []
    for p in players:
        fav = p["steamid"] in favs
        if not p.get("personastate") and not fav:
            continue
        what = p.get("gameextrainfo") or STEAM_STATES.get(p["personastate"], "Online" if p["personastate"] else "Offline")
        since = _steam_seen.setdefault((p["steamid"], what), now)
        rows.append({"name": p["personaname"], "game": what, "playing": bool(p.get("gameextrainfo")), "fav": fav,
                     "t": dur(now - since), "href": f"steam://friends/message/{p['steamid']}"})
    rows.sort(key=lambda r: (not r["fav"], not r["playing"], r["name"].lower()))
    return {"online": sum(r["game"] != "Offline" for r in rows), "friends": rows, "dms": None}


# ---------------------------------------------------------------- Twitch (followed channels that are live)

_twitch_user = {}


def twitch():
    env = dict(re.findall(r"^([A-Z_]+)=(.*)$", secret("twitch.env"), re.M))
    if "TWITCH_ACCESS_TOKEN" not in env:
        return SAMPLE["twitch"]
    tok = env["TWITCH_ACCESS_TOKEN"].removeprefix("oauth:")
    if tok not in _twitch_user:
        _twitch_user[tok] = get_json("https://id.twitch.tv/oauth2/validate", {"Authorization": "OAuth " + tok})["user_id"]
    h, uid = {"Authorization": "Bearer " + tok, "Client-Id": env["TWITCH_CLIENT_ID"]}, _twitch_user[tok]
    live = get_json(f"https://api.twitch.tv/helix/streams/followed?user_id={uid}&first=100", h)["data"]
    total = get_json(f"https://api.twitch.tv/helix/channels/followed?user_id={uid}&first=1", h)["total"]
    # extra channels to watch that the token's account doesn't follow, one login per line in twitch-extra
    seen = {s["user_login"] for s in live}
    extra = [n.lower() for n in secret("twitch-extra").split() if n.lower() not in seen][:100]
    if extra:
        live += get_json("https://api.twitch.tv/helix/streams?" + urllib.parse.urlencode([("user_login", n) for n in extra]), h)["data"]
    total += len(extra)
    return {"total": total, "items": [{"name": s["user_name"], "game": s["game_name"] or s["title"],
                                       "viewers": f"{s['viewer_count']:,}", "href": f"https://www.twitch.tv/{s['user_login']}"}
                                      for s in sorted(live, key=lambda s: -s["viewer_count"])]}


# ---------------------------------------------------------------- Jarvis workers

def workers():
    return shape_workers(get_json(JARVIS + "/api/snapshot"), time.time(), live_step)


PROJECTS = os.path.expanduser("~/.claude/projects/" + re.sub(r"[/\\:]", "-", os.path.realpath(os.path.expanduser("~"))))
_logs = {}


def session_log(name, started, session_id):
    """A worker's Claude Code session log (read only). Jarvis's events carry the session id only once a turn ends,
    so before that it is the log that began within 30 s after the worker started.
    ponytail: start-time match, two workers started in the same second could swap; upgrade is Jarvis putting the
    session id in /api/snapshot tasks."""
    if session_id:
        return os.path.join(PROJECTS, session_id + ".jsonl")
    if (name, started) not in _logs:
        best = None
        for f in os.scandir(PROJECTS):
            if f.name.endswith(".jsonl") and f.stat().st_mtime >= started:
                with open(f.path) as fh:
                    ts = json.loads(fh.readline() or "{}").get("timestamp")
                gap = ts and dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() - started
                if gap is not None and -2 < gap < 30 and (not best or gap < best[0]):
                    best = gap, f.path
        if not best:
            return None                        # not written yet; look again next time
        _logs[name, started] = best[1]
    return _logs[name, started]


def step_words(tool, data):
    base = os.path.basename(str(data.get("file_path") or data.get("notebook_path") or ""))
    act = data.get("action", "")
    words = {"Bash": data.get("description") or "Running a command", "Read": f"Reading {base}",
             "Edit": f"Editing {base}", "Write": f"Writing {base}", "NotebookEdit": f"Editing {base}",
             "Grep": "Searching the code", "Glob": "Looking for files", "WebSearch": "Searching the web",
             "WebFetch": "Reading a web page", "Agent": "Running a helper: " + data.get("description", ""),
             "Skill": f"Loading the {data.get('skill', '')} skill", "ToolSearch": "Loading tools",
             "screenshot": "Taking a screenshot", "click_at": "Clicking", "type_text": "Typing",
             "press_keys": "Pressing keys", "start_worker": "Starting a worker",
             "message_worker": "Messaging a worker"}.get(tool.rsplit("__", 1)[-1])
    if words is None and tool.endswith("__computer"):
        words = "Taking a screenshot" if act == "screenshot" else f"Using Chrome ({act.replace('_', ' ')})"
    if words is None:
        words = tool.rsplit("__", 1)[-1].replace("_", " ").replace("-", " ")
    return words[:1].upper() + words[1:]


def live_step(name, started, session_id):
    """From the end of the worker's session log: its latest tool call in plain words with when it started, and the
    latest thing it said."""
    try:
        path = session_log(name, started, session_id)
    except OSError:                            # no logs folder yet: the panel still shows the rest
        path = None
    if not path or not os.path.exists(path):
        return None, None, ""
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - 400_000))
        lines = f.read().decode(errors="replace").splitlines()[1:]     # the first may be cut
    step = at = said = None
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") != "assistant" or e.get("isSidechain"):
            continue
        for b in reversed(e["message"].get("content") or []):
            if step is None and b.get("type") == "tool_use":
                step, at = step_words(b["name"], b.get("input") or {}), e["timestamp"]
            elif said is None and b.get("type") == "text" and b["text"].strip():
                said = b["text"]
        if step and said:
            break
    return step, at and dt.datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp(), said or ""


def plain(s):
    return " ".join(re.sub(r"\*\*|`|^\s*(#+|[-*]|\d+\.)\s+", "", s or "", flags=re.M).split())


def tail_sentences(s, cut, n=230):
    """The last whole sentences of a message, up to about n characters. cut: the event kept only its tail."""
    parts = re.split(r"(?<=[.!?:])\s+", plain(s))
    if cut and len(parts) > 1:
        parts = parts[1:]                      # the first one was cut mid-word
    out = parts.pop() if parts else ""
    while parts and len(parts[-1]) + len(out) < n:
        out = parts.pop() + " " + out
    return out if len(out) <= n + 60 else "…" + out[-n:].lstrip()


WORKER_ASK = re.compile(r"\w+, the (.+) worker would like to (.+?)\.? Shall I allow it\?$")


def jarvis_step(e):
    """A tool_use event of Jarvis's own; its input is JSON cut at 1500 characters, so a long Edit may not parse."""
    try:
        data = json.loads(e.get("input") or "{}")
    except ValueError:
        data = dict(re.findall(r'"(file_path|description|action|skill)": "([^"]*)"', e["input"]))
    return step_words(e["name"], data)


def shape_workers(snap, now, step=lambda name, started, session_id: (None, None, "")):
    """Jarvis itself first while it is on a request, then only live workers (Jarvis's own task list), the ones
    waiting on you first, then oldest first.
    A permission prompt is the latest confirm_ask naming the worker; a turn that ended on a question has NEED USER:."""
    live = [(t["task_id"][7:], t) for t in snap.get("tasks") or [] if t.get("type") == "Worker"]
    last, asks, turn, mine, spoke = {}, {}, snap.get("turn"), (None, None), []
    for e in snap.get("history", []):
        if e.get("kind") == "worker":
            last[e["name"]] = e
        elif e.get("kind") == "confirm_ask":
            m = WORKER_ASK.match(e.get("question", ""))
            if m:
                asks[m[1]] = (e["ts"], m[2])
        if turn and e.get("ts", 0) >= turn["started"]:
            if e.get("kind") == "tool_use" and not e.get("sub"):
                mine = jarvis_step(e), e["ts"]
            elif e.get("kind") == "say":
                spoke.append(e["text"])
    ago = lambda at: dur(now - at) if at and now - at >= 60 else f"{max(0, int(now - at))}s" if at else ""
    rows = []
    for name, t in sorted(live, key=lambda nt: nt[1]["started"]):
        st, e = t["description"].rsplit("(", 1)[-1].rstrip(")"), last.get(name, {})
        now_step, at, said = step(name, t["started"], e.get("session_id"))
        text = said or e.get("text", "")
        doing, need, todo = text.split("NEED USER:", 1)[0], "", ""
        if st == "waiting":
            ask = asks.get(name)
            if ask and ask[0] >= e.get("ts", 0):
                need, todo = f"Wants your OK to {ask[1]}.", "Answer Jarvis yes or no."
            elif "NEED USER:" in text:
                need, todo = plain(text.split("NEED USER:", 1)[1]), "Tell Jarvis your answer."
            else:
                need, todo = "It stopped for you without saying why.", "Ask Jarvis what it needs."
        rows.append({"name": name[:1].upper() + name[1:], "state": st, "t": dur(now - t["started"]),
                     "step": now_step or "", "ago": ago(at),
                     "doing": tail_sentences(doing, not said and len(text) >= 500) or "Starting up.", "need": need, "todo": todo, "href": JARVIS + "/"})
    rows.sort(key=lambda r: r["state"] != "waiting")   # stable: oldest first within each group
    if turn:                                           # your own live request goes before every worker
        q = snap.get("question") or ""
        q = "" if WORKER_ASK.match(q) else q           # a worker's ask shows on that worker
        rows.insert(0, {"name": "Jarvis", "state": "waiting" if q else "running", "t": ago(turn["started"]),
                        "step": mine[0] or "", "ago": ago(mine[1]),
                        "doing": tail_sentences(" ".join(s for s in spoke if s != q), False) or "Thinking.",
                        "need": q, "todo": "Answer Jarvis yes or no." if q else "", "href": JARVIS + "/"})
    return {"running": sum(r["state"] != "waiting" for r in rows),
            "waiting": sum(r["state"] == "waiting" for r in rows), "items": rows}


# ---------------------------------------------------------------- System strip

_cpu = [0, 0]


def system():
    if WINDOWS:
        import psutil
        cpu = round(psutil.cpu_percent(None))          # since the last call, like the /proc version
    else:
        f = [int(x) for x in open("/proc/stat").readline().split()[1:8]]
        idle, total = f[3] + f[4], sum(f)
        d_idle, d_total = idle - _cpu[0], total - _cpu[1]
        _cpu[:] = idle, total
        cpu = round(100 * (1 - d_idle / d_total)) if d_total else 0
    try:
        gpu = int(subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=5,
                                 creationflags=0x08000000 if WINDOWS else 0).stdout.split()[0])
    except Exception:
        gpu = None
    return {"cpu": cpu, "gpu": gpu, "music": music()}


def music():
    if WINDOWS:
        return asyncio.run(win_music())
    names =subprocess.run(["busctl", "--user", "list", "--no-legend"], capture_output=True, text=True, timeout=5).stdout
    best = None
    for n in re.findall(r"^(org\.mpris\.MediaPlayer2\.\S+)", names, re.M):
        try:                                       # a busy player (Chrome) can hang the call; skip it this round
            out = subprocess.run(["busctl", "--user", "--json=short", "get-property", n, "/org/mpris/MediaPlayer2",
                                  "org.mpris.MediaPlayer2.Player", "PlaybackStatus", "Metadata", "Position"],
                                 capture_output=True, text=True, timeout=2).stdout.splitlines()
        except subprocess.TimeoutExpired:
            continue
        if len(out) < 3:
            continue
        status, meta, pos = (json.loads(x)["data"] for x in out)
        meta = {k: v["data"] for k, v in meta.items()}
        if not meta.get("xesam:title"):
            continue
        p = {"title": meta["xesam:title"], "artist": ", ".join(meta.get("xesam:artist") or []),
             "len": (meta.get("mpris:length") or 0) / 1e6, "pos": pos / 1e6, "playing": status == "Playing",
             "href": "spotify:" if "spotify" in n else ""}
        if not best or (p["playing"], "spotify" in n) > (best["playing"], False):
            best = p
    return best


async def win_music():
    """Windows' own media sessions (what the volume flyout shows): same shape as the MPRIS one, playing + Spotify first."""
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Sessions
    best = None
    for s in (await Sessions.request_async()).get_sessions():
        meta = await s.try_get_media_properties_async()
        if not meta or not meta.title:
            continue
        t, app = s.get_timeline_properties(), s.source_app_user_model_id.lower()
        playing = s.get_playback_info().playback_status == 4        # 4 = Playing
        pos = t.position.total_seconds()
        if playing:                # apps only report position now and then; add the time since that report
            pos += (dt.datetime.now(dt.timezone.utc) - t.last_updated_time).total_seconds()
        p = {"title": meta.title, "artist": meta.artist or "", "len": t.end_time.total_seconds(),
             "pos": min(pos, t.end_time.total_seconds()), "playing": playing,
             "href": "spotify:" if "spotify" in app else ""}
        if not best or (p["playing"], "spotify" in app) > (best["playing"], False):
            best = p
    return best


def weather():
    """wttr.in for the place in weather-location (a city, or "lat,lon"); without it, wttr.in guesses from your IP.
    Only the temperature and conditions are shown, never the place name."""
    d = get_json(f"https://wttr.in/{urllib.parse.quote(secret('weather-location'))}?format=j1")
    c = d["current_condition"][0]
    return {"temp": c["temp_C"], "desc": c["weatherDesc"][0]["value"].strip(), "high": d["weather"][0]["maxtempC"]}


# ---------------------------------------------------------------- Sample data (shown only while a credential is missing)

def _sample():
    c = lambda who, where, text, time, wait: {"who": who, "where": where, "text": text, "time": time, "wait": wait,
                                              "href": "slack://open"}
    t = lambda group, id, client, task, status, due, late=False: {"group": group, "id": id, "client": client, "task": task,
                                                                  "status": status, "due": due, "late": late, "href": ""}
    return {
        "slack": {"sample": True, "unread": 9, "waiting": 2, "items": [
            c("Sam Rivera", "Direct message", "Can you look at the release notes before the call? Two of the items look swapped.", "13:41", True),
            c("Jordan Lee", "mentioned you in #website", "@you the contact form is returning a 403 since this morning's deploy.", "13:22", True),
            c("Alex Kim", "#general", "Lunch order goes in at 12:30, add yours to the thread.", "12:40", False),
            c("Morgan Diaz", "#design", "The new icons are in the shared folder, final versions this afternoon.", "12:15", False)]},
        "asana": {"sample": True, "done": 2, "overdue": 1, "open": 2, "home": "", "groups": [
            {"label": "Overdue", "tasks": [t("Overdue", "#104", "Website", "Fix the contact form", "Blocked", "Due 1 Oct", True)]},
            {"label": "Today", "tasks": [t("Today", "#112", "Launch", "Write the release notes", "In progress", "Due today"),
                                         t("Today", "#115", "Admin", "Book the team offsite", "Triage", "Due today")]},
            {"label": "Tomorrow", "tasks": [t("Tomorrow", "#118", "Website", "Review the new icons", "", "")]}]},
        "twitch": {"sample": True, "total": 12, "items": [
            {"name": "SpeedrunSam", "game": "Celeste", "viewers": "1,204", "href": "https://www.twitch.tv/"},
            {"name": "CozyBuilds", "game": "Minecraft", "viewers": "312", "href": "https://www.twitch.tv/"}]},
        "steam": {"sample": True, "online": 3, "friends": [
            {"name": "Rook", "game": "Deep Rock Galactic", "playing": True, "t": "1h 42m", "href": "steam://open/friends"},
            {"name": "Pixel", "game": "Rocket League", "playing": True, "t": "38m", "href": "steam://open/friends"},
            {"name": "Nova", "game": "Online", "playing": False, "t": "2h 10m", "href": "steam://open/friends"}],
            "dms": None},
        "discord": {"sample": True, "dms": [
            {"who": "Rook", "text": "You on tonight? 9pm, same squad as last week", "time": "13:44",
             "unread": True, "href": "discord:///channels/@me"},
            {"who": "Pixel", "text": "sent you the clip, it's unreal", "time": "13:10", "unread": True,
             "href": "discord:///channels/@me"},
            {"who": "Nova", "text": "cheers for earlier", "time": "11:52", "unread": False,
             "href": "discord:///channels/@me"}],
            "mentions": [{"who": "Echo", "text": "@you can you drop the map in here", "where": "#planning",
                          "time": "13:30", "href": "discord:///channels/@me"}],
            "active": [{"who": "Pixel", "line": "streaming in Late night co-op, Game Room", "streaming": True,
                        "playing": False, "href": "discord:///channels/@me"},
                       {"who": "Rook", "line": "playing Deep Rock Galactic", "streaming": False, "playing": True,
                        "href": "discord:///channels/@me"}]},
    }


SAMPLE = _sample()


# ---------------------------------------------------------------- Links, and Discord (your user account; listen-only)

def link_cmd(url):
    """Which program opens a clicked link: web pages in your default browser, app links (slack://, discord://,
    steam://) through their registered handlers, both via xdg-open. Other schemes are refused."""
    return ["xdg-open", url] if urllib.parse.urlsplit(url).scheme in SCHEMES else None


def snowflake_ts(sid):
    """The epoch seconds a Discord snowflake id was created at (its time is baked into the id); 0 if there is none."""
    return ((int(sid) >> 22) + DISCORD_EPOCH) / 1000 if sid else 0


class DiscordState:
    """What the user gateway has told us about your own account: your recent DMs and whether each is unread,
    mentions of you, and which of your friends are in a voice channel, streaming or playing a game. Built from
    gateway events only; nothing is ever sent back."""
    def __init__(self, me, voice_servers=None):
        self.me = me
        # Voice channels are only shown for servers whose name (case-insensitive) is in this set; None means all.
        self.voice_servers = {n.lower() for n in voice_servers} if voice_servers else None
        self.names = {}                    # user id -> display name
        self.friends = set()               # user ids you are friends with
        self.presence = {}                 # friend id -> {"game": name or None, "streaming": bool}
        self.guilds = {}                   # guild id -> {"name", "channels": {id: name}, "voice": {uid: voice_state}}
        self.dms = {}                      # channel id -> {"who", "text", "ts", "unread", "group", "href"}
        self.mentions = deque(maxlen=8)

    def name(self, u):
        """Remember and return a user's display name. Accepts a user dict or a bare id."""
        if isinstance(u, dict):
            n = u.get("global_name") or u.get("username")
            if n and u.get("id"):
                self.names[u["id"]] = n
            return n or self.names.get(u.get("id"), "?")
        return self.names.get(u, "?")

    # ---- READY: the one large payload the gateway sends right after we identify
    def ready(self, d):
        if d.get("user"):
            self.name(d["user"])                                 # your own name, so your own voice row reads right
        for u in d.get("users", []):
            self.name(u)
        for r in d.get("relationships", []):
            uid = r.get("id") or (r.get("user") or {}).get("id")
            if r.get("user"):
                self.name(r["user"])
            if r.get("type") == 1 and uid:                      # type 1 is a mutual friend
                self.friends.add(uid)
        read = {}                                               # channel id -> last message id you have read
        rs = d.get("read_state")
        for e in (rs.get("entries") if isinstance(rs, dict) else rs) or []:
            read[e.get("id")] = e.get("last_message_id")
        for ch in d.get("private_channels", []):
            self.add_dm(ch, read)
        for g in d.get("guilds", []):
            self.add_guild(g)
        # ponytail: user READY carries presences either as a top-level `presences` list or under
        # `merged_presences.friends`, depending on the capabilities we identify with; handle both, and
        # PRESENCE_UPDATE events fill in anything missed as friends change state.
        mp = d.get("merged_presences") or {}
        for p in (d.get("presences") or []) + (mp.get("friends") or []):
            self.set_presence(p)

    def add_dm(self, ch, read):
        recips = ch.get("recipients") or []
        for r in recips:
            self.name(r)
        who = ch.get("name") or ", ".join(self.name(r) for r in recips) or "Direct message"
        last = ch.get("last_message_id")
        seen = read.get(ch["id"])
        # Unread only when read state says so: there is an entry and its last-read id is behind the channel's last
        # message (snowflake ids compared as numbers). No entry means Discord has it marked read, so it is not unread.
        self.dms[ch["id"]] = {"who": who, "text": "", "ts": snowflake_ts(last), "group": ch.get("type") == 3,
                              "unread": bool(last) and seen is not None and int(last) > int(seen),
                              "href": f"discord:///channels/@me/{ch['id']}"}

    def add_guild(self, g):
        gg = self.guilds.get(g["id"]) or {"name": "", "channels": {}, "voice": {}}
        gg["name"] = g.get("name", gg["name"])
        gg["channels"].update({c["id"]: c.get("name", "") for c in g.get("channels", [])})
        self.guilds[g["id"]] = gg
        for mem in g.get("members", []):                         # names of members, so voice rows aren't just "?"
            if mem.get("user"):
                self.name(mem["user"])
        for v in g.get("voice_states", []):
            if (v.get("member") or {}).get("user"):
                self.name(v["member"]["user"])
            gg["voice"][v["user_id"]] = v

    def set_presence(self, p):
        uid = p.get("user_id") or (p.get("user") or {}).get("id")
        if not uid:
            return
        game = stream = None
        for a in p.get("activities") or []:
            if a.get("type") == 1 and not stream:              # 1 = streaming (Twitch/YouTube)
                stream = a.get("name") or "a stream"
            elif a.get("type") == 0 and not game:              # 0 = playing a game
                game = a.get("name")
        off = p.get("status") in (None, "offline", "invisible")
        self.presence[uid] = {"game": None if off else game, "streaming": bool(stream) and not off}

    # ---- live events after READY
    def event(self, t, d):
        if t == "GUILD_CREATE":
            self.add_guild(d)
        elif t == "VOICE_STATE_UPDATE" and d.get("guild_id") in self.guilds:
            voice = self.guilds[d["guild_id"]]["voice"]
            if (d.get("member") or {}).get("user"):
                self.name(d["member"]["user"])
            if d.get("channel_id"):
                voice[d["user_id"]] = d
            else:
                voice.pop(d["user_id"], None)
        elif t in ("CHANNEL_CREATE", "CHANNEL_UPDATE") and d.get("guild_id") in self.guilds:
            self.guilds[d["guild_id"]]["channels"][d["id"]] = d.get("name", "")
        elif t == "CHANNEL_CREATE" and d.get("type") in (1, 3):
            self.add_dm(d, {})
        elif t == "PRESENCE_UPDATE":
            if d.get("user"):
                self.name(d["user"])
            self.set_presence(d)
        elif t == "MESSAGE_CREATE":
            self.on_message(d)

    def render_text(self, d):
        """A message's text with its id-mentions turned into @names and emoji into :names:; a short stand-in for
        a message that is only an attachment or embed."""
        s = d.get("content") or ""
        if not s:
            s = "sent an attachment" if d.get("attachments") else "sent something" if d.get("embeds") else ""
        s = re.sub(r"<@!?(\d+)>", lambda m: "@" + ("you" if m.group(1) == self.me else self.names.get(m.group(1), "someone")), s)
        s = re.sub(r"<#\d+>", "#channel", s)
        s = re.sub(r"<a?:(\w+):\d+>", r":\1:", s)
        return " ".join(s.split())

    def preview_text(self, dm, msg):
        """A one-line preview of a message for the DM list: 'You: ...' for your own, 'Name: ...' in a group,
        just the text in a one-to-one DM; a short stand-in for an attachment or embed."""
        author = msg.get("author") or {}
        body = self.render_text(msg)
        if author.get("id") == self.me:
            return "You: " + body
        return f"{self.name(author)}: {body}" if dm.get("group") else body

    def set_preview(self, cid, msg):
        """Fill a DM's preview from its last message (fetched once at startup) without touching the unread flag,
        which was already decided from read state at READY."""
        dm = self.dms.get(cid)
        if dm:
            dm["text"] = self.preview_text(dm, msg)

    def on_message(self, d):
        author = d.get("author") or {}
        cid = d.get("channel_id")
        text = self.render_text(d)
        if not d.get("guild_id") and cid:                      # a direct or group message
            dm = self.dms.get(cid) or {"who": self.name(author), "group": False,
                                       "href": f"discord:///channels/@me/{cid}"}
            dm["who"] = dm.get("who") or self.name(author)
            dm["text"] = self.preview_text(dm, d)
            dm["ts"] = snowflake_ts(d.get("id")) or time.time()
            dm["unread"] = author.get("id") != self.me         # your own replies mark it read
            self.dms[cid] = dm
        elif any(u.get("id") == self.me for u in d.get("mentions", [])):
            g = self.guilds.get(d.get("guild_id"), {"channels": {}})
            who = (d.get("member") or {}).get("nick") or author.get("global_name") or author.get("username") or "?"
            self.mentions.appendleft({"who": who, "text": text or "mentioned you",
                                      "where": "#" + g["channels"].get(cid, ""), "ts": time.time(),
                                      "href": f"discord:///channels/{d.get('guild_id', '@me')}/{cid}/{d['id']}"})

    def snapshot(self):
        # The five most recent DMs, newest first, unread ones flagged. Preview text comes from the startup reads and
        # then live MESSAGE_CREATE events.
        dms = sorted((m for m in self.dms.values() if m["ts"]), key=lambda m: -m["ts"])[:5]
        dms = [{"who": m["who"], "text": m["text"], "unread": m["unread"], "href": m["href"],
                "time": hm(m["ts"])} for m in dms]
        active, seen = [], set()
        for gid, g in self.guilds.items():                     # anyone in a voice channel of a watched server (you too)
            if self.voice_servers is not None and g["name"].lower() not in self.voice_servers:
                continue
            for uid, v in g["voice"].items():
                if uid not in seen:
                    seen.add(uid)
                    ch = g["channels"].get(v.get("channel_id"), "a voice channel")
                    streaming = bool(v.get("self_stream")) or self.presence.get(uid, {}).get("streaming", False)
                    active.append({"who": self.name(uid), "streaming": streaming, "playing": False,
                                   "line": ("streaming in " if streaming else "in ") + f"{ch}, {g['name']}",
                                   "href": f"discord:///channels/{gid}/{v.get('channel_id')}"})
        for uid, p in self.presence.items():                   # friends streaming or in a game, not already shown
            if uid in self.friends and uid not in seen and (p["game"] or p["streaming"]):
                seen.add(uid)
                line = ("streaming " + (p["game"] or "")).strip() if p["streaming"] else "playing " + p["game"]
                active.append({"who": self.name(uid), "streaming": p["streaming"],
                               "playing": bool(p["game"]) and not p["streaming"], "line": line,
                               "href": "discord:///channels/@me"})
        active.sort(key=lambda a: (not a["streaming"], not a["playing"], a["who"].lower()))
        return {"dms": dms, "mentions": [m | {"time": hm(m["ts"])} for m in self.mentions], "active": active}


def main():
    from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
    from PySide6.QtGui import QColor
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebSockets import QWebSocket
    from PySide6.QtWidgets import QApplication

    def raise_app(app_class):
        """Discord takes a link while minimized but stays hidden; un-minimize and focus the app's window via KWin."""
        path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "jarvis-os-raise.js")
        with open(path, "w") as f:
            f.write(f'const w = workspace.windowList().find(x => x.normalWindow && x.resourceClass.toLowerCase() === "{app_class}"); '
                    "if (w) { w.minimized = false; workspace.activeWindow = w; }")
        q = lambda *a: subprocess.run(["qdbus", "org.kde.KWin", *a], capture_output=True, text=True, timeout=5).stdout.strip()
        sid = q("/Scripting", "org.kde.kwin.Scripting.loadScript", path, "jarvis-os-raise")
        q(f"/Scripting/Script{sid}", "org.kde.kwin.Script.run")
        q("/Scripting", "org.kde.kwin.Scripting.unloadScript", "jarvis-os-raise")

    if WINDOWS:
        sys.path.insert(0, os.path.dirname(HERE))
        import asyncio
        import winctl

        def raise_app(app_class):                   # Windows: the same "bring the app forward" through winctl
            asyncio.run(winctl.focus(app_class))

    def open_link(url):
        cmd = link_cmd(url)
        log.info("open %s", url if cmd else "refused " + url)
        if cmd and WINDOWS:
            os.startfile(url)                       # the registered handler, like xdg-open
        elif cmd:
            subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if cmd:
            app_class = {"discord": "discord", "slack": "slack", "steam": "steam"}.get(urllib.parse.urlsplit(url).scheme)
            if app_class:
                QTimer.singleShot(1500, lambda: raise_app(app_class))

    class Page(QWebEnginePage):
        """The page never navigates. Its click handler logs "jarvis-os:open <url>" instead (Qt WebEngine keeps
        slack:// discord:// steam:// links away from acceptNavigationRequest), and Python opens the link."""
        def acceptNavigationRequest(self, url, kind, main_frame):
            return url.isLocalFile()

        def javaScriptConsoleMessage(self, level, msg, line, src):
            if msg.startswith("jarvis-os:open "):
                open_link(msg.split(" ", 1)[1])
            else:
                log.info("page: %s (line %s)", msg, line)

    class Discord(QObject):
        """Your own Discord over the user gateway: your recent DMs, mentions of you, who is in voice in the watched
        servers (you included), and friends who are streaming or playing. Listen-only: the only frames it sends are
        the protocol handshake (identify), heartbeats, and a lazy guild-subscribe (op 14) for the watched servers so the
        gateway sends their voice/presence updates (the official client sends this when you view a guild); it never
        sends a message, reaction, typing or status. The one REST read is a small, spaced set of GETs at startup (the
        last message of the five most recent DMs, seconds apart) for preview text; after that every update is a gateway
        event, no more REST."""
        def __init__(self, token, me, voice_servers=None):
            super().__init__()
            self.token, self.me = token, me
            self.seq = self.ready_at = None
            self.error, self.backoff, self.vsu = "", 30, 0
            self.state = DiscordState(me, voice_servers)
            self.ws, self.hb = QWebSocket(), QTimer(self)
            self.hb.timeout.connect(lambda: self.send(1, self.seq))
            self.ws.textMessageReceived.connect(self.on_message)
            self.ws.disconnected.connect(self.on_closed)
            self.connect_ws()

        def connect_ws(self):
            self.ws.open(QUrl("wss://gateway.discord.gg/?v=10&encoding=json"))

        def send(self, op, d):
            self.ws.sendTextMessage(json.dumps({"op": op, "d": d}))

        def identify(self):
            # A plain desktop-client identify: no intents (user tokens don't take them), status "invisible" so the
            # listener never advertises you as online or changes how your friends see you.
            self.send(2, {"token": self.token, "capabilities": 0, "compress": False,
                          "properties": {"os": "Linux", "browser": "Chrome", "device": "", "system_locale": "en-US",
                                         "browser_user_agent": UA, "browser_version": "127.0.0.0", "os_version": "",
                                         "referrer": "", "referring_domain": "", "release_channel": "stable",
                                         "client_build_number": 300000},
                          "presence": {"status": "invisible", "since": 0, "activities": [], "afk": False},
                          "client_state": {"guild_versions": {}}})

        def on_closed(self):
            self.hb.stop()
            code = self.ws.closeCode()
            code = getattr(code, "value", code)       # PySide gives an enum; 4004 etc. are plain numbers
            if code == 4004:                          # token rejected: stop, never hammer the gateway
                self.error = "discord closed 4004 (token rejected)"
                return
            delay, self.backoff = self.backoff, min(self.backoff * 2, 1200)   # exponential, capped at ~3 tries/hour
            log.info("discord gateway closed (%s), reconnecting in %ss", code, delay)
            QTimer.singleShot(int(delay * 1000), self.connect_ws)

        def on_message(self, raw):
            m = json.loads(raw)
            self.seq = m.get("s") or self.seq
            op, t, d = m["op"], m.get("t"), m.get("d")
            if op == 10:
                self.hb.start(d["heartbeat_interval"])
                self.identify()
            elif op == 1:                             # the gateway is asking for a heartbeat now
                self.send(1, self.seq)
            elif op in (7, 9):                        # reconnect / invalid session: drop and identify fresh
                self.ws.close()
            elif t == "READY":
                self.state.ready(d)
                self.ready_at = time.time()
                self.backoff = 30
                threading.Thread(target=self.prime_dms, daemon=True).start()
                QTimer.singleShot(8000, self.log_voice_servers)   # let the GUILD_CREATE burst arrive first
            elif t:
                if t == "VOICE_STATE_UPDATE":
                    self.vsu += 1
                self.state.event(t, d)

        def log_voice_servers(self):
            """Record which watched servers matched (name -> id), subscribe to them so voice/presence events flow for
            user accounts, and log voice counts (no tokens). If any watched server is missing, list every server name."""
            allow = self.state.voice_servers
            if allow is None:
                return
            matched = {g["name"]: gid for gid, g in self.state.guilds.items() if g["name"].lower() in allow}
            log.info("discord voice servers matched: %s", matched)
            missing = allow - {n.lower() for n in matched}
            if missing:
                log.info("discord voice servers NOT found: %s; your servers: %s",
                         sorted(missing), sorted(g["name"] for g in self.state.guilds.values()))
            for gid in matched.values():
                # op 14, the lazy guild subscribe the official client sends when viewing a guild; read-only, it just
                # asks the gateway to start sending this guild's presence and voice updates.
                self.send(14, {"guild_id": gid, "typing": True, "activities": True, "threads": False})
            counts = {name: len(self.state.guilds[gid]["voice"]) for name, gid in matched.items()}
            log.info("discord watched voice now: %s; VOICE_STATE_UPDATE events so far: %s", counts, self.vsu)

        def prime_dms(self):
            """Once, right after READY, read the last message of the five most recent DMs so their previews show
            before any new message arrives. Spaced a couple of seconds apart (like a client opening each DM in turn),
            stopping at the first rate-limit; after this the panel only listens. Previews only, never the unread flag."""
            targets = [cid for cid, m in sorted(self.state.dms.items(), key=lambda kv: -kv[1]["ts"]) if m["ts"]][:5]
            for cid in targets:
                try:
                    req = urllib.request.Request(f"https://discord.com/api/v10/channels/{cid}/messages?limit=1",
                                                 headers={"Authorization": self.token, "User-Agent": UA})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        msgs = json.loads(resp.read().decode())
                except urllib.error.HTTPError as e:
                    if e.code == 429:                 # rate-limited: back off, never burst
                        break
                    continue
                except Exception:
                    continue
                if msgs:
                    self.state.set_preview(cid, msgs[0])
                time.sleep(2)

        def snapshot(self):
            if self.error:
                raise RuntimeError(self.error)
            if not self.ready_at:
                raise RuntimeError("connecting")
            return self.state.snapshot()

    class Hub(QObject):
        got = Signal(str, str)

        def __init__(self, view):
            super().__init__()
            self.view, self.state, self.ok_at, self.busy, self.loaded = view, {}, {}, set(), False
            self.mode, self.ticks = None, {}
            self.got.connect(self.push)

        def check_mode(self):
            """Every second: the page switches layout and time zone when the mode changes, and the feeds whose times or
            "today" depend on the zone refetch (work feeds only on the way back to work mode)."""
            global shown_tz
            m = mode_now(secret("mode"), time.time())
            if m != self.mode:
                log.info("%s mode, times in %s", m, MODE_TZ[m])
                self.mode, shown_tz = m, ZoneInfo(MODE_TZ[m])
                self.push("mode", json.dumps({"mode": m, "tz": MODE_TZ[m]}))
                for name in ("calendar", "discord") + (WORK_FEEDS if m == "work" else ()):
                    if name in self.ticks:
                        self.ticks[name]()

        def every(self, sec, name, fn, threaded=True):
            def tick():
                if name in self.busy or (self.mode == "game" and name in WORK_FEEDS):
                    return
                if not threaded:
                    return self.run(name, fn)
                self.busy.add(name)
                threading.Thread(target=self.run, args=(name, fn), daemon=True).start()
            t = QTimer(self)
            t.timeout.connect(tick)
            t.start(sec * 1000)
            self.ticks[name] = tick
            tick()

        def run(self, name, fn):
            try:
                data = {"ok": True, **fn()}
                self.ok_at[name] = time.time()
            except Exception as e:
                log.warning("%s failed: %s", name, e)
                data = None if time.time() - self.ok_at.get(name, 0) < STALE else {"ok": False}
            finally:
                self.busy.discard(name)
            if data:
                self.got.emit(name, json.dumps(data))

        def push(self, name, data):
            self.state[name] = data
            if self.loaded:
                self.view.page().runJavaScript(f"feed({json.dumps(name)}, {data})")

        def push_all(self, ok):
            self.loaded = True
            for name, data in self.state.items():
                self.push(name, data)

    app = QApplication(sys.argv)
    app.setDesktopFileName("jarvis-os")            # the Wayland app id the KWin rule (kwin-rule.sh) matches on
    view = QWebEngineView()
    view.setPage(Page(view))
    view.page().setBackgroundColor(QColor("#030c0d"))
    view.setWindowTitle("JARVIS OS")
    view.setWindowFlags(Qt.FramelessWindowHint)
    view.setContextMenuPolicy(Qt.NoContextMenu)
    view.resize(1920, 1080)
    if WINDOWS:                # what the KWin rule does on Linux: below everything, no taskbar/alt-tab, never focused
        view.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnBottomHint | Qt.WindowDoesNotAcceptFocus)
        view.setAttribute(Qt.WA_ShowWithoutActivating)
        left = min(app.screens(), key=lambda s: s.geometry().x())      # the left monitor, as on Bazzite
        view.setGeometry(left.geometry())
    hub = Hub(view)
    view.loadFinished.connect(hub.push_all)
    view.load(QUrl.fromLocalFile(os.path.join(HERE, "index.html")))
    view.show()

    hub.check_mode()                               # before the feeds, so game mode never fetches Asana or Slack
    mode_timer = QTimer(hub)
    mode_timer.timeout.connect(hub.check_mode)
    mode_timer.start(1000)
    hub.every(5, "system", system)
    hub.every(900, "weather", weather)
    hub.every(5, "workers", workers)
    hub.every(60, "asana", asana)
    hub.every(300, "calendar", calendar)
    hub.every(60, "twitch", twitch)
    hub.every(180, "slack", slack)
    hub.every(120, "steam", steam)
    token = secret("discord-user-token")          # off unless you add it: a self-bot, against Discord's terms (README)
    voice_servers = [s.strip() for s in secret("discord-voice-servers").splitlines() if s.strip()] or None   # None: all
    discord = Discord(token, secret("discord-user-id"), voice_servers) if token else None
    hub.every(30, "discord", discord.snapshot if discord else lambda: SAMPLE["discord"], threaded=False)
    sys.exit(app.exec())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
