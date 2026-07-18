from __future__ import annotations

from typing import Any

import pytest

from make_it_so.models import (
    ApplicationSurface,
    HarnessConfig,
    ModelCapability,
    ModelExecutionMode,
    ModelPolicy,
    ModelProfile,
    ModelTarget,
    NotificationConfig,
    QAProfile,
    ReasoningEffort,
    RoleModels,
    UsageConfig,
)
from tests.helpers import model_policy


def target(**updates: object) -> ModelTarget:
    values: dict[str, Any] = {"model": "test-model", **updates}
    return ModelTarget(**values)


def test_model_target_rejects_invalid_budget_and_autonomy_provenance() -> None:
    with pytest.raises(ValueError, match="max_total_tokens"):
        target(max_input_tokens=8, max_output_tokens=8, max_total_tokens=10)
    with pytest.raises(ValueError, match="qualification=autonomous"):
        target(autonomous_eligible=True)


def test_model_target_enforces_declared_capabilities() -> None:
    capability = ModelCapability(
        supported_efforts=frozenset({ReasoningEffort.LOW}),
        supported_execution_modes=frozenset({ModelExecutionMode.PRO}),
    )
    with pytest.raises(ValueError, match="reasoning effort"):
        target(capability=capability, thinking=ReasoningEffort.HIGH)
    with pytest.raises(ValueError, match="execution mode"):
        target(capability=capability, thinking=ReasoningEffort.LOW)


def test_model_profile_validates_fallback_contracts() -> None:
    fallback = target(model="fallback")
    with pytest.raises(ValueError, match="fallback routes"):
        ModelProfile(primary=target(), fallbacks=(fallback,), allow_fallback=False)
    with pytest.raises(ValueError, match="max_attempts"):
        ModelProfile(primary=target(), fallbacks=(fallback,), max_attempts=1)


def test_model_policy_resolves_profiles_and_optional_roles() -> None:
    base = model_policy()
    tester = RoleModels(primary=target(model="tester"))
    ux = RoleModels(primary=target(model="ux"))
    policy = base.model_copy(update={"tester": tester, "ux_reviewer": ux, "profiles": {"planner": tester}})

    assert policy.for_role("planner") == tester
    assert policy.for_role("tester") == tester
    assert policy.for_role("ux_reviewer") == ux
    assert policy.effective_for_role("coder", {"coder": tester}) == tester
    with pytest.raises(ValueError, match="no configured role"):
        policy.for_role("unknown")


def test_model_policy_falls_back_for_optional_roles_and_rejects_weak_final_review() -> None:
    base = model_policy()
    assert base.for_role("tester") == base.coder
    assert base.for_role("ux_reviewer") == base.coder
    with pytest.raises(ValueError, match="final_reviewer"):
        ModelPolicy(
            baseline=base.baseline,
            planner=base.planner,
            coder=base.coder,
            reviewer=base.reviewer,
            final_reviewer=ModelProfile(primary=target()),
        )


def test_model_and_delivery_configuration_rejects_invalid_shapes() -> None:
    with pytest.raises(ValueError, match="QA profile"):
        QAProfile(key="empty", title="Empty")
    with pytest.raises(ValueError, match="openclaw_discord"):
        NotificationConfig(kind="openclaw_discord")
    with pytest.raises(ValueError, match="discord_webhook"):
        NotificationConfig(kind="discord_webhook")
    with pytest.raises(ValueError, match="model_daily_token_limits"):
        UsageConfig(model_daily_token_limits={"": 1})
    assert QAProfile(key="cli", title="CLI", surfaces=frozenset({ApplicationSurface.CLI}))
    assert HarnessConfig(kind="extension", executable="runner")
