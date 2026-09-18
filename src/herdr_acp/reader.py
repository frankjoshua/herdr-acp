"""Readers: "what happened in the pane since the prompt", as ACP session updates.

ClaudeTranscript tails <cfg>/projects/<cwd>/<session>.jsonl from a byte offset.
CodexRollout tails <CODEX_HOME>/sessions/YYYY/MM/DD/rollout-*.jsonl the same way.
PiSession tails a Pi-format session (Pi, OMP): <agent dir>/sessions/<cwd>/<ts>_<id>.jsonl.
ScreenDiff diffs successive plain-text screen snapshots (floor for shells / unknown agents).
All expose `async poll() -> list[update]`. `claude_transcript_for(pid)` / `codex_rollout_for(pid)` /
`pi_session_for(pid, kind)` find the file from the agent process itself (`proc_info`).
"""

import difflib
import glob
import json
import logging
import os
import re
import time

from acp import (
    start_tool_call,
    text_block,
    tool_content,
    update_agent_message_text,
    update_agent_thought_text,
    update_tool_call,
    update_user_message_text,
)

log = logging.getLogger("herdr-acp")
PROC = "/proc"  # discovery is process-first (env, cwd, open files); the only glob is the cwd-scoped
# fallback for a Codex that has not opened its rollout yet.

TOOL_KIND = {
    "Bash": "execute", "Read": "read", "Edit": "edit", "Write": "edit", "NotebookEdit": "edit",
    "MultiEdit": "edit", "Grep": "search", "Glob": "search", "WebFetch": "fetch",
    "WebSearch": "fetch", "Agent": "think", "Task": "think",
}

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")


def proc_info(pid: int) -> dict:
    """What the agent process tells us about itself: env, cwd, open files, start time."""
    base = f"{PROC}/{pid}"
    with open(f"{base}/environ", "rb") as f:
        env = dict(kv.split("=", 1) for kv in f.read().decode("utf-8", "replace").split("\0") if "=" in kv)
    fds = []
    for fd in os.listdir(f"{base}/fd"):
        try:
            fds.append(os.readlink(f"{base}/fd/{fd}"))
        except OSError:
            pass
    return {"env": env, "cwd": os.readlink(f"{base}/cwd"), "fds": fds, "started": os.stat(base).st_mtime}


def claude_transcript(cfg_dir: str, session: dict) -> str:
    """Claude Code keeps <cfg>/sessions/<pid>.json (sessionId, cwd); the transcript is
    <cfg>/projects/<cwd with every non-alphanumeric as '-'>/<sessionId>.jsonl."""
    return os.path.join(cfg_dir, "projects", re.sub(r"[^A-Za-z0-9]", "-", session["cwd"]), session["sessionId"] + ".jsonl")


def claude_transcript_for(pid: int) -> str:
    cfg = proc_info(pid)["env"].get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    with open(os.path.join(cfg, "sessions", f"{pid}.json")) as f:
        return claude_transcript(cfg, json.load(f))


def _tool_title(name: str, inp: dict) -> str:
    arg = inp.get("command") or inp.get("file_path") or inp.get("path") or inp.get("pattern") or inp.get("url") \
        or inp.get("title") or inp.get("description") or inp.get("prompt") or ""
    arg = str(arg).splitlines()[0] if arg else ""
    return f"{name}: {arg[:120]}" if arg else name


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def updates_from_entry(entry: dict) -> list:
    """Map one transcript line to zero or more ACP session updates."""
    if entry.get("isSidechain"):
        return []  # ponytail: subagent traffic skipped; surface as nested tool_call later if wanted
    kind = entry.get("type")
    content = (entry.get("message") or {}).get("content")
    if kind == "user" and isinstance(content, str):  # a human typed in the pane
        text = content.strip()
        # skip slash-command wrappers (<command-name>…) and interruption markers
        return [update_user_message_text(text)] if text and not text.startswith(("<", "[Request interrupted")) else []
    if kind not in ("user", "assistant") or not isinstance(content, list):
        return []
    out = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if kind == "assistant" and t == "text" and b.get("text"):
            out.append(update_agent_message_text(b["text"]))
        elif kind == "assistant" and t == "thinking" and b.get("thinking"):
            out.append(update_agent_thought_text(b["thinking"]))
        elif kind == "assistant" and t == "tool_use":
            name, inp = b.get("name", "tool"), b.get("input") or {}
            out.append(start_tool_call(
                b["id"], _tool_title(name, inp), kind=TOOL_KIND.get(name, "other"),
                status="in_progress", raw_input=inp,
            ))
        elif kind == "user" and t == "tool_result":
            text = _result_text(b.get("content"))
            out.append(update_tool_call(
                b["tool_use_id"], status="failed" if b.get("is_error") else "completed",
                content=[tool_content(text_block(text[:4000]))] if text else None,
            ))
    return out


