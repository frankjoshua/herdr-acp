# NOTES

Design decisions and their reasons, dated. Buzz-specific notes live in the herdr-buzz repo.

## Decisions
- **CI runs the live suite daily on the latest Herdr and agents** (2026-09-30).
  `.github/workflows/agents.yml` runs on push, pull requests, a daily schedule and manual
  dispatch, with `contents: read`, SHA-pinned actions and no secrets.
  - `tests/ci-install.sh` installs each tool with its official installer:
    - Herdr: `herdr.dev/install.sh`;
    - Claude: `claude.ai/install.sh`;
    - Codex: `chatgpt.com/codex/install.sh`;
    - OMP: `omp.sh/install --binary`;
    - Pi: npm, which needs Node ≥ 22.19, so the script fetches Node 24 when the runner's is older.
  - It then runs the self-checks and `tests/agents.py -v`, and uploads the work directories when
    something fails.
  - Versions are deliberately not pinned: the point is to learn the day an update breaks herdr-acp.
  - A clean `ubuntu:24.04` container run of the same steps passed every pane (herdr 0.9.3,
    claude 2.1.286, codex 0.159.3, omp 18.4.6, pi 0.99.2).
  - That container run found that Codex 0.159 shows a "try the new model" prompt at startup for
    catalog models that have an upgrade. The suite now names its model `hacp-fake`, which Codex
    doesn't know and so never migrates; that is version-proof where pinning a mapping is not.
  - Codex inside tmux sometimes swallowed the Enter typed with its prompt (1 run in 5). For an
    agent Herdr can't see, herdr-acp now checks the transcript records the prompt and presses
    Enter again if not (`_type`, same bound as dialog keys).
  - Herdr's unblock count now needs an actual change from `blocked`: a stale `working` that
    arrived after a dialog showed made a lost key look taken.
  - *GitHub's runners found a reordering:* Claude's first turn hung in 3 of 4 CI runs.
    - On a busy machine Claude writes the reply and `turn_duration` to the file *before* the
      user message, while each entry's timestamp is still in true order. Reproduced locally
      with the suite pinned to one CPU (`taskset -c 0`): 2 of 4 runs.
    - Readers now emit a `Mark` with the entry's timestamp for each user message and each turn
      end. A turn ends at an end stamped no earlier than its first user message; entries
      without timestamps fall back to read order.
    - A cancelled turn's late end is stamped before the next prompt, so it still doesn't count.
