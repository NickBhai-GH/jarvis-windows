"""Run: .venv/bin/python test_workers.py  (checks worker permission routing and finish states, no Claude needed)"""
import asyncio
import brain, worker

worker.events.emit = lambda *a, **k: None                     # keep the real event log clean
asked = []


async def say_yes(q):
    asked.append(q)
    return True

worker.confirm = say_yes
verdicts = []                                                  # what the stubbed judge will say, in order


async def fake_judge(task, tool_name, data):
    return verdicts.pop(0)

worker.judge = fake_judge
w = worker.Worker.__new__(worker.Worker)
w.name, w.state, w.session_id, w.last = "test", "running", None, ""
w.approved = False

w.task = "draw a map"
verdicts.append((True, "part of the job"))
r = asyncio.run(w.gate("mcp__notes__add_block", {"title": "x"}, None))
assert r.behavior == "allow" and not asked                    # the judge approved it, the user not asked
verdicts.append((False, "it sends an email"))
r = asyncio.run(w.gate("Write", {"file_path": "/etc/hosts", "content": ""}, None))
assert r.behavior == "allow" and asked == ["Sir, the test worker would like to change the file hosts. Shall I allow it?"]
verdicts.append((False, "deletes things"))
r = asyncio.run(w.gate("Bash", {"command": "rm -r /tmp/x"}, None))
assert r.behavior == "allow" and len(asked) == 1 and verdicts  # one yes covers the rest: no judge, no question

# Main Jarvis: one yes per request, and the question is one short line even for a heredoc with no description
b = brain.Brain.__new__(brain.Brain)
b.approved = False
b.confirm = say_yes
asked.clear()
heredoc = {"command": "cd ~/jarvis && python3 - <<'EOF'\np='widget.py'\nprint(1)\nEOF\nsystemctl --user restart x"}
assert brain.describe("Bash", heredoc) == "run a systemctl command", brain.describe("Bash", heredoc)  # names the risky part
assert asyncio.run(b.gate("Bash", heredoc, None)).behavior == "allow"
assert asyncio.run(b.gate("Bash", {"command": "rm -r /tmp/y"}, None)).behavior == "allow"
assert asked == ["Shall I run a systemctl command?"], asked     # asked once, not twice
b.approved = False                                             # what ask() does when the next request starts
asyncio.run(b.gate("Bash", {"command": "rm -r /tmp/z", "description": "Delete temp z\nand more"}, None))
assert asked[-1] == "Shall I delete temp z, which runs rm?", asked
assert w.state == "running"
assert worker.outcome("Done. Saved to ~/x.md", False) == "done"
assert worker.outcome("NEED USER: 2x or 4x?", False) == "waiting"
assert worker.outcome("", True) == "failed"
print("ok")