def _new_lines(path: str, offset: int) -> list[bytes]:
    """Complete lines appended since `offset`; a torn trailing line waits for the next poll."""
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    if not data.endswith(b"\n"):
        data = data[: data.rfind(b"\n") + 1]
    return data.splitlines(keepends=True)


def _parse_lines(path: str, lines: list[bytes], offset: int, to_updates) -> tuple[list, int]:
    out = []
    for line in lines:
        try:
            out.extend(to_updates(json.loads(line.decode("utf-8", "replace"))))
        except Exception as e:  # one bad line (torn json, odd shape) must not cost the rest
            log.warning("transcript %s: skipped line: %r", path, e)
        offset += len(line)
    return out, offset


class ClaudeTranscript:
    """Tail one known transcript file. It may not exist yet (Claude creates it on the first turn)."""

    def __init__(self, path: str):
        self.path = path
        self.offset = os.path.getsize(path) if os.path.exists(path) else 0

    async def poll(self) -> list:
        if not os.path.exists(self.path):
            return []
        out, self.offset = _parse_lines(self.path, _new_lines(self.path, self.offset), self.offset, updates_from_entry)
        return out


# ---- Codex: <CODEX_HOME>/sessions/YYYY/MM/DD/rollout-*.jsonl ---------------------------------
# Codex holds its rollout open, so /proc/<pid>/fd names it exactly. Before the first turn no
# file is open yet; then it is the newest rollout in that Codex home whose session_meta.cwd is
# the process cwd and which is younger than the process. `event_msg`/`item_completed` items are
# the clean record of what happened; raw responses, token counts, sub-agent chatter are ignored.

def codex_rollout(cwd: str, home: str, newer_than: float = 0.0) -> str | None:
    best = None
    for f in glob.glob(f"{home}/sessions/*/*/*/rollout-*.jsonl"):
        m = os.path.getmtime(f)
        if m <= newer_than or (best and m <= best[0]):
            continue
        try:
            with open(f) as fh:
                meta = json.loads(fh.readline())
        except (OSError, ValueError) as e:
            log.debug("codex rollout %s skipped: %r", f, e)
            continue
        if meta.get("type") == "session_meta" and (meta.get("payload") or {}).get("cwd") == cwd:
            best = (m, f)
    return best[1] if best else None


def _item_text(content) -> str:
    return "".join(c.get("text", "") for c in (content or []) if isinstance(c, dict) and c.get("type") in ("text", "Text"))


def codex_updates(entry: dict) -> list:
    """Map one rollout line to ACP updates (only `item_completed` events carry anything)."""
    p = entry.get("payload") or {}
    if entry.get("type") != "event_msg" or p.get("type") != "item_completed":
        return []
    it = p.get("item") or {}
    kind, iid = it.get("type"), it.get("id", "")
    if kind == "UserMessage":
        text = _item_text(it.get("content")).strip()
        return [update_user_message_text(text)] if text else []
    if kind == "AgentMessage":
        text = _item_text(it.get("content"))
        return [update_agent_message_text(text)] if text.strip() else []
    if kind == "Reasoning":
        text = "\n".join(it.get("summary_text") or [])
        return [update_agent_thought_text(text)] if text.strip() else []
    if kind == "CommandExecution":
        cmd = it.get("command") or []
        cmd = cmd[2] if len(cmd) == 3 and cmd[1] in ("-lc", "-c") else " ".join(cmd)
        out = (it.get("stdout") or "")[:4000]
        failed = it.get("status") == "failed" or (it.get("exit_code") not in (None, 0))
        return [start_tool_call(iid, f"exec: {cmd[:120]}", kind="execute", status="in_progress", raw_input={"command": cmd}),
                update_tool_call(iid, status="failed" if failed else "completed",
                                 content=[tool_content(text_block(out))] if out else None)]
    if kind == "FileChange":
        paths = ", ".join(os.path.basename(x) for x in (it.get("changes") or {}))
        return [start_tool_call(iid, f"edit: {paths[:120]}", kind="edit", status="in_progress"),
                update_tool_call(iid, status="completed")]
    if kind == "McpToolCall":
        res = _item_text((it.get("result") or {}).get("content"))[:4000]
        return [start_tool_call(iid, f"{it.get('server')}.{it.get('tool')}", kind="other", status="in_progress", raw_input=it.get("arguments")),
                update_tool_call(iid, status="failed" if it.get("status") == "failed" else "completed",
                                 content=[tool_content(text_block(res))] if res else None)]
    return []


