"""Workers: separate Claude Code sessions Jarvis starts for longer jobs, running in the background in this process.
They load your normal Claude Code settings (user + project allow/deny rules). Risky steps go to a judge model, and
anything it isn't sure of is asked out loud through Jarvis's yes/no. When a worker finishes or needs you, Jarvis says so."""
import asyncio
import logging
import time

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow,
                              PermissionResultDeny, ResultMessage, SystemMessage, TextBlock, tool)

import config
import events
from pctools import text

log = logging.getLogger("worker")

confirm = None      # async (question) -> bool, asks the user out loud; set by jarvis.py
notify = None       # (message) -> None, hands a message to Jarvis's session; set by jarvis.py
workers = {}        # name -> Worker
LIVE = ("starting", "running", "waiting")

BRIEF = """
# You are a Jarvis worker
Jarvis, {name}'s voice assistant, started you to do one job in the background. {name} is not watching this session.
- Work on your own. Anything your settings don't already allow is asked out loud for you; if they say no, find another way or stop.
- End every turn with one or two plain sentences: what you did and where the result is. That line is read out to them.
- Chrome: other sessions share this browser. Always work in a new tab you create yourself (tabs_create_mcp), never use or close tabs you didn't open, and close your own tabs when done.
- If you need a decision or information from them, end the turn with a line starting "NEED USER:" and the question.
"""


JUDGE = """You approve tool calls for a background worker on {name}'s PC, on their behalf. {name} wants the job done
without being asked about routine steps. Reply with exactly one line: "ALLOW: <reason>" or "ASK: <short reason>".
ALLOW anything that serves the worker's brief and is local or reversible: reading, running scripts or shell commands that
read or compute, writing files in temp, home or work folders, and creating or editing on a board, doc or file the brief
tells it to build. ASK for: sending, posting, emailing or messaging anyone; publishing; payments or purchases; deleting or
overwriting data the worker did not create in this job; changes to live systems or accounts (CRMs, automations,
shared docs and so on) unless the brief explicitly asks for that change; credentials, system settings, installs; anything outside the brief."""


async def judge(task, tool_name, data):
    """Jarvis's call on a worker's permission request: (allow, reason). Any failure means ask the user."""
    import json
    from claude_agent_sdk import query
    prompt = f"Worker brief:\n{task[:4000]}\n\nTool: {tool_name}\nInput: {json.dumps(data, default=str)[:3000]}"
    try:
        said = ""
        async for m in query(prompt=prompt, options=ClaudeAgentOptions(
                model=config.JUDGE_MODEL, system_prompt=JUDGE.format(name=config.USER_NAME), max_turns=1, allowed_tools=[], setting_sources=[])):
            if isinstance(m, AssistantMessage):
                said += "".join(b.text for b in m.content if isinstance(b, TextBlock))
        said = said.strip()
        return said.upper().startswith("ALLOW"), said.split(":", 1)[-1].strip()
    except Exception:
        log.exception("judge failed")
        return False, ""


def outcome(result, is_error):
    return "failed" if is_error else "waiting" if "NEED USER:" in (result or "") else "done"


