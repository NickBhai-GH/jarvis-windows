"""The Claude side: one persistent Claude Code session, streamed, with a spoken permission gate."""
import asyncio
import json
import logging
import os
import re
import string
import time

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, PermissionResultAllow,
                              PermissionResultDeny, ResultMessage, SystemMessage, ToolResultBlock, ToolUseBlock,
                              UserMessage)
from claude_agent_sdk.types import (TERMINAL_TASK_STATUSES, StreamEvent, TaskNotificationMessage, TaskProgressMessage,
                                    TaskStartedMessage, TaskUpdatedMessage)

import config
import events
import pctools
from pctools import kwin
import worker
from mouth import SentenceSplitter

log = logging.getLogger("brain")


def persona():
    """persona.md with your name, honorific and city filled in."""
    with open(config.PERSONA_FILE) as f:
        return string.Template(f.read()).safe_substitute(
            USER_NAME=config.USER_NAME, HONORIFIC=config.HONORIFIC, CITY=config.CITY,
            LINES_FILE=os.path.join(config.JARVIS_DIR, "lines.md"))


READ_ONLY_TOOLS = {"Read", "Glob", "Grep", "LS", "WebSearch", "WebFetch", "ToolSearch", "TodoWrite", "Agent", "Task",
                   "Skill", "TaskOutput", "TaskStop", "ListMcpResourcesTool", "ReadMcpResourceTool",
                   "ReadMcpResourceDirTool", "ExitPlanMode", "EnterPlanMode", "AskUserQuestion", "Monitor"}
READ_VERBS = re.compile(r"(^|_)(get|list|search|read|fetch|query|find|describe|whoami|help|context|metadata|"
                        r"screenshot|shape_details|page_text|console|network|tabs_context|navigate|resize|"
                        r"shortcuts_list|select_browser|switch_browser)", re.I)
CHROME_GATED = {"file_upload", "upload_image", "shortcuts_execute"}  # javascript_tool is allowed
# Only count a word as a command when it sits where a command goes (start, after ; && | ( $( `, or after
# sudo/xargs/then/do), so a folder named "rm-old-notes" or a grep for "kill" doesn't trigger the gate.
_CMD = (r"(?:^|[;&|(\n`]|\$\(|\b(?:sudo|xargs|then|do|else|exec|nohup|time|env|nice|timeout|command|ionice)\s)"
        r"(?:\s*-\S+|\s*\d+[smh]?\b)*\s*(?:\S*/)?")        # options/durations after a wrapper; /bin/rm counts as rm
RISKY_BASH = re.compile(
    _CMD + r"(rm|rmdir|mv|dd|shred|mkfs\S*|sudo|su|kill|pkill|killall|shutdown|reboot|poweroff|"
    r"systemctl|rpm-ostree|truncate|crontab|ssh|scp|rsync)(?=\s|$)"
    r"|" + _CMD + r"flatpak\s+(uninstall|remove)\b|" + _CMD + r"git\s+(push|reset|clean|checkout\s+--)"
    r"|curl\b.*(-X\s*(POST|PUT|PATCH|DELETE)|\s(-d|--data|-F|--form)\b)|>\s*/(etc|usr|var|boot)"
    r"|\bfind\b[^;&|\n]*\s-(delete|exec(dir)?\s+(\S*/)?(rm|shred|mv))\b"           # find ... -delete / -exec rm
    r"|\b(shutil\.rmtree|os\.(remove|unlink|rmdir|removedirs)|\.unlink\(|\.rmdir\(|send2trash)"  # deletes inside scripts
    r"|>>?\s*~?\S*/\.(ssh/|bashrc|profile|bash_profile|config/(systemd|autostart)/|claude/settings)",  # shell writes to protected files
    re.I | re.M)
