# NOTES

Design decisions and their reasons, dated. Buzz-specific notes live in the herdr-buzz repo.

## Decisions
- **No decision waits out a delay** (2026-09-30). Every turn end, dialog and reader swap follows
  a signal; `POLL` (0.5s) only paces reading the transcript or screen.
  - *Transport is Herdr's socket API* (`herdr api schema --json`), not the CLI: the CLI has no
    `send_input` and no event subscription. Text and Enter go in one request (`pane.send_input`,
    or `agent.prompt` for an agent Herdr sees, which also refuses with `agent_blocked` while a
    dialog is open). Verified on Claude, Codex and OMP; the 0.5s Enter gap is gone.
  - *Turn end = the agent's own record.* Claude writes `system/turn_duration` after every turn,
    including errors ("Login expired") and interrupts; Codex writes `task_complete` or
    `turn_aborted`; Pi/OMP end a turn with an assistant `stopReason` other than `toolUse`. Readers
    emit `TURN_END` there. A turn ends at the first `TURN_END` read after a user message that
    was itself read after the prompt, so a late marker from a cancelled turn doesn't end the next.
  - *A prompt that starts no turn* (Claude's `/cost`) records nothing. Herdr's `agent.prompt`
    wait reports `agent_prompt_stalled` when it sees no lifecycle change for 5s. That stall, with
    nothing read from the transcript since the prompt, ends the turn. It is the one timer left,
    and it is Herdr's.
  - *Herdr's lifecycle is not enough on its own*: it never reported OMP as `working`, staying
    `idle` through a whole turn and its approval box. So Herdr's state drives only agents without
    a transcript reader, where its settled state ends the turn.
  - *Shell turn end*: the shell owns the terminal again (`pane.process_info`: foreground pgid =
    shell pid), the screen changed since the command was typed, and the bottom row isn't the
    typed command (the moment between echo and fork). Herdr emits no event for a shell.
  - *Dialogs*: during a turn, `pane.wait_for_output` (Herdr-side) waits for a dialog-looking row
    (`DIALOG_HINT`); `parse_dialog` decides. A dialog is over once the transcript moves or Herdr
    reports the agent leaving `blocked`. That rule replaces the 3s "answered" window and keeps
    an identical next dialog (OMP's `Allow tool: bash`) from looking like the old one. A late
    `blocked` event doesn't count. The choice is typed as Up/Down, and Enter is pressed only once
    `wait_for_output` sees the cursor on that option's row (`cursor_on`). If it never does
    within 5s, nothing is pressed.
  - *Reader swap*: Herdr's `pane.agent_detected` / `pane.agent_status_changed` events flag a
    re-pick. The tail does it on its next pass, retrying while the new agent hasn't written its
    session file yet. This replaces the 5s process re-check, and was verified live with Claude
    exited and restarted in the pane.
  - `--quiet` and `--debounce` are gone (herdr-buzz passed neither).
  - Known limits: an interactive program started in a shell (a REPL) keeps the shell out of the
    foreground, so that turn ends only by cancel. Prompting an agent that is already mid-turn
    queues the prompt, and the current turn's end ends ours. A built-in slash command sent to an
    agent Herdr can't see (codex under tmux) has no stall verdict, so it ends only by cancel.
- **Pane dialogs go to the client as `session/request_permission`** (2026-09-29). A dialog is a
  numbered list with one cursor row (`❯ 1. Yes`, Claude and Codex) or an unnumbered list above a
  `↑/↓ navigate` hint (OMP). Options keep the dialog's own labels, and their ACP kind is guessed
  from the wording (Yes/No, "don't ask again"). Digits don't work for answering: Codex's trust
  dialog ignores `1`. The request references the open tool call when the transcript already
  streamed one; otherwise it becomes a `dialog-…` tool call titled with the question and
  carrying the dialog text. Claude sometimes writes the tool call only after the dialog is
  answered. A dialog answered at the pane drops the request without `$/cancel_request`, which the
  SDK doesn't send. A `cancelled` outcome sends Esc and ends the turn `cancelled`. The
  `session/cancel` that follows sends no second Esc, because Esc-Esc opens Claude's rewind menu.
  If `request_permission` fails once, dialogs are left to the pane for the rest of the process.
  Dialogs outside a turn are not asked, because a human typed that prompt at the pane. Screens
  are read from the `visible` source: Claude draws on the alternate screen, and `recent` returns
  nothing for it. Verified live (2026-09-30) against Claude 2.1.285 (allow, reject,
  client-cancelled, a multi-step turn), Codex (allow, reject) and OMP
  `--approval-mode always-ask` (approve, deny).
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
  process. Herdr's agent events flag a re-pick, so a restarted agent gets a fresh reader. All
  the glob/mtime/session-id heuristics are gone. OMP also holds side files open
  (`<session>/__advisor.scribe.jsonl`); names starting with `__` are skipped.
- **The pane is a shared session** (2026-09-13). From
  `session/new` on, herdr-acp tails the pane continuously and streams everything as
  `session/update`: a human typing in the pane → `user_message_chunk`, agent text, thoughts, tool
  calls. Not only during a prompt. The echo of a prompt we typed ourselves is suppressed (last 5
  prompt texts).
- **herdr-acp is client-agnostic** (2026-09-13). Everything Buzz-specific lives in
  the herdr-buzz repo (identity minting, buzz-acp launcher, Buzz UI notes). The only hook the client
  gets is `--footer` / `HERDR_ACP_FOOTER`: text appended to every prompt (herdr-buzz uses it to
  ask for top-level replies).
- **No built-in footer.** buzz-acp already puts `[Context] Channel: … (#uuid)`, thread root, and
  "reply with `buzz messages send --reply-to <id>`" into every prompt (queue.rs `format_context_hints`),
  and the standing/base prompt arrives in the first prompt of a session. Claude replied threaded
  without any help from us. `--reply-from-output` (for blank shells) is still a follow-up.
- **Screen diff uses `difflib.SequenceMatcher`** on stripped lines; emits insert/replace lines.
  Good enough for shells; prompt-marker detection (ccgram shell_infra) only if this proves noisy.
- **Sidechain (subagent) transcript entries are skipped.**
- Unimplemented ACP methods (load_session, set_session_mode, …) fall through to the SDK's
  method_not_found; buzz-acp is fine with that (`steering_supported=false`).

## Not yet done
- `session/cancel` from a client is exercised live only through a dismissed permission request
  (same `cancel()` path), not on its own.
- Interleaved turns (a human typing mid-turn) are by design absorbed into the turn.
- `--reply-from-output` for blank shells: not written.
- Claude's folder-trust dialog on a fresh cwd blocks `herdr agent start` (`agent_not_ready`); answer it
  by hand (Down, Enter) once per new directory.
