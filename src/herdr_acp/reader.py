"""Readers: "what happened in the pane since the prompt", as ACP session updates.

ClaudeTranscript tails ~/.claude/projects/*/<session>.jsonl from a byte offset.
ScreenDiff diffs successive plain-text screen snapshots (floor for shells / unknown agents).
"""

import difflib
import glob
import json
import os
import re

from acp import (
    start_tool_call,
    text_block,
    tool_content,
    update_agent_message_text,
    update_agent_thought_text,
    update_tool_call,
)

TOOL_KIND = {
    "Bash": "execute", "Read": "read", "Edit": "edit", "Write": "edit", "NotebookEdit": "edit",
    "MultiEdit": "edit", "Grep": "search", "Glob": "search", "WebFetch": "fetch",
    "WebSearch": "fetch", "Agent": "think", "Task": "think",
}

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")


def transcript_path(session_id: str) -> str | None:
    # ponytail: glob by session id instead of re-deriving Claude's cwd->dirname mangling
    hits = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session_id}.jsonl"))
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
        self.last_text = ""  # final assistant text of the turn, for --reply-from-output

    def poll(self) -> list:
        self.path = self.path or transcript_path(self.session_id)
        if not self.path:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read()
        if not data.endswith(b"\n"):  # partial line: leave it for next poll
            data = data[: data.rfind(b"\n") + 1]
        self.offset += len(data)
        out = []
        for line in data.decode("utf-8", "replace").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ups = updates_from_entry(entry)
            for u in ups:
                if u.session_update == "agent_message_chunk":
                    self.last_text = u.content.text
            out.extend(ups)
        return out


class ScreenDiff:
    """Emit lines that appeared since the last snapshot. Blank-shell floor."""

    def __init__(self, first_screen: str = ""):
        self.prev = self._lines(first_screen)
        self.last_text = ""

    @staticmethod
    def _lines(screen: str) -> list[str]:
        return [ln.rstrip() for ln in ANSI.sub("", screen).splitlines()]

    def feed(self, screen: str) -> list:
        cur = self._lines(screen)
        new = []
        for op, _, _, j1, j2 in difflib.SequenceMatcher(None, self.prev, cur, autojunk=False).get_opcodes():
            if op in ("insert", "replace"):
                new.extend(ln for ln in cur[j1:j2] if ln.strip())
        self.prev = cur
        if not new:
            return []
        text = "\n".join(new) + "\n"
        self.last_text += text
        return [update_agent_message_text(text)]


def _selfcheck() -> None:
    import tempfile

    lines = [
        {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hmm"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pwd"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "/tmp"}]}},
        {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "sub"}]}},
        {"type": "user", "message": {"content": "typed by human"}},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"type": "user", "message": {"content": "old"}}) + "\n")
        path = f.name
    r = ClaudeTranscript("selfcheck")
    assert r.path is None and r.poll() == []
    r.path, r.offset = path, os.path.getsize(path)
    assert r.poll() == []  # nothing after the offset
    with open(path, "a") as f:
        for e in lines:
            f.write(json.dumps(e) + "\n")
        f.write('{"type": "assistant", "message": {"content": [{"type": "text", "te')  # partial
    ups = r.poll()
    kinds = [u.session_update for u in ups]
    assert kinds == ["agent_thought_chunk", "agent_message_chunk", "tool_call", "tool_call_update"], kinds
    assert ups[2].title == "Bash: pwd" and ups[2].kind == "execute"
    assert ups[3].status == "completed" and ups[3].content[0].content.text == "/tmp"
    assert r.last_text == "hello"
    with open(path, "a") as f:
        f.write('xt": "done"}]}}\n')
    assert [u.content.text for u in r.poll()] == ["done"]
    os.unlink(path)

    s = ScreenDiff("$ \n")
    assert s.feed("$ pwd\n/home/x\n$ \n")[0].content.text == "$ pwd\n/home/x\n"
    assert s.feed("$ pwd\n/home/x\n$ \n") == []
    assert s.feed("/home/x\n$ ls\n\x1b[31ma.txt\x1b[0m\n$ \n")[0].content.text == "$ ls\na.txt\n"
    print("reader ok")


if __name__ == "__main__":
    _selfcheck()
