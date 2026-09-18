# herdr-acp

Turns a Herdr pane into an [ACP](https://agentclientprotocol.com) agent. Any ACP client spawns
`herdr-acp --pane <id>` over stdio. Prompts get typed into the pane; a turn ends when the pane
goes idle. Everything that happens in the pane, whether a client prompted it or a human typed
there, streams to the client as `session/update`, so the pane and the client are one shared
session you can pick up from either side.

```
ACP client  --stdio/ACP-->  herdr-acp  --herdr CLI-->  pane (claude / codex / pi / shell)
                               ^-- transcript or screen diff --'
```

## Install

```
git clone https://github.com/frankjoshua/herdr-acp && cd herdr-acp
python3 -m venv .venv && .venv/bin/pip install -e .      # needs the `herdr` CLI on PATH
.venv/bin/herdr-acp --pane <pane-id>                     # speaks ACP on stdin/stdout
```

## Layers (`src/herdr_acp/`)
- **transport.py** — send text, read screen, pane state and process, via the `herdr` CLI.
- **reader.py** — what happened in the pane. The transcript is found from the agent's own process
  (Claude's per-PID session file, Codex's open rollout), so no hooks or config are needed in the
  agent. Screen diff is the floor for a bare shell.
- **main.py** — the ACP server. Turn = prompt → stream → idle → `end_turn`; the tail runs for the
  whole session, not just during turns.

Flags: `--quiet` (shell turn ends after N quiet seconds), `--debounce` (agent turn ends N seconds
after idle), `--footer` / `HERDR_ACP_FOOTER` (text appended to every prompt).

Self-checks: `python -m herdr_acp.reader`, `python -m herdr_acp.transport <pane>`,
`python tests/roundtrip.py <pane> "pwd"`.

Clients: the Buzz bridge (identity minting, buzz-acp launcher, Herdr plugin) is
[herdr-buzz](https://github.com/frankjoshua/herdr-buzz).