class Worker:
    def __init__(self, name, task):
        self.name, self.task = name, task
        self.approved = False                      # the user said yes once for this instruction: no more asking
        self.state, self.session_id, self.last, self.client = "starting", None, "", None
        self.started = time.time()
        self.runner = asyncio.create_task(self.run())

    def set(self, state):
        self.state = state
        events.emit("worker", name=self.name, state=state, session_id=self.session_id, text=self.last[-500:])

    def tell(self):
        notify(f"[worker update, not from {config.USER_NAME}] The {self.name} worker is {self.state} (session {self.session_id}). "
               f"Its last message:\n{self.last[-1500:]}\n"
               f"Tell {config.USER_NAME} in one short sentence; if it needs them, ask its question. Don't read out the session id.")

    async def gate(self, tool_name, data, context):
        """Called for anything the policy would ask about, including steps your allow rules would wave through."""
        import brain                               # brain imports this module
        verdict, why = brain.Brain.policy(tool_name, data)
        if verdict == "deny":                      # hard rules in policy still hold
            return PermissionResultDeny(message=why)
        if verdict == "allow":                     # same gate as main Jarvis: only Enter, risky Bash and some writes ask
            return PermissionResultAllow(updated_input=data)
        if self.approved:                          # one yes per request, not one per step
            return PermissionResultAllow(updated_input=data)
        enter = tool_name == "mcp__jarvis__press_keys"   # Enter sends messages: always the user's call
        allow, reason = (False, "") if enter else await judge(self.task, tool_name, data)
        log.info("worker %s %s %s: %s", self.name, "allowed" if allow else "asks the user", tool_name, reason)
        if allow:                                  # the judge decides first, only real calls reach the user
            return PermissionResultAllow(updated_input=data)
        self.set("waiting")
        # the judge's reason stays in the log: spoken, it can run to paragraphs
        ok = await confirm(f"{config.HONORIFIC.capitalize()}, the {self.name} worker would like to {brain.describe(tool_name, data)}. Shall I allow it?")
        self.set("running")
        if ok:
            self.approved = True
            return PermissionResultAllow(updated_input=data)
        return PermissionResultDeny(message="The user said no (or didn't answer). Don't retry it; find another way or stop and say why.")

    async def run(self):
        import brain                               # brain imports this module
        opts = ClaudeAgentOptions(
            model=config.MODEL, cwd=config.HOME, setting_sources=["user", "project"], extra_args={"chrome": None},
            system_prompt={"type": "preset", "preset": "claude_code", "append": BRIEF.format(name=config.USER_NAME)},
            permission_mode="default", can_use_tool=self.gate, hooks=brain.gate_hooks(self.gate), disallowed_tools=["AskUserQuestion"],
            max_buffer_size=20 * 1024 * 1024)
        try:
            async with ClaudeSDKClient(options=opts) as self.client:
                await self.client.query(self.task)
                self.set("running")
                async for m in self.client.receive_messages():
                    if isinstance(m, SystemMessage) and m.subtype == "init":
                        self.session_id = m.data.get("session_id")
                    elif isinstance(m, AssistantMessage) and not m.parent_tool_use_id:
                        said = "".join(b.text for b in m.content if isinstance(b, TextBlock)).strip()
                        self.last = said or self.last
                    elif isinstance(m, ResultMessage):
                        self.session_id, self.last = m.session_id, m.result or self.last
                        self.set(outcome(m.result, m.is_error))
                        self.tell()
        except asyncio.CancelledError:
            raise                                  # stopped on purpose
        except Exception as e:
            log.exception("worker %s died", self.name)
            self.last = f"It crashed: {e}"
            self.set("failed")
            self.tell()


def find(name):
    return workers.get(name.strip().lower())


def names():
    return ", ".join(workers) or "none"


def as_tasks():
    """Live workers, shaped like background tasks for the dashboard and widget."""
    return [{"task_id": "worker:" + w.name, "description": f"{w.name} ({w.state})", "type": "Worker",
             "started": w.started} for w in workers.values() if w.state in LIVE]


async def stop(w):
    w.runner.cancel()
    await asyncio.wait({w.runner}, timeout=15)     # the Claude process closes before anyone resumes it
    w.set("stopped")


async def stop_all():
    await asyncio.gather(*(stop(w) for w in workers.values() if not w.runner.done()))


@tool("start_worker", "Start a background worker: a separate Claude Code session with the user's normal permissions, for a job "
      "that will take more than a minute or so, or that they want run in parallel. name: short spoken name, e.g. "
      "'video edit'. task: a complete, self-contained brief (the worker cannot see this conversation).",
      {"name": str, "task": str})
async def start_worker(args):
    name = args["name"].strip().lower()
    old = workers.get(name)
    if old and old.state in LIVE:
        return text(f"A worker called {name} is already {old.state}. Message it, stop it, or pick another name.")
    if old:
        await stop(old)
    workers[name] = Worker(name, args["task"])
    return text(f"Started worker {name}. You'll be told when it finishes or needs the user.")


@tool("list_workers", "List workers: name, state (starting, running, waiting on the user, done, failed, stopped), minutes "
      "since start, session id (the user can take one over with: claude --resume <id>, after stop_worker), last message.", {})
async def list_workers(args):
    return text("\n".join(f"{w.name}: {w.state}, {round((time.time() - w.started) / 60)} min, session {w.session_id}, "
                          f"last said: {w.last[-300:]}" for w in workers.values()) or "No workers.")


@tool("message_worker", "Send a follow-up instruction, or the user's answer to its question, to a named worker.",
      {"name": str, "text": str})
async def message_worker(args):
    w = find(args["name"])
    if not w or w.runner.done():
        return text(f"No live worker by that name. Workers: {names()}.")
    if not w.client:
        return text(f"{w.name} is still starting; try again in a few seconds.")
    w.approved = False                         # a new instruction gets its own yes
    await w.client.query(args["text"])
    w.set("running")
    return text(f"Sent to {w.name}.")


@tool("stop_worker", "Stop a worker by name. Also do this before the user takes one over in a terminal.", {"name": str})
async def stop_worker(args):
    w = find(args["name"])
    if not w:
        return text(f"No worker by that name. Workers: {names()}.")
    await stop(w)
    return text(f"Stopped {w.name}. To take it over: claude --resume {w.session_id}")


TOOLS = [start_worker, list_workers, message_worker, stop_worker]