- **A live suite runs the installed agents against a fake provider** (2026-09-30).
  `tests/agents.py` gives each of Claude Code, Codex, OMP, Pi, Codex inside a tmux client, and a
  bare shell a pane on a private, headless Herdr server (`herdr --session hacp-agents-<pid> server`).
  Its session lives under the run's temp dir (`XDG_CONFIG_HOME`), so it never shows in the user's
  `herdr session list`. It stops on exit, on SIGTERM, and via `PR_SET_PDEATHSIG` even if the suite
  is killed; two orphaned servers from killed runs are why. Each agent gets its
  own HOME and config dir, so no login, no MCP servers, no skills and no user config leak in.
  herdr-acp drives each pane over ACP. `tests/fakellm.py` speaks the Anthropic Messages and
  OpenAI Responses APIs: `HACP-SAY` / `HACP-RUN` / `HACP-SLOW` markers script the answers. It reads tool names
  and argument schemas from each request, so an agent update that renames a tool or changes its
  schema doesn't break it.
  - Setup per agent:
    - *Claude*: `CLAUDE_CONFIG_DIR` with a `.claude.json` that has onboarding done, the fake key
      approved and the work dir trusted, plus `ANTHROPIC_BASE_URL`.
    - *Codex*: `CODEX_HOME` with `config.toml` that sets a custom `model_provider` with
      `wire_api = "responses"`, the work dir trusted and the update check off.
    - *OMP*: `PI_CODING_AGENT_DIR` with a `models.yml` custom `anthropic-messages` provider and
      `config.yml` set to `setupVersion: 2`, which skips the login wizard.
    - *Pi*: `PI_CODING_AGENT_DIR` with a `models.json` custom `anthropic-messages` provider, and
      `PI_OFFLINE=1`. Pi has no approval dialogs, so it runs a tool unasked.
    - *codex-tmux*: the pane runs `tmux -L <private> new-session codex …`. Herdr detects no agent
      there, so herdr-acp's own path is exercised: it resolves the tmux client to Codex,
      types with `send_input`, and reads dialogs through tmux's screen.
  - A slow model (`HACP-SLOW`: 3s to the first word, then 0.1s per word) and a 3s silent tool both
    hold the turn. Slash commands (`/cost`, `/status`, `/session`) end on Herdr's stall verdict.
  - Runs take ~30s, all panes in parallel. A failure prints the screen and keeps the logs. The
    report shows each agent's version and what Herdr detects in its pane.
  - The suite found these, now handled:
    - *OMP doesn't hold its session file open during the first turn*, so rediscovery must be the
      full one (open files, then the cwd-scoped search, ~2ms), not open files alone.
    - *Claude and Codex drop a key that arrives as their dialog is drawn.* Claude ignored Enter
      sent at 0s in 2 of 3 tries, never from 0.1s. Nothing tells when a dialog starts listening.
      So a key the agent hasn't acted on within `KEY_TAKEN` (1s) is pressed again, up to 3
      times. It is pressed only while that same dialog is still on screen and the agent shows no
      answer, checked by reading the screen and then the transcript. That is the one herdr-acp
      bound that leads to an action; it recovers a lost key and never decides success. An
      auto-approving client hits this in real use: it answers the moment the dialog shows.
    - *A dialog counts as answered only on a tool result, a turn end, or Herdr seeing the agent
      leave `blocked`*: Claude writes the dialog's own tool call (and text) just after the
      dialog shows, so "the transcript moved" was not proof.
    - *Esc on OMP's approval denies the tool and OMP goes on*: a dismissal sends a second Esc to
      OMP only, if its turn hasn't ended, so the turn stops as ACP `cancelled` requires. This is
      decided by agent kind, not marker timing: on Claude, Herdr's unblock can come before the
      turn-end marker, and a second Esc there opens the rewind menu.
    - A prompt refused because a dialog is open isn't recorded as sent, so identical words a
      human types later still stream as theirs.
    - *Herdr's status lags the screen both ways*: it stayed `blocked` after a dialog closed, and
      it accepted a prompt while a dialog was still up. herdr-acp now refuses a prompt itself
      (`invalid_request`) when the screen shows a dialog, instead of typing into it.
    - *Claude merges the next prompt into the rejected tool's result message*; the fake answers
      whichever of the two comes last.
- **No decision waits out a delay** (2026-09-30). Every turn end, dialog and reader swap follows
  a signal; `POLL` (0.5s) only paces reading the transcript or screen.
  - *Transport is Herdr's socket API* (`herdr api schema --json`), not the CLI: the CLI has no
    `send_input` and no event subscription. Text and Enter go in one request (`pane.send_input`,
    or `agent.prompt` for an agent Herdr sees, which also refuses with `agent_blocked` while a
    dialog is open). Verified on Claude, Codex and OMP; the 0.5s Enter gap is gone.
  - *Turn end = the agent's own record.* Claude writes `system/turn_duration` after every turn,
    including errors ("Login expired") and interrupts; Codex writes `task_complete` or
    `turn_aborted`; Pi/OMP end a turn with an assistant `stopReason` other than `toolUse`. Readers
    emit a timestamped `Mark` there and after every user message. A turn ends at the first end
    stamped no earlier than the first user message recorded since the prompt (read order when
    there are no timestamps), so a late end of a cancelled turn doesn't end the next one, and an
    agent writing its lines out of order under load doesn't hang the turn.
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
    (`DIALOG_HINT`); `parse_dialog` decides. A dialog is over once the agent shows it was answered
    (see the live-suite entry above). That replaces the 3s "answered" window and keeps an
    identical next dialog (OMP's `Allow tool: bash`) from looking like the old one. A late
    `blocked` event doesn't count. The choice is typed as Up/Down, and Enter is pressed only once
    `wait_for_output` sees the cursor on that option's row (`cursor_on`).
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
