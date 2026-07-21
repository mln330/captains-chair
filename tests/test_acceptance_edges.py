from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC
from pathlib import Path
from typing import Any, Literal, cast

import pytest

import make_it_so.completion_gate as completion_gate
import make_it_so.direct_orchestrator as direct
import make_it_so.direct_workers as direct_workers
from make_it_so.command import CommandResult
from make_it_so.direct_orchestrator import DirectOrchestrator
from make_it_so.direct_workers import (
    CommandWorkerExecutor,
    WorkerExecutionError,
    WorkerExecutionResult,
)
from make_it_so.models import (
    ActionKind,
    ApplicationSurface,
    Checkpoint,
    CheckpointKind,
    CommentTriage,
    Course,
    CourseStatus,
    PlanDecision,
    QAProfile,
    ReadinessCheckStatus,
    ReadinessRequirement,
    ReadinessReviewRecord,
    ReadinessReviewVerdict,
    RequirementStatus,
    ReviewVerdict,
    WorkPackage,
)
from make_it_so.orchestration import QueueCard, QueueCardSpec, QueueStatus
from make_it_so.readiness import ReadinessReviewDecision, apply_readiness_review
from tests.helpers import repo_config
from tests.test_courses import course, ready_course
from tests.test_readiness_review import decision as readiness_decision
from tests.test_readiness_review import models as readiness_models
from tests.test_readiness_review import result as readiness_result


def _card(*, agent_id: str | None = "worker", metadata: dict[str, Any] | None = None) -> QueueCard:
    return QueueCard(
        id="card-1",
        title="Acceptance card",
        status=QueueStatus.READY,
        agent_id=agent_id,
        metadata=metadata or {},
    )


def test_worker_result_and_parser_fail_closed_on_untrustworthy_outcomes() -> None:
    with pytest.raises(ValueError, match="reason"):
        WorkerExecutionResult(status="blocked", summary="Blocked without a reason")

    fenced = direct_workers._parse_result(  # pyright: ignore[reportPrivateUsage]
        "```json\n"
        + json.dumps(
            {
                "status": "completed",
                "summary": "Checks passed",
                "proof": [{"status": "passed"}],
            }
        )
        + "\n```"
    )
    assert fenced.status == "completed"
    assert direct_workers._runtime_model("codex", "codex/gpt-test") == "gpt-test"  # pyright: ignore[reportPrivateUsage]
    assert direct_workers._runtime_model("openclaw", "codex/gpt-test") == "openai/gpt-test"  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(WorkerExecutionError, match="JSON object"):
        direct_workers._parse_result("no structured result")  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(WorkerExecutionError, match="schema validation"):
        direct_workers._parse_result('{"status":"completed","summary":"missing proof"}')  # pyright: ignore[reportPrivateUsage]


class _Runner:
    def __init__(self, result: CommandResult | BaseException, *, write_output: str | None = None) -> None:
        self.result = result
        self.write_output = write_output

    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        if isinstance(self.result, BaseException):
            raise self.result
        argv = list(command)
        if self.write_output is not None and "--output-last-message" in argv:
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text(self.write_output, encoding="utf-8")
        return self.result


@pytest.mark.parametrize("runtime", ("codex", "openclaw"))
def test_worker_process_errors_are_normalized(
    tmp_path: Path,
    runtime: Literal["codex", "openclaw"],
) -> None:
    executor = CommandWorkerExecutor(runtime, runtime, _Runner(OSError("process unavailable")))
    with pytest.raises(WorkerExecutionError, match="process failed"):
        executor.execute(
            _card(),
            attempt_id="attempt",
            workspace=tmp_path,
            model="model",
            timeout_seconds=30,
        )

    failed = CommandWorkerExecutor(runtime, runtime, _Runner(CommandResult(7, "bad", "failed")))
    with pytest.raises(WorkerExecutionError, match="failed"):
        failed.execute(
            _card(),
            attempt_id="attempt",
            workspace=tmp_path,
            model="model",
            timeout_seconds=30,
        )


