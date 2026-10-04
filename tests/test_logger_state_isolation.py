"""Regression for #35: logger state must not leak from one test into the next.

The two tests depend on running in definition order: ``test_a`` pollutes the
logging configuration, ``test_b`` checks the autouse restore fixture undid it.
"""

from __future__ import annotations

import logging

import pytest

_DUMMY = logging.NullHandler()


def test_a_pollutes_logger_state() -> None:
    logging.getLogger("okto_neuron").propagate = False
    logging.getLogger("okto_neuron").addHandler(_DUMMY)
    logging.getLogger().setLevel(logging.CRITICAL)


def test_b_sees_clean_logger_state(caplog: pytest.LogCaptureFixture) -> None:
    assert logging.getLogger("okto_neuron").propagate is True
    assert _DUMMY not in logging.getLogger("okto_neuron").handlers
    assert logging.getLogger().level != logging.CRITICAL
    with caplog.at_level(logging.WARNING):
        logging.getLogger("okto_neuron.x").warning("isolation-probe")
    assert "isolation-probe" in caplog.text
