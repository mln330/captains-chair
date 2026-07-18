from __future__ import annotations

from importlib.resources import files


def load_prompt(name: str) -> str:
    resource = files("make_it_so").joinpath("prompts", name)
    return resource.read_text(encoding="utf-8").strip()
