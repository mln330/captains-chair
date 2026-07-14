from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from captains_chair.command import CommandResult, run_command
from captains_chair.harness import CodexAdapter
from captains_chair.models import HarnessConfig, HarnessHealth, ModelTarget, RoleModels

FAKE_CODEX = r'''
from __future__ import annotations

import json
import sys
from pathlib import Path


args = sys.argv[1:]
if not args or args[0] != "exec" or "--json" not in args:
    print("unsupported fake Codex command", file=sys.stderr)
    raise SystemExit(2)


def option(name: str) -> str:
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        print(f"missing option: {name}", file=sys.stderr)
        raise SystemExit(2) from None


schema = json.loads(Path(option("--output-schema")).read_text(encoding="utf-8"))
if "status" not in schema.get("properties", {}) or "message" not in schema.get("properties", {}):
    print("unexpected output schema", file=sys.stderr)
    raise SystemExit(2)

model = option("--model")
sandbox = option("--sandbox")
cwd = option("--cd")
output_path = Path(option("--output-last-message"))
payload = {
    "status": "ok",
    "message": f"sandbox={sandbox};cwd={cwd}",
}
output_path.write_text(json.dumps(payload), encoding="utf-8")
print(
    json.dumps(
        {
            "type": "turn.completed",
            "model": model,
            "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        }
    )
)
'''


@pytest.mark.parametrize(
    ("writable", "expected_sandbox"),
    ((False, "read-only"), (True, "workspace-write")),
)
def test_codex_adapter_crosses_process_boundary_with_schema_and_usage(
    tmp_path: Path,
    writable: bool,
    expected_sandbox: str,
) -> None:
    executable = tmp_path / "fake_codex.py"
    executable.write_text(FAKE_CODEX, encoding="utf-8")

    def process_runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        return run_command(
            [*command[:1], str(executable), *command[1:]],
            cwd=cwd,
            input_text=input_text,
            timeout=timeout,
        )

    adapter = CodexAdapter(
        HarnessConfig(kind="codex", executable=sys.executable, timeout_seconds=30),
        process_runner,
    )
    result = adapter.run(
        prompt="Return the health response.",
        models=RoleModels(
            primary=ModelTarget(model="gpt-5.3-codex-spark", thinking="medium"),
        ),
        role="coder",
        output_model=HarnessHealth,
        cwd=tmp_path,
        writable=writable,
    )

    assert result.output["status"] == "ok"
    assert expected_sandbox in result.output["message"]
    assert result.resolved_model == "gpt-5.3-codex-spark"
    assert result.attempts[0].reported_model == "gpt-5.3-codex-spark"
    assert result.attempts[0].input_tokens == 11
    assert result.attempts[0].output_tokens == 7
    assert result.attempts[0].total_tokens == 18
