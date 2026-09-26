"""ChatGPT-subscription provider — EXPERIMENTAL and opt-in; never for published results.

TERMS
-----
This provider signs in with a ChatGPT consumer subscription. OpenAI's terms for
those subscriptions may not allow access from third-party tools. Okto Neuron
does not decide that for the user: it stays off unless the user sets
``OKTO_NEURON_ENABLE_CHATGPT=1``, and it shows :data:`CHATGPT_TERMS_WARNING`
at login, when it is enabled, and when it is refused.

WHAT THIS IS
------------
litellm 1.87 ships a complete ``chatgpt`` provider that talks to the ChatGPT
backend (``https://chatgpt.com/backend-api/codex``) using the OAuth tokens a
ChatGPT subscription issues, via the Responses API. Okto Neuron already accepted
``chatgpt`` as a driver name (``config/_vault.py`` ``_LLM_PROVIDERS``); this
module is the wiring that makes it usable, and — more importantly — the wiring
that makes it HONEST about what it cannot do.

WHY IT IS GATED
---------------
A ChatGPT subscription is a flat-rate consumer plan, not a metered API plan.
Numbers produced through it are not comparable with anything, cannot be costed
(see "cost" below), and are shaped by constraints litellm applies silently (see
"narrowing"). So the provider refuses to construct unless
``OKTO_NEURON_ENABLE_CHATGPT=1`` is set explicitly in the environment. There is
no config key and no default — the same shape as the telemetry opt-in — so it
can never become a default and never be reached by a run that did not ask for
it by name.

FOUR THINGS THAT ARE TRUE AND SURPRISING
----------------------------------------
1. **Narrowing.** litellm whitelists the request body down to eleven keys
   (``llms/chatgpt/responses/transformation.py:94-108``). ``temperature``,
   ``top_p``, ``seed``, ``max_tokens`` and ``response_format`` are all dropped
   silently, while ``ChatGPTConfig`` (a plain ``OpenAIConfig`` subclass) keeps
   advertising all of them. The correction lives in
   ``llm/__init__._CHATGPT_TRANSMITTED_PARAMS`` so request shaping, the param
   accounting and the Config UI all read the narrowed truth from one place.
   Structured output in particular does NOT work here: curation that assumes
   JSON mode gets prose.
2. **Originator.** litellm defaults the ``originator`` header and user-agent to
   ``codex_cli_rs`` (``llms/chatgpt/common_utils.py:23``, ``:212-214``) — that
   claims to OpenAI's servers that the traffic is OpenAI's own first-party
   Codex CLI. Okto Neuron is not that, so this module pins
   ``CHATGPT_ORIGINATOR`` (``_compat.CHATGPT_ORIGINATOR``) before the first call.
3. **Prompt accounting.** Unless ``CHATGPT_DEFAULT_INSTRUCTIONS`` is set,
   litellm prepends the entire Codex CLI system prompt (~80 lines hardcoded at
   ``common_utils.py:25-105``) to every request. Measured: a six-word prompt
   billed 1638 prompt tokens. Left alone, ``prompt_tokens`` would be mostly
   somebody else's prompt while telemetry called it ours. This module sets the
   variable to Okto Neuron's own one-line instruction, and records what it set
   so a trace can be interpreted later.
4. **Cost.** Every ``chatgpt/*`` entry in ``litellm.model_cost`` has
   ``input_cost_per_token: None``. A cost consumer that treats that as zero
   would report a free run. This provider therefore attaches an explicit
   ``cost_unavailable_reason`` to the per-call stats instead of a number.

CREDENTIALS — DO NOT SHARE A FILE WITH codex OR pi
--------------------------------------------------
litellm reads a FLAT ``{access_token, refresh_token, id_token, account_id,
expires_at}`` JSON from ``$CHATGPT_TOKEN_DIR`` (default
``~/.config/litellm/chatgpt/auth.json``). Two hazards, both verified in
``llms/chatgpt/authenticator.py``:

* It REWRITES that file on a plain read — ``_is_token_expired`` (``:110-119``)
  and ``get_account_id`` (``:74-87``) both call ``_write_auth_file`` when a
  field is missing, and ``_write_auth_file`` (``:103-108``) is a bare
  ``open(..., "w")`` with no atomic rename, no ``0600``, and no locking.
* OpenAI rotates the refresh token on use.

So pointing ``CHATGPT_TOKEN_DIR`` at ``~/.codex`` or ``~/.pi/agent`` would
corrupt codex's nested schema (codex stores its tokens under a ``tokens`` key,
not flat) and would log the user out of the other tool the first time either
side rotated. Okto Neuron gets its OWN directory, provisioned by a one-time
interactive device-code login. See ``docs/remote-providers.md``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from okto_neuron._compat import CHATGPT_ORIGINATOR
from okto_neuron._compat import getenv as _compat_getenv
from okto_neuron.llm import LiteLLMProvider, LLMProviderError, logger

if TYPE_CHECKING:
    from okto_neuron.config._vault import ResolvedLLM

ENV_OPT_IN = "OKTO_NEURON_ENABLE_CHATGPT"

# Where the traffic actually goes. litellm's default
# (``llms/chatgpt/common_utils.py:20``), repeated here ONLY so the telemetry
# span records a truthful endpoint: ``resolved.api_base`` for this provider is
# the vault's unrelated loopback default, and a span that named that would make
# a hosted run look local forever after.
CHATGPT_BACKEND = "https://chatgpt.com/backend-api/codex"

# Replaces litellm's ~1.6K-token Codex CLI preamble. Short on purpose: the
# point is that ``prompt_tokens`` should be dominated by Okto Neuron's own
# prompt, so that a token count means what a reader assumes it means.
OKTO_NEURON_INSTRUCTIONS = (
    "You are a helpful assistant answering exactly what the user asks. "
    "Respond directly, with no preamble."
)

ORIGINATOR = CHATGPT_ORIGINATOR

# litellm's own default, mirrored so the preflight below can look for the file
# WITHOUT constructing an ``Authenticator`` (whose ``__init__`` already
# ``os.makedirs``-es the directory, and whose reads rewrite the file).
DEFAULT_TOKEN_DIR = "~/.config/litellm/chatgpt"

_ENV_PINNED = False


def credential_path() -> str:
    """The auth file litellm will read, expanded — without touching it."""

    token_dir = os.environ.get("CHATGPT_TOKEN_DIR") or DEFAULT_TOKEN_DIR
    auth_file = os.environ.get("CHATGPT_AUTH_FILE") or "auth.json"
    return os.path.join(os.path.expanduser(token_dir), auth_file)


def assert_credential_present() -> None:
    """Fail fast rather than let litellm start an interactive login.

    Observed live, and the reason this function exists: with no credential
    file, a plain ``completion()`` call does not raise. litellm's authenticator
    BEGINS AN INTERACTIVE DEVICE-CODE FLOW — it prints a URL and an eight-digit
    code to stdout and then blocks, polling, for as long as the flow allows.
    Inside ``okto-neuron serve`` that is a completion that never returns and a
    device code written into a log nobody is reading; in a benchmark it is a
    wedged run. An unattended process must never be the thing that starts an
    OAuth flow, so the provider checks first and refuses with instructions.

    The check is deliberately a read-only ``os.path.exists`` plus a JSON parse.
    Constructing litellm's ``Authenticator`` to ask it would itself create the
    directory and, on several paths, rewrite the file.
    """

    path = credential_path()
    if not os.path.exists(path):
        raise LLMProviderError(
            f"no ChatGPT credential at {path}. litellm would respond to this by "
            "starting an INTERACTIVE device-code login and blocking on it, which "
            "an unattended process must never do, so the call is refused instead. "
            "Provision it once, in a terminal, with `okto-neuron provider login "
            "chatgpt`. It gets its OWN directory: litellm rewrites this file even "
            "on a plain read and OpenAI rotates the refresh token, so pointing "
            "CHATGPT_TOKEN_DIR at ~/.codex or ~/.pi/agent would corrupt or "
            "invalidate those tools' logins. See docs/remote-providers.md.",
            category="authentication",
            retryable=False,
        )
    try:
        import json

        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise LLMProviderError(
            f"ChatGPT credential at {path} is unreadable or not JSON: {exc}. "
            "litellm writes this file non-atomically and without locking "
            "(authenticator.py:103-108), so a concurrent write can truncate it; "
            "redo the one-time login with `okto-neuron provider login chatgpt`. "
            "See docs/remote-providers.md.",
            category="authentication",
            retryable=False,
        ) from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise LLMProviderError(
            f"ChatGPT credential at {path} has no 'access_token'. litellm expects "
            "a FLAT {access_token, refresh_token, id_token, account_id, "
            "expires_at} object; codex stores its tokens nested under a 'tokens' "
            "key, so a copied ~/.codex/auth.json will not work (and must not be "
            "shared anyway). Provision Okto Neuron's own with `okto-neuron provider "
            "login chatgpt`. See docs/remote-providers.md.",
            category="authentication",
            retryable=False,
        )


# Directories other tools keep their OWN ChatGPT/OpenAI logins in. litellm
# rewrites its credential file on a plain read and OpenAI rotates the refresh
# token on use, so sharing either with Okto Neuron would corrupt the other
# tool's file (codex nests its tokens under ``tokens``) or log it out.
_FOREIGN_TOKEN_DIRS = ("~/.codex", "~/.pi/agent")


def token_dir() -> str:
    """Okto Neuron's credential directory, expanded against THIS process's HOME.

    Resolved in the caller's process on purpose: a harness that isolates a
    daemon's ``HOME`` must resolve this first and hand the result down as
    ``CHATGPT_TOKEN_DIR``, or the daemon looks inside the isolated home and
    finds nothing.
    """

    return os.path.expanduser(os.environ.get("CHATGPT_TOKEN_DIR") or DEFAULT_TOKEN_DIR)


def refuse_foreign_token_dir() -> None:
    """Refuse a credential directory that belongs to codex or pi."""

    real = os.path.realpath(token_dir())
    for foreign in _FOREIGN_TOKEN_DIRS:
        foreign_real = os.path.realpath(os.path.expanduser(foreign))
        if real == foreign_real or real.startswith(foreign_real + os.sep):
            raise LLMProviderError(
                f"CHATGPT_TOKEN_DIR resolves to {real}, which is {foreign}'s own "
                "login directory. litellm rewrites its credential file even on a "
                "plain read and OpenAI rotates the refresh token, so sharing it "
                "would corrupt or log out that tool. Leave CHATGPT_TOKEN_DIR "
                f"unset (default {DEFAULT_TOKEN_DIR}) or point it at a directory "
                "of Okto Neuron's own.",
                category="authentication",
                retryable=False,
            )


def _jwt_claims(token: object) -> dict[str, object]:
    """Decode a JWT payload WITHOUT verifying it — display only, never trust."""

    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    import base64
    import json

    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def credential_summary() -> dict[str, object]:
    """What the stored credential says, read without writing anything.

    Opens the file directly rather than through litellm's ``Authenticator``,
    whose reads rewrite the file. The identity fields come from the id_token's
    claims, which are decoded for display only and never trusted for access:
    the live backend is the only thing that decides whether a call succeeds
    (a cached ``chatgpt_plan_type`` was observed stale for months).
    """

    import json
    from datetime import datetime, timezone

    path = credential_path()
    assert_credential_present()
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    claims = _jwt_claims(data.get("id_token"))
    auth = claims.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    expires_at = data.get("expires_at")
    expires_iso: str | None = None
    expired: bool | None = None
    if isinstance(expires_at, int | float):
        seconds = expires_at / 1000 if expires_at > 1e11 else expires_at
        moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
        expires_iso = moment.isoformat(timespec="seconds")
        expired = moment <= datetime.now(timezone.utc)
    return {
        "path": path,
        "email": claims.get("email"),
        "plan": auth.get("chatgpt_plan_type"),
        "subscription_active_until": auth.get("chatgpt_subscription_active_until"),
        "access_token_expires_at": expires_iso,
        "access_token_expired": expired,
    }


def _refuse_during_device_code_cooldown(path: str) -> None:
    """Refuse instead of letting litellm wait in silence.

    litellm allows one device-code request per five minutes. Inside that
    window a new login prints nothing and polls the file for up to the rest of
    it, assuming another terminal is completing the first login (observed
    2026-09-22: a second attempt right after an aborted one sat silent).
    """

    import json
    import time

    from litellm.llms.chatgpt.authenticator import DEVICE_CODE_COOLDOWN_SECONDS

    try:
        with open(path, encoding="utf-8") as handle:
            requested_at = float(json.load(handle).get("device_code_requested_at"))
    except (OSError, ValueError, TypeError, AttributeError):
        return
    remaining = int(DEVICE_CODE_COOLDOWN_SECONDS - (time.time() - requested_at))
    if remaining > 0:
        raise LLMProviderError(
            f"a device-code login was started {int(time.time() - requested_at)}s ago "
            f"and not finished ({path} holds only its cooldown record). litellm "
            "allows one code per 5 minutes and would wait silently until then. "
            f"Finish that login where its code is shown, or retry in {remaining}s.",
            category="authentication",
            retryable=True,
        )


def interactive_login(*, force: bool = False) -> dict[str, object]:
    """Provision the credential through litellm's device-code flow.

    Interactive-only by construction: it refuses unless both stdin and stdout
    are a terminal, because the flow prints a URL and a code and then blocks
    polling until a human authorizes it. Nothing unattended — no daemon, no
    benchmark, no request path — calls this; ``assert_credential_present``
    stays the guard on those paths.

    With an existing credential it reports it and stops, unless ``force``, in
    which case the old file is kept aside as ``<file>.bak-<timestamp>`` before
    the new login writes a fresh one.
    """

    import sys
    import time

    refuse_foreign_token_dir()
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise LLMProviderError(
            "the ChatGPT login is interactive (it prints a device code and waits "
            "for you to authorize it in a browser); run it from a terminal.",
            category="authentication",
            retryable=False,
        )
    path = credential_path()
    if os.path.exists(path):
        try:
            existing = credential_summary()
        except LLMProviderError:
            # Not a usable credential. An ABORTED device-code login leaves
            # exactly this behind: a small file holding only litellm's
            # device-code cooldown record, no access_token (observed
            # 2026-09-22). It is not a credential to protect, and litellm needs
            # the cooldown record, so the login proceeds over it untouched.
            existing = None
            _refuse_during_device_code_cooldown(path)
        if existing is not None:
            if not force:
                existing["already_provisioned"] = True
                return existing
            os.replace(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")

    from litellm.llms.chatgpt.authenticator import Authenticator

    Authenticator().get_access_token()
    summary = credential_summary()
    summary["already_provisioned"] = False
    return summary


#: Shown at login, when the provider is enabled, and when it is refused.
CHATGPT_TERMS_WARNING = (
    "The chatgpt provider is experimental. It uses a ChatGPT consumer subscription, and "
    "OpenAI's terms for those subscriptions may not allow access from third-party tools "
    "such as Okto Neuron. Read those terms before enabling it; you use it at your own risk."
)


def opt_in_enabled() -> bool:
    """Whether the operator explicitly turned this provider on."""

    return _compat_getenv(ENV_OPT_IN, "").strip().lower() in {"1", "true", "yes", "on"}


def pin_provider_env() -> dict[str, str]:
    """Pin the two env vars litellm reads, and report what was pinned.

    ``setdefault`` semantics are deliberately NOT used for the originator: an
    inherited ``CHATGPT_ORIGINATOR=codex_cli_rs`` (litellm's default is applied
    when the variable is absent, but a shell may also set it outright) would
    keep Okto Neuron impersonating OpenAI's first-party CLI. The instructions
    variable IS left alone when the operator set it — that one is a legitimate
    prompt-engineering choice, and overriding it would be us silently editing
    their prompt.
    """

    global _ENV_PINNED
    os.environ["CHATGPT_ORIGINATOR"] = ORIGINATOR
    instructions = os.environ.get("CHATGPT_DEFAULT_INSTRUCTIONS")
    if not instructions:
        instructions = OKTO_NEURON_INSTRUCTIONS
        os.environ["CHATGPT_DEFAULT_INSTRUCTIONS"] = instructions
    if not _ENV_PINNED:
        _ENV_PINNED = True
        logger.warning(CHATGPT_TERMS_WARNING)
        logger.info(
            "chatgpt provider enabled (EXPERIMENTAL, EXPLORATION ONLY): originator=%s, "
            "default instructions pinned (%d chars) in place of litellm's Codex "
            "CLI preamble. Structured output, temperature, top_p, seed and "
            "max_tokens are NOT transmitted by this provider; per-token cost is "
            "unavailable. Do not publish results from this endpoint.",
            ORIGINATOR,
            len(instructions),
        )
    return {
        "CHATGPT_ORIGINATOR": ORIGINATOR,
        "CHATGPT_DEFAULT_INSTRUCTIONS": instructions,
    }


class ChatGPTProvider(LiteLLMProvider):
    """``LiteLLMProvider`` restricted to what the ChatGPT backend really accepts.

    Everything about request assembly is inherited — the narrowing is applied
    upstream, by ``parameter_capabilities`` returning the
    ``litellm_chatgpt_narrowed`` capability set for this provider, which
    ``_add_supported_param`` then enforces. This subclass exists for the three
    things capabilities cannot express: the opt-in gate, the env pinning, and
    the cost/endpoint facts the per-call stats must carry.
    """

    def __init__(self, resolved: "ResolvedLLM") -> None:
        if not opt_in_enabled():
            raise LLMProviderError(
                "the 'chatgpt' provider is opt-in and disabled. It reaches a "
                "ChatGPT SUBSCRIPTION, not a metered API plan: litellm silently "
                "drops response_format, temperature, top_p, seed and max_tokens "
                f"for it, and no per-token cost exists, so results from it are "
                f"for local exploration only and must never be published. Set "
                f"{ENV_OPT_IN}=1 to enable it for this process, and read "
                "docs/remote-providers.md first. " + CHATGPT_TERMS_WARNING,
                category="unavailable",
                retryable=False,
            )
        self._chatgpt_env = pin_provider_env()
        super().__init__(resolved)
        # Overrides the loopback default inherited from ``resolved``. Read by
        # the telemetry wrapper, so every span from an affected run carries the
        # real endpoint and the run stays identifiable after the fact.
        self.api_base = CHATGPT_BACKEND

    def complete(self, messages, **kwargs) -> str:  # type: ignore[no-untyped-def]
        # BEFORE litellm gets the chance to start a device-code login.
        assert_credential_present()
        response = super().complete(messages, **kwargs)
        # An EXPLICIT branch, not a silent zero. ``litellm.model_cost`` carries
        # ``input_cost_per_token: None`` for all fourteen ``chatgpt/*`` entries,
        # so any consumer that multiplies tokens by a price gets nothing
        # meaningful; saying why is the only honest output.
        from okto_neuron.llm import _set_last_call_stats, last_call_stats

        stats = dict(last_call_stats() or {})
        stats["cost_unavailable_reason"] = (
            "chatgpt is a flat-rate subscription; litellm.model_cost has "
            "input_cost_per_token=None for every chatgpt/* model"
        )
        stats["chatgpt_default_instructions_chars"] = len(
            self._chatgpt_env.get("CHATGPT_DEFAULT_INSTRUCTIONS", "")
        )
        _set_last_call_stats(stats)
        return response


__all__ = [
    "CHATGPT_BACKEND",
    "DEFAULT_TOKEN_DIR",
    "assert_credential_present",
    "credential_path",
    "ChatGPTProvider",
    "CHATGPT_TERMS_WARNING",
    "ENV_OPT_IN",
    "OKTO_NEURON_INSTRUCTIONS",
    "ORIGINATOR",
    "opt_in_enabled",
    "pin_provider_env",
]
