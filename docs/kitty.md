# Working with kitty Sessions

CAO agent sessions run as kitty sessions. Each CAO session gets a generated
kitty session file and a per-session UNIX remote-control socket under
`~/.aws/cli-agent-orchestrator/kitty/`.

## Useful commands

```bash
# List CAO kitty sockets
ls ~/.aws/cli-agent-orchestrator/kitty/*.sock

# Inspect windows in a session
kitten @ --to unix:~/.aws/cli-agent-orchestrator/kitty/<session-name>.sock ls

# Focus the first CAO window in a session
kitten @ --to unix:~/.aws/cli-agent-orchestrator/kitty/<session-name>.sock \
  focus-window --match env:CAO_SESSION_NAME=<session-name>

# Focus a specific CAO window
kitten @ --to unix:~/.aws/cli-agent-orchestrator/kitty/<session-name>.sock \
  focus-window --match env:CAO_WINDOW_NAME=<window-name>

# Delete a session cleanly via CAO
cao shutdown --session <session-name>
```

## Forwarding Env Vars To Spawned Agents

By default, only a tight allowlist of env vars (`HOME`, `PATH`, `SHELL`, plus
`CAO_*` / `KIRO_*` / `MISE_*` / `AWS_*` prefixes) reaches agents spawned inside
kitty. The filter keeps terminal launcher arguments small and prevents
nested-session loops when CAO itself runs inside a provider.

To forward additional vars to the supervisor and every worker spawned later in
the same session, pass `--env KEY=VALUE` to `cao launch`:

```bash
cao launch --agents code_supervisor \
  --env MNEMOSYNE_DIR=/root/mnemosyne \
  --env ISAAC_CHANNEL=room:engineering
```

The flag is repeatable. Values travel in the request body, not the URL, so
secrets do not land in cao-server's HTTP access log.

Rejected at the CLI boundary:

- Keys matching `CLAUDE` / `CODEX_` / `__MISE_` except the allowlisted Claude Code auth flags.
- Keys outside `[A-Za-z_][A-Za-z0-9_]*`.
- Values >= 2048 bytes.

Forwarded vars are held in process memory on cao-server and dropped when the
session is deleted; restarting cao-server wipes them.

## Notes

- CAO session names are automatically prefixed with `cao-`.
- Prefer `cao shutdown` over raw `kitten @ close-window`: `cao shutdown` exits each provider cleanly before closing kitty windows.
