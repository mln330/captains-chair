from make_it_so.model_policy import models_match, runtime_model


def test_models_match_accepts_unqualified_provider_model() -> None:
    assert models_match("codex/gpt-5.3-codex-spark", "gpt-5.3-codex-spark")


def test_models_match_accepts_gpt56_alias_and_canonical_model() -> None:
    assert models_match("codex/gpt-5.6", "gpt-5.6-sol")
    assert models_match("codex/gpt-5.6", "codex/gpt-5.6-sol")


def test_models_match_accepts_codex_openai_route_alias() -> None:
    assert models_match("codex/gpt-5.5", "openai/gpt-5.5")
    assert models_match("openai/gpt-5.5", "codex/gpt-5.5")


def test_models_match_rejects_different_provider_route() -> None:
    assert not models_match("codex/gpt-5.5", "ollama/gpt-5.5")


def test_models_match_rejects_different_model() -> None:
    assert not models_match("codex/gpt-5.5", "openai/gpt-5.3-codex-spark")


def test_runtime_model_translates_portable_routes_per_adapter() -> None:
    assert runtime_model("codex", "codex/gpt-5.6-terra") == "gpt-5.6-terra"
    assert runtime_model("openclaw", "codex/gpt-5.6-terra") == "openai/gpt-5.6-terra"
    assert runtime_model("openclaw", "openai/gpt-5.6-terra") == "openai/gpt-5.6-terra"
