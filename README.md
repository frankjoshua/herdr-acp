# herdr-acp

Turns a Herdr pane into an ACP agent. buzz-acp (or any ACP client) spawns
`herdr-acp --pane <id>` over stdio; prompts get typed into the pane, and what
the pane's agent does streams back as ACP `session/update` events.

```
buzz-acp  --stdio/ACP-->  herdr-acp  --herdr CLI-->  pane (claude / codex / pi / shell)
                              ^-- transcript + status events --'
```

Layers:
- **transport** — send text, read screen, session id, status events. Herdr first; tmux later.
- **reader** — what happened since the prompt. Claude transcript first; screen diff is the floor.
- **acp** — the protocol server. Owns turn = prompt → reader stream → idle.

Status: scaffold. See TODO.md in agent_workflow for the plan.
