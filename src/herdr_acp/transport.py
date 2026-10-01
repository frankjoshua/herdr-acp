"""Herdr transport: everything herdr-acp asks Herdr for, over its socket API (newline-delimited
JSON; `herdr api schema --json` documents every method). One connection per request; `events()`
and the waits keep theirs open until they return or are cancelled.

A tmux transport later is a class with the same method names (duck typing, no ABC).
"""

import asyncio
import json
import logging
import os
import sys

log = logging.getLogger("herdr-acp")
SETTLED = ["idle", "done", "blocked"]


class HerdrError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def socket_path() -> str:
    """Where the Herdr server listens, resolved as the `herdr` CLI does."""
    if os.environ.get("HERDR_SOCKET_PATH"):
        return os.environ["HERDR_SOCKET_PATH"]
    cfg = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "herdr")
    session = os.environ.get("HERDR_SESSION")
    return os.path.join(cfg, "sessions", session, "herdr.sock") if session else os.path.join(cfg, "herdr.sock")


def tmux_socket(cmdline) -> str | None:
    """The `-L <name>` socket of a tmux command line (string or argv)."""
    parts = cmdline.split() if isinstance(cmdline, str) else list(cmdline)
    for i, a in enumerate(parts):
        if a == "-L" and i + 1 < len(parts):
            return parts[i + 1]
        if a.startswith("-L") and len(a) > 2:
            return a[2:]
    return None


async def _run_argv(*argv: str, timeout: float = 10.0) -> str:
    """Run a command; stdout on success, HerdrError (naming the command) on timeout or nonzero exit."""
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    cmd = " ".join(argv[:4])
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HerdrError("timeout", f"{cmd} timed out")
    if proc.returncode:
        raise HerdrError("failed", f"{cmd}: {(err or out).decode().strip()}")
    return out.decode()


def _result(method: str, line: bytes) -> dict:
    if not line:
        raise HerdrError("closed", f"{method}: Herdr closed the connection")
    m = json.loads(line)
    if "error" in m:
        raise HerdrError(m["error"].get("code", "error"), f"{method}: {m['error'].get('message', '')}")
    return m["result"]


async def _call(method: str, params: dict, timeout: float | None = 10.0) -> dict:
    """One request, one response. `timeout=None` for server-side waits, which bound themselves."""
    reader, writer = await asyncio.open_unix_connection(socket_path(), limit=1 << 24)
    try:
        writer.write((json.dumps({"id": method, "method": method, "params": params}) + "\n").encode())
        await writer.drain()
        return _result(method, await asyncio.wait_for(reader.readline(), timeout))
    except asyncio.TimeoutError:
        raise HerdrError("timeout", f"{method} timed out")
    finally:
        writer.close()


