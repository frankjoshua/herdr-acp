# herdr-acp — agent brief

You are building **herdr-acp**: an ACP (Agent Client Protocol) *agent* that wraps a Herdr pane.
An ACP client (first: Buzz's `buzz-acp` harness) spawns `herdr-acp --pane <id>` over stdio.
Prompts arriving over ACP get typed into the pane. Whatever the pane's agent does streams back
as ACP `session/update` notifications, and the turn ends when the pane's agent goes idle.

Owner: Joshua Frank (Josh). He is in another Herdr Space; ask him via your pane only when a
decision is genuinely his. Otherwise make the routine call and note it in `NOTES.md`.

## Why this exists (constraints Josh has stated — do not re-litigate)
- Herdr is Josh's surface and owns the agent. Buzz is **communication only**. Nothing may
  launch, resume, or replace the agent in the pane.
- He wants Buzz's typing indicator, presence, and the owner-only observer stream. Those are
  produced by `buzz-acp` from the ACP traffic, so we speak ACP to it and it needs **no changes**.
- Every channel message routes to the pane (no @mention requirement). `buzz-acp` handles the
  author gate (`--respond-to`, `--no-mention-filter`, `--subscribe all`) — not our concern.
- Interleaved turns are fine: Josh may type in the pane while a Buzz turn is open. Don't build
  turn isolation. Rule: a turn owns everything from its prompt until the next idle.
- Must work for Claude Code, Codex, Pi, and a blank shell. Claude first.
- No systemd, no supervisor, no auto-restart. Start/stop by hand (later: a Herdr keybinding).
- Ponytail rules apply: stdlib first, fewest files, no speculative abstractions, one self-check
  per non-trivial piece of logic.

## Architecture (three layers, one Python package `src/herdr_acp/`)
1. **transport** — interface with four calls: `send_text(pane, text)`, `read_screen(pane)`,
   `session_id(pane)`, `status_events(pane)` (idle/working stream). Implement **Herdr** only,
   via the `herdr` CLI (`herdr pane send-text`, `herdr pane read`, `herdr pane get` → 
   `agent_session.value`, `herdr agent wait --until idle`, `herdr agent get`). tmux comes later
   (ccgram has one to copy); keep the interface honest but don't write tmux now.
2. **reader** — "what happened since the prompt". Implement **Claude transcript** first:
   `~/.claude/projects/<cwd-with-slashes-as-dashes>/<session-id>.jsonl`, tail from a byte
   offset, map `assistant` text → `agent_message_chunk`, `tool_use` → `tool_call`,
   `tool_result` → `tool_call_update`, thinking → `agent_thought_chunk`. The **screen-diff**
   reader (strip ANSI, dedupe unchanged lines, emit new lines as text chunks) is the floor for
   unknown agents and blank shells; stdlib is enough, `pyte` only if raw bytes are needed.
   Codex (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`) and Pi
   (`~/.pi/agent/sessions/--<path>--/*.jsonl`, documented at pi.dev/docs/latest/session-format)
   are follow-ups, not now.
3. **acp** — the server, using the official Python SDK `agent-client-protocol` (installed in
   `.venv`, v0.12.1; exports `acp.Agent`, `acp.run_agent`). Handle `initialize`, `session/new`,
   `session/prompt`, `session/cancel`. On prompt: transport.send_text → stream reader events as
   `session/update` → wait for idle (debounce ~2s) → return `end_turn`. Announce the real
   protocol version. Cancel = `herdr agent send-keys` Escape (best effort).

Turn end: prefer Herdr's agent status (idle). Fallback for blank shells: screen quiet for N
seconds (configurable, default 5).

Reply path: buzz-acp does **not** post streamed text to the channel; the agent in the pane is
expected to reply itself with the Buzz CLI (`buzz messages send --channel <id> --content ...`).
So: (a) pass a footer on each prompt telling the agent the channel id / thread and to reply
with `buzz messages send`; the pane env must carry `BUZZ_PRIVATE_KEY`/`BUZZ_RELAY_URL`;
(b) add a `--reply-from-output` flag (default off) that posts the turn's final assistant text
itself, for blank shells that can't run the CLI. Do (a) first.

## Reference code (read, copy small pieces, do not vendor)
- ccgram (MIT): `/tmp/claude-1000/-home-josh-development-workspace-agent-workflow/f199ecd2-5bf6-4067-8721-538cd05acbfb/scratchpad/ccgram/src/ccgram/`
  — `providers/claude.py` + `providers/_jsonl.py` (Claude transcript parsing),
  `multiplexer/herdr.py` + `multiplexer/herdr_events.py` (Herdr transport incl. socket events),
  `providers/shell_infra.py` (prompt-marker detection for shells), `screen_buffer.py` (pyte).
  If that scratchpad path is gone: `git clone --depth 1 https://github.com/alexei-led/ccgram`.
- buzz-acp (the client we must satisfy): `~/buzz/crates/buzz-acp/` — README.md for the
  contract (bottom section lists what an agent must implement), `src/acp.rs` for how it
  consumes `session/update`, `src/lib.rs` ~L800-850 for which update types feed the observer.
- ACP spec + SDKs: https://agentclientprotocol.com , https://github.com/agentclientprotocol/python-sdk
  (look at the examples dir for a minimal agent). Use `context7` MCP if available for docs.
- A working reference for the *headless* path (what buzz-acp does with a real ACP agent):
  `~/development/workspace/buzz-acp-agent/run.sh`. Do not use it as the design; it owns the agent.

## Live environment facts
- Real Buzz relay: `ws://100.103.219.102:3000` (tailnet host `nostr-relay`). The local
  `~/buzz/deploy/compose` stack is a **stale clone — leave it stopped**.
- Agent keypair for testing: `~/.config/buzz-acp/agent.env` (`BUZZ_PRIVATE_KEY`, `BUZZ_PUBLIC_KEY`
  `3f4d0b23…`, `BUZZ_RELAY_URL` already pointed at the real relay). That key is a bot member of
  the test channel `#mini-games`, id `006645fa-f55b-4fc3-96fa-aa706f1ee0ab`. Josh's pubkey
  (owner): `a0226410133793033176c39185d9996d19aa55de9e7b6249f19fbee5ea176f54`.
- `buzz-acp` binary: `~/buzz/target/release/buzz-acp` (`--help` is thorough). Flags for our
  use: `--agent-command <path-to-herdr-acp> --agent-args "--pane <id>" --subscribe all
  --no-mention-filter --agent-owner <josh> --channels <id> --idle-timeout 120
  --multiple-event-handling queue`. Run it in **its own pane** in the target Space, never as a
  service. SIGTERM waits for in-flight turns; SIGKILL after a few seconds is fine.
- `buzz` CLI is on PATH (`~/.local/bin/buzz`). `buzz channels members/search`, `buzz messages
  get/send` are the useful ones. Do not archive/delete channels.
- Herdr CLI: `herdr pane list|get|read|send-text|send-keys|run|split|close`, `herdr agent
  get|prompt|wait|send-keys|read`. `herdr pane get <id>` returns `agent_session.value` = the
  Claude session id. Herdr socket: `~/.config/herdr/herdr.sock`. Plugin manifest format:
  https://raw.githubusercontent.com/herdrdev/herdr/v0.8.2/docs/next/website/src/content/docs/plugins.mdx
  (only needed for the later plugin wrapper; not now).
- Test pane: create your **own** test Space/pane with a Claude agent (`herdr workspace create
  --cwd <tmp repo> --no-focus`, then `herdr agent start`). Do not use Josh's panes, and do not
  send messages as Josh. For the author gate during tests use `--respond-to allowlist` with a
  test key you generate (the buzz CLI can't derive pubkeys; `agent.env` already holds one pair,
  and a second throwaway pair can be minted with `openssl rand -hex 32` + any nostr lib, or just
  use `--respond-to anyone` in a test channel you create).

## Definition of done for the first slice (prove the round trip by hand)
1. `herdr-acp --pane <id>` speaks ACP on stdio: `initialize` and `session/new` answered; a
   `session/prompt` types the text into the pane and returns `end_turn` when the pane goes idle,
   streaming at least `agent_message_chunk`s from the Claude transcript in between.
2. `buzz-acp` pointed at it, in a pane, connected to the real relay, subscribed to a test channel.
3. A message posted in that channel appears in the pane agent's session, the agent's reply lands
   back in the channel thread, and Buzz shows typing while it works and clears it after.
4. A blank-shell pane (no agent) round-trips a `pwd` via the screen-diff reader.
5. One runnable self-check per layer (`python -m herdr_acp.<layer>` or a tiny `tests/`), no
   frameworks beyond stdlib `unittest`/`assert`.
Commit small, often, on `main`. Keep `NOTES.md` with decisions + what's verified vs. assumed.
Tell Josh in your pane when the round trip works, or when you're blocked on something only he can
decide (e.g. Buzz UI questions — he has the phone app).
