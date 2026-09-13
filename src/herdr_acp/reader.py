"""Readers: "what happened in the pane since the prompt", as ACP session updates.

ClaudeTranscript tails ~/.claude/projects/*/<session>.jsonl from a byte offset.
ScreenDiff diffs successive plain-text screen snapshots (floor for shells / unknown agents).
Both expose `async poll() -> list[update]`.
"""

import difflib
import glob
import json
import logging
import os
import re

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
PROJECTS = os.path.expanduser("~/.claude/projects")

TOOL_KIND = {
    "Bash": "execute", "Read": "read", "Edit": "edit", "Write": "edit", "NotebookEdit": "edit",
    "MultiEdit": "edit", "Grep": "search", "Glob": "search", "WebFetch": "fetch",
    "WebSearch": "fetch", "Agent": "think", "Task": "think",
}

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")


def transcript_path(session_id: str) -> str | None:
    # ponytail: glob by session id instead of re-deriving Claude's cwd->dirname mangling
    hits = glob.glob(os.path.join(PROJECTS, "*", f"{session_id}.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


def newest_transcript(project_dir: str) -> str | None:
    hits = glob.glob(os.path.join(project_dir, "*.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


def _tool_title(name: str, inp: dict) -> str:
    arg = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("url") \
        or inp.get("description") or inp.get("prompt") or ""
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
                raw_output=text[:4000] or None,
            ))
    return out


class ClaudeTranscript:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.path = transcript_path(session_id)  # None until Claude's first turn creates it
        self.offset = os.path.getsize(self.path) if self.path else 0
        # Herdr's session id can go stale (pane cwd deleted, Claude restarted): if a different
        # transcript in the same project grows before ours does, that is the live one.
        self.alt = newest_transcript(os.path.dirname(self.path)) if self.path else None
        self.alt_size = os.path.getsize(self.alt) if self.alt else 0

    def _resolve(self) -> None:
        if self.alt and self.alt != self.path and os.path.getsize(self.alt) > self.alt_size:
            self.path, self.offset = self.alt, self.alt_size
        elif not self.path:
            self.path = transcript_path(self.session_id)
        if self.path and (self.path == self.alt or os.path.getsize(self.path) > self.offset):
            self.alt = None  # decided

    async def poll(self) -> list:
        self._resolve()
        if not self.path:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read()
        if not data.endswith(b"\n"):  # partial line: leave it for next poll
            data = data[: data.rfind(b"\n") + 1]
        out = []
        for line in data.splitlines(keepends=True):
            try:
                out.extend(updates_from_entry(json.loads(line.decode("utf-8", "replace"))))
            except Exception as e:  # one bad line (torn json, odd shape) must not cost the rest
                log.warning("transcript %s: skipped line: %r", self.path, e)
            self.offset += len(line)
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
        global PROJECTS
        PROJECTS = tmp
        proj_a, proj_b = os.path.join(tmp, "projA"), os.path.join(tmp, "projB")
        os.makedirs(proj_a); os.makedirs(proj_b)
        sid = os.path.join(proj_a, "sid.jsonl")
        # no transcript yet: nothing to tail, no fallback
        r = ClaudeTranscript("sid")
        assert r.path is None and r.alt is None and await r.poll() == []
        # (a) normal tail: the sid file appears, is picked up, and is read from its offset on
        append(sid, {"type": "user", "message": {"content": "old"}})
        r = ClaudeTranscript("sid")
        assert r.path == sid and await r.poll() == []  # nothing after the offset
        append(sid, *lines, '{"type": "assistant", "message": {"content": [{"type": "text", "te')  # partial
        ups = await r.poll()
        kinds = [u.session_update for u in ups]
        assert kinds == ["agent_thought_chunk", "agent_message_chunk", "tool_call", "tool_call_update", "user_message_chunk"], kinds
        assert ups[4].content.text == "typed by human"
        assert updates_from_entry({"type": "user", "message": {"content": "<command-name>/clear</command-name>"}}) == []
        assert ups[2].title == "Bash: pwd" and ups[2].kind == "execute"
        assert ups[3].status == "completed" and ups[3].content[0].content.text == "/tmp"
        append(sid, 'xt": "done"}]}}\n')
        assert [u.content.text for u in await r.poll()] == ["done"]
        # a torn line is skipped, the lines around it still stream
        append(sid, "{not json\n", text("after"))
        assert [u.content.text for u in await r.poll()] == ["after"]
        # (b) stale sid: it never grows, a newer file in the SAME project grows -> switch to it
        live = os.path.join(proj_a, "live.jsonl")
        append(live, text("earlier"))
        os.utime(sid, (1, 1))  # sid is the older file
        r = ClaudeTranscript("sid")
        assert r.path == sid and r.alt == live, (r.path, r.alt)
        assert await r.poll() == []
        append(live, text("live"))
        assert [u.content.text for u in await r.poll()] == ["live"] and r.path == live
        # (c) a newer file in a DIFFERENT project is never adopted
        other = os.path.join(proj_b, "other.jsonl")
        append(other, text("other"))
        r = ClaudeTranscript("sid")
        assert r.alt == live, r.alt
        append(other, text("more"))
        assert await r.poll() == [] and r.path == sid

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(go(tmp))

    s = ScreenDiff(None)
    assert s.feed("$ \n") == []
    assert s.feed("$ pwd\n/home/x\n$ \n")[0].content.text == "$ pwd\n/home/x\n"
    assert s.feed("$ pwd\n/home/x\n$ \n") == []
    assert s.feed("/home/x\n$ ls\n\x1b[31ma.txt\x1b[0m\n$ \n")[0].content.text == "$ ls\na.txt\n"
    print("reader ok")


if __name__ == "__main__":
    _selfcheck()
