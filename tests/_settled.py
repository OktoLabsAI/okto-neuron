"""Poll the projection-backed reads until they report a current answer.

``GET /api/v1/graph/stats`` and ``GET /api/v1/upkeep/predicates`` serve the vault's maintained
projection: 202 ``{"status": "building"}`` while it is cold, ``stale``/``rebuilding`` flags while
it lags a write. A test that needs the numbers for the graph as it is NOW polls through this.
"""

from __future__ import annotations

import time
from typing import Any


def get_settled(client: Any, url: str, *, timeout: float = 60.0) -> Any:
    """GET ``url`` until a 200 with ``stale`` and ``rebuilding`` both false; return it."""
    deadline = time.monotonic() + timeout
    while True:
        response = client.get(url)
        if response.status_code == 200:
            body = response.json()
            if not body.get("stale") and not body.get("rebuilding"):
                return response
        assert time.monotonic() < deadline, f"{url} never settled: {response.status_code} {response.text[:200]}"
        time.sleep(0.05)
