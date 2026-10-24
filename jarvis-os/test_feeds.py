#!/usr/bin/python3
"""Self-check for the parsing that turns raw API data into what the dashboard shows. Run: python3 test_feeds.py"""
import datetime as dt
from zoneinfo import ZoneInfo

import jarvis_os
from jarvis_os import (DiscordState, ics_today, link_cmd, mode_now, shape_tasks, shape_workers, slack_text, snowflake_ts,
                       step_words)

today = dt.date(2026, 10, 2)
jarvis_os.shown_tz = ZoneInfo("UTC")
if jarvis_os.WINDOWS:          # Windows is always game mode; the schedule checks below test the Linux schedule
    assert mode_now("", 0) == "game"
    jarvis_os.WINDOWS = False

# Asana: grouped by due date (Overdue, Today, Tomorrow, weekday, date, No due date), then status; done-today counted
task = lambda name, due=None, status=None, **kw: {
    "name": name, "due_on": due, "completed": False, "custom_fields": [{"name": "Status", "display_value": status}], **kw}
t = shape_tasks([
    task("[#17] Dashboard", "2026-10-05", "Blocked", projects=[{"name": "Website"}]),
    task("[#21] Enquiry form", "2026-10-06", "Blocked"),
    task("[#05] Weekly email", "2026-10-06", "In Progress"),
    task("[#64] Old thing", "2026-09-29", "Blocked"),
    task("[#32] Launch notes", "2026-10-02", "In Progress"),
    task("[#18] Rebuild", "2026-10-13"),
    task("Tomorrow's call prep", "2026-10-03"),
    task("Someday"),
    {"name": "Done today", "completed": True, "completed_at": "2026-10-02T10:00:00.000Z"},
    {"name": "Done before", "completed": True, "completed_at": "2026-09-30T10:00:00.000Z"},
], today)
assert [(g["label"], [r["id"] or r["task"] for r in g["tasks"]]) for g in t["groups"]] == [
    ("Overdue", ["#64"]), ("Today", ["#32"]), ("Tomorrow", ["Tomorrow's call prep"]), ("Monday", ["#17"]),
    ("Tuesday", ["#05", "#21"]), ("Tue 13 Oct", ["#18"]), ("No due date", ["Someday"])], t["groups"]
assert (t["done"], t["overdue"], t["open"]) == (1, 1, 1), t
first = t["groups"][3]["tasks"][0]
assert (first["client"], first["task"], first["status"], first["due"], first["late"]) == \
    ("Website", "Dashboard", "Blocked", "Due 5 Oct", False), first
assert t["groups"][0]["tasks"][0]["late"] and t["groups"][1]["tasks"][0]["due"] == "Due today"
assert t["groups"][4]["tasks"][0]["status"] == "In progress"

# Calendar: one-off, weekly series in London time (DST shift), cancelled, moved instance, exdate, other day
ny = ZoneInfo("America/New_York")
ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:a
DTSTART:20261002T083000Z
DTEND:20261002T090000Z
SUMMARY:Kickoff\\, website
LOCATION:https://meet.google.com/abc-defg-hij
END:VEVENT
BEGIN:VEVENT
UID:b
DTSTART;TZID=Europe/London:20260904T100000
DTEND;TZID=Europe/London:20260904T103000
RRULE:FREQ=WEEKLY;BYDAY=FR
SUMMARY:Weekly ops
END:VEVENT
BEGIN:VEVENT
UID:c
DTSTART:20261002T120000Z
STATUS:CANCELLED
SUMMARY:Gone
END:VEVENT
BEGIN:VEVENT
UID:d
DTSTART;TZID=Europe/London:20260901T150000
DTEND;TZID=Europe/London:20260901T153000
RRULE:FREQ=DAILY
EXDATE;TZID=Europe/London:20261002T150000
SUMMARY:Skipped today
END:VEVENT
BEGIN:VEVENT
UID:e
DTSTART;TZID=Europe/London:20260901T160000
RRULE:FREQ=DAILY
SUMMARY:Daily standup
END:VEVENT
BEGIN:VEVENT
UID:e
RECURRENCE-ID;TZID=Europe/London:20261002T160000
DTSTART;TZID=Europe/London:20261002T170000
SUMMARY:Daily standup (moved)
END:VEVENT
BEGIN:VEVENT
UID:f
DTSTART:20261005T083000Z
SUMMARY:Monday thing
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n ").replace("\r\n ", "\r\n")
ev = ics_today(ics, today, ny)
got = [(dt.datetime.fromtimestamp(e["start"], ny).strftime("%H:%M"), e["title"]) for e in ev]
assert got == [("04:30", "Kickoff, website"), ("05:00", "Weekly ops"), ("12:00", "Daily standup (moved)")], got
assert ev[0]["href"] == "https://meet.google.com/abc-defg-hij"
# the UK clocks go back on 25 Oct, the US ones on 1 Nov: in between, the London 10:00 call is 06:00 in New York
late = ics_today(ics, dt.date(2026, 10, 30), ny)
assert [dt.datetime.fromtimestamp(e["start"], ny).strftime("%H:%M") for e in late if e["title"] == "Weekly ops"] == ["06:00"]