# PowerShell (Claude Code's shell on Windows): deletes, moves, kills, power, services, registry, policy, installs,
# web writes, and anything after the same command positions as above (cmd /c del counts too)
_PS = r"(?:^|[;&|({\n`]|\$\(|\b(?:sudo|cmd(?:\.exe)?\s+/[ck]|start-process|saps|start|invoke-expression|iex)\s)\s*(?:\S*[\\/])?"
RISKY_PS = re.compile(
    _PS + r"(remove-item|ri|rm|rmdir|rd|del|erase|move-item|mi|mv|move|rename-item|ren|clear-content|clc|"
    r"stop-process|spps|kill|taskkill|stop-computer|restart-computer|shutdown|logoff|"
    r"stop-service|set-service|remove-service|regedit|bcdedit|manage-bde|"
    r"set-itemproperty|sp|new-itemproperty|remove-itemproperty|rp|set-executionpolicy|set-mppreference|"
    r"add-mppreference|disable-\S+|enable-\S+|uninstall-\S+|install-\S+|"
    r"ssh|scp|rsync|robocopy|takeown|icacls|cipher|vssadmin|wsl)(\.exe)?(?=[\s;|)]|$)"
    r"|" + _PS + r"(reg(\.exe)?\s+(add|delete|import|restore|load|unload|copy)|sc(\.exe)?\s+(stop|delete|config|create)|"
    r"net\s+(user|localgroup|stop|share|use)|schtasks(\.exe)?\s+/(create|delete|change|run)|"
    r"(winget|choco|scoop)\s+(install|uninstall|remove|upgrade|update))\b"
    r"|" + _PS + r"git\s+(push|reset|clean|checkout\s+--)"
    r"|-verb\s+runas|\b(invoke-webrequest|iwr|invoke-restmethod|irm|curl|wget)\b[^;\n|]*-(method|body)\b"
    r"|\b(curl(\.exe)?)\b.*(-X\s*(POST|PUT|PATCH|DELETE)|\s(-d|--data|-F|--form)\b)"
    r"|\|\s*(iex|invoke-expression)\b"
    r"|\[(system\.)?io\.(file|directory)\]::(delete|move)|\.(delete|moveto)\(\)"
    r"|(>>?|\b(out-file|set-content|add-content|sc|ac))\s+[^;\n|]*(profile|\\startup\\|\.claude[\\/]settings)",
    re.I | re.M)
# Never, whatever anyone says: this PC's other drives hold the Linux install and its data
NEVER_PS = re.compile(_PS + r"(format-volume|format|clear-disk|initialize-disk|diskpart|remove-partition|new-partition|"
                      r"set-partition|resize-partition|set-disk|mkfs\S*|dd)(\.exe|\.com)?(?=[\s;|)]|$)", re.I)
SENSITIVE_PATHS = [os.path.join(config.HOME, p) for p in (".ssh", ".gnupg", ".config/systemd", ".config/autostart",
                                                            ".claude/settings.json", ".claude/settings.local.json",
                                                            ".claude/hooks", ".bashrc", ".profile",
                                                            "AppData/Roaming/Microsoft/Windows/Start Menu",
                                                            "Documents/WindowsPowerShell", "Documents/PowerShell")] + \
                  [os.path.join(config.JARVIS_DIR, f) for f in ("brain.py", "worker.py", "config.py", "config_local.py",
                                                                 "winctl.py")]
# ^ the gate, its rules and your allow-lists: an edit here needs your yes (one per request)


def risky_shell(tool_name, cmd):
    """The first risky part of a shell command, or None. On Windows both rule sets apply to both shells."""
    if tool_name == "PowerShell" or config.WINDOWS:
        return RISKY_PS.search(cmd) or RISKY_BASH.search(cmd)
    return RISKY_BASH.search(cmd)


def humanize(tool_name):
    name = tool_name.split("__")[-1]
    return re.sub(r"[_-]+", " ", name).strip()


