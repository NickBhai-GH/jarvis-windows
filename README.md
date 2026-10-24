# Jarvis

A voice assistant for your Windows 11 PC that talks like J.A.R.V.I.S. from the Iron Man films. Say **"Hey Jarvis"**, wait for the chime, talk. The brain is a long-running Claude Code session (through the Claude Agent SDK), so it can do anything Claude Code can: run commands, edit files, search the web, drive Chrome. On top of that it gets its own desktop tools: open apps, focus windows, take screenshots, click, scroll, type, control media and volume, and draw on your screen to show you where things are. Longer jobs go to background workers so it stays free to talk.

How it fits together:

- `ears.py`: microphone, "Hey Jarvis" wake word (openWakeWord), end-of-speech detection, speech to text (faster-whisper).
- `brain.py`: one persistent Claude Code session, the persona, and the safety gate that asks you out loud before risky actions.
- `mouth.py`: text to speech (Kokoro by default, or a Piper voice), cut off the moment you talk over it.
- `pctools.py` + `winctl.py`: the desktop tools (`winctl.py` is the Windows part: windows, focus, monitors, mouse, keyboard, media, volume, toasts, Start menu apps), served to Claude as an in-process MCP server called `jarvis`.
- `worker.py`: background workers, each its own Claude Code session.
- `jarvis.py`: the main loop. `dashboard.py` + `dashboard.html`: a local web dashboard. `widget.py`: the on-screen status line. `overlay.py`: draws rings, arrows and labels on screen. `design.py`: their shared look. `events.py`: the activity log.
- `jarvis-os/`: JARVIS OS, an optional full-screen dashboard that sits under your windows like a wallpaper (its own README).
- `persona.md`: the system prompt, a template filled in from your config. `lines.md`: real JARVIS lines by situation, used to tune the voice.

## Requirements

- **Windows 11** (10 should work, untested).
- **Claude Code** installed and logged in (`claude`, then log in with your Claude subscription). Jarvis uses your normal Claude Code login, settings, memory and MCP servers.
- **Python 3.12** from python.org.
- A microphone. Headphones are recommended if you want to interrupt it by talking over it.
- Optional: the Claude in Chrome extension, so it can work in your browser; `nvidia-smi` (comes with the NVIDIA driver) for the GPU bar in JARVIS OS.

## Install

In PowerShell:

```
git clone https://github.com/NickBhai-GH/jarvis-windows.git $HOME\jarvis
cd $HOME\jarvis
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# Kokoro voice model
mkdir models
curl.exe -L -o models\kokoro-v1.0.onnx https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl.exe -L -o models\voices-v1.0.bin https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin

# "Hey Jarvis" wake word and voice detection models
.venv\Scripts\python -c "import openwakeword.utils as u; u.download_models(['hey_jarvis'])"
```

The whisper models download by themselves on first run.

## Configure

Settings live in `config.py`. Put your own values in `config_local.py` (it is gitignored and overrides `config.py`), for example:

```
USER_NAME = "Pepper"
HONORIFIC = "ma'am"
CITY = "Manchester"
WRITE_OK_DIRS = [r"D:\Projects"]
VOCAB = "Jarvis, Pepper, Manchester, Spotify, Discord, Valorant."
```

| Setting | What it does |
|---|---|
| `USER_NAME` | Your name, as Jarvis refers to you |
| `HONORIFIC` | How it addresses you: "sir", "ma'am", "boss" |
| `CITY` | City for the weather line in the morning greeting (wttr.in, use + for spaces) |
| `WRITE_OK_DIRS` | Folders outside your home it may write to without asking |
| `VOCAB` | Names and words whisper keeps mishearing |
| `VOICE` | Kokoro voice: bm_lewis, bm_george, bm_daniel, bm_fable |
| `VOICE_ENGINE` / `PIPER_MODEL` | Switch to your own Piper voice (see below) |
| `MODEL` / `EFFORT` | Claude model and effort level |
| `JUDGE_MODEL` | Cheaper model that approves routine worker steps |
| `POLITE` | Hold unprompted speech while you are on a call (`POLITE_*` tune it) |
| `WAKE_THRESHOLD` | Raise if it wakes on its own, lower if it ignores you |
| `END_SILENCE_S` | Raise if it cuts you off mid-sentence |
| `MIC_DEVICE` | None for the system default input |
| `WIDGET_SCREEN` | Which monitor the widget sits on |