# Slack markup -> plain text
assert slack_text("<@U1> see <#C2|website> and <https://x.io|the doc> &amp; <!here>", lambda u: "Sam") == \
    "@Sam see #website and the doc & @here"

# Discord user gateway: snowflake time, READY (friends, unread read straight from read state, voice, presence)
assert 1420070400 < snowflake_ts("175928847299117063") < 1500000000 and snowflake_ts(None) == 0
S = {"a": "300000000000000000", "b": "250000000000000000", "c": "200000000000000000", "d": "150000000000000000",
     "old": "100000000000000000"}                              # distinct snowflakes, newest first a > b > c > d
ds = DiscordState("100", ["GAME ROOM", "night owls"])       # case-insensitive; "Big Public Server" is not watched
ds.ready({
    "user": {"id": "100", "global_name": "Me"},                 # you; not a friend, but shown in your own voice
    "users": [{"id": "1", "username": "rook", "global_name": "Rook"},
              {"id": "2", "username": "nova", "global_name": "Nova"}, {"id": "3", "username": "stranger"}],
    "relationships": [{"id": "1", "type": 1}, {"id": "2", "type": 1}, {"id": "9", "type": 2}],  # 9 is a pending request
    "read_state": {"entries": [{"id": "dm1", "last_message_id": S["old"]},   # behind -> unread
                               {"id": "dm4", "last_message_id": S["c"]},     # caught up -> read
                               {"id": "dm5", "last_message_id": S["old"]}]}, # behind -> unread
    "private_channels": [
        {"id": "dm1", "type": 1, "recipients": [{"id": "1"}], "last_message_id": S["a"]},   # unread (a > old)
        {"id": "dm2", "type": 1, "recipients": [{"id": "2"}], "last_message_id": S["b"]},   # NO read entry -> read
        {"id": "dm3", "type": 1, "recipients": [{"id": "2"}], "last_message_id": None},     # no messages -> dropped
        {"id": "dm4", "type": 1, "recipients": [{"id": "1"}], "last_message_id": S["c"]},   # read (c == c)
        {"id": "dm5", "type": 1, "recipients": [{"id": "2"}], "last_message_id": S["d"]}],  # unread (d > old)
    "guilds": [{"id": "g1", "name": "Game Room", "channels": [{"id": "v1", "name": "Late night co-op"}],
                "voice_states": [{"user_id": "1", "channel_id": "v1", "self_stream": True},
                                 {"user_id": "100", "channel_id": "v1"}]},    # you in voice -> must show, friend or not
               {"id": "g2", "name": "Big Public Server", "channels": [{"id": "vx", "name": "general voice"}],
                "voice_states": [{"user_id": "2", "channel_id": "vx"}]}],   # not watched: this voice must be ignored
    "presences": [{"user": {"id": "2"}, "status": "online", "activities": [{"type": 0, "name": "Rocket League"}]},
                  {"user": {"id": "3"}, "status": "online", "activities": [{"type": 0, "name": "Tetris"}]}],  # 3 not a friend
})
# the five most recent DMs, newest first; a DM with no read-state entry is NOT flagged unread
snap = ds.snapshot()
assert [(d["who"], d["unread"]) for d in snap["dms"]] == \
    [("Rook", True), ("Nova", False), ("Rook", False), ("Nova", True)], snap["dms"]
assert snap["dms"][0]["href"] == "discord:///channels/@me/dm1"
# a startup read fills preview text without disturbing the unread flag (own message shows "You:")
ds.set_preview("dm1", {"id": S["a"], "author": {"id": "100"}, "content": "see you then"})
ds.set_preview("dm2", {"id": S["b"], "author": {"id": "2", "global_name": "Nova"}, "content": "cheers"})
d1, d2 = ds.snapshot()["dms"][:2]
assert (d1["text"], d1["unread"]) == ("You: see you then", True) and d2["text"] == "cheers", (d1, d2)
# a live incoming message updates preview and marks the DM unread
ds.event("MESSAGE_CREATE", {"id": S["a"], "channel_id": "dm4", "author": {"id": "1", "global_name": "Rook"},
                            "content": "you around?", "mentions": []})
