# Captain's Chair Codex Plugin

This plugin is the P1 Codex host boundary for Captain's Chair. It contains the
Codex skill and a small MCP bridge that delegates to the installed
`captains-chair` CLI. Policy, GitHub gates, workflow state, worktrees, and token
telemetry stay in the shared Python core.

Set `CAPTAINS_CHAIR_CONFIG` or pass `config_path` to an MCP tool. The available
tools include `doctor`, deep `baseline`, `status`, bounded `cycle`, token `usage`,
complete course/readiness/checkpoint controls, attention acknowledgement, and the
runtime-neutral direct worker lifecycle. The included planning and execution
skills keep native Codex conversations aligned with those durable APIs.

The shared React UI can be hosted locally for Codex after building the OpenClaw
package UI:

```text
python -m pip install -e .
python codex-plugin/captains-chair/scripts/serve_ui.py \
  --config /path/to/config.yaml \
  --ui-root openclaw-plugin/dist/ui
```

The server binds to loopback by default, rejects cross-origin and non-JSON API
requests, disables framing/caching, and serves a restrictive content security
policy. A non-loopback bind requires `--token` or
`CAPTAINS_CHAIR_UI_TOKEN`; open `/captains-chair/?token=...` once to establish a
SameSite, HttpOnly session cookie. Use a URL-safe token of at least 16 characters.
Put TLS in front of any remote binding and do not expose the service directly to
the public internet.
