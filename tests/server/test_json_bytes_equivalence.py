"""Pre-encoded responses are byte-identical to ``JSONResponse`` (issue #13)."""

from __future__ import annotations

import pytest
from starlette.responses import JSONResponse

from okto_neuron.server._store_io import encode_json, json_bytes_response


def _rows(count: int) -> list[dict[str, object]]:
    return [
        {
            "id": f"n{i}",
            "text": 'café ☃   "quoted" \\ / \n',
            "score": i / 7,
            "ok": i % 2 == 0,
            "none": None,
            "nested": {"k": [1, 2.5, "x"]},
        }
        for i in range(count)
    ]


_SHAPES = {
    "small dict": {"status": "ok", "items": []},
    "ascii list": [1, 2, 3],
    "non-ascii scalar": "日本語 ☃",
    "big list": _rows(200),
    "dict with big list": {"status": "ok", "items": _rows(200), "total": 200},
    "dict with several big values": {
        "a": _rows(50),
        "b": {str(i): _rows(1) for i in range(60)},
        "c": 1,
    },
    "list of big lists": [_rows(40), _rows(40)],
    "non-str keys fall back": {1: _rows(40), "a": _rows(40)},
    "empty": {},
}


@pytest.mark.parametrize("name", sorted(_SHAPES))
def test_encoded_bytes_equal_jsonresponse(name: str) -> None:
    obj = _SHAPES[name]
    assert encode_json(obj) == JSONResponse(obj).body


def test_nan_is_rejected_like_jsonresponse() -> None:
    with pytest.raises(ValueError):
        encode_json({"x": [float("nan")] * 40})
    with pytest.raises(ValueError):
        JSONResponse({"x": [float("nan")] * 40})


def test_response_headers_match_jsonresponse() -> None:
    obj = {"status": "ok"}
    extra = {"Cache-Control": "no-store"}
    ours = json_bytes_response(encode_json(obj), status_code=202, headers=extra)
    theirs = JSONResponse(obj, status_code=202, headers=extra)
    assert ours.body == theirs.body
    assert ours.status_code == theirs.status_code == 202
    assert dict(ours.headers) == dict(theirs.headers)
