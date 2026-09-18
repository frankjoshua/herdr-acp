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

from .reader import ClaudeTranscript, CodexRollout, ScreenDiff, claude_transcript_for
from .transport import Herdr

log = logging.getLogger("herdr-acp")
KNOWN = ("claude", "codex")  # agents with a transcript reader; anything else gets the screen diff
POLL = 0.5
GRACE = 10.0  # end an agent turn without ever seeing "working" only after this long idle


class PaneAgent:
    def __init__(self, transport, quiet: float, debounce: float, footer: str = ""):
        self.transport = transport
        self.quiet, self.debounce, self.footer = quiet, debounce, footer
        self.conn = None
        self.session_id = None
        self.tail = None  # the _tail task
        self.reader = None
        self.agent, self.status = None, "unknown"
        self.pid = None  # the agent process the current reader was built for
        self.last_update_at = 0.0  # monotonic time of the last update we streamed
        self.recent_prompts = []  # so a prompt's transcript echo isn't re-streamed as user input
        self.cancelled = False

    def on_connect(self, conn):
        self.conn = conn

    async def initialize(self, protocol_version: int, **kw):
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_info=Implementation(name="herdr-acp", version="0.0.1"),
        )

    async def new_session(self, cwd: str, **kw):
        await self.transport.info()  # fail fast if the pane is gone
        self.session_id = str(uuid.uuid4())
        if self.tail and not self.tail.done():
            self.tail.cancel()
        self.tail = asyncio.create_task(self._tail())
        return NewSessionResponse(session_id=self.session_id)

    async def cancel(self, session_id: str, **kw):
        self.cancelled = True
        try:
            await self.transport.send_keys("esc")
        except Exception as e:  # best effort, like _tail
            log.warning("cancel: %s", e)

    async def _pick_reader(self) -> None:
        """Key the reader on the agent process: a (re)started agent gets a fresh reader."""
        pid, name = await self.transport.process()
        # Herdr may not detect an agent it can't see (e.g. codex inside tmux); the process name will.
        kind = next((k for k in (self.agent, name) if k in KNOWN), None)
        if self.reader and pid == self.pid and (kind or isinstance(self.reader, ScreenDiff)):
            return
        if kind == "claude" and pid:
            reader = ClaudeTranscript(claude_transcript_for(pid))
            log.info("tailing claude transcript %s", reader.path)
        elif kind == "codex" and pid:
            reader = CodexRollout(pid)
            log.info("tailing codex rollout %s", reader.path)
        elif isinstance(self.reader, ScreenDiff):
            reader = self.reader
        else:
            reader = ScreenDiff(self.transport.read_screen)
            log.info("tailing screen (agent=%s)", self.agent)
        self.reader, self.pid = reader, pid  # together, and only once the build succeeded (else retried next tick)

    async def _tail(self) -> None:
        """Follow whatever is in the pane right now; swap readers if the agent (re)starts."""
        tick = 0
        while True:
            try:
                self.agent, self.status = await self.transport.state()
                if tick % 10 == 0 or self.reader is None:  # ponytail: process-info every 5s
                    await self._pick_reader()
                tick += 1
                for u in await self.reader.poll():
                    if u.session_update == "user_message_chunk" and u.content.text.strip() in self.recent_prompts:
                        continue
                    await self.conn.session_update(self.session_id, u)
                    self.last_update_at = time.monotonic()
            except Exception as e:  # ponytail: pane gone or herdr hiccup; keep tailing
                log.warning("tail: %s", e)
            await asyncio.sleep(POLL)

    def _agent_settled(self, now: float, start: float, seen_working: bool, idle_since: float, fresh: bool) -> bool:
        """Idle for `debounce`s with no new updates, once "working" was seen (or GRACE elapsed)."""
        return (seen_working or now - start >= GRACE) and not fresh and now - idle_since >= self.debounce

    def _shell_quiet(self, now: float, start: float) -> bool:
        """`quiet`s since the prompt and since the last new output."""
        return now - start >= self.quiet and now - self.last_update_at >= self.quiet

    async def _wait_turn_end(self, start: float) -> str:
        """Agent turns end by `_agent_settled`, shell turns by `_shell_quiet`, either by cancel."""
        seen_working, idle_since, seen = False, None, self.last_update_at
        while True:
            await asyncio.sleep(POLL)
            now = time.monotonic()
            if self.cancelled:
                return "cancelled"
            fresh, seen = self.last_update_at > seen, self.last_update_at
            if not self.agent:
                if self._shell_quiet(now, start):
                    break
            elif self.status == "working":
                seen_working, idle_since = True, None
            else:
                idle_since = idle_since or now
                if self._agent_settled(now, start, seen_working, idle_since, fresh):
                    break
        log.info("turn done (%s)", "agent idle" if self.agent else "screen quiet")
        return "end_turn"

    async def prompt(self, session_id: str, prompt: list, **kw):
        text = "\n".join(b.text for b in prompt if getattr(b, "type", None) == "text")
        if self.footer:
            text += "\n\n" + self.footer
        self.recent_prompts = (self.recent_prompts + [text.strip()])[-5:]
        self.cancelled = False
        await self.transport.send_text(text)
        return PromptResponse(stop_reason=await self._wait_turn_end(time.monotonic()))


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
    asyncio.run(run_agent(PaneAgent(Herdr(a.pane), a.quiet, a.debounce, a.footer)))


