from __future__ import annotations

_MODEL_ALIASES = {
    "gpt-5.6": "gpt-5.6-sol",
}


def models_match(requested: str, reported: str) -> bool:
    """Match model identity, including provider and GPT-5.6 aliases."""
    requested_provider, requested_model = _canonical_route(requested)
    reported_provider, reported_model = _canonical_route(reported)
    if (requested_provider, requested_model) == (reported_provider, reported_model):
        return True
    if requested_model != reported_model:
        return False
    if requested_provider is None or reported_provider is None:
        return True
    return {requested_provider, reported_provider} == {"codex", "openai"}


def runtime_model(runtime: str, model: str) -> str:
    """Translate a portable route into the runtime's native model route.

    Make It So stores Codex-family routes as ``codex/<model>`` so the same
    policy can be used by the direct Codex adapter and OpenClaw. OpenClaw's
    Native Codex provider is exposed by its CLI as ``openai/<model>``; keeping
    that translation here prevents an adapter from passing a portable alias
    literally to the provider.
    """
    normalized_runtime = runtime.strip().lower()
    if normalized_runtime == "codex" and model.startswith("codex/"):
        return model.split("/", 1)[1]
    if normalized_runtime == "openclaw" and model.startswith("codex/"):
        return f"openai/{model.split('/', 1)[1]}"
    return model


def _split_model_route(value: str) -> tuple[str | None, str]:
    provider, separator, model = value.lower().partition("/")
    return (provider, model) if separator else (None, provider)


def _canonical_route(value: str) -> tuple[str | None, str]:
    provider, model = _split_model_route(value.strip())
    return provider, _MODEL_ALIASES.get(model, model)


__all__ = ["models_match", "runtime_model"]
