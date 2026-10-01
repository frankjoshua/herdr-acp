"""Readers: "what happened in the pane since the prompt", as ACP session updates.

ClaudeTranscript tails <cfg>/projects/<cwd>/<session>.jsonl from a byte offset.
CodexRollout tails <CODEX_HOME>/sessions/YYYY/MM/DD/rollout-*.jsonl the same way.
PiSession tails a Pi-format session (Pi, OMP): <agent dir>/sessions/<cwd>/<ts>_<id>.jsonl.
ScreenDiff diffs successive plain-text screen snapshots (floor for shells / unknown agents).
`parse_dialog(screen)` finds an approval/question dialog waiting at the bottom of the pane.
All expose `async poll() -> list[update]`. The transcript readers also put TURN_END where the
agent recorded the end of a turn. `claude_transcript_for(pid)` / `codex_rollout_for(pid)` /
`pi_session_for(pid, kind)` find the file from the agent process itself (`proc_info`).
"""

import difflib
import glob
import json
import logging
import os
import re
from typing import NamedTuple

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
# fallback for an agent that has not opened its session file yet.
TURN_END = "turn_end"  # in a reader's updates: the agent recorded the end of a turn here

TOOL_KIND = {
    "Bash": "execute", "Read": "read", "Edit": "edit", "Write": "edit", "NotebookEdit": "edit",
    "MultiEdit": "edit", "Grep": "search", "Glob": "search", "WebFetch": "fetch",
    "WebSearch": "fetch", "Agent": "think", "Task": "think",
}

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")


def open_files(pid: int) -> list[str]:
    """Paths the process holds open (/proc/<pid>/fd)."""
    out = []
    for fd in os.listdir(f"{PROC}/{pid}/fd"):
        try:
            out.append(os.readlink(f"{PROC}/{pid}/fd/{fd}"))
        except OSError:
            pass
    return out


def proc_info(pid: int) -> dict:
    """What the agent process tells us about itself: env, cwd, open files, start time."""
    base = f"{PROC}/{pid}"
    with open(f"{base}/environ", "rb") as f:
        env = dict(kv.split("=", 1) for kv in f.read().decode("utf-8", "replace").split("\0") if "=" in kv)
    return {"env": env, "cwd": os.readlink(f"{base}/cwd"), "fds": open_files(pid), "started": os.stat(base).st_mtime}


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
    if kind == "system" and entry.get("subtype") == "turn_duration":  # Claude closes every turn with it
        return [TURN_END]
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
    if entry.get("type") == "event_msg" and p.get("type") in ("task_complete", "turn_aborted"):
        return [TURN_END]
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


def _is_rollout(path: str) -> bool:
    return "/sessions/" in path and os.path.basename(path).startswith("rollout-") and path.endswith(".jsonl")


def codex_rollout_for(pid: int) -> str | None:
    p = proc_info(pid)
    return next(filter(_is_rollout, p["fds"]), None) \
        or codex_rollout(p["cwd"], p["env"].get("CODEX_HOME") or os.path.expanduser("~/.codex"), p["started"])


class CodexRollout:
    def __init__(self, pid: int, path: str | None = None):
        self.pid = pid
        self.path = path or codex_rollout_for(pid)  # None until Codex creates its rollout
        self.offset = os.path.getsize(self.path) if self.path else 0

    async def poll(self) -> list:
        if not self.path:  # created at the first turn; full discovery costs ~1ms
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


def _is_pi_session(path: str) -> bool:
    """A session file; OMP also holds side files (`__advisor.scribe.jsonl`) under the session's dir."""
    return "/sessions/" in path and path.endswith(".jsonl") and not os.path.basename(path).startswith("__")


def pi_session_for(pid: int, kind: str) -> str | None:
    p = proc_info(pid)
    if found := next(filter(_is_pi_session, p["fds"]), None):
        return found
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
    if m.get("stopReason") not in (None, "toolUse"):  # stop, aborted, error, length: the turn is over
        out.append(TURN_END)
    return out


