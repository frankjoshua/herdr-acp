# NOTES

Buzz-specific notes moved to ../herdr-buzz/NOTES.md (2026-09-13).


## Decisions
- **Transcript discovery reads the agent process, nothing else** (Josh, 2026-09-13: no hooks, no
  extra setup, only at plugin trigger time). `herdr pane process-info` gives the foreground PID.
  Claude: `$CLAUDE_CONFIG_DIR/sessions/<pid>.json` (Claude writes it itself) holds sessionId + cwd;
  transcript = `<cfg>/projects/<cwd with non-alphanumerics as '-'>/<sessionId>.jsonl`. Codex:
  `/proc/<pid>/fd` names the open rollout; before the first turn, newest rollout in `$CODEX_HOME`
  (from the process env) whose session_meta.cwd is the process cwd and which is younger than the
  process. The tail re-checks the PID every 5s, so a restarted agent gets a fresh reader. All the
  glob/mtime/session-id heuristics are gone.
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
- Pi reader: not written (screen diff is the floor for it today).
- Claude's folder-trust dialog on a fresh cwd blocks `herdr agent start` (`agent_not_ready`); answer it
  by hand (Down, Enter) once per new directory.
