"""Config-specific error import path for the 2A storage layout."""

from __future__ import annotations

from okto_neuron.errors import ConfigNotFound, ConfigParseError, ConfigVersionUnsupported

__all__ = ["ConfigNotFound", "ConfigParseError", "ConfigVersionUnsupported"]