def codex_rollout_for(pid: int) -> str | None:
    p = proc_info(pid)
    for f in p["fds"]:
        if "/sessions/" in f and os.path.basename(f).startswith("rollout-") and f.endswith(".jsonl"):
            return f
    return codex_rollout(p["cwd"], p["env"].get("CODEX_HOME") or os.path.expanduser("~/.codex"), p["started"])


class CodexRollout:
    def __init__(self, pid: int, path: str | None = None):
        self.pid = pid
        self.path = path or codex_rollout_for(pid)  # None until Codex opens its rollout
        self.offset = os.path.getsize(self.path) if self.path else 0
        self.checked = 0.0

    async def poll(self) -> list:
        if not self.path and time.monotonic() - self.checked >= 2:
            self.checked = time.monotonic()
            self.path, self.offset = codex_rollout_for(self.pid), 0
        if not self.path:
            return []
        out, self.offset = _parse_lines(self.path, _new_lines(self.path, self.offset), self.offset, codex_updates)
        return out


# ---- Pi / OMP: <agent dir>/sessions/<cwd mangled>/<timestamp>_<session id>.jsonl -------------
# Pi's session format (pi.dev/docs/latest/session-format), which OMP shares. The agent holds the
# file open once the first message exists, so /proc/<pid>/fd names it; before that, the newest
# session under the agent dir whose `session` header cwd is the process cwd and which is younger
# than the process. Agent dir: $PI_CODING_AGENT_DIR, else ~/.<kind>/agent; sessions dir may be
# overridden by $PI_CODING_AGENT_SESSION_DIR.

PI_TOOL_KIND = {"bash": "execute", "eval": "execute", "read": "read", "write": "edit", "edit": "edit",
                "grep": "search", "glob": "search", "find": "search", "fetch": "fetch", "web": "fetch"}


def pi_session(cwd: str, sessions_dir: str, newer_than: float = 0.0) -> str | None:
    best = None
    for f in glob.glob(f"{sessions_dir}/*/*.jsonl"):
        m = os.path.getmtime(f)
        if m <= newer_than or (best and m <= best[0]):
            continue
        try:
            with open(f) as fh:
                for _ in range(3):  # `session` header is within the first lines
                    head = json.loads(fh.readline() or "{}")
                    if head.get("type") == "session":
                        break
        except (OSError, ValueError) as e:
            log.debug("pi session %s skipped: %r", f, e)
            continue
        if head.get("type") == "session" and head.get("cwd") == cwd:
            best = (m, f)
    return best[1] if best else None


def pi_session_for(pid: int, kind: str) -> str | None:
    p = proc_info(pid)
    for f in p["fds"]:
        if "/sessions/" in f and f.endswith(".jsonl"):
            return f
    agent_dir = p["env"].get("PI_CODING_AGENT_DIR") or os.path.expanduser(f"~/.{kind}/agent")
    sessions = p["env"].get("PI_CODING_AGENT_SESSION_DIR") or os.path.join(agent_dir, "sessions")
    return pi_session(p["cwd"], sessions, p["started"])


def pi_updates(entry: dict) -> list:
    """Map one Pi-format session line to ACP updates."""
    if entry.get("type") != "message":
        return []
    m = entry.get("message") or {}
    role, content = m.get("role"), m.get("content")
    if role == "user":
        text = content if isinstance(content, str) else "".join(b.get("text", "") for b in content or [] if b.get("type") == "text")
        return [update_user_message_text(text.strip())] if text.strip() else []
    if role == "toolResult":
        text = content if isinstance(content, str) else _result_text(content)
        return [update_tool_call(m.get("toolCallId", ""), status="failed" if m.get("isError") else "completed",
                                 content=[tool_content(text_block(text[:4000]))] if text else None)]
    if role != "assistant" or not isinstance(content, list):
        return []
    out = []
    for b in content:
        t = b.get("type")
        if t == "text" and b.get("text"):
            out.append(update_agent_message_text(b["text"]))
        elif t == "thinking" and b.get("thinking"):
            out.append(update_agent_thought_text(b["thinking"]))
        elif t == "toolCall":
            name, args = b.get("name", "tool"), b.get("arguments") or {}
            out.append(start_tool_call(b.get("id", ""), _tool_title(name, args), kind=PI_TOOL_KIND.get(name, "other"),
                                       status="in_progress", raw_input=args))
    return out


