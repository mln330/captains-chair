from __future__ import annotations


def models_match(requested: str, reported: str) -> bool:
    """Match model identity while allowing the Codex/OpenAI route alias."""
    requested = requested.strip()
    reported = reported.strip()
    if requested == reported:
        return True
    requested_provider, requested_model = _split_model_route(requested)
    reported_provider, reported_model = _split_model_route(reported)
    if requested_model != reported_model:
        return False
    if requested_provider is None or reported_provider is None:
        return True
    return {requested_provider, reported_provider} == {"codex", "openai"}


def _split_model_route(value: str) -> tuple[str | None, str]:
    provider, separator, model = value.lower().partition("/")
    return (provider, model) if separator else (None, provider)


__all__ = ["models_match"]