def test_worker_adapters_reject_missing_or_malformed_runtime_results(tmp_path: Path) -> None:
    with pytest.raises(WorkerExecutionError, match="assigned agent"):
        CommandWorkerExecutor("openclaw", "openclaw").execute(
            _card(agent_id=None),
            attempt_id="attempt",
            workspace=tmp_path,
            model="model",
            timeout_seconds=30,
        )

    missing = CommandWorkerExecutor("codex", "codex", _Runner(CommandResult(0, "", "")))
    with pytest.raises(WorkerExecutionError, match="did not write"):
        missing.execute(
            _card(),
            attempt_id="attempt",
            workspace=tmp_path,
            model="model",
            timeout_seconds=30,
        )

    for output in ("[]", '{"result":{"payloads":[]}}'):
        malformed = CommandWorkerExecutor(
            "openclaw",
            "openclaw",
            _Runner(CommandResult(0, output, "")),
        )
        with pytest.raises(WorkerExecutionError, match="object|outcome"):
            malformed.execute(
                _card(),
                attempt_id="attempt",
                workspace=tmp_path,
                model="model",
                timeout_seconds=30,
            )


class _Executor:
    def __init__(self, result: WorkerExecutionResult | BaseException) -> None:
        self.result = result

    def execute(
        self,
        card: QueueCard,
        *,
        attempt_id: str,
        workspace: Path,
        model: str,
        timeout_seconds: int,
    ) -> WorkerExecutionResult:
        del card, attempt_id, workspace, model, timeout_seconds
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def test_direct_orchestrator_validates_claims_models_and_managed_failures(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="lease_seconds"):
        DirectOrchestrator(tmp_path / "lease.db", lease_seconds=0)
    with pytest.raises(ValueError, match="max_dispatch_workers"):
        DirectOrchestrator(tmp_path / "workers.db", max_dispatch_workers=0)

    external = DirectOrchestrator(tmp_path / "external.db")
    assert external.validate_worker_models()["status"] == "not_supported"
    external.ensure_board("board", "Board", "Acceptance", tmp_path)
    card = external.create_card(
        "board", QueueCardSpec(key="one", title="One", notes="Claim this card", agent_id="coder")
    )
    external.dispatch("board")
    assert external.claim_next_card("board", owner_id="owner", token="token", agent_id="other") is None
    with pytest.raises(ValueError, match="owner and token"):
        external.claim_card(card.id, owner_id="", token="")
    with pytest.raises(KeyError, match="unknown direct card"):
        external.claim_card("missing", owner_id="owner", token="token")
    with pytest.raises(ValueError, match="requester and reason"):
        external.cancel_claimed_card(card.id, requested_by="", reason="")
    with pytest.raises(PermissionError, match="no active claim"):
        external.cancel_claimed_card(card.id, requested_by="captain", reason="stop")

    blocked_result = WorkerExecutionResult(
        status="blocked",
        summary="Cannot proceed",
        reason="TECHNICAL: fixture blocker",
    )
    managed = DirectOrchestrator(
        tmp_path / "managed.db",
        executor=_Executor(blocked_result),
        worker_models={},
    )
    managed.ensure_board("board", "Board", "Managed", tmp_path)
    missing_model = managed.create_card(
        "board",
        QueueCardSpec(
            key="missing-model",
            title="Missing model",
            notes="Fail without a model",
            agent_id="coder",
        ),
    )
    assert managed.validate_worker_models() == {"status": "degraded", "missing_agents": ["coder"]}
    result = managed.dispatch("board")
    assert missing_model.id in result["blocked"]

    failing = DirectOrchestrator(
        tmp_path / "failing.db",
        executor=_Executor(WorkerExecutionError("worker crashed")),
        worker_models={"coder": "test-model"},
    )
    failing.ensure_board("board", "Board", "Failing", tmp_path)
    failed_card = failing.create_card(
        "board",
        QueueCardSpec(
            key="failure",
            title="Failure",
            notes="Normalize the worker failure",
            agent_id="coder",
        ),
    )
    dispatched = failing.dispatch("board")
    assert failed_card.id in dispatched["blocked"]
    assert failing.validate_worker_models()["status"] == "ok"


def test_direct_orchestrator_defensive_metadata_helpers() -> None:
    assert direct._parse_datetime(None) is None  # pyright: ignore[reportPrivateUsage]
    assert direct._parse_datetime("not-a-date") is None  # pyright: ignore[reportPrivateUsage]
    parsed = direct._parse_datetime("2026-07-15T12:00:00")  # pyright: ignore[reportPrivateUsage]
    assert parsed is not None and parsed.tzinfo == UTC
    assert direct._failure_count(_card(metadata={"failures": -1})) == 0  # pyright: ignore[reportPrivateUsage]
    assert direct._retry_limit(_card(metadata={"automation": "invalid"})) == 0  # pyright: ignore[reportPrivateUsage]
    assert direct._retry_limit(_card(metadata={"automation": {"maxRetries": -1}})) == 0  # pyright: ignore[reportPrivateUsage]
    assert direct._retry_limit(_card(metadata={"automation": {"maxRetries": 3}})) == 3  # pyright: ignore[reportPrivateUsage]
    assert direct._runtime_limit(_card(metadata={"automation": {"maxRuntimeSeconds": 0}}), 30) == 30  # pyright: ignore[reportPrivateUsage]
    assert direct._runtime_limit(_card(metadata={"automation": {"maxRuntimeSeconds": 90}}), 30) == 90  # pyright: ignore[reportPrivateUsage]
    assert direct._runtime_limit(_card(metadata={"automation": "invalid"}), 30) == 30  # pyright: ignore[reportPrivateUsage]


