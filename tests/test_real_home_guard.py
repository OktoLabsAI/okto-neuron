"""The real-home guard in tests/conftest.py is active inside every test."""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from tests.conftest import _NEURON_PATH_VARS, assert_home_is_isolated


def test_home_is_not_the_real_home() -> None:
    real = Path(os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir))
    assert Path(os.path.realpath(Path.home())) != real
    assert Path(os.path.realpath(os.path.expanduser("~"))) != real
    for dirname in (".marginalia", ".okto-neuron"):
        assert real not in Path(os.path.realpath(Path.home() / dirname)).parents


def test_neuron_path_variables_are_unset() -> None:
    assert [n for n in _NEURON_PATH_VARS if n in os.environ] == []


def test_guard_fires_when_home_is_real(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)
    with pytest.raises(pytest.UsageError, match="REAL-HOME GUARD"):
        assert_home_is_isolated()


def test_write_under_real_app_dir_is_refused() -> None:
    real_dir = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".okto-neuron" / "no-such-dir-guard-probe" / "child"
    with pytest.raises(PermissionError, match="REAL-HOME GUARD"):
        os.mkdir(real_dir)
    with pytest.raises(PermissionError, match="REAL-HOME GUARD"):
        open(real_dir / "x.lock", "w")