def describe(tool_name, data):
    if tool_name in ("Bash", "PowerShell"):
        desc = data.get("description")
        if desc:                                   # one short spoken line, never a multi-line read-out
            desc = desc.splitlines()[0][:100]
            desc = desc[0].lower() + desc[1:]
        cmd = data.get("command", "")
        m = risky_shell(tool_name, cmd)
        runs = "curl" if m and "curl" in m.group(0) else os.path.basename(m.group(0).split()[-1].strip("`$();&|>")) if m else ""
        if desc:
            return desc + (f", which runs {runs}" if runs and runs.lower() not in desc.lower() else "")
        words = re.sub(r"^\s*cd\s+\S+\s*&&\s*", "", cmd).split()
        return f"run a {runs or (os.path.basename(words[0]) if words else 'shell')} command"
    if tool_name in ("Write", "Edit", "NotebookEdit"):
        return f"change the file {os.path.basename(data.get('file_path', ''))}"
    hints = [str(v) for k, v in data.items() if isinstance(v, str) and 0 < len(v) < 80
             and k in ("to", "recipient", "subject", "title", "name", "summary", "query", "url", "text")][:2]
    return humanize(tool_name) + (": " + ", ".join(hints) if hints else "")


def gate_hooks(gate):
    """Run the gate as a PreToolUse hook, which comes before Claude Code's allow rules. Calls an allow rule in
    your settings approves (say python3:* or ssh:*) never reach can_use_tool, so without this the gate never sees
    them. Steps the policy allows go on exactly as before; the rest are decided here, so nothing is asked twice."""
    async def hook(data, tool_use_id, context):
        name, args = data["tool_name"], data["tool_input"]
        if Brain.policy(name, args)[0] == "allow":
            return {}
        r = await gate(name, args, context)
        ok = isinstance(r, PermissionResultAllow)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow" if ok else "deny",
                                       "permissionDecisionReason": "" if ok else r.message}}
    return {"PreToolUse": [HookMatcher(hooks=[hook], timeout=600)]}   # long enough to wait for your answer


NOTES_TEMPLATE = """# Jarvis notes

Last updated: never

## Open threads

## Recent

## Working with {name}
"""

NOTES_PROMPT = """Housekeeping, not a message from {name}. Nothing you write here is spoken.

This Jarvis session is about to be closed and the next one starts fresh. Update the running summary file {path} so the next session knows what happened. Read it first, then rewrite it with Edit or Write. Keep this structure:

# Jarvis notes
Last updated: <YYYY-MM-DD HH:MM>

## Open threads
Things still in progress, follow-ups you promised, reminders with their times, anything {name} is waiting on. Remove threads that are finished.

## Recent
One line per thing {name} asked, newest first: "YYYY-MM-DD HH:MM  what they asked. What you did and how it turned out." Keep the last 40 lines. Fold older days into one line per day.

## Working with {name}
Short, lasting lessons about how they like Jarvis to work and quirks of their apps (what worked, what failed, what they corrected). Only add a lesson that will still matter next week.

Rules: only facts from this session or already in the file; nothing invented. Under 150 lines. No em dashes. When the file is written, reply with just: done."""