def test_current_head_qa_proof_requires_structure_provenance_and_ui_evidence() -> None:
    profile = QAProfile(
        key="web-ui-qa",
        title="Web UI QA",
        surfaces=frozenset({ApplicationSurface.WEB_UI}),
    )

    cases: tuple[tuple[dict[str, Any], str], ...] = (
        ({}, "structured proof"),
        ({"proof": [{"status": "failed"}]}, "passed proof"),
        ({"proof": [{"status": "passed", "note": "QA_PASSED:web-ui-qa:dead111"}]}, "stale"),
        (
            {"proof": [{"status": "passed", "note": "QA_PASSED:web-ui-qa:abcdef1"}]},
            "provenance",
        ),
        (
            {
                "proof": [
                    {
                        "status": "passed",
                        "note": "QA_PASSED:web-ui-qa:abcdef1",
                        "model": "qa-model",
                        "provider": "test",
                    }
                ]
            },
            "lacks evidence",
        ),
        (
            {
                "proof": [
                    {
                        "status": "passed",
                        "note": "QA_PASSED:web-ui-qa:abcdef1",
                        "model": "qa-model",
                        "provider": "test",
                        "evidence": ["accessibility and contrast checked"],
                    }
                ]
            },
            "responsive",
        ),
    )
    for metadata, message in cases:
        result = completion_gate._validate_qa_evidence(  # pyright: ignore[reportPrivateUsage]
            _card(metadata=metadata), profile, "abcdef1"
        )
        assert result.allowed is False
        assert message in result.reason

    passed = completion_gate._validate_qa_evidence(  # pyright: ignore[reportPrivateUsage]
        _card(
            metadata={
                "proof": [
                    {
                        "status": "passed",
                        "label": "QA_PASSED:web-ui-qa:abcdef1",
                        "model": "qa-model",
                        "provider": "test",
                        "evidence": [
                            "accessibility",
                            "contrast",
                            "responsive",
                            "flow",
                            "cohesion",
                        ],
                    }
                ]
            }
        ),
        profile,
        "abcdef1",
    )
    assert passed.allowed is True


