# NOTES

Buzz-specific notes moved to ../herdr-buzz/NOTES.md (2026-09-13).


## Decisions
- **The pane is a shared session** (Josh, 2026-09-13; "like ccbot with Telegram"). From
  `session/new` on, herdr-acp tails the pane continuously and streams everything as
  `session/update`: a human typing in the pane → `user_message_chunk`, agent text, thoughts, tool
  calls. Not only during a prompt. The echo of a prompt we typed ourselves is suppressed (last 5
  prompt texts). A turn still ends on idle + debounce; "fresh activity" now comes from the tail.
- The tail re-reads `herdr pane get` every poll, so if the agent in the pane restarts (new session
  id) or a shell becomes an agent, the reader swaps automatically.
- **herdr-acp is client-agnostic** (Josh, 2026-09-13). Everything Buzz-specific lives in
  `../herdr-buzz` (identity minting, buzz-acp launcher, Buzz UI notes). The only hook the client
  gets is `--footer` / `HERDR_ACP_FOOTER`: text appended to every prompt (herdr-buzz uses it to
  ask for top-level replies).
- **No built-in footer.** buzz-acp already puts `[Context] Channel: … (#uuid)`, thread root, and
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

- **Codex reader** (2026-09-13): Herdr's reported Codex session id matches nothing on disk, so the
  rollout (`~/.codex/sessions/Y/M/D/rollout-*.jsonl`) is found by `session_meta.cwd == pane cwd`,
  newest first, rescanned every 5s so a new session in the same cwd is adopted. Only
  `event_msg/item_completed` items are mapped (UserMessage, AgentMessage, Reasoning summary,
  CommandExecution, FileChange, McpToolCall); sub-agent chatter and raw responses are ignored.
  Codex writes CommandExecution only on completion, so `tool_call` and its update arrive together.
  Verified live: `tests/roundtrip.py <codex pane> "Run pwd …"`.

## Assumed / not yet done
- Cancel (`session/cancel` → Escape) is implemented but untested.
- Interleaved turns (Josh typing mid-turn) untested; by design the turn just absorbs it.
- `--reply-from-output` for blank shells: not written.
- Pi reader: not written (screen diff is the floor for it today).
- Claude's folder-trust dialog on a fresh cwd blocks `herdr agent start` (`agent_not_ready`); answer it
  by hand (Down, Enter) once per new directory.
