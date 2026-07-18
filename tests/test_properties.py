from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import given
from hypothesis import strategies as st

from make_it_so.courses import eligible_work_packages
from make_it_so.models import (
    ActionKind,
    CompletionPolicy,
    Course,
    CourseKind,
    CourseStatus,
    OperationMode,
    PlanDecision,
    WorkPackage,
)
from make_it_so.policy import evaluate_action
from tests.helpers import repo_config


@st.composite
def package_graphs(draw: st.DrawFn) -> tuple[WorkPackage, ...]:
    count = draw(st.integers(min_value=1, max_value=8))
    packages: list[WorkPackage] = []
    for index in range(count):
        dependencies = draw(
            st.lists(
                st.integers(min_value=0, max_value=index - 1),
                unique=True,
                max_size=min(index, 3),
            )
        ) if index else []
        packages.append(
            WorkPackage(
                key=f"package-{index}",
                title=f"Package {index}",
                objective="A generated dependency-safe work package.",
                dependencies=tuple(f"package-{dependency}" for dependency in dependencies),
            )
        )
    return tuple(packages)


@given(package_graphs(), st.sets(st.integers(min_value=0, max_value=7), max_size=8))
def test_eligible_work_never_bypasses_a_dependency(
    packages: tuple[WorkPackage, ...], completed_indexes: set[int]
) -> None:
    keys = {package.key for package in packages}
    completed = {f"package-{index}" for index in completed_indexes if f"package-{index}" in keys}
    course = Course(
        key="property-course",
        repository="example/project",
        kind=CourseKind.FEATURE,
        title="Generated course",
        goal="Exercise dependency eligibility across many valid course graphs.",
        status=CourseStatus.ENGAGED,
        approved_by="property-test",
        approved_at=datetime(2026, 7, 14, tzinfo=UTC),
        work_packages=packages,
    )

    for package in eligible_work_packages(course, completed):
        assert set(package.dependencies).issubset(completed)


@given(
    st.sampled_from(
        (
            ActionKind.MERGE_PR,
            ActionKind.RELEASE,
            ActionKind.PRODUCTION_DEPLOY,
            ActionKind.SECRETS,
            ActionKind.BILLING,
            ActionKind.DESTRUCTIVE,
            ActionKind.FORCE_PUSH,
            ActionKind.DELETE_BRANCH,
        )
    ),
    st.sampled_from((OperationMode.SUPERVISED, OperationMode.AUTONOMOUS)),
)
def test_protected_actions_never_run_without_explicit_approval(
    action: ActionKind, mode: OperationMode
) -> None:
    with TemporaryDirectory() as directory:
        repo = repo_config(Path(directory), mode=mode, completion=CompletionPolicy.OWNER_APPROVAL)
        result = evaluate_action(
            repo,
            PlanDecision(action=action, summary="Protected property test", reason="Policy invariant"),
            execute=True,
            shadow=False,
        )

        assert result.allowed is False
        assert result.requires_owner is True