def _course_payload(**updates: Any) -> dict[str, Any]:
    payload = course().model_dump(mode="python")
    if "work_packages" in updates and "checkpoints" not in updates:
        payload["checkpoints"] = ()
    payload.update(updates)
    return payload


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        ({"work_packages": (WorkPackage(key="one", title="One", objective="One"),) * 2}, "unique"),
        (
            {
                "work_packages": (
                    WorkPackage(key="one", title="One", objective="One", dependencies=("missing",)),
                )
            },
            "unknown dependencies",
        ),
        (
            {
                "work_packages": (
                    WorkPackage(key="one", title="One", objective="One", dependencies=("two",)),
                    WorkPackage(key="two", title="Two", objective="Two", dependencies=("one",)),
                )
            },
            "cycle",
        ),
        (
            {
                "work_packages": (
                    WorkPackage(key="one", title="One", objective="One", checkpoint_keys=("missing",)),
                )
            },
            "unknown checkpoints",
        ),
        (
            {
                "work_packages": (
                    WorkPackage(key="one", title="One", objective="One", qa_profiles=("missing",)),
                )
            },
            "unknown QA profiles",
        ),
        (
            {
                "work_packages": (WorkPackage(key="one", title="One", objective="One"),),
                "checkpoints": (
                    Checkpoint(
                        key="gate",
                        title="Gate",
                        kind=CheckpointKind.MILESTONE_DEMO,
                        reason="Review",
                        blocks_work_packages=("missing",),
                        owner_decision_required=False,
                    ),
                ),
            },
            "unknown work packages",
        ),
        ({"status": CourseStatus.ENGAGED}, "approval provenance"),
    ),
)
def test_course_graph_validation_rejects_unsafe_execution_plans(
    updates: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        Course.model_validate(_course_payload(**updates))


def test_readiness_and_review_models_require_independent_evidence() -> None:
    base = course().readiness[0].model_dump(mode="python")
    with pytest.raises(ValueError, match="needs an answer"):
        ReadinessRequirement.model_validate({**base, "status": RequirementStatus.ANSWERED})
    with pytest.raises(ValueError, match="owner decision"):
        ReadinessRequirement.model_validate(
            {
                **base,
                "status": RequirementStatus.WAIVED,
                "owner_decision_required": False,
            }
        )
    with pytest.raises(ValueError, match="verification provenance"):
        ReadinessRequirement.model_validate(
            {**base, "status": RequirementStatus.VERIFIED, "answer": "Known answer"}
        )

    record = ready_course().readiness_review
    assert record is not None
    payload = record.model_dump(mode="python")
    duplicate = payload["checks"][0]
    with pytest.raises(ValueError, match="unique"):
        ReadinessReviewRecord.model_validate({**payload, "checks": (duplicate, duplicate)})
    blocked = {**duplicate, "status": ReadinessCheckStatus.BLOCKED}
    with pytest.raises(ValueError, match="blocked checks"):
        ReadinessReviewRecord.model_validate(
            {**payload, "verdict": ReadinessReviewVerdict.READY, "checks": (blocked,)}
        )


def test_readiness_review_rejects_ambiguous_or_incomplete_decisions() -> None:
    payload = readiness_decision().model_dump(mode="python")
    with pytest.raises(ValueError, match="categories must be unique"):
        ReadinessReviewDecision.model_validate(
            {**payload, "checks": (payload["checks"][0], payload["checks"][0])}
        )
    with pytest.raises(ValueError, match="decisions must be unique"):
        ReadinessReviewDecision.model_validate(
            {
                **payload,
                "requirements": (
                    payload["requirements"][0],
                    payload["requirements"][0],
                ),
            }
        )

    answered = course().model_copy(
        update={
            "readiness": (
                course()
                .readiness[0]
                .model_copy(update={"status": RequirementStatus.ANSWERED, "answer": "Known answer"}),
            )
        }
    )
    with pytest.raises(ValueError, match="requirement decisions do not match"):
        apply_readiness_review(
            answered,
            ReadinessReviewDecision.model_validate({**payload, "requirements": ()}),
            readiness_result(),
            readiness_models(),
            provider="test",
        )
    with pytest.raises(ValueError, match="actionable work-package graph"):
        apply_readiness_review(
            answered.model_copy(update={"work_packages": ()}),
            readiness_decision(),
            readiness_result(),
            readiness_models(),
            provider="test",
        )

    unverified_payload = readiness_decision().model_dump(mode="python")
    unverified_payload["requirements"][0]["verified"] = False
    unverified = ReadinessReviewDecision.model_validate(unverified_payload)
    with pytest.raises(ValueError, match="cannot leave a required requirement unverified"):
        apply_readiness_review(
            answered,
            unverified,
            readiness_result(),
            readiness_models(),
            provider="test",
        )

    needs_input_payload = unverified.model_dump(mode="python")
    needs_input_payload["verdict"] = "needs_input"
    blocked_course = apply_readiness_review(
        answered,
        ReadinessReviewDecision.model_validate(needs_input_payload),
        readiness_result(),
        readiness_models(),
        provider="test",
    )
    assert blocked_course.readiness[0].status == RequirementStatus.BLOCKED

    waived = answered.model_copy(
        update={
            "readiness": (
                answered.readiness[0].model_copy(
                    update={
                        "status": RequirementStatus.WAIVED,
                        "owner_decision_required": True,
                    }
                ),
            )
        }
    )
    waived_payload = readiness_decision().model_dump(mode="python")
    waived_payload["requirements"][0]["verified"] = False
    reviewed = apply_readiness_review(
        waived,
        ReadinessReviewDecision.model_validate(waived_payload),
        readiness_result(),
        readiness_models(),
        provider="test",
    )
    assert reviewed.readiness[0].status == RequirementStatus.WAIVED


def test_live_qa_resolution_rejects_untyped_github_evidence(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    workflow = [_card().model_copy(update={"source_url": "https://github.com/example/project/pull/42"})]

    class MissingMethods:
        def gate(self, *_args: Any) -> Any:
            raise AssertionError("gate is not used")

    assert completion_gate.GitHubCompletionValidator(MissingMethods()).required_qa(repo, workflow) is None  # type: ignore[arg-type]

    class Provider:
        def __init__(self, pr: Any, files: Any) -> None:
            self.pr = pr
            self.files = files

        def gate(self, *_args: Any) -> Any:
            raise AssertionError("gate is not used")

        def pull_request(self, *_args: Any) -> Any:
            return self.pr

        def pull_request_files(self, *_args: Any) -> Any:
            return self.files

    for pr, files in (
        ("invalid", ("src/a.py",)),
        ({"headRefOid": ""}, ("src/a.py",)),
        ({"headRefOid": "abcdef1"}, ["src/a.py"]),
        ({"headRefOid": "abcdef1"}, ("src/a.py", 7)),
    ):
        assert (
            completion_gate.GitHubCompletionValidator(cast(Any, Provider(pr, files))).required_qa(
                repo, workflow
            )
            is None
        )


def test_completion_gate_rechecks_qa_profiles_and_ignores_malformed_pr_proof(
    tmp_path: Path,
) -> None:
    repo = repo_config(tmp_path)
    validator = completion_gate.GitHubCompletionValidator(cast(Any, object()))
    qa_card = _card(metadata={"qaProfile": "web-ui-qa"}).model_copy(update={"labels": ("stage:test",)})
    result = validator.validate(repo, qa_card, [])
    assert result.allowed is False
    assert "before a PR exists" in result.reason

    class Provider:
        def gate(self, *_args: Any) -> Any:
            raise AssertionError("gate is not used")

        def pull_request(self, *_args: Any) -> dict[str, str]:
            return {"headRefOid": "abcdef1"}

        def pull_request_files(self, *_args: Any) -> tuple[str, ...]:
            return ("src/service.py",)

    validator = completion_gate.GitHubCompletionValidator(cast(Any, Provider()))
    implementation = _card(
        metadata={
            "plannedChangedPaths": ["src/planned.py", "", 7],
            "proof": [
                "invalid",
                {"status": "failed", "url": "https://github.com/example/project/pull/99"},
            ],
        }
    ).model_copy(update={"source_url": "https://github.com/example/project/pull/42"})
    result = validator.validate(repo, qa_card, [implementation, qa_card])
    assert result.allowed is True
    assert "no longer required" in result.reason

    final = _card(
        metadata={
            "proof": [
                "invalid",
                {"status": "failed", "note": "READY_FOR_OWNER:dead111"},
            ]
        }
    ).model_copy(update={"labels": ("stage:final_review",)})
    assert validator.validate(repo, final, [implementation, final]).allowed is False

    final_with_qa = final.model_copy(
        update={
            "source_url": "https://github.com/example/project/pull/42",
            "metadata": {
                "qaEvidenceVersion": 1,
                "proof": [{"status": "passed", "note": "READY_FOR_OWNER:abcdef1"}],
            },
        }
    )
    unavailable = completion_gate.GitHubCompletionValidator(cast(Any, object())).validate(
        repo, final_with_qa, [final_with_qa]
    )
    assert unavailable.allowed is False
    assert "required QA cannot be resolved" in unavailable.reason


def test_decision_models_require_actionable_owner_and_review_work() -> None:
    with pytest.raises(ValueError, match="requires requires_owner_approval"):
        PlanDecision(
            action=ActionKind.REPORT_ONLY,
            summary="Need owner input",
            reason="A secret is missing",
            owner_blocker="USER_SECRET: provide token",
        )
    with pytest.raises(ValueError, match="owner_blocker must begin"):
        PlanDecision(
            action=ActionKind.REPORT_ONLY,
            summary="Need owner input",
            reason="Input is missing",
            requires_owner_approval=True,
            owner_blocker="please decide",
        )
    with pytest.raises(ValueError, match="label_issue requires target_issue"):
        PlanDecision(
            action=ActionKind.LABEL_ISSUE,
            summary="Label issue",
            reason="Triage",
            issue_labels=("priority",),
        )
    with pytest.raises(ValueError, match="retarget_issue requires target_issue"):
        PlanDecision(
            action=ActionKind.RETARGET_ISSUE,
            summary="Retarget issue",
            reason="Triage",
            issue_assignees=("builder",),
        )
    with pytest.raises(ValueError, match="actionable finding"):
        CommentTriage(
            head_sha="abcdef1",
            verdict=ReviewVerdict.REQUEST_CHANGES,
            summary="Changes requested without a finding",
        )
