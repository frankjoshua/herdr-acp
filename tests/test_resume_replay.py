"""A resumed Pi/OMP session (or Codex rollout) found after the reader started must not be replayed.

CIO-62: `omp --resume` in a bridged pane replayed a 1.7k-message session into the Buzz channel
(~300 posts/min until the relay answered 429). The reader is built before the resumed process
opens its session file, so it finds the file later and read it from byte 0.
Run: python3 tests/test_resume_replay.py
"""

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from herdr_acp import reader  # noqa: E402


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"


def pi_line(t: float, text: str) -> str:
    return json.dumps({"type": "message", "timestamp": iso(t),
                       "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}) + "\n"


def codex_line(t: float, text: str) -> str:
    return json.dumps({"timestamp": iso(t), "type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "AgentMessage", "id": text, "content": [{"type": "text", "text": text}]}}}) + "\n"


def texts(updates) -> list[str]:
    return [u.content.text for u in updates]


def check(kind: str, cls, finder: str, line, resumed: bool = True, quiet: bool = False) -> None:
    now = time.time()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sessions", "s.jsonl")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:  # yesterday's session, about to be resumed
            f.write(json.dumps({"type": "session", "timestamp": iso(now - 86400 if resumed else now - 1)}) + "\n")
            if resumed:
                f.write(line(now - 86400, "old 1") + line(now - 3600, "old 2"))
        found = iter([None, path])  # not open yet when the reader is built, open on the next look
        setattr(reader, finder, lambda *a: next(found))
        setattr(reader, "proc_info", lambda pid: {"started": now - 5})  # the resumed process started 5s ago
        r = cls(os.getpid()) if kind == "codex" else cls(os.getpid(), "omp")
        assert r.path is None
        if quiet:  # found before the resumed process writes anything: no line stamped after its start
            r.checked = 0.0
            assert asyncio.run(r.poll()) == [] and r.path == path, kind
        with open(path, "a") as f:
            f.write(line(now, "new 1"))
        r.checked = 0.0  # skip the 2s discovery throttle
        got = texts(asyncio.run(r.poll()))
        assert got == ["new 1"], f"{kind} (resumed={resumed}, quiet={quiet}): {got}"
        with open(path, "a") as f:
            f.write(line(now + 1, "new 2"))
        assert texts(asyncio.run(r.poll())) == ["new 2"], kind
    print(f"ok {kind} resumed={resumed} quiet={quiet}")


if __name__ == "__main__":
    check("pi", reader.PiSession, "pi_session_for", pi_line)
    check("codex", reader.CodexRollout, "codex_rollout_for", codex_line)
    check("pi", reader.PiSession, "pi_session_for", pi_line, resumed=False)  # a new session: its first message is news
    check("codex", reader.CodexRollout, "codex_rollout_for", codex_line, resumed=False)
    check("pi", reader.PiSession, "pi_session_for", pi_line, quiet=True)
    check("codex", reader.CodexRollout, "codex_rollout_for", codex_line, quiet=True)
