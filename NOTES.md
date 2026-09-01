# NOTES

## Run it
```
# in its own pane in the target Space (never a service):
set -a; . ~/.config/buzz-acp/agent.env; set +a
~/buzz/target/release/buzz-acp --agent-command $REPO/.venv/bin/herdr-acp --agent-args=--pane,<PANE> \
  --subscribe all --no-mention-filter --agent-owner <josh-pubkey> --channels <channel-id> \
  --idle-timeout 120 --multiple-event-handling queue
```
`--agent-args` is comma-delimited in clap (`--pane,w47:p1`); a quoted `"--pane w47:p1"` is rejected.
The pane's process env must carry `BUZZ_PRIVATE_KEY`/`BUZZ_RELAY_URL` (`herdr workspace create --env ...`).

Self-checks: `python -m herdr_acp.reader`, `python -m herdr_acp.transport <pane>`,
`python tests/roundtrip.py <pane> "pwd"`.

## Decisions
- **Reply goes to the channel, not a thread** (Josh, 2026-09-01). buzz-acp's prompt tells the agent
  to `--reply-to` the trigger; there is no flag to turn that off, so `--reply channel` (default)
  appends a short override footer; `--reply thread` sends the prompt untouched. Verified: reply
  event has no `e` tag.
- **No other footer on prompts.** buzz-acp already puts `[Context] Channel: … (#uuid)`, thread root, and
  "reply with `buzz messages send --reply-to <id>`" into every prompt (queue.rs `format_context_hints`),
  and the standing/base prompt arrives in the first prompt of a session. Claude replied threaded
  without any help from us. `--reply-from-output` (for blank shells) is still a follow-up.
- **Turn end = poll `herdr pane get` every 0.5s.** `herdr agent wait --until idle` returns immediately
  if Herdr hasn't flipped the pane to `working` yet, so instead: end when status is idle/done/blocked
  for `--debounce` (2s) with no new transcript lines, after `working` was seen at least once
  (or 10s grace if it never was — fast answers). `blocked` (permission prompt) ends the turn too;
  Josh unblocks in the pane. Shell panes: screen unchanged for `--quiet` (5s).
- **Transcript located by glob** `~/.claude/projects/*/<session>.jsonl`, not by re-deriving Claude's
  cwd mangling. The file doesn't exist until Claude's first turn, so it's resolved lazily per poll.
- **Screen diff uses `difflib.SequenceMatcher`** on stripped lines; emits insert/replace lines.
  Good enough for shells; prompt-marker detection (ccgram shell_infra) only if this proves noisy.
- **Sidechain (subagent) transcript entries are skipped.**
- **Enter is sent 0.5s after `send-text`** (ccgram finding: a batched Enter is swallowed by TUIs).
- Unimplemented ACP methods (load_session, set_session_mode, …) fall through to the SDK's
  method_not_found; buzz-acp is fine with that (`steering_supported=false`).

## Verified (2026-09-01)
- DoD 1: `initialize`/`session/new`/`session/prompt` over stdio; tool_call, tool_call_update,
  agent_message_chunk streamed from the transcript; `end_turn` on idle. (tests/roundtrip.py)
- DoD 2: buzz-acp in pane w47:p2 of test Space "herdr-acp-test", real relay, channel
  `#herdr-acp-test` (5f620455-4d51-4950-89a6-7a896d63abf7, ephemeral, 3-day idle TTL).
- DoD 3: message from a throwaway key (allowlisted) → typed into Claude pane w47:p1 → Claude ran
  `pwd` → replied in the thread with `buzz messages send --reply-to` as the agent key (event d46ffdfb…).
  Typing indicator: buzz-acp logs `typing=true`; **not visually verified** (needs the phone app).
- DoD 4: blank shell `pwd` round-trips via screen diff (before Claude was started in the pane).
- DoD 5: self-checks above.

## Assumed / not yet done
- Cancel (`session/cancel` → Escape) is implemented but untested.
- Interleaved turns (Josh typing mid-turn) untested; by design the turn just absorbs it.
- `--reply-from-output` for blank shells: not written.
- Codex / Pi readers: not written (screen diff is the floor for them today).
- Claude's folder-trust dialog on a fresh cwd blocks `herdr agent start` (`agent_not_ready`); answer it
  by hand (Down, Enter) once per new directory.
