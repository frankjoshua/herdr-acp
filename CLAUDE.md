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
- Must work for Claude Code, Codex, Pi, and a blank shell. Pi is not done.
- No systemd, no supervisor, no auto-restart.
- Ponytail rules: stdlib first, fewest files, no speculative abstractions, one runnable
  self-check per non-trivial piece of logic (`python -m herdr_acp.<module>`).

## Layout (`src/herdr_acp/`)
- `transport.py` — the `herdr` CLI: state, process (sees through a tmux client), send text/keys,
  read screen.
- `reader.py` — what happened in the pane, as ACP updates. Transcript discovery reads the agent
  *process* (PID from Herdr): Claude's `<cfg>/sessions/<pid>.json` names the transcript; Codex holds
  its rollout open in `/proc/<pid>/fd`. Screen diff is the floor for a bare shell.
- `main.py` — the ACP server: `initialize`, `session/new` (starts the tail), `session/prompt`
  (type, wait for idle + debounce), `session/cancel` (Escape). Reader is keyed on the agent PID
  and re-picked every 5s so a restarted agent is followed.

Self-checks: `python -m herdr_acp.reader`, `python -m herdr_acp.main --selfcheck`,
`python -m herdr_acp.transport <pane>`; live: `python tests/roundtrip.py <pane> "<prompt>"`.
Test in a Herdr Space and pane of your own (`herdr workspace create`, `herdr agent start`), never
in someone's working pane. Commit small, on `main`.
