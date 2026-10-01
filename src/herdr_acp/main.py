"""ACP server: `herdr-acp --pane <id>` on stdio.

The pane is a shared session: from `session/new` on, everything that happens in it (a human
typing, the agent's text and tool calls) streams to the client as `session/update`, whether or
not a prompt is in flight. A prompt is typed into the pane. Its turn ends where the agent's own
transcript records the end of a turn; an agent without a transcript reader ends it when Herdr
says it settled; a shell, when it is back at its prompt. A choice dialog the agent opens during
a turn (approval, question) goes to the client as `session/request_permission`; the answer is
typed back into the pane. No decision here waits out a delay: POLL only paces reading."""

import argparse
import asyncio
import logging
import os
import sys
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
from acp.exceptions import RequestError
from acp.schema import Implementation, PermissionOption, ToolCallUpdate

from .reader import (
    DIALOG_HINT,
    TURN_END,
    Mark,
    ClaudeTranscript,
    CodexRollout,
    PiSession,
    ScreenDiff,
    claude_transcript_for,
    cursor_on,
    option_kind,
    parse_dialog,
)
from .transport import Herdr, HerdrError

log = logging.getLogger("herdr-acp")
KNOWN = ("claude", "codex", "omp", "pi")  # agents with a transcript reader; anything else gets the screen diff
POLL = 0.5  # how often the tail reads the transcript or screen, and a shell turn looks at its shell
# A key sent the moment a dialog is drawn can be lost (Claude: 2 in 3 at 0s, none from 0.1s): the
# dialog shows before it listens, and nothing tells when it starts to. A key the agent hasn't
# acted on within KEY_TAKEN is pressed again, at most PRESSES times, and only while that same
# dialog is still on screen with nothing moved on since.
KEY_TAKEN = 1.0
PRESSES = 3


