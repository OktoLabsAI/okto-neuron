"""Private JSON-over-stdio worker for one cancellable LiteLLM completion."""

from __future__ import annotations

import contextlib
import json
import sys


def _value(obj: object, key: str) -> object | None:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _scalar(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _native_finish_reason(choice: object) -> str | None:
    """The provider's OWN finish reason, before litellm rewrote it.

    litellm keeps it in ``provider_specific_fields`` (types/utils.py) only
    when it actually rewrote something, so ``None`` here means "litellm passed
    the provider's reason through unchanged", not "unknown".
    """
    fields = _value(choice, "provider_specific_fields")
    if isinstance(fields, dict):
        value = fields.get("native_finish_reason")
        return None if value is None else str(value)
    return None


def _tool_calls(message: object) -> list | None:
    """Tool calls as plain JSON, or ``None`` when the model made none."""
    raw = _value(message, "tool_calls")
    if not raw:
        return None
    calls = []
    for call in raw:  # type: ignore[union-attr]
        function = _value(call, "function")
        calls.append(
            {
                "id": _scalar(_value(call, "id")),
                "type": _scalar(_value(call, "type")),
                "name": _scalar(_value(function, "name")),
                "arguments": _scalar(_value(function, "arguments")),
            }
        )
    return calls or None


def main() -> int:
    request: dict = {}
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise TypeError("request must be a JSON object")
        # Keep the protocol stream clean even if a provider prints diagnostics.
        with contextlib.redirect_stdout(sys.stderr):
            import litellm

            response = litellm.completion(**request)
        choices = _value(response, "choices") or []
        first = choices[0] if choices else None  # type: ignore[index]
        message = _value(first, "message")
        usage = _value(response, "usage")
        details = _value(usage, "prompt_tokens_details")
        completion_details = _value(usage, "completion_tokens_details")
        # PROTOCOL v2 (additive, backward compatible — see the contract note in
        # ``_litellm_process._response_from_payload``). The parent process only
        # ever sees what this dict carries, so anything the parent's own
        # forensics read off a native litellm response has to be carried here
        # explicitly or it is silently lost in the daemon, where EVERY
        # completion goes through this worker. ``native_finish_reason`` is the
        # headline case: litellm's ``map_finish_reason`` rewrites an unmapped
        # provider reason to a clean ``"stop"`` and keeps the truth only in
        # ``provider_specific_fields``, which the v1 payload dropped.
        payload = {
            "ok": True,
            "protocol": 2,
            "content": str(_value(message, "content") or ""),
            "finish_reason": _scalar(_value(first, "finish_reason")),
            "native_finish_reason": _native_finish_reason(first),
            "reasoning_content": _scalar(_value(message, "reasoning_content")),
            "tool_calls": _tool_calls(message),
            "prompt_tokens": _scalar(_value(usage, "prompt_tokens")),
            "completion_tokens": _scalar(_value(usage, "completion_tokens")),
            "total_tokens": _scalar(_value(usage, "total_tokens")),
            "cached_tokens": _scalar(_value(details, "cached_tokens")),
            "reasoning_tokens": _scalar(_value(completion_details, "reasoning_tokens")),
        }
    except Exception as exc:  # noqa: BLE001 - serialize the remote provider failure
        from okto_neuron.llm import classify_provider_exception

        classification = classify_provider_exception(exc)
        safe_error = str(exc)
        api_key = request.get("api_key") if isinstance(request, dict) else None
        if isinstance(api_key, str) and api_key:
            safe_error = safe_error.replace(api_key, "[redacted]")
        payload = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": safe_error,
            "error_category": classification.category,
            "retry_after_s": classification.retry_after_s,
            "retryable": classification.retryable,
        }
    json.dump(payload, sys.stdout, separators=(",", ":"), ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
