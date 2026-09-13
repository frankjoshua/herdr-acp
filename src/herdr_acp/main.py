"""ACP server: `herdr-acp --pane <id>` on stdio.

The pane is a shared session: from `session/new` on, everything that happens in it (a human
typing, the agent's text and tool calls) streams to the client as `session/update`, whether or
not a prompt is in flight. A prompt is typed into the pane and its turn ends when the pane's
agent goes idle (or, for shells, the screen goes quiet)."""

import argparse
import asyncio
import logging
import os
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


class PaneAgent:
    def __init__(self, pane: str, quiet: float, debounce: float, footer: str = ""):
        self.herdr = Herdr(pane)
        self.quiet, self.debounce, self.footer = quiet, debounce, footer
        self.conn = None
        self.session_id = None
        self.reader = None
        self.agent, self.status = None, "unknown"
        self.activity = 0.0  # monotonic time of the last update we streamed
        self.typed = []  # recent prompt texts, so their transcript echo isn't re-streamed
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
        self.session_id = str(uuid.uuid4())
        asyncio.create_task(self._tail())
        return NewSessionResponse(session_id=self.session_id)

    async def cancel(self, session_id: str, **kw):
        self.cancelled = True
        try:
            await self.herdr.send_keys("esc")
        except HerdrError as e:
            log.warning("cancel: %s", e)

    async def _poll(self) -> list:
        """Follow whatever is in the pane right now; swap readers if the agent (re)starts."""
        info = await self.herdr.info()
        self.agent, self.status = info.get("agent"), info.get("agent_status", "unknown")
        sess = (info.get("agent_session") or {}).get("value")
        if self.agent == "claude" and sess:
            if not isinstance(self.reader, ClaudeTranscript) or self.reader.session_id != sess:
                self.reader = ClaudeTranscript(sess)
                log.info("tailing claude transcript %s", self.reader.path)
            return self.reader.poll()
        if not isinstance(self.reader, ScreenDiff):
            self.reader = ScreenDiff(await self.herdr.read_screen())
            log.info("tailing screen (agent=%s)", self.agent)
            return []
        return self.reader.feed(await self.herdr.read_screen())

    async def _tail(self) -> None:
        while True:
            try:
                for u in await self._poll():
                    if u.session_update == "user_message_chunk" and u.content.text.strip() in self.typed:
                        continue
                    await self.conn.session_update(self.session_id, u)
                    self.activity = time.monotonic()
            except Exception as e:  # ponytail: pane gone or herdr hiccup; keep tailing
                log.warning("tail: %s", e)
            await asyncio.sleep(POLL)

    async def prompt(self, session_id: str, prompt: list, **kw):
        text = "\n".join(b.text for b in prompt if getattr(b, "type", None) == "text")
        if self.footer:
            text += "\n\n" + self.footer
        self.typed = (self.typed + [text.strip()])[-5:]
        self.cancelled = False
        await self.herdr.send_text(text)
        start = time.monotonic()
        seen_working, idle_since, seen_activity = False, None, self.activity
        while True:
            await asyncio.sleep(POLL)
            now = time.monotonic()
            if self.cancelled:
                return PromptResponse(stop_reason="cancelled")
            fresh, seen_activity = self.activity > seen_activity, self.activity
            if self.agent:
                if self.status == "working":
                    seen_working, idle_since = True, None
                    continue
                idle_since = idle_since or now
                settled = seen_working or now - start >= GRACE
                if settled and not fresh and now - idle_since >= self.debounce:
                    break
            elif now - start >= self.quiet and now - self.activity >= self.quiet:
                break
        log.info("turn done (%s)", "agent idle" if self.agent else "screen quiet")
        return PromptResponse(stop_reason="end_turn")


def main() -> None:
    ap = argparse.ArgumentParser(prog="herdr-acp")
    ap.add_argument("--pane", required=True, help="Herdr pane id, e.g. wV:p9")
    ap.add_argument("--quiet", type=float, default=5.0, help="shell turns end after N quiet seconds")
    ap.add_argument("--debounce", type=float, default=2.0, help="agent turns end N seconds after idle")
    ap.add_argument("--footer", default=os.environ.get("HERDR_ACP_FOOTER", ""),
                    help="text appended to every prompt (env HERDR_ACP_FOOTER); clients use it for reply instructions")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG if a.verbose else logging.INFO,
                        format="herdr-acp %(levelname)s %(message)s")
    asyncio.run(run_agent(PaneAgent(a.pane, a.quiet, a.debounce, a.footer)))


if __name__ == "__main__":
    main()
