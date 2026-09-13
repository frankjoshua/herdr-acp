# herdr-acp

Turns a Herdr pane into an [ACP](https://agentclientprotocol.com) agent. Any ACP client spawns
`herdr-acp --pane <id>` over stdio; prompts get typed into the pane, and what the pane's agent
does streams back as `session/update` until the pane goes idle, then the turn ends.

```
ACP client  --stdio/ACP-->  herdr-acp  --herdr CLI-->  pane (claude / codex / pi / shell)
                               ^-- transcript or screen diff --'
```

Layers (`src/herdr_acp/`):
- **transport.py** — send text, read screen, session id, status. Herdr via the `herdr` CLI.
- **reader.py** — what happened since the prompt. Claude transcript first; screen diff is the floor.
- **main.py** — the ACP server. Turn = prompt → reader stream → idle → `end_turn`.

Flags: `--quiet` (shell turn ends after N quiet seconds), `--debounce` (agent turn ends N seconds
after idle), `--footer` / `HERDR_ACP_FOOTER` (text appended to every prompt).

Self-checks: `python -m herdr_acp.reader`, `python -m herdr_acp.transport <pane>`,
`python tests/roundtrip.py <pane> "pwd"`.

Clients: Buzz glue (identity minting, buzz-acp launcher) lives in `../herdr-buzz`.
