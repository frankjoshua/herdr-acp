"""ACP server: `herdr-acp --pane <id>` on stdio.

The pane is a shared session: from `session/new` on, everything that happens in it (a human
typing, the agent's text and tool calls) streams to the client as `session/update`, whether or
not a prompt is in flight. A prompt is typed into the pane and its turn ends when the pane's
agent goes idle (or, for shells, the screen goes quiet). A choice dialog the agent opens during
a turn (approval, question) goes to the client as `session/request_permission`; the answer is
typed back into the pane."""

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
    text_block,
    tool_content,
)
from acp.schema import Implementation, PermissionOption, ToolCallUpdate

from .reader import (
    ClaudeTranscript,
    CodexRollout,
    PiSession,
    ScreenDiff,
    claude_transcript_for,
    option_kind,
    parse_dialog,
)
from .transport import Herdr

log = logging.getLogger("herdr-acp")
KNOWN = ("claude", "codex", "omp", "pi")  # agents with a transcript reader; anything else gets the screen diff
POLL = 0.5
GRACE = 10.0  # end an agent turn without ever seeing "working" only after this long idle
ANSWERED = 3.0  # how long a dialog we answered may stay on screen before it counts as a new one


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
        self.open_tool = None  # id of the last streamed tool call that has not finished
        self.can_ask = True  # False once the client failed a request_permission
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
        if self.cancelled:
            return  # already cancelled (e.g. via a cancelled permission request): a second Esc opens Claude's rewind
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
        elif kind in ("omp", "pi") and pid:
            reader = PiSession(pid, kind)
            log.info("tailing %s session %s", kind, reader.path)
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
                    if u.session_update == "tool_call":
                        self.open_tool = u.tool_call_id
                    elif u.session_update == "tool_call_update" and u.status in ("completed", "failed") \
                            and u.tool_call_id == self.open_tool:
                        self.open_tool = None
            except Exception as e:  # ponytail: pane gone or herdr hiccup; keep tailing
                log.warning("tail: %s", e)
            await asyncio.sleep(POLL)

    def _agent_settled(self, now: float, start: float, seen_working: bool, idle_since: float, fresh: bool) -> bool:
        """Idle for `debounce`s with no new updates, once "working" was seen (or GRACE elapsed)."""
        return (seen_working or now - start >= GRACE) and not fresh and now - idle_since >= self.debounce

    def _shell_quiet(self, now: float, start: float) -> bool:
        """`quiet`s since the prompt and since the last new output."""
        return now - start >= self.quiet and now - self.last_update_at >= self.quiet

    async def _dialog(self):
        """The choice dialog waiting in the pane, or None (also when the pane can't be read)."""
        try:
            return parse_dialog(await self.transport.read_visible())
        except Exception as e:  # herdr hiccup: no dialog this poll
            log.warning("dialog: %s", e)
            return None

    async def _ask(self, dialog) -> bool:
        """Put the pane's dialog to the client and type its choice. Returns once the dialog is
        answered (by the client or at the pane) or the turn is cancelled; False if the client
        can't answer, which leaves the dialog to the person at the pane."""
        question, labels, _, text = dialog
        if self.open_tool:  # the client already shows this tool call
            tool = ToolCallUpdate(tool_call_id=self.open_tool)
        else:
            tool = ToolCallUpdate(tool_call_id=f"dialog-{uuid.uuid4().hex[:8]}", title=question, kind="other",
                                  status="pending", content=[tool_content(text_block(text))] if text else None)
        options = [PermissionOption(option_id=str(i), name=lb, kind=option_kind(lb)) for i, lb in enumerate(labels)]
        shown = dialog[:2]
        log.info("asking: %s %s", question, labels)
        req = asyncio.create_task(self.conn.request_permission(session_id=self.session_id, tool_call=tool, options=options))
        while not req.done():
            await asyncio.wait([req], timeout=POLL)
            if req.done():
                break
            now = await self._dialog()
            if self.cancelled or (now and now[:2]) != shown:
                req.cancel()  # cancelled, or answered at the pane; the client's late answer is dropped
                log.info("dialog closed without the client's answer")
                return True
        try:
            outcome = req.result().outcome
        except Exception as e:
            log.warning("request_permission failed, leaving dialogs to the pane: %s", e)
            self.can_ask = False
            return False
        if outcome.outcome != "selected":  # the client cancelled the turn: close the dialog and end it
            await self.cancel(self.session_id)
            return True
        now = await self._dialog()
        if not now or now[:2] != shown:
            return True  # answered at the pane meanwhile
        await self.transport.select(int(outcome.option_id) - now[2])
        log.info("answered: %s", labels[int(outcome.option_id)])
        for _ in range(int(ANSWERED / POLL)):  # don't ask again while the TUI catches up
            await asyncio.sleep(POLL)
            now = await self._dialog()
            if not now or now[:2] != shown:
                break
        return True

    async def _wait_turn_end(self, start: float) -> str:
        """Agent turns end by `_agent_settled`, shell turns by `_shell_quiet`, either by cancel.
        A dialog on screen when the agent is blocked, or would otherwise settle, holds the turn
        open while the client answers it (Herdr doesn't flag every agent's dialogs as blocked)."""
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
                settled = self._agent_settled(now, start, seen_working, idle_since, fresh)
                if (settled or self.status == "blocked") and self.can_ask:
                    dialog = await self._dialog()
                    if dialog and await self._ask(dialog):
                        idle_since = None  # answered: the debounce starts over
                        continue
                if settled:
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
    """Turn-end rule and dialog answering against a scripted transport; no pane needed."""
    from acp import text_block
    from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

    class Fake:  # state()/read_screen() play their scripts, then repeat the last entry
        def __init__(self, states, screens=("",)):
            self.states, self.screens, self.sent, self.keys = list(states), list(screens), [], []
        async def info(self): return {"pane_id": "fake"}
        async def state(self): return self.states.pop(0) if len(self.states) > 1 else self.states[0]
        async def process(self): return (None, None)
        async def send_text(self, text): self.sent.append(text)
        async def send_keys(self, *keys): self.keys += keys
        async def read_screen(self, lines=200): return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
        async def read_visible(self): return ""

    class Dialog(Fake):  # blocked on a dialog until select()/Esc closes it or a mid-turn "human" answers it
        def __init__(self):
            super().__init__([("fake", "working")])
            self.dialog, self.steps = " Do you want to proceed?\n ❯ 1. Yes\n   2. Yes, and don't ask again\n   3. No (esc)\n", []
        async def state(self): return ("fake", "blocked") if self.dialog else ("fake", "idle")
        async def read_visible(self): return self.dialog or ""
        async def select(self, steps): self.steps.append(steps); self.dialog = None
        async def send_keys(self, *keys): self.keys += keys; self.dialog = None

    class Conn:
        def __init__(self, answer=None): self.ups, self.asked, self.answer = [], [], answer
        async def session_update(self, sid, u): self.ups.append(u)
        async def request_permission(self, session_id, tool_call, options):
            self.asked.append((tool_call, options))
            return await self.answer()

    async def run(fake, quiet=0.1, debounce=0.0, mid=None, answer=None):
        agent = PaneAgent(fake, quiet, debounce, footer="reply here")
        agent.on_connect(Conn(answer))
        sid = (await agent.new_session("/tmp")).session_id
        t = time.monotonic()
        task = asyncio.create_task(agent.prompt(sid, [text_block("hi")]))
        if mid:
            await asyncio.sleep(0.03)
            await mid(agent, sid)
        r = await task
        assert fake.sent == ["hi\n\nreply here"], fake.sent
        return r.stop_reason, time.monotonic() - t, agent

    async def pick_third():
        return RequestPermissionResponse(outcome=AllowedOutcome(option_id="2", outcome="selected"))

    async def never():
        await asyncio.Event().wait()

    async def fail():
        raise RuntimeError("method not found")

    async def dismissed():
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def close_at_pane(agent, sid):
        agent.transport.dialog = None

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
        # (e) a dialog goes to the client; its choice is typed (cursor on 1, "3." chosen: 2 rows down)
        stop, _, a = await run(f := Dialog(), answer=pick_third)
        (tool, options), = a.conn.asked
        assert stop == "end_turn" and f.steps == [2], (stop, f.steps)
        assert [(o.name, o.kind) for o in options] == [("Yes", "allow_once"), ("Yes, and don't ask again", "allow_always"),
                                                       ("No", "reject_once")], options
        assert tool.title == "Do you want to proceed?" and tool.tool_call_id.startswith("dialog-"), tool
        # (f) answered at the pane first: the request is dropped, nothing typed
        stop, _, a = await run(f := Dialog(), answer=never, mid=close_at_pane)
        assert stop == "end_turn" and f.steps == [] and len(a.conn.asked) == 1, (stop, f.steps)
        # (g) a client that can't answer: asked once, then the dialog is left to the pane
        stop, _, a = await run(f := Dialog(), answer=fail, mid=close_at_pane)
        assert stop == "end_turn" and len(a.conn.asked) == 1 and not a.can_ask, (stop, a.conn.asked)
        # (h) the client cancels the request: one Esc closes the dialog and the turn ends cancelled;
        # the session/cancel that follows sends no second Esc
        stop, _, a = await run(f := Dialog(), answer=dismissed)
        await a.cancel(a.session_id)
        assert stop == "cancelled" and f.keys == ["esc"] and len(a.conn.asked) == 1, (stop, f.keys, a.conn.asked)
        print("main ok")

    asyncio.run(go())


if __name__ == "__main__":
    _selfcheck() if "--selfcheck" in sys.argv else main()
