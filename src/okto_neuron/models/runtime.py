from __future__ import annotations

import logging
import platform
from functools import lru_cache
from typing import Any, Literal
from okto_neuron._compat import getenv as _compat_getenv

_LOG = logging.getLogger(__name__)
Backend = Literal["mlx", "llama_cpp"]
_REASONS = {
    "override_env",
    "override_config",
    "auto_darwin_arm64_mlx",
    "auto_non_darwin",
    "auto_intel_mac",
    "auto_ollama_lt_0_8",
    "auto_ollama_unreachable",
    "auto_no_mlx_runner",
}
_LOGGED = set()


@lru_cache(maxsize=None)
def _detect_cached(
    env_value: str,
    config_value: str,
    system: str,
    machine: str,
    ollama_info_repr: str,
) -> tuple[Backend, str]:
    if env_value in {"mlx", "llama_cpp"}:
        return env_value, "override_env"
    if config_value in {"mlx", "llama_cpp"}:
        return config_value, "override_config"
    if system != "Darwin":
        return "llama_cpp", "auto_non_darwin"
    if machine != "arm64":
        return "llama_cpp", "auto_intel_mac"
    if not ollama_info_repr:
        return "llama_cpp", "auto_ollama_unreachable"

    fields = dict(item.split("=", 1) for item in ollama_info_repr.split(";") if "=" in item)
    version = fields.get("version", "")
    try:
        major, minor = (int(part) for part in version.split(".")[:2])
    except ValueError:
        major, minor = 0, 0

    if (major, minor) < (0, 8):
        return "llama_cpp", "auto_ollama_lt_0_8"

    runners = set(filter(None, fields.get("runners", "").split(",")))
    if "mlx" not in runners:
        return "llama_cpp", "auto_no_mlx_runner"
    return "mlx", "auto_darwin_arm64_mlx"


def detect_backend(config: Any = None, *, ollama_client: Any = None) -> Backend:
    env_value = _compat_getenv("OKTO_NEURON_OLLAMA_BACKEND", "").strip().lower()
    config_value = ""
    if config is not None:
        config_value = (
            str(
                getattr(config, "ollama_backend", "")
                or (config.get("ollama_backend", "") if isinstance(config, dict) else "")
            )
            .strip()
            .lower()
        )
    system = platform.system()
    machine = platform.machine().lower()
    ollama_info_repr = ""
    if not env_value and not config_value and system == "Darwin" and machine == "arm64":
        try:
            info = ollama_client.info() if ollama_client is not None else None
            if info:
                ver = str(info.get("version", ""))
                runners = ",".join(sorted(info.get("runners", []) or info.get("runner_names", [])))
                ollama_info_repr = f"version={ver};runners={runners}"
        except Exception:
            ollama_info_repr = ""

    backend, reason = _detect_cached(env_value, config_value, system, machine, ollama_info_repr)
    key = (backend, reason)
    if key not in _LOGGED:
        _LOG.info("backend=%s reason=%s", backend, reason)
        _LOGGED.add(key)
    return backend
