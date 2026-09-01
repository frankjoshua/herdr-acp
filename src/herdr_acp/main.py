"""ACP server: `herdr-acp --pane <id>` on stdio. A turn = prompt typed into the pane
until the pane's agent goes idle (or, for shells, the screen goes quiet)."""

import argparse
import asyncio
import logging
import sys
import time
import uuid

from acp import (
    PROTOCOL_VERSION,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    run_agent,
)
from acp.schema import Implementation

from .reader import ClaudeTranscript, ScreenDiff
from .transport import Herdr, HerdrError

log = logging.getLogger("herdr-acp")
POLL = 0.5
GRACE = 10.0  # end an agent turn without ever seeing "working" only after this long idle
# buzz-acp's prompt tells the agent to thread replies (--reply-to); Josh wants top-level posts.
CHANNEL_FOOTER = (
    "\n\n[herdr-acp] Override for this Buzz turn: post your reply as a top-level channel message, "
    "i.e. `buzz messages send --channel <id> --content ...` WITHOUT `--reply-to`, unless the "
    "human explicitly asked for a threaded reply."
)


class PaneAgent:
    def __init__(self, pane: str, quiet: float, debounce: float, reply: str = "channel"):
        self.herdr = Herdr(pane)
        self.quiet, self.debounce, self.reply = quiet, debounce, reply
        self.conn = None
        self.cancelled = False

    def on_connect(self, conn):
        self.conn = conn

    async def initialize(self, protocol_version: int, **kw):
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_info=Implementation(name="herdr-acp", version="0.0.1"),
        )

    async def new_session(self, cwd: str, **kw):
        await self.herdr.info()  # fail fast if the pane is gone
        return NewSessionResponse(session_id=str(uuid.uuid4()))

    async def cancel(self, session_id: str, **kw):
        self.cancelled = True
        try:
            await self.herdr.send_keys("esc")
        except HerdrError as e:
            log.warning("cancel: %s", e)

    async def prompt(self, session_id: str, prompt: list, **kw):
        text = "\n".join(b.text for b in prompt if getattr(b, "type", None) == "text")
        if self.reply == "channel" and "buzz messages send" in text:
            text += CHANNEL_FOOTER
        info = await self.herdr.info()
        agent, sess = info.get("agent"), (info.get("agent_session") or {}).get("value")
        if agent == "claude" and sess:
            reader = ClaudeTranscript(sess)
            log.info("turn: claude transcript %s", reader.path)

            async def poll():
                return reader.poll()
        else:
            reader = ScreenDiff(await self.herdr.read_screen())
            log.info("turn: screen diff (agent=%s)", agent)

            async def poll():
                return reader.feed(await self.herdr.read_screen())

        self.cancelled = False
        await self.herdr.send_text(text)
        start = last_change = time.monotonic()
        seen_working, idle_since = False, None
        while True:
            await asyncio.sleep(POLL)
            ups = await poll()
            for u in ups:
                await self.conn.session_update(session_id, u)
            now = time.monotonic()
            if ups:
                last_change = now
            if self.cancelled:
                return PromptResponse(stop_reason="cancelled")
            if agent:
                st = await self.herdr.status()
                if st == "working":
                    seen_working, idle_since = True, None
                    continue
                idle_since = idle_since or now
                settled = seen_working or now - start >= GRACE
                if settled and not ups and now - idle_since >= self.debounce:
                    break
            elif now - last_change >= self.quiet:
                break
        log.info("turn done (%s)", "agent idle" if agent else "screen quiet")
        return PromptResponse(stop_reason="end_turn")


def main() -> None:
    ap = argparse.ArgumentParser(prog="herdr-acp")
    ap.add_argument("--pane", required=True, help="Herdr pane id, e.g. wV:p9")
    ap.add_argument("--quiet", type=float, default=5.0, help="shell turns end after N quiet seconds")
    ap.add_argument("--debounce", type=float, default=2.0, help="agent turns end N seconds after idle")
    ap.add_argument("--reply", choices=["channel", "thread"], default="channel",
                    help="where the pane agent is told to post its reply (default: channel)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG if a.verbose else logging.INFO,
                        format="herdr-acp %(levelname)s %(message)s")
    asyncio.run(run_agent(PaneAgent(a.pane, a.quiet, a.debounce, a.reply)))


if __name__ == "__main__":
    main()