dm4 = next(d for d in ds.snapshot()["dms"] if d["href"].endswith("dm4"))
assert (dm4["text"], dm4["unread"]) == ("you around?", True), dm4
# mentions of you only, server nickname used as the name; friends in voice/streaming/playing, streamers first
ds.event("MESSAGE_CREATE", {"id": "301", "guild_id": "g1", "channel_id": "v1", "author": {"id": "2", "global_name": "Nova"},
                            "member": {"nick": "Novs"}, "content": "<@100> raid at 9?", "mentions": [{"id": "100"}]})
ds.event("MESSAGE_CREATE", {"id": "302", "guild_id": "g1", "channel_id": "v1", "author": {"id": "3"},
                            "content": "ignore me", "mentions": []})
snap = ds.snapshot()
assert [(m["who"], m["text"], m["where"]) for m in snap["mentions"]] == \
    [("Novs", "@you raid at 9?", "#Late night co-op")], snap["mentions"]
assert [(a["who"], a["line"], a["streaming"], a["playing"]) for a in snap["active"]] == \
    [("Rook", "streaming in Late night co-op, Game Room", True, False),
     ("Nova", "playing Rocket League", False, True),
     ("Me", "in Late night co-op, Game Room", False, False)], snap["active"]
# everyone leaving voice (channel_id None) and going offline empties the active list
ds.event("VOICE_STATE_UPDATE", {"guild_id": "g1", "user_id": "1", "channel_id": None})
ds.event("VOICE_STATE_UPDATE", {"guild_id": "g1", "user_id": "100", "channel_id": None})
ds.event("PRESENCE_UPDATE", {"user": {"id": "2"}, "status": "offline", "activities": []})
assert ds.snapshot()["active"] == [], ds.snapshot()["active"]
# no watched-server list: every server's voice shows
assert len(DiscordState("100").voice_servers or "") == 0

# Link routing: web and app links through xdg-open, nothing else
assert link_cmd("https://app.asana.com/0/1/2") == ["xdg-open", "https://app.asana.com/0/1/2"]
assert link_cmd("slack://channel?team=T1&id=C1") == ["xdg-open", "slack://channel?team=T1&id=C1"]
assert link_cmd("javascript:alert(1)") is None and link_cmd("file:///etc/passwd") is None

# Workers: only Jarvis's live list shows, waiting first; a permission prompt and a NEED USER question both surface
w = shape_workers({"tasks": [
    {"task_id": "worker:old", "description": "old (running)", "type": "Worker", "started": 100},
    {"task_id": "worker:asks", "description": "asks (waiting)", "type": "Worker", "started": 200},
    {"task_id": "worker:needs", "description": "needs (waiting)", "type": "Worker", "started": 300},
    {"task_id": "worker:new", "description": "new (starting)", "type": "Worker", "started": 400},
    {"task_id": "bg1", "description": "a background job", "type": "local_bash", "started": 1}], "history": [
    {"kind": "worker", "name": "gone", "state": "done", "ts": 1, "text": "Finished."},
    {"kind": "worker", "name": "old", "state": "running", "ts": 2, "text": "Read the spec. Now **testing** `x.py`."},
    {"kind": "confirm_ask", "ts": 3, "question": "Sir, the asks worker would like to change the file a.py. Shall I allow it?"},
    {"kind": "worker", "name": "asks", "state": "waiting", "ts": 5, "text": "Building."},
    {"kind": "confirm_ask", "ts": 6, "question": "Boss, the asks worker would like to run the tests, which runs rm. Shall I allow it?"},
    {"kind": "confirm_ask", "ts": 7, "question": "Sir, the needs worker would like to stale one. Shall I allow it?"},
    {"kind": "worker", "name": "needs", "state": "waiting", "ts": 8,
     "text": "x" * 440 + "ed mid. Drafts are in ~/out.\n\nNEED USER: Option A or B?\n- A is faster"}]}, 1000)
