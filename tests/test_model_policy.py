from captains_chair.model_policy import models_match


def test_models_match_accepts_unqualified_provider_model() -> None:
    assert models_match("codex/gpt-5.3-codex", "gpt-5.3-codex")


def test_models_match_accepts_codex_openai_route_alias() -> None:
    assert models_match("codex/gpt-5.5", "openai/gpt-5.5")
    assert models_match("openai/gpt-5.5", "codex/gpt-5.5")


def test_models_match_rejects_different_provider_route() -> None:
    assert not models_match("codex/gpt-5.5", "ollama/gpt-5.5")


def test_models_match_rejects_different_model() -> None:
    assert not models_match("codex/gpt-5.5", "openai/gpt-5.3-codex")
