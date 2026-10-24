"""Everything Jarvis does is reported here, kept in memory, saved to logs/events.jsonl and
streamed to the dashboard."""
import asyncio
import collections
import itertools
import json
import os
import time

import config

EVENTS_FILE = os.path.join(config.JARVIS_DIR, "logs", "events.jsonl")
KEEP = 3000
LIVE_ONLY = {"meter", "procs", "delta", "partial", "speaking_now"}   # too chatty to store

_ids = itertools.count(1)
history = collections.deque(maxlen=KEEP)
_subscribers = {}                               # queue -> tag
_listeners = []                                 # plain functions called with every event


def load():
    """Pull the tail of the saved log back in, so history survives restarts."""
    global _ids
    try:
        with open(EVENTS_FILE) as f:
            lines = collections.deque(f, maxlen=KEEP)
    except FileNotFoundError:
        return
    for line in lines:
        try:
            history.append(json.loads(line))
        except ValueError:
            pass
    if history:
        _ids = itertools.count(history[-1]["id"] + 1)
    if len(lines) == KEEP:                      # trim the file so it doesn't grow forever
        with open(EVENTS_FILE, "w") as f:
            f.writelines(lines)


def emit(kind, **data):
    ev = {"id": next(_ids), "ts": time.time(), "kind": kind, **data}
    if kind not in LIVE_ONLY:
        history.append(ev)
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(ev, default=str) + "\n")
    for fn in _listeners:
        try:
            fn(ev)
        except Exception:
            pass
    for q in list(_subscribers):
        if q.full():
            _subscribers.pop(q, None)           # a stuck browser tab; drop it
        else:
            q.put_nowait(ev)
    return ev


def listen(fn):
    _listeners.append(fn)


def subscribe(tag="dashboard"):
    q = asyncio.Queue(maxsize=2000)
    _subscribers[q] = tag
    return q


def unsubscribe(q):
    _subscribers.pop(q, None)


def watchers(tag="dashboard"):
    return sum(1 for t in _subscribers.values() if t == tag)