class PiSession:
    def __init__(self, pid: int, kind: str = "omp", path: str | None = None):
        self.pid, self.kind = pid, kind
        self.path = path or pi_session_for(pid, kind)  # None until the first message creates it
        self.offset = os.path.getsize(self.path) if self.path else 0
        self.checked = 0.0

    async def poll(self) -> list:
        if not self.path and time.monotonic() - self.checked >= 2:
            self.checked = time.monotonic()
            self.path, self.offset = pi_session_for(self.pid, self.kind), 0
        if not self.path:
            return []
        out, self.offset = _parse_lines(self.path, _new_lines(self.path, self.offset), self.offset, pi_updates)
        return out


class ScreenDiff:
    """Emit lines that appeared since the last snapshot. Blank-shell floor."""

    def __init__(self, read_screen):
        self.read_screen = read_screen  # async () -> str
        self.prev = None  # the first screen is only a baseline

    @staticmethod
    def _lines(screen: str) -> list[str]:
        return [ln.rstrip() for ln in ANSI.sub("", screen).splitlines()]

    async def poll(self) -> list:
        return self.feed(await self.read_screen())

    def feed(self, screen: str) -> list:
        cur, prev = self._lines(screen), self.prev
        self.prev = cur
        if prev is None:
            return []
        new = []
        for op, _, _, j1, j2 in difflib.SequenceMatcher(None, prev, cur, autojunk=False).get_opcodes():
            if op in ("insert", "replace"):
                new.extend(ln for ln in cur[j1:j2] if ln.strip())
        return [update_agent_message_text("\n".join(new) + "\n")] if new else []


