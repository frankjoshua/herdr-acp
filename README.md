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

Layers (`src/herdr_acp/`):
- **transport.py** — send text, read screen, session id, status. Herdr via the `herdr` CLI.
- **reader.py** — what happened in the pane. Claude transcript, Codex rollout; screen diff is the floor.
- **main.py** — the ACP server. Turn = prompt → reader stream → idle → `end_turn`.

Flags: `--quiet` (shell turn ends after N quiet seconds), `--debounce` (agent turn ends N seconds
after idle), `--footer` / `HERDR_ACP_FOOTER` (text appended to every prompt).

Self-checks: `python -m herdr_acp.reader`, `python -m herdr_acp.transport <pane>`,
`python tests/roundtrip.py <pane> "pwd"`.

Clients: Buzz glue (identity minting, buzz-acp launcher) lives in `../herdr-buzz`.