def read_state():
    try:
        with open(config.STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def write_state(**changes):
    st = read_state() | changes
    with open(config.STATE_FILE, "w") as f:
        json.dump(st, f)


def read_notes():
    try:
        with open(config.NOTES_FILE) as f:
            return f.read()
    except FileNotFoundError:
        return ""


async def summarize(session_id):
    """Resume a finished session just long enough for it to update the notes file."""
    if not os.path.exists(config.NOTES_FILE):
        with open(config.NOTES_FILE, "w") as f:
            f.write(NOTES_TEMPLATE.format(name=config.USER_NAME))
    notes_path = os.path.realpath(config.NOTES_FILE)

    async def only_notes(tool_name, data, context):
        if tool_name == "Read":
            return PermissionResultAllow(updated_input=data)
        if tool_name in ("Write", "Edit") and os.path.realpath(data.get("file_path", "")) == notes_path:
            return PermissionResultAllow(updated_input=data)
        return PermissionResultDeny(message="Only the notes file may be changed during housekeeping.")

    events.emit("notes", status="updating", session_id=session_id)
    opts = ClaudeAgentOptions(model=config.MODEL, effort="low", cwd=config.HOME, resume=session_id,
                              setting_sources=[], permission_mode="default", can_use_tool=only_notes,
                              allowed_tools=["Read"], max_turns=10)
    async with ClaudeSDKClient(options=opts) as c:
        await c.query(NOTES_PROMPT.format(path=config.NOTES_FILE, name=config.USER_NAME))
        async for m in c.receive_response():
            pass
    write_state(summarized=True)
    events.emit("notes", status="updated", session_id=session_id)
    log.info("notes updated from session %s", session_id)


def preview(content, n=300):
    """Short text version of a tool result for the dashboard."""
    if isinstance(content, str):
        return content[:n]
    parts = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text", ""))
        elif isinstance(c, dict) and c.get("type") == "image":
            parts.append("[image]")
    return " ".join(parts)[:n]


class Brain:
    """One long-lived Claude Code session. A single reader task consumes everything the session
    says, including turns it starts by itself (e.g. when a background timer finishes)."""

    def __init__(self, confirm, on_sentence, on_auto_turn, on_crash):
        self.confirm = confirm            # async (question) -> bool, asks the user out loud
        self.on_sentence = on_sentence
        self.on_auto_turn = on_auto_turn  # async (started: bool) for turns the user didn't ask for
        self.on_crash = on_crash
        self.client = None
        self.session_id = None
        self.turn_future = None
        self.in_turn = False
        self.auto_turn = False
        self.splitter = SentenceSplitter()
        self.tasks = {}                   # background tasks the session is waiting on
        self.finished = {}
        self.info = {}
        self.context = {}
        self.reader = None
        self.session_started = time.time()
        self.session_turns = 0
        self.approved = False          # you said yes once in this request: the rest of it doesn't ask again

    def _options(self):
        notes = read_notes().strip()
        append = persona()
        if notes:
            append += ("\n# Your notes from earlier Jarvis sessions\n"
                       "Each Jarvis session starts fresh. This file is what earlier sessions left you "
                       f"({config.NOTES_FILE}). Normal Claude Code memory applies as well.\n\n" + notes)
        return ClaudeAgentOptions(
            model=config.MODEL, effort=config.EFFORT, cwd=config.HOME, include_partial_messages=True,
            setting_sources=["user", "project", "local"], extra_args={"chrome": None},
            system_prompt={"type": "preset", "preset": "claude_code", "append": append},
            mcp_servers={"jarvis": pctools.server(worker.TOOLS)}, permission_mode="default", can_use_tool=self.gate, hooks=gate_hooks(self.gate),
            max_buffer_size=20 * 1024 * 1024)   # screenshots blew the 1 MB default and killed the reader

    async def start(self):
        self.session_id = None
        self.session_started = time.time()
        self.session_turns = 0
        self.client = ClaudeSDKClient(options=self._options())
        await self.client.connect()
        self.tasks.clear()
        self.reader = asyncio.create_task(self._read())
        events.emit("brain", status="connected", model=config.MODEL, effort=config.EFFORT)
        log.info("new session started")

        async def later():
            await asyncio.sleep(20)       # connectors take a while to come up
            await self.refresh_context()
        asyncio.create_task(later())

    async def stop(self):
        if self.reader and self.reader is not asyncio.current_task():   # on_crash runs inside the reader
            self.reader.cancel()
        if self.client:
            await self.client.disconnect()

    # ---------- permission gate ----------

    async def gate(self, tool_name, data, context):
        verdict, why = self.policy(tool_name, data)
        log.info("gate %s -> %s", tool_name, verdict)
        if verdict != "allow":
            events.emit("gate", tool=tool_name, verdict=verdict, reason=why)
        if verdict == "allow":
            return PermissionResultAllow(updated_input=data)
        if verdict == "deny":
            return PermissionResultDeny(message=why)
        if self.approved:              # one yes per request, not one per step
            return PermissionResultAllow(updated_input=data)
        question = f"Shall I {describe(tool_name, data)}?"
        if tool_name == "mcp__jarvis__press_keys":
            w = await kwin.active_window()
            question = f"Shall I press Enter in {w['title'] if w else 'the current window'}?"
        if await self.confirm(question):
            self.approved = True
            return PermissionResultAllow(updated_input=data)
        return PermissionResultDeny(message="The user said no (or didn't answer). Don't retry; tell them briefly.")

    @staticmethod
    def policy(tool_name, data):
        short = tool_name.split("__")[-1]
        if tool_name == "mcp__jarvis__press_keys" and re.search(r"\b(enter|return)\b", data.get("keys", ""), re.I):
            return "confirm", ""
        if tool_name in READ_ONLY_TOOLS or tool_name.startswith("mcp__jarvis__"):
            return "allow", ""
        if tool_name.startswith("mcp__claude-in-chrome__"):
            return ("confirm", "") if short in CHROME_GATED else ("allow", "")
        if tool_name in ("Bash", "PowerShell"):
            cmd = data.get("command", "")
            if config.WINDOWS and NEVER_PS.search(cmd):
                return "deny", "Disk formatting and partitioning are off limits on this PC. Don't retry; tell them briefly."
            return ("confirm", "") if risky_shell(tool_name, cmd) else ("allow", "")
        if tool_name in ("Write", "Edit", "NotebookEdit"):
            real = lambda p: os.path.normcase(os.path.realpath(os.path.expanduser(p)))   # Windows paths ignore case
            path = real(data.get("file_path") or data.get("notebook_path", ""))
            ok_roots = [real(config.HOME), real(config.RUNTIME_DIR)] + [real(d) for d in config.WRITE_OK_DIRS]
            outside = not any(path.startswith(r + os.sep) for r in ok_roots)
            sensitive = any(path.startswith(real(p)) for p in SENSITIVE_PATHS)
            return ("confirm", "") if outside or sensitive else ("allow", "")
        if tool_name.startswith("mcp__") and READ_VERBS.search(short):
            return "allow", ""
        return "confirm", ""

    # ---------- talking to the session ----------

    async def interrupt(self):
        try:
            await self.client.interrupt()
        except Exception:
            log.exception("interrupt failed")

    async def ask(self, prompt):
        self.turn_future = asyncio.get_running_loop().create_future()
        self.in_turn, self.auto_turn, self.approved = True, False, False
        await self.client.query(prompt)
        return await self.turn_future

    async def _read(self):
        try:
            async for m in self.client.receive_messages():
                try:
                    await self._handle(m)
                except Exception:
                    log.exception("handling %s", type(m).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("session reader died")
            events.emit("brain", status="crashed", error=str(e))
        if self.turn_future and not self.turn_future.done():
            self.turn_future.set_exception(RuntimeError("Claude session ended"))
        await self.on_crash()

    async def _begin_auto_turn(self):
        if not self.in_turn:
            self.in_turn, self.auto_turn, self.approved = True, True, False
            events.emit("turn_start", source="self", text="(started on its own, e.g. a timer or background job finished)")
            await self.on_auto_turn(True)

    async def _handle(self, m):
        if isinstance(m, StreamEvent):
            if m.parent_tool_use_id:              # a subagent talking to itself, not to the user
                return
            ev = m.event
            if ev.get("type") == "message_start":
                await self._begin_auto_turn()
            elif ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                for s in self.splitter.feed(ev["delta"]["text"]):
                    self.on_sentence(s)
            elif ev.get("type") == "content_block_stop":
                for s in self.splitter.flush():
                    self.on_sentence(s)
        elif isinstance(m, AssistantMessage):
            await self._begin_auto_turn()
            for b in m.content:
                if isinstance(b, ToolUseBlock):
                    log.info("tool: %s %s", b.name, json.dumps(b.input)[:200])
                    events.emit("tool_use", tool_id=b.id, name=b.name, input=json.dumps(b.input)[:1500],
                                sub=bool(m.parent_tool_use_id), background=bool(b.input.get("run_in_background")))
        elif isinstance(m, UserMessage) and isinstance(m.content, list):
            for b in m.content:
                if isinstance(b, ToolResultBlock):
                    events.emit("tool_result", tool_id=b.tool_use_id, error=bool(b.is_error), preview=preview(b.content))
        elif isinstance(m, TaskStartedMessage):
            self.tasks[m.task_id] = {"task_id": m.task_id, "description": m.description, "type": m.task_type,
                                     "started": time.time(), "last_tool": None, "tokens": 0}
            events.emit("task_started", **self.tasks[m.task_id])
        elif isinstance(m, TaskProgressMessage):
            t = self.tasks.setdefault(m.task_id, {"task_id": m.task_id, "description": m.description,
                                                  "started": time.time(), "type": None})
            t.update(last_tool=m.last_tool_name, tokens=(m.usage or {}).get("total_tokens", 0))
            events.emit("task_progress", **t)
        elif isinstance(m, TaskNotificationMessage):
            t = self.tasks.pop(m.task_id, None) or self.finished.pop(m.task_id, {})
            events.emit("task_done", task_id=m.task_id, status=m.status, summary=m.summary[:500],
                        description=t.get("description", ""))
        elif isinstance(m, TaskUpdatedMessage):
            # the finished notice (with its summary) usually follows; just stop showing it as running
            if (m.status or m.patch.get("status")) in TERMINAL_TASK_STATUSES and m.task_id in self.tasks:
                self.finished[m.task_id] = self.tasks.pop(m.task_id)
                events.emit("task_progress", task_id=m.task_id, status=m.status or m.patch.get("status"))
        elif isinstance(m, SystemMessage) and m.subtype == "init":
            tools = m.data.get("tools", [])
            self.info = {"model": m.data.get("model"), "tools": len(tools),
                         "chrome_tools": len([t for t in tools if "claude-in-chrome" in t]),
                         "mcp": [{"name": s.get("name"), "status": s.get("status")} for s in m.data.get("mcp_servers", [])]}
            events.emit("brain_info", **self.info)
        elif isinstance(m, ResultMessage):
            for s in self.splitter.flush():
                self.on_sentence(s)
            self.session_id = m.session_id
            self.session_turns += 1
            write_state(session_id=m.session_id, ts=time.time(), summarized=False)
            usage = m.usage or {}
            events.emit("turn_end", duration_s=(m.duration_ms or 0) / 1000, error=m.is_error,
                        steps=m.num_turns, cost=m.total_cost_usd, session_id=m.session_id,
                        tokens_in=usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0), tokens_out=usage.get("output_tokens", 0))
            log.info("turn done in %.1fs", (m.duration_ms or 0) / 1000)
            was_auto = self.auto_turn
            self.in_turn = self.auto_turn = False
            if self.turn_future and not self.turn_future.done():
                self.turn_future.set_result(m)
            if was_auto:
                await self.on_auto_turn(False)
            asyncio.create_task(self.refresh_context())

    async def refresh_mcp(self):
        try:
            st = await self.client.get_mcp_status()
            self.info["mcp"] = [{"name": x.get("name"), "status": x.get("status")} for x in st.get("mcpServers", [])]
            events.emit("brain_info", **self.info)
        except Exception:
            log.debug("mcp status unavailable", exc_info=True)

    async def refresh_context(self):
        await self.refresh_mcp()
        try:
            u = await self.client.get_context_usage()
            self.context = u if isinstance(u, dict) else getattr(u, "__dict__", {})
            events.emit("context", **{k: v for k, v in self.context.items()
                                      if isinstance(v, (int, float, str)) and k != "categories"})
        except Exception:
            log.debug("context usage unavailable", exc_info=True)
