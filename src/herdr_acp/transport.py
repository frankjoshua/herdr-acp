"""Herdr transport: everything herdr-acp asks a multiplexer for, via the `herdr` CLI.

A tmux transport later is a class with the same method names (duck typing, no ABC).
"""

import asyncio
import json
import sys

ENTER_GAP = 0.5  # TUIs drop an Enter batched with the text; ccgram found 0.5s is enough


class HerdrError(RuntimeError):
    pass


async def _run(*args: str, timeout: float | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "herdr", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise HerdrError(f"herdr {' '.join(args[:3])} timed out")
    if proc.returncode:
        raise HerdrError((err or out).decode().strip())
    return out.decode()


class Herdr:
    def __init__(self, pane: str):
        self.pane = pane

    async def info(self) -> dict:
        return json.loads(await _run("pane", "get", self.pane))["result"]["pane"]

    async def agent(self) -> str | None:
        return (await self.info()).get("agent")

    async def status(self) -> str:
        return (await self.info()).get("agent_status", "unknown")

    async def session_id(self) -> str | None:
        return ((await self.info()).get("agent_session") or {}).get("value")

    async def send_text(self, text: str) -> None:
        await _run("pane", "send-text", self.pane, text)
        await asyncio.sleep(ENTER_GAP)
        await _run("pane", "send-keys", self.pane, "Enter")

    async def send_keys(self, *keys: str) -> None:
        await _run("pane", "send-keys", self.pane, *keys)

    async def read_screen(self, lines: int = 200) -> str:
        return await _run("pane", "read", self.pane, "--lines", str(lines), "--format", "text")

    async def wait(self, *until: str, timeout: float | None = None) -> str:
        """Block until the pane's agent reaches one of `until`; returns the status seen."""
        args = ["agent", "wait", self.pane]
        for s in until:
            args += ["--until", s]
        if timeout is not None:
            args += ["--timeout", str(int(timeout * 1000))]
        return json.loads(await _run(*args))["result"]["agent"]["agent_status"]


async def _selfcheck(pane: str) -> None:
    h = Herdr(pane)
    info = await h.info()
    assert info["pane_id"] == pane, info
    print("agent:", await h.agent(), "status:", await h.status(), "session:", await h.session_id())
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
