from __future__ import annotations

import pytest
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

import okto_neuron


def test_ts_8259abd4_validation_error_is_pydantic_alias() -> None:
    assert okto_neuron.ValidationError is PydanticValidationError


def test_ts_8259abd4_pydantic_validation_raises_marginalia_alias() -> None:
    class Example(BaseModel):
        value: int

    with pytest.raises(okto_neuron.ValidationError):
        Example.model_validate({"value": "not an integer"})
