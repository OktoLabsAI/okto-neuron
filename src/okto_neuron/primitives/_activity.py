"""Activity primitive (prov:Activity / crm:E7 / schema:Event).

Meetings, events, decisions-as-act, conversations, action items.
Carries optional temporal extents (``started_at``, ``ended_at``) with an
ordering invariant enforced at validation time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar

from pydantic import (
    AliasChoices,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from ._base import Primitive


def _activity_schema_extra(schema: dict[str, Any]) -> None:
    schema["x-marginalia-standards"] = list(Activity.__standards__)


def _raise(code: str, msg: str) -> None:
    raise PydanticCustomError(code, msg, {"code": code})


class Activity(Primitive):
    """Activity — a happening with optional start/end timestamps."""

    __standards__: ClassVar[tuple[str, ...]] = (
        "prov:Activity",
        "crm:E7",
        "schema:Event",
    )
    __schema_version__: ClassVar[str] = "0.1.0"

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_assignment=True,
        json_schema_extra=_activity_schema_extra,
    )

    type: str = Field(default="core:Activity")
    started_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("started_at", "started_at_time"),
    )
    ended_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("ended_at", "ended_at_time"),
    )

    @field_validator("started_at", "ended_at", mode="after")
    @classmethod
    def _ts_utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        if v.tzinfo is None:
            _raise("datetime_not_tz_aware", "datetime must be timezone-aware")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _ordering(self) -> "Activity":
        if (
            self.started_at is not None
            and self.ended_at is not None
            and self.started_at > self.ended_at
        ):
            _raise("temporal_order_violation", "ended_at must be >= started_at")
        return self

    @property
    def started_at_time(self) -> datetime | None:
        return self.started_at

    @property
    def ended_at_time(self) -> datetime | None:
        return self.ended_at