class Herdr:
    def __init__(self, pane: str):
        self.pane = pane

    async def info(self) -> dict:
        return (await _call("pane.get", {"pane_id": self.pane}))["pane"]

    async def _process_info(self) -> dict:
        return (await _call("pane.process_info", {"pane_id": self.pane}))["process_info"]

    async def process(self) -> tuple[int | None, str | None]:
        """(pid, name) of the pane's foreground process, e.g. the running agent. A tmux client
        (the Codex desktop harness wraps codex in `tmux -L <socket>`) is resolved to the process
        inside its pane."""
        procs = (await self._process_info()).get("foreground_processes") or []
        if not procs:
            return None, None
        pid, name = procs[0]["pid"], procs[0]["name"]
        if name.startswith("tmux"):
            sock = tmux_socket(procs[0].get("cmdline") or procs[0].get("argv") or "")
            if sock:
                try:
                    out = await _run_argv("tmux", "-L", sock, "list-panes", "-a", "-F", "#{pane_pid} #{pane_current_command}")
                except HerdrError as e:  # no tmux, or the server is gone: Herdr's own view will do
                    log.debug("tmux lookup: %s", e)
                    out = ""
                inner = out.split()
                if len(inner) >= 2:
                    return int(inner[0]), inner[1]
        return pid, name

    async def shell_foreground(self) -> bool:
        """True when the pane's shell itself owns the terminal, i.e. no command is running."""
        p = await self._process_info()
        return p.get("foreground_process_group_id") == p.get("shell_pid")

    async def send(self, text: str) -> None:
        """Type `text` and press Enter, in one request (Herdr orders text and key)."""
        await _call("pane.send_input", {"pane_id": self.pane, "text": text, "keys": ["enter"]})

    async def prompt(self, text: str) -> str:
        """Submit `text` to the pane's agent and wait, in Herdr, for it to settle: the status it
        settled in. Raises HerdrError `agent_blocked` (nothing sent) when a dialog is open, and
        `agent_prompt_stalled` when Herdr saw no lifecycle change within its stall window."""
        r = await _call("agent.prompt", {"target": self.pane, "text": text, "wait": {"until": SETTLED}}, timeout=None)
        return ((r.get("wait") or r.get("agent") or {}).get("agent_status")) or "unknown"

    async def send_keys(self, *keys: str) -> None:
        await _call("pane.send_keys", {"pane_id": self.pane, "keys": list(keys)})

    async def select(self, steps: int, row: str, timeout_ms: int = 5000) -> None:
        """Move a TUI list's cursor `steps` rows (negative: up) and press Enter once the cursor row
        matches the regex `row`, also when no move was needed (someone may have moved it since).
        Raises HerdrError `timeout` (nothing pressed) if it never does."""
        if steps:
            await self.send_keys(*["down" if steps > 0 else "up"] * abs(steps))
        await self.wait_output(row, timeout_ms)
        await self.send_keys("enter")

    async def wait_output(self, regex: str, timeout_ms: int | None = None, lines: int = 25) -> str:
        """Wait, in Herdr, until a line of the visible screen's last `lines` rows matches `regex`
        (a Rust regex); the snapshot that matched."""
        params = {"pane_id": self.pane, "source": "visible", "lines": lines, "match": {"type": "regex", "value": regex}}
        if timeout_ms is not None:
            params["timeout_ms"] = timeout_ms
        return (await _call("pane.wait_for_output", params, timeout=None))["read"]["text"]

    async def _read(self, source: str, lines: int | None = None) -> str:
        params = {"pane_id": self.pane, "source": source, "format": "text"}
        if lines is not None:
            params["lines"] = lines
        return (await _call("pane.read", params))["read"]["text"]

    async def read_screen(self, lines: int = 200) -> str:
        return await self._read("recent", lines)

    async def read_visible(self) -> str:
        """The rendered viewport. Full-screen TUIs (Claude) draw on the alternate screen, which
        the default `recent` source doesn't show."""
        return await self._read("visible")

    async def events(self):
        """Yield (event, data) whenever Herdr detects an agent in the pane or the pane's agent
        changes status. Raises HerdrError when the subscription ends (`events_lost`, pane gone)."""
        subs = [{"type": "pane.agent_detected"}, {"type": "pane.agent_status_changed", "pane_id": self.pane}]
        reader, writer = await asyncio.open_unix_connection(socket_path(), limit=1 << 24)
        try:
            writer.write((json.dumps({"id": "events", "method": "events.subscribe",
                                      "params": {"subscriptions": subs}}) + "\n").encode())
            await writer.drain()
            _result("events.subscribe", await reader.readline())
            while True:
                line = await reader.readline()
                m = json.loads(line) if line else None
                if not m or "error" in m:
                    _result("events.subscribe", line)
                data = m.get("data") or {}
                if data.get("pane_id") == self.pane:
                    yield m.get("event"), data
        finally:
            writer.close()


async def _selfcheck(pane: str) -> None:
    h = Herdr(pane)
    info = await h.info()
    assert info["pane_id"] == pane, info
    assert tmux_socket("tmux -L codex-account-1 -f /dev/null new-session") == "codex-account-1"
    assert tmux_socket(["tmux", "-Lfoo", "new"]) == "foo" and tmux_socket("bash") is None
    pid, name = await h.process()
    print("agent:", info.get("agent"), "status:", info.get("agent_status"), "process:", pid, name,
          "shell in foreground:", await h.shell_foreground())
    screen = await h.read_visible()
    print("screen tail:", screen.strip().splitlines()[-1:] or "(blank)")
    try:
        await h.wait_output("^this line is not on screen$", timeout_ms=200)
    except HerdrError as e:
        assert e.code == "timeout", e
    try:
        await Herdr("nope:p0").info()
    except HerdrError as e:
        assert e.code == "pane_not_found" or "not found" in str(e), e
    print("transport ok")


if __name__ == "__main__":
    asyncio.run(_selfcheck(sys.argv[1]))