class PaneAgent:
    def __init__(self, transport, footer: str = ""):
        self.transport, self.footer = transport, footer
        self.conn = None
        self.session_id = None
        self.tasks = []  # the session's tail and Herdr-event follower
        self.reader = None
        self.agent = None  # Herdr's name for the pane's agent; None for a shell
        self.pid = None  # the agent process the current reader was built for
        self.recent_prompts = []  # so a prompt's transcript echo isn't re-streamed as user input
        self.open_tool = None  # id of the last streamed tool call that has not finished
        self.can_ask = True  # False once the client failed a request_permission
        self.cancelled = False
        self.dismissed = False  # the client dismissed a dialog this turn: the turn ends cancelled
        self.turn_base = 0  # len(self.marks) when the current turn's prompt went out
        # What the pane did, as counters the turn waits on (all advanced under `changed`):
        self.moves = 0  # updates read from the reader
        self.starts = 0  # user messages recorded (a typed prompt or a human's input)
        self.marks = []  # the reader's Marks (user messages, turn ends), in read order
        # Signs that a dialog was answered: tool results and turn ends (not a new tool call or text,
        # which Claude may write just after its dialog shows), and Herdr seeing the agent leave blocked.
        self.results = 0
        self.unblocked = 0
        self.herdr_status = None  # the pane's agent status as Herdr last reported it
        self.repick = True  # the agent may have changed (Herdr said so): re-pick the reader on the next pass
        self.pumping = asyncio.Lock()  # one reader pass at a time
        self.changed = asyncio.Condition()

    def on_connect(self, conn):
        self.conn = conn

    async def initialize(self, protocol_version: int, **kw):
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_info=Implementation(name="herdr-acp", version="0.0.1"),
        )

    async def new_session(self, cwd: str, **kw):
        self.agent = (await self.transport.info()).get("agent")  # also fails fast if the pane is gone
        self.session_id = str(uuid.uuid4())
        for t in self.tasks:
            t.cancel()
        self.tasks = [asyncio.create_task(self._tail()), asyncio.create_task(self._follow())]
        return NewSessionResponse(session_id=self.session_id)

    async def cancel(self, session_id: str, **kw):
        if self.cancelled:
            return  # already cancelled (e.g. via a cancelled permission request): a second Esc opens Claude's rewind
        self.cancelled = True
        try:
            await self.transport.send_keys("esc")
        except Exception as e:  # best effort, like _tail
            log.warning("cancel: %s", e)
        await self._notify()

    async def _notify(self) -> None:
        async with self.changed:
            self.changed.notify_all()

    async def _until(self, done) -> None:
        async with self.changed:
            await self.changed.wait_for(done)

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
        self.reader, self.pid = reader, pid  # together, and only once the build succeeded (else retried next pass)

    async def _pump(self) -> None:
        """Read what the reader has now and stream it."""
        async with self.pumping:
            if self.reader is None or self.repick:  # a failed pick (agent still starting) is retried next pass
                await self._pick_reader()
                self.repick = False
            ups = await self.reader.poll()
            for u in ups:
                if isinstance(u, Mark):
                    self.marks.append(u)
                    if u.kind == "end":
                        self.results += 1
                    else:
                        self.starts += 1
                    continue
                self.moves += 1
                if u.session_update == "tool_call_update" and u.status in ("completed", "failed"):
                    self.results += 1
                if u.session_update == "user_message_chunk" and u.content.text.strip() in self.recent_prompts:
                    continue
                await self.conn.session_update(self.session_id, u)
                if u.session_update == "tool_call":
                    self.open_tool = u.tool_call_id
                elif u.session_update == "tool_call_update" and u.status in ("completed", "failed") \
                        and u.tool_call_id == self.open_tool:
                    self.open_tool = None
        if ups:
            await self._notify()

    async def _tail(self) -> None:
        """Stream whatever happens in the pane, every POLL."""
        while True:
            try:
                await self._pump()
            except Exception as e:  # ponytail: pane gone or herdr hiccup; keep tailing
                log.warning("tail: %s", e)
            await asyncio.sleep(POLL)

    async def _follow(self) -> None:
        """Herdr's events for the pane: flag a reader re-pick when the agent changes (so a
        restarted agent is followed), and count each time the agent leaves blocked (a stale
        `working` from the turn's start, arriving after a dialog showed, is not one)."""
        while True:
            try:
                async for _, data in self.transport.events():
                    status = data.get("agent_status")
                    if status:
                        if self.herdr_status == "blocked" and status != "blocked":
                            self.unblocked += 1
                        self.herdr_status = status
                    if "agent" in data:
                        self.agent = None if data.get("released") else data["agent"]
                    self.repick = True
                    await self._notify()
            except HerdrError as e:
                if e.code == "events_lost":
                    continue
                log.warning("events: %s; a restarted agent is picked up at the next prompt", e)
                return
            except Exception as e:
                log.warning("events: %r; a restarted agent is picked up at the next prompt", e)
                return

    async def prompt(self, session_id: str, prompt: list, **kw):
        text = "\n".join(b.text for b in prompt if getattr(b, "type", None) == "text")
        if self.footer:
            text += "\n\n" + self.footer
        self.cancelled = self.dismissed = False
        info = await self.transport.info()
        self.agent, self.herdr_status, self.repick = info.get("agent"), info.get("agent_status"), True
        if open_dialog := parse_dialog(await self.transport.read_visible()):  # typing now would land in it
            raise RequestError.invalid_request({"reason": "the pane is waiting on a dialog", "dialog": open_dialog.question})
        await self._pump()  # picks the reader; anything already written belongs before this prompt
        self.recent_prompts = (self.recent_prompts + [text.strip()])[-5:]  # only a prompt actually sent
        if not isinstance(self.reader, ScreenDiff):
            stop = await self._transcript_turn(text)
        elif self.agent:
            stop = await self._herdr_turn(text)
        else:
            stop = await self._shell_turn(text)
        await self._pump()  # the turn's last lines go out before its answer
        log.info("turn done (%s)", stop)
        return PromptResponse(stop_reason=stop)

    def _turn_ended(self, base: int) -> bool:
        """A turn end recorded for the first user message read since mark `base`: stamped no earlier
        than it, or (no timestamps) read after it. A late end of an earlier, cancelled turn is
        stamped before the new prompt, so it doesn't count, wherever it lands in the file."""
        new = self.marks[base:]
        user = next((i for i, m in enumerate(new) if m.kind == "user"), None)
        if user is None:
            return False
        u = new[user]
        return any(m.kind == "end" and (m.at >= u.at if m.at and u.at else i > user) for i, m in enumerate(new))

    async def _transcript_turn(self, text: str) -> str:
        """Ends at the turn end recorded for this prompt (`_turn_ended`). A prompt that starts no
        turn (a built-in slash command) records nothing: then Herdr's stall verdict ends it."""
        starts, moves, stalled = self.starts, self.moves, False
        self.turn_base = base = len(self.marks)

        async def submit():
            nonlocal stalled
            try:
                if self.agent:
                    await self.transport.prompt(text)  # Herdr refuses (agent_blocked) while a dialog is open
                else:  # an agent Herdr can't see (codex under tmux): we type it, and check it went
                    await self._type(text, starts)
            except HerdrError as e:
                if e.code != "agent_prompt_stalled":
                    raise
                await self._pump()
                stalled = True
            finally:
                await self._notify()

        sent = asyncio.create_task(submit())
        dialogs = asyncio.create_task(self._dialogs())
        try:
            await self._until(lambda: self.cancelled or self._turn_ended(base)
                              or (sent.done() and (sent.exception() or (stalled and self.moves == moves))))
            if sent.done() and sent.exception():
                raise sent.exception()
            return "cancelled" if self.cancelled or self.dismissed else "end_turn"
        finally:
            sent.cancel()
            dialogs.cancel()

    async def _herdr_turn(self, text: str) -> str:
        """An agent without a transcript reader: Herdr's lifecycle ends the turn."""
        sent = asyncio.create_task(self.transport.prompt(text))
        stop = asyncio.create_task(self._until(lambda: self.cancelled))
        try:
            await asyncio.wait([sent, stop], return_when=asyncio.FIRST_COMPLETED)
            if sent.done():
                try:
                    sent.result()
                except HerdrError as e:
                    if e.code != "agent_prompt_stalled":  # stalled: Herdr saw no turn start
                        raise
            return "cancelled" if self.cancelled else "end_turn"
        finally:
            sent.cancel()
            stop.cancel()

    async def _shell_turn(self, text: str) -> str:
        """Ends when the shell is back at its prompt: it owns the terminal again, the screen
        changed since the command was typed, and the bottom row is no longer the typed command."""
        before = await self.transport.read_visible()
        typed = (text.strip().splitlines() or [""])[-1].strip()
        await self.transport.send(text)
        while not self.cancelled:
            await asyncio.sleep(POLL)
            if await self.transport.shell_foreground():
                screen = await self.transport.read_visible()
                bottom = next((r.strip() for r in reversed(screen.splitlines()) if r.strip()), "")
                if screen != before and not (typed and bottom.endswith(typed)):
                    return "end_turn"
        return "cancelled"

    async def _dialogs(self) -> None:
        """For the length of a turn: each dialog the screen shows goes to the client. A dialog is
        over once the agent shows it was answered (a tool result or turn end in the transcript, or
        Herdr seeing it leave blocked), so one dialog is asked once."""
        try:
            while self.can_ask and not self.cancelled and not self.dismissed:
                screen = await self.transport.wait_output(DIALOG_HINT)
                await self._pump()
                results, unblocked = self.results, self.unblocked

                def moved():
                    return self.results > results or self.unblocked > unblocked

                if dialog := parse_dialog(screen):
                    await self._ask(dialog, moved)
                await self._until(lambda: self.cancelled or self.dismissed or moved())
        except HerdrError as e:
            log.warning("dialogs: %s; left to the pane for this turn", e)

    async def _ask(self, dialog, moved) -> None:
        """Put the dialog to the client and type its choice, unless the agent moves on first
        (answered at the pane). A client that can't answer leaves dialogs to the pane from then on."""
        if self.open_tool:  # the client already shows this tool call
            tool = ToolCallUpdate(tool_call_id=self.open_tool)
        else:
            tool = ToolCallUpdate(tool_call_id=f"dialog-{uuid.uuid4().hex[:8]}", title=dialog.question, kind="other",
                                  status="pending", content=[tool_content(text_block(dialog.text))] if dialog.text else None)
        options = [PermissionOption(option_id=str(i), name=lb, kind=option_kind(lb)) for i, lb in enumerate(dialog.labels)]
        log.info("asking: %s %s", dialog.question, dialog.labels)
        req = asyncio.create_task(self.conn.request_permission(session_id=self.session_id, tool_call=tool, options=options))
        gone = asyncio.create_task(self._until(lambda: self.cancelled or moved()))
        try:
            await asyncio.wait([req, gone], return_when=asyncio.FIRST_COMPLETED)
            if not req.done():
                log.info("dialog answered at the pane; the client's request is dropped")
                return
            try:
                outcome = req.result().outcome
            except Exception as e:
                log.warning("request_permission failed, leaving dialogs to the pane: %s", e)
                self.can_ask = False
                return
            if outcome.outcome != "selected":  # the client cancelled the turn: Esc closes the dialog, the turn ends
                self.dismissed = True
                await self._press(dialog, moved, lambda now: self.transport.send_keys("esc"))
                await self._pump()
                if getattr(self.reader, "kind", None) == "omp" and not self._turn_ended(self.turn_base):
                    await self.transport.send_keys("esc")  # OMP: Esc only denied the tool and the turn goes on
                self.cancelled = True  # session/cancel that follows sends no further Esc
                await self._notify()
                return
            i = int(outcome.option_id)
            await self._press(dialog, moved, lambda now: self.transport.select(i - now.cursor, cursor_on(now, i), int(KEY_TAKEN * 1000)))
            log.info("answered: %s", dialog.labels[i])
        except HerdrError as e:
            log.warning("couldn't answer the dialog, left to the pane: %s", e)
        finally:
            req.cancel()
            gone.cancel()

    async def _type(self, text: str, starts: int) -> None:
        """Type a prompt and Enter, then confirm the agent took it: its transcript records the user
        message. Through a tmux client an Enter can arrive with the pasted text and be swallowed
        (seen: the prompt sat unsent in Codex's input box), so it is pressed again if nothing is
        recorded within KEY_TAKEN. An extra Enter on an emptied input box does nothing."""
        await self.transport.send(text)
        for _ in range(PRESSES - 1):
            try:
                await asyncio.wait_for(self._until(lambda: self.starts > starts), KEY_TAKEN)
                return
            except asyncio.TimeoutError:
                await self._pump()
                if self.starts > starts:
                    return
                log.info("the prompt wasn't submitted; pressing Enter again")
                await self.transport.send_keys("enter")

    async def _press(self, dialog, moved, press) -> None:
        """`press(dialog now on screen)` until the agent takes it: the agent moves on or that
        dialog leaves the screen. Pressed again only while it is still there unanswered."""
        for attempt in range(PRESSES):
            now = parse_dialog(await self.transport.read_visible())
            # Read the transcript after the screen: a dialog identical to this one (OMP's next "Allow
            # tool: bash") only shows once the previous tool's result is written, so it counts as moved.
            await self._pump()
            if moved() or not now or now[:2] != dialog[:2]:
                return  # taken (or answered at the pane)
            if attempt:
                log.info("the dialog didn't take the key; pressing again")
            try:
                await press(now)
                await asyncio.wait_for(self._until(moved), KEY_TAKEN)
                return
            except (asyncio.TimeoutError, HerdrError) as e:  # not taken, or the cursor never got there
                if isinstance(e, HerdrError) and e.code != "timeout":
                    raise
        raise HerdrError("not_taken", f"the dialog ignored {PRESSES} presses")