def _selfcheck() -> None:
    """Turn-end rule against a scripted transport; no pane needed."""
    from acp import text_block

    class Fake:  # state()/read_screen() play their scripts, then repeat the last entry
        def __init__(self, states, screens=("",)):
            self.states, self.screens, self.sent, self.keys = list(states), list(screens), [], []
        async def info(self): return {"pane_id": "fake"}
        async def state(self): return self.states.pop(0) if len(self.states) > 1 else self.states[0]
        async def process(self): return (None, None)
        async def send_text(self, text): self.sent.append(text)
        async def send_keys(self, *keys): self.keys += keys
        async def read_screen(self, lines=200): return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]

    class Conn:
        def __init__(self): self.ups = []
        async def session_update(self, sid, u): self.ups.append(u)

    async def run(fake, quiet=0.1, debounce=0.0, mid=None):
        agent = PaneAgent(fake, quiet, debounce, footer="reply here")
        agent.on_connect(Conn())
        sid = (await agent.new_session("/tmp")).session_id
        t = time.monotonic()
        task = asyncio.create_task(agent.prompt(sid, [text_block("hi")]))
        if mid:
            await asyncio.sleep(0.03)
            await mid(agent, sid)
        r = await task
        assert fake.sent == ["hi\n\nreply here"], fake.sent
        return r.stop_reason, time.monotonic() - t, agent

    async def go():
        global POLL, GRACE
        POLL, GRACE = 0.01, 10.0  # (a) GRACE out of reach: only working->idle may end the turn
        stop, dt, a = await run(Fake([("fake", "working")] * 10 + [("fake", "idle")]), debounce=0.1)
        assert stop == "end_turn" and dt >= 0.1, (stop, dt)
        assert a.agent == "fake" and a.status == "idle" and a.last_update_at == 0.0, (a.agent, a.status)
        GRACE = 0.05  # (b) never saw "working": ends after GRACE
        stop, dt, _ = await run(Fake([("fake", "idle")]))
        assert stop == "end_turn" and dt >= 0.05, (stop, dt)
        # (c) shell: ends after `quiet` of unchanged screen; new lines were streamed
        stop, dt, a = await run(Fake([(None, "unknown")], ["$ \n", "$ pwd\n/tmp\n$ \n"]), quiet=0.1)
        assert stop == "end_turn" and dt >= 0.1, (stop, dt)
        assert [u.content.text for u in a.conn.ups if u.session_update == "agent_message_chunk"] == ["$ pwd\n/tmp\n"], a.conn.ups
        assert isinstance(a.reader, ScreenDiff) and a.last_update_at > 0
        # a second session/new replaces the tail task instead of stacking another
        old = a.tail
        await a.new_session("/tmp")
        await asyncio.wait([old])
        assert old.cancelled() and a.tail is not old and not a.tail.done()
        # (d) cancel mid-turn
        f = Fake([("fake", "working")])
        stop, _, _ = await run(f, mid=lambda ag, sid: ag.cancel(sid))
        assert stop == "cancelled" and f.keys == ["esc"], (stop, f.keys)
        print("main ok")

    asyncio.run(go())


if __name__ == "__main__":
    _selfcheck() if "--selfcheck" in sys.argv else main()
