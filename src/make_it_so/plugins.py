from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from importlib.metadata import entry_points as metadata_entry_points
from typing import Any, cast


class PluginDiscoveryError(RuntimeError):
    """Raised when an installed adapter plugin cannot be loaded safely."""


EntryPointProvider = Callable[[], Any]


def load_entrypoint_plugins(
    registry: Any,
    *,
    group: str,
    provider: EntryPointProvider = metadata_entry_points,
    loaded: set[str] | None = None,
) -> tuple[str, ...]:
    """Load adapter registrars from one explicit entry-point group.

    A plugin is a callable accepting the target registry and registering one or
    more typed builders. The core never imports a runtime package directly.
    """
    loaded_names: set[str] = loaded if loaded is not None else set()
    try:
        raw_points = provider()
        points = _select_group(raw_points, group)
    except Exception as exc:
        raise PluginDiscoveryError(f"could not inspect {group} entry points: {exc}") from exc

    discovered: list[str] = []
    for point in points:
        name = str(getattr(point, "name", "")).strip()
        if not name or name in loaded_names:
            continue
        try:
            registrar = point.load()
        except Exception as exc:
            raise PluginDiscoveryError(f"could not load {group} plugin {name!r}: {exc}") from exc
        if not callable(registrar):
            raise PluginDiscoveryError(f"{group} plugin {name!r} did not expose a callable registrar")
        try:
            registrar(registry)
        except Exception as exc:
            raise PluginDiscoveryError(f"{group} plugin {name!r} failed during registration: {exc}") from exc
        loaded_names.add(name)
        discovered.append(name)
    return tuple(discovered)


def _select_group(raw_points: Any, group: str) -> tuple[Any, ...]:
    select = getattr(raw_points, "select", None)
    if callable(select):
        selected = cast(Iterable[Any], select(group=group))
        return tuple(selected)
    if isinstance(raw_points, Mapping):
        mapping = cast(Mapping[str, Any], raw_points)
        values = cast(object, mapping.get(group, ()))
        if isinstance(values, Iterable) and not isinstance(values, (str, bytes)):
            return tuple(cast(Iterable[Any], values))
        return ()
    if isinstance(raw_points, Iterable) and not isinstance(raw_points, (str, bytes)):
        points = cast(Iterable[Any], raw_points)
        return tuple(point for point in points if getattr(point, "group", None) == group)
    return ()


__all__ = ["PluginDiscoveryError", "load_entrypoint_plugins"]