Times in the greetings use your system clock and time zone.

## Run

Double-click `start-jarvis.cmd`. It starts Jarvis in a hidden console (so the Claude processes it spawns never flash a window), the widget and JARVIS OS. The log is `logs\jarvis.log`.

To start everything at login, run once: `powershell -ExecutionPolicy Bypass -File autostart.ps1` (it adds three scheduled tasks; `-Remove` takes them out again). Restart after changing the config: end the Python processes in Task Manager, then run `start-jarvis.cmd` again.

Dashboard: **http://127.0.0.1:8765** (this PC only). It shows what Jarvis is doing, background jobs, history with every step, what it heard, and has Yes/No buttons and a box for typed commands.

## Controls

| Do | How |
|---|---|
| Talk | "Hey Jarvis", or `jarvisctl listen` |
| Interrupt / cancel | Talk over it, say "Hey Jarvis", or `jarvisctl stop` |
| Type instead of talk | `jarvisctl say open spotify` |
| Answer a "Shall I...?" | Say yes or no, or `jarvisctl yes` / `jarvisctl no` |
| Voice test | `jarvisctl speak hello` |
| Status | `jarvisctl status` |
| Fresh session | Say "new session" |

`jarvisctl` runs as `.venv\Scripts\python jarvisctl <command>` from the Jarvis folder. On Windows it talks to Jarvis over 127.0.0.1:8766 with a token file only you can read.

Hotkeys: make two Start menu shortcuts whose target is `<jarvis>\.venv\Scripts\pythonw.exe "<jarvis>\jarvisctl" listen` (and `... stop`), then give them a shortcut key in their Properties (say Ctrl+Alt+J and Ctrl+Alt+Shift+J).

After you answer, it listens for 5 seconds so you can follow up without the wake word.

## Sessions and notes

Each conversation is a fresh Claude session. A new one starts after 30 minutes idle (`SESSION_IDLE_RESET_MIN`), or when you say "new session". Before a session closes it writes a running summary into `notes.md` (open threads, a dated log of what you asked, lessons about working with you), and the next session reads it first. `notes.md` is created on first use (see `notes.example.md`), stays out of git, and you can edit it by hand.

## Customising the persona

`persona.md` is the system prompt. `$USER_NAME`, `$HONORIFIC`, `$CITY` and `$LINES_FILE` are filled in from the config. Change anything you like. The speaking style is built from real JARVIS lines, collected by situation in `lines.md`. Every message gets a time tag (`[Mon 28 Sep, 07:42, first today]`), so it greets you on your first message of the day, says welcome back after a few hours away, and notices when it is past midnight.

## Widget

A thin glowing "emitter line" at the bottom centre of a monitor, always on top, click-through and never focused. No box, no blur: at rest it draws nothing. When something happens the line lights up, one colour per state, and the words float above it:

