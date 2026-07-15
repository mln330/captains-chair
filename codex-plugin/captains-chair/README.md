# Captain's Chair Codex Plugin

This plugin is the P1 Codex host boundary for Captain's Chair. It contains the
Codex skill and a small MCP bridge that delegates to the installed
`captains-chair` CLI. Policy, GitHub gates, workflow state, worktrees, and token
telemetry stay in the shared Python core.

Set `CAPTAINS_CHAIR_CONFIG` or pass `config_path` to an MCP tool. The available
tools are `doctor`, deep `baseline`, `status`, bounded `cycle`, and token `usage`.

The shared React UI can be hosted locally for Codex after building the OpenClaw
package UI:

```text
python -m pip install -e .
python codex-plugin/captains-chair/scripts/serve_ui.py \
  --config /path/to/config.yaml \
  --ui-root openclaw-plugin/dist/ui
```
