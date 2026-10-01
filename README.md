# herdr-acp

[![HOL Guard Scanner](https://img.shields.io/badge/HOL%20Guard-passing-00a67e)](https://github.com/hashgraph-online/hol-guard)

Turns a Herdr pane into an [ACP](https://agentclientprotocol.com) agent. Any ACP client spawns
`herdr-acp --pane <id>` over stdio. Prompts get typed into the pane. A turn ends where the agent
records it in its own transcript (Claude, Codex, Pi/OMP); an agent without a transcript reader
ends it when Herdr sees it settle; a shell, when it is back at its prompt. Nothing waits out a
fixed delay. Everything that happens in the pane, whether a client prompted it or a human typed
there, streams to the client as `session/update`, so the pane and the client are one shared
session you can pick up from either side.

When the agent opens a choice dialog during a turn (a tool approval or a question), herdr-acp
sends it to the client as `session/request_permission` and types the client's choice into the
pane. If someone answers at the pane first, the client's request is dropped. A client that
can't answer leaves the dialog to the pane.

```
ACP client  --stdio/ACP-->  herdr-acp  --Herdr socket API-->  pane (claude / codex / pi / shell)
                               ^-- transcript or screen diff --'
```

## Install

```
git clone https://github.com/frankjoshua/herdr-acp && cd herdr-acp
python3 -m venv .venv && .venv/bin/pip install -e .      # needs a running Herdr server (its socket)
.venv/bin/herdr-acp --pane <pane-id>                     # speaks ACP on stdin/stdout
```

## Layers (`src/herdr_acp/`)
- **transport.py** — Herdr's socket API: send text and keys, submit a prompt, pick a list entry
  once the cursor is on it, read the screen, wait for screen output, pane process, agent events.
- **reader.py** — what happened in the pane. Claude, Codex, and Pi/OMP transcripts are found from
  the agent's own process (Claude's per-PID session file, the open session file for Codex and Pi),
  so no hooks or config are needed in the agent. Screen diff is the floor for a bare shell.
  `parse_dialog` reads the approval/question dialog off the screen (Claude, Codex, OMP).
- **main.py** — the ACP server. Turn = prompt → stream → the transcript's turn end →
  `end_turn`; a dialog on screen goes to the client while the turn runs. The tail runs for the
  whole session, not just during turns.

Flags: `--footer` / `HERDR_ACP_FOOTER` (text appended to every prompt).

Self-checks (no Herdr needed): `python -m herdr_acp.reader`, `python -m herdr_acp.main --selfcheck`,
`python tests/fakellm.py --selfcheck`. Against a pane: `python -m herdr_acp.transport <pane>`,
`python tests/roundtrip.py <pane> "pwd" [allow_once]`.

Live suite: `.venv/bin/python tests/agents.py [claude codex omp shell] [-k scenario]`. It runs
the installed Claude Code, Codex and OMP (and a bare shell) on a private Herdr server, against a
fake model provider (`tests/fakellm.py`). That needs no login and no tokens, and leaves your
agent configs alone. Each agent gets the same scenarios: a reply, an approval allowed, rejected
and dismissed, a turn after the dismissal, and input typed at the pane; Claude also gets a
slash command. The shell gets an echo, a silent command and a builtin. It takes about 20s. Run
it after the agents update.

Clients: the Buzz bridge (identity minting, buzz-acp launcher, Herdr plugin) is
[herdr-buzz](https://github.com/frankjoshua/herdr-buzz).
