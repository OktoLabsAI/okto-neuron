"""The shutdown force-cancel flags must not leak from one test into the next.

``tests/conftest.py::_reset_model_call_force_cancel`` clears them around every
test. These two tests rely on definition order: the first raises the flags the
way a ``runtime._run_async`` shutdown does, the second proves they are down.
"""

from __future__ import annotations

from okto_neuron.llm import LLMCallCancelled, _cli_provider, _litellm_process


def test_a_sets_the_force_cancel_flags() -> None:
    _litellm_process.cancel_active_litellm_calls()
    _cli_provider.kill_active_cli_processes()
    assert _litellm_process._FORCE_CANCEL.is_set()
    assert _cli_provider._FORCE_CANCEL_ALL.is_set()


def test_b_model_calls_work_after_it() -> None:
    assert not _litellm_process._FORCE_CANCEL.is_set()
    assert not _cli_provider._FORCE_CANCEL_ALL.is_set()
    # The guard at the top of run_cancellable_completion raises before any
    # process is spawned when the flag is up; with it down, a model call is
    # not rejected by that guard (an invalid request fails later, not as
    # LLMCallCancelled).
    try:
        _litellm_process.run_cancellable_completion({"bad": object()}, lambda: False)
    except LLMCallCancelled:
        raise AssertionError("model call was cancelled by a leaked force-cancel flag")
    except Exception:  # noqa: BLE001 - any non-cancel outcome is fine here
        pass