def _selfcheck() -> None:
    import asyncio
    import tempfile

    def append(path, *entries):
        with open(path, "a") as f:
            for e in entries:
                f.write(e if isinstance(e, str) else json.dumps(e) + "\n")

    def text(entry_text):
        return {"type": "assistant", "message": {"content": [{"type": "text", "text": entry_text}]}}

    lines = [
        {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hmm"}]}},
        text("hello"),
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pwd"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "/tmp"}]}},
        {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "sub"}]}},
        {"type": "user", "message": {"content": "typed by human"}},
    ]

    async def go(tmp):
        # discovery: the process's session file + cwd mangling name the transcript exactly
        session = {"sessionId": "sid", "cwd": "/home/j/dev/my_app/.wt"}
        path = claude_transcript(tmp, session)
        assert path == os.path.join(tmp, "projects", "-home-j-dev-my-app--wt", "sid.jsonl"), path
        me = proc_info(os.getpid())
        assert me["cwd"] == os.getcwd() and "PATH" in me["env"] and me["started"] > 0
        os.makedirs(os.path.dirname(path))
        # no transcript yet: nothing to tail
        r = ClaudeTranscript(path)
        assert await r.poll() == []
        # normal tail: read from the offset on, partial line waits, torn line is skipped
        append(path, {"type": "user", "message": {"content": "old"}})
        r = ClaudeTranscript(path)
        assert await r.poll() == []
        append(path, *lines, '{"type": "assistant", "message": {"content": [{"type": "text", "te')
        ups = await r.poll()
        kinds = [u.session_update for u in ups]
        assert kinds == ["agent_thought_chunk", "agent_message_chunk", "tool_call", "tool_call_update", "user_message_chunk"], kinds
        assert ups[4].content.text == "typed by human"
        assert updates_from_entry({"type": "user", "message": {"content": "<command-name>/clear</command-name>"}}) == []
        assert ups[2].title == "Bash: pwd" and ups[2].kind == "execute"
        assert ups[3].status == "completed" and ups[3].content[0].content.text == "/tmp"
        append(path, 'xt": "done"}]}}\n')
        assert [u.content.text for u in await r.poll()] == ["done"]
        append(path, "{not json\n", text("after"))
        assert [u.content.text for u in await r.poll()] == ["after"]

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(go(tmp))

    s = ScreenDiff(None)
    assert s.feed("$ \n") == []
    assert s.feed("$ pwd\n/home/x\n$ \n")[0].content.text == "$ pwd\n/home/x\n"
    assert s.feed("$ pwd\n/home/x\n$ \n") == []
    assert s.feed("/home/x\n$ ls\n\x1b[31ma.txt\x1b[0m\n$ \n")[0].content.text == "$ ls\na.txt\n"
    # Codex: rollout by cwd+home (pre-first-turn path), item_completed → updates
    home = tempfile.mkdtemp()
    d = os.path.join(home, "sessions", "2026", "09", "13"); os.makedirs(d)
    def rollout(name, cwd, items):
        rows = [{"type": "session_meta", "payload": {"cwd": cwd}}]
        rows += [{"type": "event_msg", "payload": {"type": "item_completed", "item": it}} for it in items]
        rows.append({"type": "event_msg", "payload": {"type": "token_count"}})
        with open(os.path.join(d, name), "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))
    rollout("rollout-1-a.jsonl", "/other", [{"type": "AgentMessage", "id": "x", "content": [{"type": "Text", "text": "wrong cwd"}]}])
    rollout("rollout-2-b.jsonl", "/work", [])
    os.utime(os.path.join(d, "rollout-1-a.jsonl"), (1, 1))
    assert codex_rollout("/work", home) == os.path.join(d, "rollout-2-b.jsonl")
    assert codex_rollout("/work", home, newer_than=2e10) is None  # older than the process: not ours
    c = CodexRollout(0, path=os.path.join(d, "rollout-2-b.jsonl"))
    assert asyncio.run(c.poll()) == []
    with open(c.path, "a") as f:
        for it in [
            {"type": "UserMessage", "id": "u1", "content": [{"type": "text", "text": "run pwd"}]},
            {"type": "Reasoning", "id": "r1", "summary_text": ["thinking"]},
            {"type": "CommandExecution", "id": "e1", "command": ["/bin/bash", "-lc", "pwd"], "status": "completed", "exit_code": 0, "stdout": "/work\n"},
            {"type": "FileChange", "id": "f1", "changes": {"/work/a.py": {"type": "update"}}},
            {"type": "AgentMessage", "id": "m1", "content": [{"type": "Text", "text": "done"}]},
            {"type": "SubAgentActivity", "id": "s1"},
        ]:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": it}}) + "\n")
    ups = asyncio.run(c.poll())
    assert [u.session_update for u in ups] == ["user_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update",
                                              "tool_call", "tool_call_update", "agent_message_chunk"], [u.session_update for u in ups]
    assert ups[2].title == "exec: pwd" and ups[3].content[0].content.text == "/work\n" and ups[4].title == "edit: a.py"
    # Pi/OMP: session found by cwd under the agent dir, message roles → updates
    home = tempfile.mkdtemp()
    d = os.path.join(home, "sessions", "-work"); os.makedirs(d)
    def pi_file(name, cwd, rows):
        with open(os.path.join(d, name), "w") as f:
            f.write(json.dumps({"type": "title", "title": "t"}) + "\n")
            f.write(json.dumps({"type": "session", "id": "s", "cwd": cwd}) + "\n")
            f.write("".join(json.dumps(r) + "\n" for r in rows))
    pi_file("2026-01-01T00-00-00-000Z_a.jsonl", "/other", [])
    pi_file("2026-01-01T00-00-01-000Z_b.jsonl", "/work", [])
    os.utime(os.path.join(d, "2026-01-01T00-00-00-000Z_a.jsonl"), (1, 1))
    sess = os.path.join(d, "2026-01-01T00-00-01-000Z_b.jsonl")
    assert pi_session("/work", os.path.join(home, "sessions")) == sess
    assert pi_session("/work", os.path.join(home, "sessions"), newer_than=2e10) is None
    r = PiSession(0, "omp", path=sess)
    assert asyncio.run(r.poll()) == []
    with open(sess, "a") as f:
        for row in [
            {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "run pwd"}]}},
            {"type": "message", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "ok"}, {"type": "text", "text": "Sure."},
                {"type": "toolCall", "id": "t1", "name": "bash", "arguments": {"command": "pwd"}}]}},
            {"type": "custom", "customType": "tool_execution_start", "data": {"toolCallId": "t1"}},
            {"type": "message", "message": {"role": "toolResult", "toolCallId": "t1", "toolName": "bash",
                                            "content": [{"type": "text", "text": "/work\n"}]}},
            {"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}},
        ]:
            f.write(json.dumps(row) + "\n")
    ups = asyncio.run(r.poll())
    assert [u.session_update for u in ups] == ["user_message_chunk", "agent_thought_chunk", "agent_message_chunk",
                                              "tool_call", "tool_call_update", "agent_message_chunk"], [u.session_update for u in ups]
    assert ups[3].title == "bash: pwd" and ups[3].kind == "execute" and ups[4].content[0].content.text == "/work\n"
    print("reader ok")


if __name__ == "__main__":
    _selfcheck()
