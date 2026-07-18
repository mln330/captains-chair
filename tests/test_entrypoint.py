from __future__ import annotations

import runpy
import sys

import pytest


def test_python_module_entrypoint_delegates_to_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["make_it_so", "--help"])
    with pytest.raises(SystemExit) as raised:
        runpy.run_module("make_it_so.__main__", run_name="__main__")
    assert raised.value.code == 0
