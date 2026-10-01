# herdr-acp — agent brief

herdr-acp exposes a [Herdr](https://herdr.dev) pane as an
[Agent Client Protocol](https://agentclientprotocol.com) agent over stdio. An ACP client spawns
`herdr-acp --pane <id>`; prompts are typed into the pane, and everything the pane does streams
back as `session/update`. The pane and the client are one shared session.

Ask the maintainer only for decisions that are genuinely theirs; otherwise make the routine
call and record it in `NOTES.md` (decisions and their reasons, dated).

## Constraints (do not re-litigate)
- Herdr owns the agent. herdr-acp never launches, resumes, or replaces what runs in a pane.
- The agent in the pane stays oblivious: no hooks, no env, no instructions, no tokens spent on the
  bridge. Anything client-specific (Buzz identity, posting, prompt shaping) lives in the client
  glue, not here. Buzz glue: https://github.com/frankjoshua/herdr-buzz.
- Client-agnostic. The only client hook is `--footer` / `HERDR_ACP_FOOTER`.
- No turn isolation: a turn owns everything from its prompt until the pane goes idle; a human
  typing in the pane mid-turn is part of the turn.
- Must work for Claude Code, Codex, Pi/OMP, and a blank shell.
- No systemd, no supervisor, no auto-restart.
- Ponytail rules: stdlib first, fewest files, no speculative abstractions, one runnable
  self-check per non-trivial piece of logic (`python -m herdr_acp.<module>`).

## Layout (`src/herdr_acp/`)
- `transport.py` — Herdr's socket API: process (sees through a tmux client), send text/keys,
  submit a prompt, select a list entry, read screen, wait for output, agent events.
- `reader.py` — what happened in the pane, as ACP updates. Transcript discovery reads the agent
  *process* (PID from Herdr): Claude's `<cfg>/sessions/<pid>.json` names the transcript; Codex and
  Pi/OMP hold their session file open in `/proc/<pid>/fd`. Screen diff is the floor for a bare shell.
  `parse_dialog` reads an approval/question dialog off the screen.
- `main.py` — the ACP server: `initialize`, `session/new` (starts the tail), `session/prompt`
  (type; the turn ends at the transcript's turn-end marker, a shell's return to its prompt, or
  Herdr's settle for agents without a reader; a dialog becomes `session/request_permission` and
  the choice is typed back), `session/cancel` (Escape). Reader is keyed on the agent PID and
  re-picked when Herdr reports the pane's agent changed. No decision waits out a delay.

Self-checks: `python -m herdr_acp.reader`, `python -m herdr_acp.main --selfcheck`,
`python tests/fakellm.py --selfcheck`, `python -m herdr_acp.transport <pane>`. Live:
`.venv/bin/python tests/agents.py` (installed Claude/Codex/OMP/Pi, Codex in tmux, and a shell, on a
private Herdr server against a fake provider; ~30s, no logins). Run it after any change to reading
dialogs, turn ends or input, and after agent updates. CI (`.github/workflows/agents.yml`, installs
via `tests/ci-install.sh`) runs it daily on the latest Herdr and agents. Ad hoc:
`python tests/roundtrip.py <pane> "<prompt>"`.
Test in a Herdr Space and pane of your own (`herdr workspace create`, `herdr agent start`), never
in someone's working pane. Commit small, on `main`.