- **Listening**: white, rippling with your voice, your words live above it.
- **Thinking**: cyan, a spark sweeping to and fro, with the step it's on.
- **Speaking**: blue, rippling with Jarvis's voice, each sentence as it says it.
- **Waiting for yes/no**: orange, breathing. **Update ready** (held while you're on a call): green, breathing slower.
- **Jarvis not running**: a dim red line. Background work at rest: a faint spark drifting along the line.

It fades away a couple of seconds after Jarvis finishes. It looks best on a dark wallpaper with a line of its own at that spot: set `LINE_W` (length) and `LINE_UP` (pixels above the bottom edge) at the top of `widget.py` to sit exactly on it. The words use Rajdhani and Share Tech Mono if you have them installed (both free on Google Fonts), else your normal sans and monospace.

| Listening | Thinking |
|---|---|
| ![Listening](docs/widget-listening.png) | ![Thinking](docs/widget-thinking.png) |
| **Speaking** | **At rest** |
| ![Speaking](docs/widget-speaking.png) | ![At rest](docs/widget-idle.png) |

It sits just above the taskbar and is started by `start-jarvis.cmd`. Windows has no compositor blur for it, so there is none. Pick the monitor with `WIDGET_SCREEN`. Colours, fonts and motion live in `design.py`.

## JARVIS OS (optional dashboard)

A live dashboard for a second monitor that sits under every window like a wallpaper: Slack, Asana, today's calendar,
Jarvis and its workers, Steam, Twitch, Discord, time, weather, CPU/GPU and what's playing, around a day ring, with a
work mode and a game mode. Every feed is read-only and optional (missing credentials show sample data); Discord is off
by default. Setup is in [`jarvis-os/README.md`](jarvis-os/README.md).

![JARVIS OS in work mode](docs/jarvis-os-work.png)

## Show me where

Ask "where's the graphics setting?" or "how do I open the map?" and it takes a screenshot, then draws on your screen (rings, arrows, boxes, numbered steps; see `docs/overlay-preview.png`) while it talks you through it. It doesn't click unless you ask.

- Tools: `annotate` (shapes in the latest screenshot's pixels, same mapping as `click_at`) and `clear_annotations`.
- `overlay.py` is a topmost, click-through window per monitor that never takes focus. Exclusive-fullscreen games can't be drawn over; borderless windowed works.
- Try it: `.venv\Scripts\python overlay.py '{"duration": 5, "shapes": [{"type": "ring", "x": 960, "y": 540, "radius": 60, "label": "Here", "step": 1}]}'`

## Workers

For a job that takes more than a minute or so, or when you say "and also have Y going", Jarvis starts a **worker**: a separate Claude Code session in the background with a short name ("the video edit"), and stays free for you. Several can run at once.

- "What are the workers doing?", "tell the video one to use 2x", "stop the report": `list_workers` / `message_worker` / `stop_worker`.
- When one finishes, or needs you, Jarvis says so in a sentence.
- Permissions: a worker's risky steps go to a judge model (`JUDGE_MODEL`) first, which approves routine steps that fit the brief and passes the rest to you as a spoken question. Pressing Enter always comes to you.
- Take one over: ask Jarvis to stop it, then `claude --resume <session id>` in a terminal.

## Not talking over your calls

Not on Windows yet: call detection reads PulseAudio on Linux. Things Jarvis says on its own are spoken straight away.

## Safety gate

The gate is `policy` in `brain.py`. It runs as a PreToolUse hook, before Claude Code's own allow rules, so an "always allow" rule in your settings can't skip it.

- Allowed without asking: reading, searching, the desktop tools (except Enter), Chrome browsing and clicking, safe Bash, file edits inside your home folder and `WRITE_OK_DIRS`.
- Asks out loud first: deleting or moving files (Remove-Item, del, deletes inside scripts), stopping processes and services, registry and policy changes, installs (winget, choco, scoop), scheduled tasks, ssh/scp, git push, POST requests, writes to shell and Claude settings files, edits to the gate's own code and config, and pressing Enter (that is how most apps send).
- **One yes per request**: once you say yes, the rest of that request doesn't ask again. Questions are one short line and name the risky command ("Shall I restart the widget, which runs systemctl?").
- A yes only counts at the start of your answer, so a "yes" in the middle of something you said to someone else doesn't approve anything. No answer counts as no.
- Never allowed: formatting or partitioning a disk (Format-Volume, diskpart, Clear-Disk and friends). It refuses outright.
- Clicks are not gated, so the persona tells it to ask before clicking Send, Post, Buy or Delete. Clicking and typing refuse if another window took focus since the last screenshot, or the screenshot is over a minute old.

The command rules are pattern matching, not a sandbox. Read `policy` before you trust it with anything important, and add your own rules there.

## Voice

Kokoro works out of the box with British voices (`bm_lewis` is the default). If you want a closer JARVIS sound, you can train your own Piper voice (see the Piper project's training guide), then set `VOICE_ENGINE = "piper"` and `PIPER_MODEL` to the `.onnx` file with its `.onnx.json` next to it. No trained voice or film audio ships with this repo.

## Tests

```
.venv\Scripts\python test_gate.py        # which commands ask, the gate hook, the wrong-window guard
.venv\Scripts\python test_workers.py     # worker permission routing, one yes per request
.venv\Scripts\python test_annotate.py    # screenshot-to-desktop mapping for the overlay
.venv\Scripts\python test_time_tag.py    # the greeting time tags
.venv\Scripts\python test_voice.py "Good evening."   # writes logs/voice-kokoro.wav (and piper if set)
```

## Licence

MIT, see `LICENSE`.
