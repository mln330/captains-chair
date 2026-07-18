from collections.abc import Callable
from typing import Any

import pytest

from make_it_so.plugins import PluginDiscoveryError, load_entrypoint_plugins


class EntryPoint:
    def __init__(self, name: str, group: str, registrar: Callable[[Any], None]) -> None:
        self.name = name
        self.group = group
        self._registrar = registrar

    def load(self) -> Callable[[Any], None]:
        return self._registrar


def test_plugin_loader_supports_legacy_mapping_and_deduplicates() -> None:
    calls: list[object] = []

    def register(target: Any) -> None:
        calls.append(target)

    entry_point = EntryPoint("one", "make_it_so.example", register)
    loaded: set[str] = set()
    assert load_entrypoint_plugins("registry", group="make_it_so.example", provider=lambda: {"make_it_so.example": [entry_point]}, loaded=loaded) == ("one",)
    assert load_entrypoint_plugins("registry", group="make_it_so.example", provider=lambda: [entry_point], loaded=loaded) == ()
    assert calls == ["registry"]


def test_plugin_loader_rejects_provider_failure() -> None:
    def provider() -> object:
        raise RuntimeError("entry point index unavailable")

    with pytest.raises(PluginDiscoveryError, match="could not inspect"):
        load_entrypoint_plugins("registry", group="make_it_so.example", provider=provider)
