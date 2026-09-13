"""Herdr transport: everything herdr-acp asks a multiplexer for, via the `herdr` CLI.

A tmux transport later is a class with the same method names (duck typing, no ABC).
"""

import asyncio
import json
import sys

ENTER_GAP = 0.5  # TUIs drop an Enter batched with the text; ccgram found 0.5s is enough


class HerdrError(RuntimeError):
    pass


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
        """(pid, name) of the pane's foreground process, e.g. the running agent."""
        r = json.loads(await _run("pane", "process-info", "--pane", self.pane))["result"]["process_info"]
        procs = r.get("foreground_processes") or []
        return (procs[0]["pid"], procs[0]["name"]) if procs else (None, None)

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