assert [r["name"] for r in w["items"]] == ["Asks", "Needs", "Old", "New"], w["items"]
assert (w["waiting"], w["running"]) == (2, 2), w
asks, needs, old, new = w["items"]
assert asks["need"] == "Wants your OK to run the tests, which runs rm." and asks["todo"] == "Answer Jarvis yes or no.", asks
assert needs["need"] == "Option A or B? A is faster" and needs["doing"] == "Drafts are in ~/out.", needs
assert old["doing"] == "Read the spec. Now testing x.py." and old["need"] == "" and old["t"] == "15m", old
assert new["doing"] == "Starting up.", new
assert shape_workers({"tasks": [], "history": []}, 0)["items"] == []
w = shape_workers({"tasks": [{"task_id": "worker:x", "description": "x (running)", "type": "Worker", "started": 0}], "history": []},
                  100, lambda *a: (step_words("Edit", {"file_path": "/a/index.html"}), 90, "Checking it. Done soon."))
assert (w["items"][0]["step"], w["items"][0]["ago"], w["items"][0]["doing"]) == ("Editing index.html", "10s", "Checking it. Done soon.")
assert step_words("mcp__claude-in-chrome__computer", {"action": "screenshot"}) == "Taking a screenshot"

# Jarvis on a request comes first, its own yes/no question shows, a worker's ask stays on the worker, idle = gone
tasks = [{"task_id": "worker:w", "description": "w (waiting)", "type": "Worker", "started": 0}]
hist = [{"kind": "say", "ts": 40, "text": "Old turn."},
        {"kind": "tool_use", "ts": 60, "name": "Edit", "sub": False, "input": '{"file_path": "/a/index.html", "old_str'},
        {"kind": "tool_use", "ts": 70, "name": "Bash", "sub": True, "input": '{"description": "A helper step"}'},
        {"kind": "say", "ts": 65, "text": "On it, sir."}, {"kind": "say", "ts": 66, "text": "Shall I delete it?"}]
w = shape_workers({"tasks": tasks, "history": hist, "turn": {"started": 50}, "question": "Shall I delete it?"}, 100)
j = w["items"][0]
assert [r["name"] for r in w["items"]] == ["Jarvis", "W"] and (w["waiting"], w["running"]) == (2, 0), w
assert (j["step"], j["ago"], j["doing"], j["t"], j["need"]) == ("Editing index.html", "40s", "On it, sir.", "50s", "Shall I delete it?"), j
j = shape_workers({"tasks": tasks, "history": [], "turn": {"started": 50},
                   "question": "Sir, the w worker would like to x. Shall I allow it?"}, 100)["items"][0]
assert (j["name"], j["state"], j["need"], j["doing"]) == ("Jarvis", "running", "", "Thinking."), j
assert [r["name"] for r in shape_workers({"tasks": tasks, "history": hist, "turn": None}, 100)["items"]] == ["W"]
assert step_words("mcp__jarvis__screenshot", {}) == "Taking a screenshot"

# Work / game mode: weekdays inside WORK_HOURS are work; a manual switch holds until the next scheduled switch
zone, (h0, h1) = jarvis_os.SCHEDULE_TZ, jarvis_os.WORK_HOURS
at = lambda d, h, m=0: dt.datetime(2026, 10, d, h, m, tzinfo=zone).timestamp()   # 2 Oct 2026 is a Friday
assert [mode_now("", at(2, h, m)) for h, m in ((h0 - 1, 59), (h0, 0), (h1 - 1, 59), (h1, 0))] == ["game", "work", "work", "game"]
assert mode_now("", at(3, 12)) == "game" and mode_now("auto 1", at(2, h0 + 1)) == "work" and mode_now("junk", at(2, h0 + 1)) == "work"
assert mode_now(f"game {at(2, h0 + 1):.0f}", at(2, h1 - 1, 59)) == "game"     # holds through the work day
assert mode_now(f"game {at(2, h0 + 1):.0f}", at(5, h0 + 1)) == "work"         # gone at Friday's end of work
assert mode_now(f"work {at(3, 12):.0f}", at(4, 23)) == "work"                 # Saturday's work holds to Monday's start
assert mode_now(f"work {at(3, 12):.0f}", at(5, h1, 30)) == "game"

# Every time shown follows the mode's zone (06:00 UTC on 3 Oct is 02:00 in New York, 08:00 in Berlin)
for zone_name, want in (("America/New_York", "02:00"), ("Europe/Berlin", "08:00")):
    jarvis_os.shown_tz = ZoneInfo(zone_name)
    assert jarvis_os.hm(dt.datetime(2026, 10, 3, 6, tzinfo=dt.timezone.utc).timestamp()) == want, (zone_name, want)

print("ok")