class PiSession:
    def __init__(self, pid: int, kind: str = "omp", path: str | None = None):
        self.pid, self.kind = pid, kind
        self.path = path or pi_session_for(pid, kind)  # None until the first message creates it
        self.offset = os.path.getsize(self.path) if self.path else 0

    async def poll(self) -> list:
        if not self.path:  # created at the first turn and not held open yet (OMP); ~2ms
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


CURSOR = "❯›>▶►→➜\uf054"  # the selected-row marker of TUI lists (\uf054: OMP's nerd-font chevron)
NUMBERED = re.compile(rf"^(\s*)([{CURSOR}]\s*)?(\d+)\.\s+(\S.*)$")  # Claude, Codex: "❯ 1. Yes"
SHORTCUT = re.compile(r"\s+\((?:esc|[a-z])\)$")  # Codex: "Yes, proceed (y)"
FRAME = "─━═▔╭╮╰╯├┤┌┐└┘"
DIALOG_ROWS = 25  # a dialog waiting for an answer sits at the bottom of the screen
# A row that may belong to a dialog, as a Rust regex for Herdr's wait_for_output: a cursor on a
# numbered option (Claude, Codex) or a "↑/↓ navigate" hint (OMP). parse_dialog decides.
DIALOG_HINT = rf"^\s*[│┃║]?\s*[{CURSOR}]\s*\d+\.\s|↑/?↓"


class Dialog(NamedTuple):
    question: str
    labels: list[str]  # one per option, wrapped rows joined, shortcut hints dropped
    cursor: int  # index of the selected option
    text: str  # what the dialog shows above its options
    heads: list[str]  # each option's first screen row, after the cursor column


def _rx(s: str) -> str:
    """`s` as a literal in a Rust regex."""
    return "".join("\\" + c if c in r"\.+*?()|[]{}^$#&-~" else c for c in s)


def cursor_on(dialog: Dialog, i: int) -> str:
    """Rust regex matching option `i`'s row once the cursor is on it."""
    return rf"[{CURSOR}]\s*{_rx(dialog.heads[i])}"


def _unbox(line: str) -> str:
    line = line.rstrip()
    if line[:1] in "│┃║":
        line = line[1:]
    if line[-1:] in "│┃║":
        line = line[:-1]
    return line.rstrip()


def _numbered(rows: list[str]) -> tuple[int, list[str], int, list[str]] | None:
    """The last "1. … 2. …" list with exactly one cursor row: (first row, labels, cursor index, heads)."""
    found, run = None, None  # run: [first row, labels, cursor indexes, label column, heads]
    for i, row in enumerate(rows):
        m = NUMBERED.match(row)
        if m and int(m[3]) == 1:
            run = [i, [m[4]], [0] if m[2] else [], m.start(4), [row[m.start(3):].rstrip()]]
        elif m and run and int(m[3]) == len(run[1]) + 1:
            run[1].append(m[4])
            run[2] += [len(run[1]) - 1] if m[2] else []
            run[3] = m.start(4)
            run[4].append(row[m.start(3):].rstrip())
        elif run and row.strip() and len(row) - len(row.lstrip()) >= run[3]:
            run[1][-1] += " " + row.strip()  # a label wrapped onto the next row
        else:
            run = None
        if run and len(run[1]) >= 2 and len(run[2]) == 1:
            found = (run[0], list(run[1]), run[2][0], list(run[4]))
    return found


def _navigated(rows: list[str]) -> tuple[int, list[str], int, list[str]] | None:
    """An unnumbered list right above a "↑/↓ navigate" hint (OMP): one cursor row, the other rows
    indented to the cursor row's label."""
    hint = next((i for i in range(len(rows) - 1, -1, -1) if "↑/↓" in rows[i] or "↑↓" in rows[i]), None)
    if hint is None:
        return None
    end = hint
    while end > 0 and not rows[end - 1].strip():
        end -= 1
    start = end
    while start > 0 and rows[start - 1].strip():
        start -= 1
    opts = rows[start:end]
    cursors = [i for i, o in enumerate(opts) if o.lstrip()[:1] in CURSOR]
    if len(opts) < 2 or len(cursors) != 1:
        return None
    marked = opts[cursors[0]]
    col = len(marked) - len(marked.lstrip()[1:].lstrip())
    if any(len(o) - len(o.lstrip()) != col for i, o in enumerate(opts) if i != cursors[0]):
        return None
    labels = [o.strip().lstrip(CURSOR).strip() for o in opts]
    return start, labels, cursors[0], labels


