from __future__ import annotations

import hashlib
import json
import shlex
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from captains_chair.command import CommandRunner, run_command
from captains_chair.config import load_project_manifest
from captains_chair.github import GitHubProvider
from captains_chair.harness import HarnessAdapter, HarnessExecutionError
from captains_chair.models import (
    AppConfig,
    BaselineAnalysis,
    HarnessResult,
    ModelPolicy,
    RepoConfig,
    RunState,
)
from captains_chair.prompting import load_prompt
from captains_chair.security import scan_secrets
from captains_chair.state import StateStore

IGNORED_PARTS = {".git", ".venv", "node_modules", "bin", "obj", "dist", "build", "__pycache__"}
MANIFEST_NAMES = {
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "go.mod",
    "Cargo.toml",
    "global.json",
    "Directory.Build.props",
    "Dockerfile",
    "docker-compose.yml",
}
SOURCE_SUFFIXES = {".py", ".cs", ".ts", ".tsx", ".js", ".go", ".rs", ".java"}
# OpenClaw runs model calls inside the gateway process. Keep evidence batches
# conservative so deep baselines do not put memory pressure on small hosts.
BASELINE_BATCH_CHARS = 24_000
ModelInvoker = Callable[..., HarnessResult]


class DeepBaselineCollector:
    def __init__(
        self,
        config: AppConfig,
        state: StateStore,
        github: GitHubProvider,
        models: ModelPolicy,
        runner: CommandRunner = run_command,
        model_invoker: ModelInvoker | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.github = github
        self.models = models
        self.runner = runner
        self.model_invoker = model_invoker

    def collect(
        self,
        repo: RepoConfig,
        *,
        harness: HarnessAdapter | None = None,
        analyze: bool = True,
        run_checks: bool = True,
    ) -> tuple[dict[str, Any], Path]:
        root = repo.local_path.resolve()
        if not root.is_dir():
            raise FileNotFoundError(root)
        manifest = load_project_manifest(root, repo.project_manifest)
        docs = manifest.canonical_docs if manifest else repo.canonical_docs
        checks = manifest.checks if manifest else repo.checks
        files = self._source_files(root)
        source_files = [path for path in files if Path(path).suffix.lower() in SOURCE_SUFFIXES]
        evidence_exclusions = set(scan_secrets(root, [*docs, *source_files]))
        payload: dict[str, Any] = {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "repo": repo.full_name,
            "local_path": str(root),
            "default_branch": repo.default_branch,
            "project_manifest": manifest.model_dump(mode="json") if manifest else None,
            "github": self.github.snapshot(repo).as_dict(),
            "git": self._git_state(root),
            "documents": self._read_documents(root, docs, evidence_exclusions),
            "source_inventory": self._inventory(root, files),
            "source_contents": self._source_contents(root, source_files, evidence_exclusions),
            "evidence_exclusions": sorted(evidence_exclusions),
            "dependency_manifests": self._manifests(root, files),
            "ci_workflows": self._ci_workflows(root),
            "tests": [path for path in files if _is_test_path(path)],
            "checks": self._run_checks(root, checks) if run_checks else [],
        }
        fingerprint_payload = {key: value for key, value in payload.items() if key != "generated_at"}
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        payload["fingerprint"] = fingerprint
        run_dir = self.config.artifact_dir / _slug(repo.full_name) / "baselines"
        run_dir.mkdir(parents=True, exist_ok=True)
        artifact = run_dir / (
            f"baseline-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{fingerprint[:12]}.json"
        )
        analysis: BaselineAnalysis | None = None
        analysis_reused = False
        self.state.transition(repo.full_name, RunState.BASELINE_REVIEW)
        if analyze:
            if harness is None and self.model_invoker is None:
                raise ValueError("analyze=True requires a harness or model_invoker")
            analysis = self._cached_analysis(repo.full_name, fingerprint)
            if analysis is not None:
                analysis_reused = True
            else:
                analysis = self._analyze(repo, root, payload, fingerprint, harness, run_dir)
            payload["analysis"] = analysis.model_dump(mode="json")
        payload["analysis_reused"] = analysis_reused
        artifact.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
        self.state.save_baseline(repo.full_name, fingerprint, artifact, analyzed=analysis is not None)
        self.state.transition(repo.full_name, RunState.READY)
        self.state.record_event(
            repo=repo.full_name,
            run_id=fingerprint[:16],
            state=RunState.READY,
            event_type="BASELINE_READY",
            summary=analysis.summary if analysis else "Deep baseline collected",
            reason="Repository docs, implementation inventory, GitHub state, CI, branches, and checks were collected.",
            fingerprint=fingerprint,
            evidence={
                "next_action": "Review the baseline conclusions before enabling live execution.",
                "counts": payload["source_inventory"]["counts"],
                "check_results": payload["checks"],
                "analysis_reused": analysis_reused,
            },
        )
        return payload, artifact

    def _cached_analysis(self, repo: str, fingerprint: str) -> BaselineAnalysis | None:
        """Reuse a validated analysis when the collected evidence did not change."""
        previous = self.state.baseline(repo)
        if not previous or not previous.get("analyzed") or previous.get("fingerprint") != fingerprint:
            return None
        artifact_value = previous.get("artifact_path")
        if not artifact_value:
            return None
        try:
            value = json.loads(Path(str(artifact_value)).read_text(encoding="utf-8"))
            analysis = value.get("analysis")
            return BaselineAnalysis.model_validate(analysis) if isinstance(analysis, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def _analyze(
        self,
        repo: RepoConfig,
        root: Path,
        payload: dict[str, Any],
        fingerprint: str,
        harness: HarnessAdapter | None,
        run_dir: Path,
    ) -> BaselineAnalysis:
        batches = baseline_batches(payload)
        checkpoint = run_dir / f"analysis-{fingerprint[:12]}.checkpoint.json"
        partials = self._load_partial_analyses(checkpoint, fingerprint, len(batches))
        for index, evidence in enumerate(batches[len(partials) :], start=len(partials) + 1):
            prompt = (
                load_prompt("baseline.md")
                + f"\n\nThis is evidence batch {index} of {len(batches)}. Analyze only this batch "
                "and return partial findings; a separate fresh-context pass will synthesize all batches."
                + f"\n\nRepository path (reference only): {root}\n\n{evidence}"
            )
            result = self._run_analysis_model(
                repo,
                fingerprint,
                harness,
                prompt,
                f"baseline-part-{index}",
                root,
            )
            partials.append(BaselineAnalysis.model_validate(result.output))
            checkpoint.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "fingerprint": fingerprint,
                        "batch_count": len(batches),
                        "partial_analyses": [item.model_dump(mode="json") for item in partials],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        synthesis_evidence = json.dumps(
            {
                "repo": repo.model_dump(mode="json"),
                "batch_count": len(batches),
                "partial_analyses": [item.model_dump(mode="json") for item in partials],
                "checks": payload["checks"],
                "source_counts": payload["source_inventory"]["counts"],
            },
            indent=2,
            default=str,
        )
        synthesis_prompt = (
            load_prompt("baseline.md")
            + "\n\nSynthesize the following independent evidence-batch analyses into one coherent, "
            "deduplicated baseline. Preserve material disagreements and test failures.\n\n"
            + synthesis_evidence
        )
        synthesis = self._run_analysis_model(
            repo,
            fingerprint,
            harness,
            synthesis_prompt,
            "baseline-synthesis",
            root,
        )
        checkpoint.unlink(missing_ok=True)
        return BaselineAnalysis.model_validate(synthesis.output)

    def _load_partial_analyses(
        self,
        checkpoint: Path,
        fingerprint: str,
        batch_count: int,
    ) -> list[BaselineAnalysis]:
        """Load only a structurally valid checkpoint for the exact evidence set."""
        try:
            value = json.loads(checkpoint.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return []
            checkpoint_value = cast(dict[str, Any], value)
            if checkpoint_value.get("schema_version") != 1 or checkpoint_value.get("fingerprint") != fingerprint:
                return []
            if checkpoint_value.get("batch_count") != batch_count:
                return []
            raw_partials = checkpoint_value.get("partial_analyses")
            partial_values = cast(list[Any], raw_partials) if isinstance(raw_partials, list) else None
            if partial_values is None or len(partial_values) > batch_count:
                return []
            if any(not isinstance(item, dict) for item in partial_values):
                return []
            return [BaselineAnalysis.model_validate(item) for item in partial_values]
        except (OSError, ValueError, TypeError):
            return []

    def _run_analysis_model(
        self,
        repo: RepoConfig,
        fingerprint: str,
        harness: HarnessAdapter | None,
        prompt: str,
        role: str,
        root: Path,
    ) -> Any:
        baseline_models = self.models.for_role("baseline")
        if self.model_invoker is not None:
            return self.model_invoker(
                repo,
                fingerprint[:16],
                role,
                prompt,
                models=baseline_models,
                output_model=BaselineAnalysis,
                cwd=root,
                writable=False,
            )
        if harness is None:
            raise ValueError("baseline analysis requires a harness or model_invoker")
        try:
            result = harness.run(
                prompt=prompt,
                models=baseline_models,
                role=role,
                output_model=BaselineAnalysis,
                cwd=root,
                writable=False,
            )
        except HarnessExecutionError as exc:
            if exc.attempts:
                self.state.record_model_call(
                    repo.full_name,
                    fingerprint[:16],
                    role,
                    "unresolved",
                    [item.model_dump(mode="json") for item in exc.attempts],
                    prompt=prompt,
                    session_id=exc.session_id,
                    runtime=str(getattr(getattr(harness, "config", None), "kind", None) or "unknown"),
                )
            raise
        self.state.record_model_call(
            repo.full_name,
            fingerprint[:16],
            role,
            result.resolved_model,
            [item.model_dump(mode="json") for item in result.attempts],
            prompt=prompt,
            session_id=result.session_id,
            runtime=str(getattr(getattr(harness, "config", None), "kind", None) or "unknown"),
        )
        return result

    def _source_files(self, root: Path) -> list[str]:
        operational_roots = tuple(
            path.resolve()
            for path in (self.config.state_dir, self.config.artifact_dir)
            if _is_within(path.resolve(), root)
        )
        values: list[str] = []
        for path in root.rglob("*"):
            resolved = path.resolve()
            if (
                not path.is_file()
                or any(part in IGNORED_PARTS for part in path.relative_to(root).parts)
                or any(_is_within(resolved, operational_root) for operational_root in operational_roots)
            ):
                continue
            values.append(path.relative_to(root).as_posix())
        return sorted(values)

    def _git_state(self, root: Path) -> dict[str, Any]:
        def text(args: list[str]) -> str:
            result = self.runner(["git", "-C", str(root), *args], timeout=120)
            if result.returncode:
                return f"ERROR: {(result.stderr or result.stdout).strip()[:1000]}"
            return result.stdout.strip()

        return {
            "status": text(["status", "--short", "--branch"]),
            "head": text(["rev-parse", "HEAD"]),
            "branch": text(["branch", "--show-current"]),
            "recent_commits": text(["log", "--oneline", "-25"]),
            "worktrees": text(["worktree", "list", "--porcelain"]),
            "remote": text(["remote", "-v"]),
        }

    def _read_documents(self, root: Path, docs: tuple[str, ...], excluded: set[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for relative in docs:
            path = root / relative
            if relative in excluded:
                result[relative] = "<excluded because a secret-like pattern was detected>"
            else:
                result[relative] = (
                    path.read_text(encoding="utf-8", errors="replace")[:100_000]
                    if path.is_file()
                    else "<missing>"
                )
        return result

    def _inventory(self, root: Path, files: list[str]) -> dict[str, Any]:
        suffixes = Counter(Path(path).suffix.lower() or "<none>" for path in files)
        source_files = [path for path in files if Path(path).suffix.lower() in SOURCE_SUFFIXES]
        return {
            "counts": {
                "files": len(files),
                "source_files": len(source_files),
                "tests": sum(_is_test_path(path) for path in files),
            },
            "extensions": dict(suffixes.most_common()),
            "source_files": source_files,
            "all_files": files,
        }

    def _source_contents(self, root: Path, files: list[str], excluded: set[str]) -> dict[str, str]:
        return {
            relative: (root / relative).read_text(encoding="utf-8", errors="replace")
            for relative in files
            if relative not in excluded
        }

    def _manifests(self, root: Path, files: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for relative in files:
            path = Path(relative)
            if path.name in MANIFEST_NAMES or path.suffix in {".sln", ".csproj"}:
                result[relative] = (root / relative).read_text(encoding="utf-8", errors="replace")[:50_000]
        return result

    def _ci_workflows(self, root: Path) -> dict[str, str]:
        directory = root / ".github" / "workflows"
        if not directory.is_dir():
            return {}
        return {
            path.relative_to(root).as_posix(): path.read_text(encoding="utf-8", errors="replace")[:50_000]
            for path in sorted(directory.glob("*.y*ml"))
        }

    def _run_checks(self, root: Path, checks: tuple[str, ...]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for check in checks:
            command = shlex.split(check, posix=True)
            try:
                result = self.runner(command, cwd=root, timeout=1800)
            except (OSError, TimeoutError) as exc:
                results.append(
                    {
                        "command": check,
                        "returncode": None,
                        "stdout_tail": "",
                        "stderr_tail": str(exc)[:4000],
                        "execution_error": type(exc).__name__,
                    }
                )
            else:
                results.append(
                    {
                        "command": check,
                        "returncode": result.returncode,
                        "stdout_tail": result.stdout[-4000:],
                        "stderr_tail": result.stderr[-4000:],
                    }
                )
        return results


def _is_test_path(relative: str) -> bool:
    lowered = relative.lower()
    return "/test" in lowered or lowered.startswith("test") or ".test." in lowered or ".spec." in lowered


def _slug(value: str) -> str:
    return value.replace("/", "__").replace(":", "_")


def _is_within(path: Path, root: Path) -> bool:
    """Return whether path is root or a descendant of root."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def baseline_batches(payload: dict[str, Any]) -> list[str]:
    overview_keys = (
        "schema_version",
        "generated_at",
        "repo",
        "default_branch",
        "project_manifest",
        "github",
        "git",
        "source_inventory",
        "dependency_manifests",
        "ci_workflows",
        "tests",
        "checks",
    )
    sections: list[tuple[str, str]] = [
        (
            "## Repository and live-state evidence",
            json.dumps({key: payload[key] for key in overview_keys}, indent=2, default=str),
        )
    ]
    sections.extend(
        (f"## Canonical document: {path}", content)
        for path, content in payload["documents"].items()
    )
    sections.extend(
        (f"## Source file: {path}", content)
        for path, content in payload["source_contents"].items()
    )
    batches: list[str] = []
    current = ""
    for title, content in sections:
        offset = 0
        continuation = False
        while offset < len(content) or not content and not continuation:
            marker = f"{title}{' (continued)' if continuation else ''}\n"
            if current and len(current) + len(marker) > BASELINE_BATCH_CHARS:
                batches.append(current)
                current = ""
            if len(marker) > BASELINE_BATCH_CHARS:
                raise ValueError("baseline section title exceeds the batch size")
            current += marker
            available = BASELINE_BATCH_CHARS - len(current)
            take = min(len(content) - offset, available)
            current += content[offset : offset + take]
            offset += take
            if offset < len(content):
                batches.append(current)
                current = ""
                continuation = True
            else:
                break
    if current:
        batches.append(current)
    return batches
