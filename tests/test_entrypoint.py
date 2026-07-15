from __future__ import annotations

import runpy
import sys

import pytest


def test_python_module_entrypoint_delegates_to_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["captains_chair", "--help"])
    with pytest.raises(SystemExit) as raised:
        runpy.run_module("captains_chair.__main__", run_name="__main__")
    assert raised.value.code == 0