def parse_dialog(screen: str) -> Dialog | None:
    """The choice dialog waiting at the bottom of the pane (an approval or a question), or None.
    Its text is what sits above the options, up to a frame or a double blank line."""
    rows = [_unbox(r) for r in ANSI.sub("", screen).splitlines()][-DIALOG_ROWS:]
    hit = _numbered(rows) or _navigated(rows)
    if not hit:
        return None
    first, labels, cursor, heads = hit
    above, blank = [], False
    for row in reversed(rows[:first]):
        s = row.strip()
        if not s:
            if blank and above:
                break  # a double blank row: the dialog starts below it
            blank = True
            continue
        blank, bare = False, s.strip(FRAME).strip()
        if bare:
            above.append(bare)
        if not bare or s[0] in "╭┌":
            break  # a rule, or the top of the dialog's frame ("╭─ Allow tool: bash ─╮", title kept)
    text = "\n".join(reversed(above))
    question = next((a for a in reversed(text.splitlines()) if a.endswith("?")), text.split("\n", 1)[0])
    return Dialog(question, [SHORTCUT.sub("", lb) for lb in labels], cursor, text, heads)


def option_kind(label: str) -> str:
    """ACP PermissionOption kind for a dialog choice, from its wording."""
    low = label.lower()
    no = low.startswith(("no", "deny", "reject", "decline", "cancel", "skip", "don't", "do not"))
    always = any(w in low for w in ("always", "don't ask again", "do not ask again", "until next", "never"))
    return ("reject" if no else "allow") + ("_always" if always else "_once")


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
        # every turn ends with turn_duration, also an interrupted one; a subagent's doesn't count
        append(path, {"type": "user", "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}},
               {"type": "system", "subtype": "turn_duration", "isSidechain": True},
               {"type": "system", "subtype": "turn_duration"})
        assert await r.poll() == [TURN_END]

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
    for end in ("task_complete", "turn_aborted"):  # a finished turn, a rejected one
        assert codex_updates({"type": "event_msg", "payload": {"type": end}}) == [TURN_END], end
    assert codex_updates({"type": "event_msg", "payload": {"type": "task_started"}}) == []
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
            {"type": "message", "message": {"role": "assistant", "stopReason": "toolUse", "content": [
                {"type": "thinking", "thinking": "ok"}, {"type": "text", "text": "Sure."},
                {"type": "toolCall", "id": "t1", "name": "bash", "arguments": {"command": "pwd"}}]}},
            {"type": "custom", "customType": "tool_execution_start", "data": {"toolCallId": "t1"}},
            {"type": "message", "message": {"role": "toolResult", "toolCallId": "t1", "toolName": "bash",
                                            "content": [{"type": "text", "text": "/work\n"}]}},
            {"type": "message", "message": {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "done"}]}},
        ]:
            f.write(json.dumps(row) + "\n")
    ups = asyncio.run(r.poll())
    assert [u if u == TURN_END else u.session_update for u in ups] == [
        "user_message_chunk", "agent_thought_chunk", "agent_message_chunk", "tool_call", "tool_call_update",
        "agent_message_chunk", TURN_END], ups
    assert ups[3].title == "bash: pwd" and ups[3].kind == "execute" and ups[4].content[0].content.text == "/work\n"
    # OMP holds advisor side files open next to the session; they are not the session
    assert _is_pi_session("/h/.omp/agent/sessions/-w/2026_x.jsonl")
    assert not _is_pi_session("/h/.omp/agent/sessions/-w/2026_x/__advisor.scribe.jsonl")
    # dialogs, as `herdr pane read` shows them in a 44-column pane
    codex = ("\n\n  Would you like to run the following com\n\n  Environment: local\n\n  Reason: Allow creating\n"
             "  /tmp/x with the\n  [… 9 lines] ctrl + a view all\n\n› 1. Yes, proceed (y)\n"
             "  2. Yes, and don't ask again for\n     commands that start with `touch /\n     tmp/x` (p)\n"
             "  3. No, and tell Codex what to do\n     differently (esc)\n\n  Press enter to confirm or esc to cancel\n")
    d = parse_dialog(codex)
    assert d.labels == ["Yes, proceed", "Yes, and don't ask again for commands that start with `touch / tmp/x`",
                        "No, and tell Codex what to do differently"] and d.cursor == 0, d.labels
    assert d.question == "Would you like to run the following com" and d.text.endswith("ctrl + a view all"), d
    assert [option_kind(lb) for lb in d.labels] == ["allow_once", "allow_always", "reject_once"]
    # the row regex Herdr waits on: matches option 2 only once the cursor sits on it
    moved = codex.replace("› 1. Yes", "  1. Yes").replace("  2. Yes, and", "› 2. Yes, and")
    assert d.heads[1] == "2. Yes, and don't ask again for", d.heads
    assert re.search(cursor_on(d, 1), moved, re.M) and not re.search(cursor_on(d, 1), codex, re.M)
    omp = ("│ $ touch /tmp/x    │\n╰────────────╯\n\n  \uf12b7 Creating marker file\n"
           "╭─ Allow tool: bash ─────────╮\n│                            │\n│ Command: touch /tmp/x      │\n"
           "│                            │\n│  \uf054 Approve                 │\n│    Deny                    │\n"
           "│                            │\n│ ↑/↓ navigate  \U000f0311 select  \uf12b7 cancel │\n"
           "╰────────────────────────────╯\n personal\nthink:high\n")
    d = parse_dialog(omp)
    assert d[:4] == ("Allow tool: bash", ["Approve", "Deny"], 0, "Allow tool: bash\nCommand: touch /tmp/x"), d
    moved = omp.replace("\uf054 Approve", "  Approve").replace("│    Deny", "│  \uf054 Deny")
    assert re.search(cursor_on(d, 1), moved, re.M) and not re.search(cursor_on(d, 1), omp, re.M)
    claude = ("● Creating the marker file\n  ⎿  $ touch /tmp/x\n────────────────────\n Bash command\n\n"  # Claude 2.1.285
              " Tip: auto mode handles these prompts for you — choose \"switch to auto mode\" below\n\n"
              "   touch /tmp/x\n   Create the marker file\n\n Do you want to proceed?\n ❯ 1. Yes\n"
              "   2. Yes, and always allow access to /tmp from this project\n"
              "   3. Yes, and switch to auto mode · auto mode handles these prompts for you\n   4. No\n\n"
              " Esc to cancel · Tab to amend\n")
    q, labels, cur, _, _ = parse_dialog(claude)
    assert q == "Do you want to proceed?" and cur == 0 and len(labels) == 4, (q, labels)
    assert [option_kind(lb) for lb in labels] == ["allow_once", "allow_always", "allow_once", "reject_once"]
    idle = "● Done. Options:\n  1. keep it\n  2. drop it\n────\n❯ Try \"refactor\"\n  ↑/↓ to scroll\n────\n"
    assert parse_dialog(idle) is None  # a numbered answer and the input box are not a dialog
    # the trigger fires on every dialog shape; an input box ("❯ Try …") or a plain list doesn't fire it
    assert all(re.search(DIALOG_HINT, s, re.M) for s in (codex, omp, claude))
    assert not re.search(DIALOG_HINT, "● Options:\n  1. keep it\n  2. drop it\n────\n❯ Try \"refactor\"\n", re.M)
    print("reader ok")


if __name__ == "__main__":
    _selfcheck()
