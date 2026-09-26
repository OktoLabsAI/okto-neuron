"""Shared base config and primitive aliases for the support type package.

Per Cluster 1B locked decisions (dec_cd7a06be, dec_0902c7ae): all 6 support
types are frozen Pydantic v2 value-objects with extra='forbid'. HEX64 is the
lowercase-hex sha256 alias used pervasively for content hashing / ID anchors
(br_705645b1).
"""

from __future__ import annotations

import unicodedata
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, StringConstraints

__all__ = ["SupportBase", "HEX64", "PATH_MAX_LENGTH", "validate_safe_path"]


# Max path length for vault-relative source paths. Locked by br_33ab833d.
PATH_MAX_LENGTH: Final[int] = 4096


def validate_safe_path(value: object, label: str) -> str:
    """Shared vault-relative path-safety pipeline (br_33ab833d), in order:

      1. reject non-str
      2. reject NUL byte
      3. reject absolute path (POSIX leading '/' OR Windows drive letter)
      4. reject backslash
      5. reject `..` traversal segment
      6. enforce length <= PATH_MAX_LENGTH
      7. NFC-normalize the returned path (dec_ee679ddd)

    `label` is used to prefix error messages (e.g. "Block.path") so callers
    keep their existing, test-pinned error wording.
    """
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL byte")
    # Absolute paths: POSIX leading slash OR Windows-style drive letter.
    if value.startswith("/"):
        raise ValueError(f"{label} must not be absolute (leading '/')")
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        raise ValueError(f"{label} must not be absolute (drive letter)")
    if "\\" in value:
        raise ValueError(f"{label} must not contain backslash")
    # Reject `..` as any path segment (POSIX separator).
    if ".." in value.split("/"):
        raise ValueError(f"{label} must not contain '..' traversal segment")
    if len(value) > PATH_MAX_LENGTH:
        raise ValueError(f"{label} exceeds max length {PATH_MAX_LENGTH} (got {len(value)})")
    return unicodedata.normalize("NFC", value)


# Shared model config for every support type. Locked by dec_cd7a06be + br_0d8c0707.
_SUPPORT_CONFIG = ConfigDict(
    frozen=True,
    extra="forbid",
    str_strip_whitespace=False,
    validate_assignment=True,
)


class SupportBase(BaseModel):
    """Base class for all 6 support types. Frozen, strict, no whitespace munging."""

    model_config = _SUPPORT_CONFIG


# HEX64: 64-char lowercase hex sha256. Locked by br_705645b1.
HEX64 = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64),
]
