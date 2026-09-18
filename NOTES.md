# NOTES

Design decisions and their reasons, dated. Buzz-specific notes live in the herdr-buzz repo.

## Decisions
- **Pi and OMP share one reader** (2026-09-18). Pi's session format: `message` records with roles
  user / assistant / toolResult, `toolCall` blocks in assistant content. The agent holds the file open
  after its first message, so `/proc/<pid>/fd` names it; before that, the newest session under the
  agent dir (`$PI_CODING_AGENT_DIR` or `~/.<kind>/agent`) whose header cwd matches the process.
- **Transcript discovery reads the agent process, nothing else** (2026-09-13: no hooks, no
  extra setup, only when a client attaches). `herdr pane process-info` gives the foreground PID.
  Claude: `$CLAUDE_CONFIG_DIR/sessions/<pid>.json` (Claude writes it itself) holds sessionId + cwd;
  transcript = `<cfg>/projects/<cwd with non-alphanumerics as '-'>/<sessionId>.jsonl`. Codex:
  `/proc/<pid>/fd` names the open rollout; before the first turn, newest rollout in `$CODEX_HOME`
  (from the process env) whose session_meta.cwd is the process cwd and which is younger than the
  process. The tail re-checks the PID every 5s, so a restarted agent gets a fresh reader. All the
  glob/mtime/session-id heuristics are gone.
- **The pane is a shared session** (2026-09-13). From
  `session/new` on, herdr-acp tails the pane continuously and streams everything as
  `session/update`: a human typing in the pane → `user_message_chunk`, agent text, thoughts, tool
  calls. Not only during a prompt. The echo of a prompt we typed ourselves is suppressed (last 5
  prompt texts). A turn still ends on idle + debounce; "fresh activity" now comes from the tail.
- The tail re-reads `herdr pane get` every poll, so if the agent in the pane restarts (new session
  id) or a shell becomes an agent, the reader swaps automatically.
- **herdr-acp is client-agnostic** (2026-09-13). Everything Buzz-specific lives in
  the herdr-buzz repo (identity minting, buzz-acp launcher, Buzz UI notes). The only hook the client
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
  the person at the pane unblocks it. Shell panes: screen unchanged for `--quiet` (5s).

- **Screen diff uses `difflib.SequenceMatcher`** on stripped lines; emits insert/replace lines.
  Good enough for shells; prompt-marker detection (ccgram shell_infra) only if this proves noisy.
- **Sidechain (subagent) transcript entries are skipped.**
- **Enter is sent 0.5s after `send-text`** (ccgram finding: a batched Enter is swallowed by TUIs).
- Unimplemented ACP methods (load_session, set_session_mode, …) fall through to the SDK's
  method_not_found; buzz-acp is fine with that (`steering_supported=false`).

## Not yet done
- Cancel (`session/cancel` → Escape) is only covered by the self-check, not a live agent.
- Interleaved turns (a human typing mid-turn) are by design absorbed into the turn.
- `--reply-from-output` for blank shells: not written.
- Claude's folder-trust dialog on a fresh cwd blocks `herdr agent start` (`agent_not_ready`); answer it
  by hand (Down, Enter) once per new directory.