def main() -> None:
    ap = argparse.ArgumentParser(prog="herdr-acp")
    ap.add_argument("--pane", required=True, help="Herdr pane id, e.g. wV:p9")
    ap.add_argument("--footer", default=os.environ.get("HERDR_ACP_FOOTER", ""),
                    help="text appended to every prompt (env HERDR_ACP_FOOTER); clients use it for reply instructions")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG if a.verbose else logging.INFO,
                        format="herdr-acp %(levelname)s %(message)s")
    asyncio.run(run_agent(PaneAgent(Herdr(a.pane), a.footer)))


def _selfcheck() -> None:
    """Turn ends and dialog answering against a scripted pane; no Herdr needed."""
    import re

    from acp import start_tool_call, update_agent_message_text, update_tool_call, update_user_message_text
    from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

    DIALOG = (" Do you want to proceed?\n ❯ 1. Yes\n   2. Yes, and don't ask again\n   3. No (esc)\n\n"
              " Esc to cancel · Tab to amend\n")

    class Reader:  # a transcript: poll() hands out what the script pushed
        def __init__(self, kind): self.queue, self.kind = [], kind
        def push(self, *items): self.queue += items
        async def poll(self): out, self.queue = self.queue, []; return out

    class Pane:  # a scripted Herdr pane; `on_prompt`/`on_select` play the agent
        def __init__(self, agent="claude", on_prompt=None, on_select=None, deaf=0, herdr_sees=True):
            self.agent, self.on_prompt, self.on_select = agent, on_prompt, on_select
            self.screen, self.sent, self.keys, self.selected = "", [], [], []
            self.fg, self.events_q = [True], asyncio.Queue()
            self.deaf = deaf  # how many keys the agent drops (a dialog drawn before it listens, an Enter in a paste)
            self.herdr_sees = herdr_sees  # False: behind a tmux client, Herdr reports no agent
        async def info(self): return {"pane_id": "fake", "agent": self.agent if self.herdr_sees else None}
        async def process(self): return (1, self.agent)
        async def events(self):
            while True:
                yield "pane.agent_status_changed", await self.events_q.get()
        async def prompt(self, text):
            self.sent.append(text)
            return await self.on_prompt(self)
        async def send(self, text):
            self.sent.append(text)
            if self.on_prompt and not (self.agent and self._deaf()):  # an agent may swallow the Enter
                await self.on_prompt(self)
        def _deaf(self):
            self.deaf -= 1
            return self.deaf >= 0
        async def send_keys(self, *keys):
            self.keys += keys
            if keys == ("enter",) and not self.screen and self.on_prompt:  # Enter on the unsent prompt
                await self.on_prompt(self)
            if "esc" in keys and self.screen and not self._deaf():  # Esc on the dialog: closed, Herdr sees it unblock
                self.screen = ""
                self.events_q.put_nowait({"pane_id": "fake", "agent": self.agent, "agent_status": "idle"})
                if self.agent != "omp":  # Claude and Codex end the turn there (marker written a bit later);
                    asyncio.ensure_future(later(0.05, lambda: self.reader.push(TURN_END)))  # OMP goes on
        async def select(self, steps, row, timeout_ms=5000):
            self.selected.append((steps, row))
            if not self._deaf():
                await self.on_select(self)
        async def wait_output(self, regex, timeout_ms=None, lines=25):
            while not re.search(regex, self.screen, re.M):
                await asyncio.sleep(0.001)
            return self.screen
        async def read_visible(self): return self.screen
        async def read_screen(self, lines=200): return self.screen
        async def shell_foreground(self): return self.fg.pop(0) if len(self.fg) > 1 else self.fg[0]

    class Agent(PaneAgent):  # the script's reader (shared with the pane script) instead of a real transcript
        async def _pick_reader(self):
            p = self.transport
            self.reader = self.reader or (Reader(p.agent) if p.agent in KNOWN else ScreenDiff(p.read_screen))
            p.reader = self.reader

    class Conn:
        def __init__(self, answer=None, on_ask=None):
            self.ups, self.asked, self.dropped, self.answer, self.on_ask = [], [], 0, answer, on_ask
        async def session_update(self, sid, u): self.ups.append(u)
        async def request_permission(self, session_id, tool_call, options):
            self.asked.append((tool_call, options))
            if self.on_ask:
                asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.on_ask()))
            try:
                return await self.answer()
            except asyncio.CancelledError:
                self.dropped += 1
                raise

    def said(a, kind="agent_message_chunk"):
        return [u.content.text for u in a.conn.ups if u.session_update == kind]

    async def run(pane, answer=None, on_ask=None, mid=None, text="hi", footer="reply here"):
        a = Agent(pane, footer=footer)
        a.on_connect(Conn(answer, on_ask and (lambda: on_ask(a))))
        sid = (await a.new_session("/tmp")).session_id
        task = asyncio.create_task(a.prompt(sid, [text_block(text)]))
        if mid:
            await asyncio.sleep(0.02)
            await mid(a)
        try:
            return (await task).stop_reason, a
        finally:
            for t in a.tasks:
                t.cancel()

    def echo(pane, at=""):  # the prompt as the agent records it: the message, then its Mark
        return update_user_message_text(pane.sent[-1].strip()), Mark("user", at)

    def end(at=""):
        return Mark("end", at)

    async def later(delay, fn):
        await asyncio.sleep(delay)
        fn()

    async def go():
        global POLL, KEY_TAKEN
        POLL, KEY_TAKEN = 0.002, 0.05

        # (a) a turn ends at the transcript's TURN_END, not at Herdr's verdict; the echo isn't streamed
        async def answers(p):
            asyncio.ensure_future(later(0.02, lambda: p.reader.push(*echo(p), update_agent_message_text("hello"), TURN_END)))
            return "done"  # Herdr settles before the transcript is read
        pane = Pane(on_prompt=answers)
        stop, a = await run(pane)
        assert stop == "end_turn" and said(a) == ["hello"] and pane.sent == ["hi\n\nreply here"], (stop, a.conn.ups)

        # (b) a late TURN_END of an earlier (cancelled) turn doesn't end the new one: read before
        # the new prompt is recorded, or stamped before it
        async def stale_then_real(p):
            p.reader.push(TURN_END)
            asyncio.ensure_future(later(0.03, lambda: p.reader.push(*echo(p), update_agent_message_text("real"), TURN_END)))
            return "working"
        pane = Pane(on_prompt=stale_then_real)
        stop, a = await run(pane)
        assert stop == "end_turn" and said(a) == ["real"], (stop, said(a))
        async def stale_stamped(p):
            p.reader.push(*echo(p, "2026-10-01T10:00:02Z"), end("2026-10-01T10:00:01Z"))  # written late, stamped early
            asyncio.ensure_future(later(0.03, lambda: p.reader.push(update_agent_message_text("real"), end("2026-10-01T10:00:03Z"))))
            return "working"
        stop, a = await run(Pane(on_prompt=stale_stamped))
        assert stop == "end_turn" and said(a) == ["real"], (stop, said(a))
        # under load an agent writes the turn's end before its user message (Claude, seen in CI): the
        # timestamps still say whose end it is
        async def reordered(p):
            p.reader.push(update_agent_message_text("late echo"), end("2026-10-01T10:00:02Z"))
            asyncio.ensure_future(later(0.02, lambda: p.reader.push(*echo(p, "2026-10-01T10:00:01Z"))))
            return "done"
        stop, a = await run(Pane(on_prompt=reordered))
        assert stop == "end_turn" and said(a) == ["late echo"], (stop, said(a))

        # (c) Herdr stalls: nothing recorded → the prompt started no turn; a recorded prompt → wait for TURN_END
        async def stall(p):
            raise HerdrError("agent_prompt_stalled", "no state change")
        stop, a = await run(Pane(on_prompt=stall), text="/cost")
        assert stop == "end_turn" and a.moves == 0, stop
        async def stall_but_working(p):  # OMP: Herdr never sees it work, the transcript does
            p.reader.push(*echo(p))
            asyncio.ensure_future(later(0.03, lambda: p.reader.push(update_agent_message_text("omp"), TURN_END)))
            raise HerdrError("agent_prompt_stalled", "no state change")
        stop, a = await run(Pane(agent="omp", on_prompt=stall_but_working))
        assert stop == "end_turn" and said(a) == ["omp"], said(a)

        # (d) a prompt while a dialog is open is refused, nothing typed: by Herdr (its status says
        # blocked) or by herdr-acp (the screen shows the dialog; Herdr's status can lag behind it)
        async def blocked(p):
            raise HerdrError("agent_blocked", "agent is blocked")
        try:
            await run(Pane(on_prompt=blocked))
            raise AssertionError("agent_blocked swallowed")
        except HerdrError as e:
            assert e.code == "agent_blocked", e
        pane = Pane(on_prompt=lambda p: asyncio.Event().wait())
        pane.screen = DIALOG
        a = Agent(pane, footer="")
        a.on_connect(Conn())
        sid = (await a.new_session("/tmp")).session_id
        try:
            await a.prompt(sid, [text_block("same words")])
            raise AssertionError("prompt typed into an open dialog")
        except RequestError as e:
            assert e.data["dialog"] == "Do you want to proceed?" and pane.sent == [], (e.data, pane.sent)
        # the refused prompt was never typed, so the same words typed by a human later are theirs
        await a._pump()
        a.reader.push(update_user_message_text("same words"))
        await a._pump()
        assert said(a, "user_message_chunk") == ["same words"], a.conn.ups
        for t in a.tasks:
            t.cancel()

        # (e) a dialog goes to the client against the open tool call; the choice is typed once the
        # cursor is on it (cursor on 1, "3." chosen: 2 rows down, regex on the "3. No" row)
        async def tool_then_dialog(p):
            p.reader.push(*echo(p), start_tool_call("t1", "Bash: rm x", kind="execute", status="in_progress"))
            asyncio.ensure_future(later(0.02, lambda: setattr(p, "screen", DIALOG)))
            return "blocked"
        async def rejected(p):
            p.screen = ""
            p.reader.push(update_tool_call("t1", status="failed"), TURN_END)
        async def pick_third():
            return RequestPermissionResponse(outcome=AllowedOutcome(option_id="2", outcome="selected"))
        pane = Pane(on_prompt=tool_then_dialog, on_select=rejected)
        stop, a = await run(pane, answer=pick_third)
        (tool, options), = a.conn.asked
        assert stop == "end_turn" and pane.selected == [(2, cursor_on(parse_dialog(DIALOG), 2))], pane.selected
        assert tool.tool_call_id == "t1" and [(o.name, o.kind) for o in options] == [
            ("Yes", "allow_once"), ("Yes, and don't ask again", "allow_always"), ("No", "reject_once")], options

        # (f) answered at the pane first: the transcript moves on, the request is dropped while the
        # turn goes on, nothing typed
        async def never():
            await asyncio.Event().wait()
        async def human_answers(a):
            a.transport.screen = ""
            a.reader.push(update_tool_call("t1", status="completed"))
            await asyncio.sleep(0.02)
            assert a.conn.dropped == 1, a.conn.dropped
            a.reader.push(TURN_END)
        pane = Pane(on_prompt=tool_then_dialog)
        stop, a = await run(pane, answer=never, on_ask=human_answers)
        assert stop == "end_turn" and pane.selected == [] and len(a.conn.asked) == 1, (stop, pane.selected)

        # (g) the same, seen only by Herdr (Codex records nothing until the command is done): the
        # agent leaving blocked drops the request; a late "blocked" event does not
        async def herdr_sees_it(a):
            # a stale "working" from the turn's start, arriving after the dialog showed, is not an answer
            a.transport.events_q.put_nowait({"pane_id": "fake", "agent": "claude", "agent_status": "working"})
            a.transport.events_q.put_nowait({"pane_id": "fake", "agent": "claude", "agent_status": "blocked"})
            await asyncio.sleep(0.02)
            assert a.conn.dropped == 0, "a blocked event closed the dialog"
            a.transport.screen = ""
            a.transport.events_q.put_nowait({"pane_id": "fake", "agent": "claude", "agent_status": "working"})
            await asyncio.sleep(0.02)
            assert a.conn.dropped == 1, a.conn.dropped
            a.reader.push(TURN_END)
        pane = Pane(on_prompt=tool_then_dialog)
        stop, a = await run(pane, answer=never, on_ask=herdr_sees_it)
        assert stop == "end_turn" and pane.selected == [] and len(a.conn.asked) == 1

        # an agent Herdr can't see (Codex behind tmux) swallows the Enter of the typed prompt: pressed
        # again once nothing is recorded; the turn then runs normally
        pane = Pane(agent="codex", herdr_sees=False, deaf=1, on_prompt=answers)
        stop, a = await run(pane)
        assert stop == "end_turn" and said(a) == ["hello"] and pane.keys == ["enter"], (stop, said(a), pane.keys)

        # (h) a client that can't answer: asked once, then dialogs are left to the pane
        async def fail():
            raise RuntimeError("method not found")
        async def human_at_pane(a):
            a.transport.screen = ""
            a.reader.push(update_tool_call("t1", status="completed"), TURN_END)
        pane = Pane(on_prompt=tool_then_dialog)
        stop, a = await run(pane, answer=fail, on_ask=human_at_pane)
        assert stop == "end_turn" and len(a.conn.asked) == 1 and not a.can_ask, a.conn.asked

        # (i) the client dismisses the request: one Esc, the turn ends cancelled; the session/cancel
        # that follows sends no second Esc (two open Claude's rewind)
        async def dismissed():
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        pane = Pane(on_prompt=tool_then_dialog)
        stop, a = await run(pane, answer=dismissed)
        await a.cancel(a.session_id)
        assert stop == "cancelled" and pane.keys == ["esc"] and len(a.conn.asked) == 1, (stop, pane.keys)
        # OMP: Esc only denies the tool and the turn goes on, so a second Esc stops it
        pane = Pane(agent="omp", on_prompt=tool_then_dialog)
        stop, a = await run(pane, answer=dismissed)
        assert stop == "cancelled" and pane.keys == ["esc", "esc"], (stop, pane.keys)
        # a dialog drawn before it listens drops the first key: pressed again, still asked once
        pane = Pane(on_prompt=tool_then_dialog, deaf=1)
        stop, a = await run(pane, answer=dismissed)
        assert stop == "cancelled" and pane.keys == ["esc", "esc"] and len(a.conn.asked) == 1, (stop, pane.keys)
        pane = Pane(on_prompt=tool_then_dialog, on_select=rejected, deaf=1)
        stop, a = await run(pane, answer=pick_third)
        assert stop == "end_turn" and len(pane.selected) == 2 and len(a.conn.asked) == 1, (pane.selected, a.conn.asked)
        # a taken key is never pressed again, even when the next dialog looks the same (OMP's
        # "Allow tool: bash"): its tool result is in the transcript before that dialog shows
        async def next_same_dialog(p):
            if len(p.selected) == 1:
                p.reader.push(update_tool_call("t1", status="failed"))
                p.screen = DIALOG  # the same question and options again, for the next call
            else:
                await rejected(p)
        pane = Pane(on_prompt=tool_then_dialog, on_select=next_same_dialog)
        POLL = 10.0  # the tail sleeps: only _press's own transcript read can see the tool result
        stop, a = await run(pane, answer=pick_third)
        POLL = 0.002
        assert stop == "end_turn" and len(pane.selected) == 2 and len(a.conn.asked) == 2, (pane.selected, a.conn.asked)

        # (j) shell: not done while a command owns the terminal, nor while the shell (in the
        # foreground already) still shows only the typed command; done back at the prompt
        async def typed(p):
            p.screen = "$ sleep 1\n"
            asyncio.ensure_future(later(0.03, lambda: setattr(p, "screen", "$ sleep 1\n$ \n")))
        pane = Pane(agent=None, on_prompt=typed)
        pane.screen, pane.fg = "$ \n", [False, False, True]
        stop, a = await run(pane, text="sleep 1", footer="")
        assert stop == "end_turn" and pane.screen == "$ sleep 1\n$ \n", (stop, pane.screen)
        # (k) cancel mid-turn: Esc, cancelled; a second session/new replaces the session's tasks
        async def busy(p):
            return await asyncio.Event().wait()
        pane = Pane(on_prompt=busy)
        stop, a = await run(pane, mid=lambda a: a.cancel(a.session_id))
        assert stop == "cancelled" and pane.keys == ["esc"], (stop, pane.keys)
        old = a.tasks
        await a.new_session("/tmp")
        await asyncio.wait(old)
        assert all(t.cancelled() for t in old) and not any(t.done() for t in a.tasks)
        for t in a.tasks:
            t.cancel()
        # (l) an agent without a transcript reader: Herdr's settle ends the turn
        async def settles(p):
            return "done"
        stop, a = await run(Pane(agent="gemini", on_prompt=settles))
        assert stop == "end_turn" and isinstance(a.reader, ScreenDiff), stop
        print("main ok")

    asyncio.run(asyncio.wait_for(go(), 20))  # a broken case hangs a turn; fail it instead


if __name__ == "__main__":
    _selfcheck() if "--selfcheck" in sys.argv else main()
