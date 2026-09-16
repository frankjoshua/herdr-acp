"""Herdr transport: everything herdr-acp asks a multiplexer for, via the `herdr` CLI.

A tmux transport later is a class with the same method names (duck typing, no ABC).
"""

import asyncio
import json
import sys

ENTER_GAP = 0.5  # TUIs drop an Enter batched with the text; ccgram found 0.5s is enough


class HerdrError(RuntimeError):
    pass


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
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    return out.decode()


async def _run(*args: str, timeout: float = 10.0) -> str:
    proc = await asyncio.create_subprocess_exec(
        "herdr", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    cmd = f"herdr {' '.join(args[:3])}"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HerdrError(f"{cmd} timed out")
    if proc.returncode:
        raise HerdrError(f"{cmd}: {(err or out).decode().strip()}")
    return out.decode()


class Herdr:
    def __init__(self, pane: str):
        self.pane = pane

    async def info(self) -> dict:
        return json.loads(await _run("pane", "get", self.pane))["result"]["pane"]

    async def state(self) -> tuple[str | None, str, str | None, str | None]:
        """(agent, status, session_id, cwd) from one `pane get`."""
        p = await self.info()
        return (p.get("agent"), p.get("agent_status", "unknown"),
                (p.get("agent_session") or {}).get("value"), p.get("foreground_cwd") or p.get("cwd"))

    async def process(self) -> tuple[int | None, str | None]:
        """(pid, name) of the pane's foreground process, e.g. the running agent. A tmux client
        (the Codex desktop harness wraps codex in `tmux -L <socket>`) is resolved to the process
        inside its pane."""
        r = json.loads(await _run("pane", "process-info", "--pane", self.pane))["result"]["process_info"]
        procs = r.get("foreground_processes") or []
        if not procs:
            return None, None
        pid, name = procs[0]["pid"], procs[0]["name"]
        if name.startswith("tmux"):
            sock = tmux_socket(procs[0].get("cmdline") or procs[0].get("argv") or "")
            if sock:
                out = await _run_argv("tmux", "-L", sock, "list-panes", "-a", "-F", "#{pane_pid} #{pane_current_command}")
                inner = out.split()
                if len(inner) >= 2:
                    return int(inner[0]), inner[1]
        return pid, name

    async def send_text(self, text: str) -> None:
        await _run("pane", "send-text", self.pane, text)
        await asyncio.sleep(ENTER_GAP)
        await _run("pane", "send-keys", self.pane, "Enter")

    async def send_keys(self, *keys: str) -> None:
        await _run("pane", "send-keys", self.pane, *keys)

    async def read_screen(self, lines: int = 200) -> str:
        return await _run("pane", "read", self.pane, "--lines", str(lines), "--format", "text")


async def _selfcheck(pane: str) -> None:
    h = Herdr(pane)
    info = await h.info()
    assert info["pane_id"] == pane, info
    agent, status, session, cwd = await h.state()
    assert isinstance(status, str), status
    assert tmux_socket("tmux -L codex-account-1 -f /dev/null new-session") == "codex-account-1"
    assert tmux_socket(["tmux", "-Lfoo", "new"]) == "foo" and tmux_socket("bash") is None
    pid, name = await h.process()
    print("agent:", agent, "status:", status, "session:", session, "cwd:", cwd, "process:", pid, name)
    screen = await h.read_screen(5)
    assert isinstance(screen, str)
    print("screen tail:", screen.strip().splitlines()[-1:] or "(blank)")
    try:
        await _run("pane", "get", "nope:p0")
    except HerdrError as e:
        assert "not found" in str(e), e
    print("transport ok")


if __name__ == "__main__":
    asyncio.run(_selfcheck(sys.argv[1]))
