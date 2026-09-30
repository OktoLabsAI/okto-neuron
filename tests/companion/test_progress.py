"""Within-file callback contracts for ``Companion.remember`` (Feature 2).

Pins the optional keyword-only ``on_progress`` sink: it is invoked with
monotonic stages and ``blocks_done <= blocks_total`` on every block-population
call (the dedup/curation stages tick an item ordinal with an undeclared total), and omitting
it leaves ``remember`` behaviour byte-for-byte unchanged. Also pins the optional
``on_event`` sink used by the web ingest inspector.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from okto_neuron import Vault
import okto_neuron.companion as companion_module
from okto_neuron.companion import (
    Companion,
    LLMUnavailableError,
    RememberCancelled,
    RememberResult,
    _StepLabelledProvider,
    _TracingLLMProvider,
)
from okto_neuron.config._vault import ResolvedLLM
from okto_neuron.llm import (
    LLMCallCancelled,
    LLMProviderError,
    LiteLLMProvider,
    Message,
    StubLLM,
    _current_call_cancel_predicate,
    _set_call_cancel_predicate,
)
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection

# Canonical stage order remember() advances through (terminal done/error are set
# by the queue worker, not remember itself).
_STAGE_ORDER = {
    "queued": 0,
    "parsing": 1,
    "extracting": 2,
    "embedding": 3,
    "dedup": 4,
    "committing": 5,
}
# Stages whose on_progress pairs are a real blocks_done/blocks_total population.
# "dedup"/"committing" tick a sub-stage item ordinal instead (undeclared total).
_BLOCK_POPULATION_STAGES = {"parsing", "extracting", "embedding"}


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


# Multi-paragraph note → multiple anchored Blocks → blocks_total > 1.
# Sized to split into TWO ~12k-char windows (=> blocks_total >= 2): a small
# leading section carrying the partner relationship, then a long filler line
# that forces a second window. Proves progress counts blocks across windows.
_MULTI_BLOCK = (
    "# Okto Neuron\n\n"
    "The project partner on Okto Neuron is Jordan Lee Carter.\n\n"
    "Okto Neuron is a local-first knowledge graph.\n\n"
    + ("It ingests markdown as its trust root. " * 350)
    + "\n"
)


def _bedrock_resolved() -> ResolvedLLM:
    return ResolvedLLM(
        provider="bedrock",
        api_base="http://127.0.0.1:8123/v1",
        model="anthropic.claude-3-5-sonnet-20241022-v2:0",
        api_key_env=None,
        max_tokens=1024,
        temperature=0.0,
        top_p=1.0,
        top_k=0,
        min_p=0.0,
        presence_penalty=0.0,
        enable_thinking=False,
    )


class _PartnerExtractor:
    """Deterministic extractor: emits one relationship for the partner block so
    the pipeline reaches the dedup/committing stages without a network."""

    def extract(self, text: str, *, provenance=None):  # type: ignore[no-untyped-def]
        from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
        from okto_neuron.core.schema import Provenance
        from okto_neuron.extract import ExtractionResult

        prov = provenance or Provenance()
        if "Jordan Lee Carter" not in text:
            return ExtractionResult(node_candidates=[], edge_candidates=[])
        subj = NodeCandidate(type="Concept", title="Okto Neuron", content=text, provenance=prov)
        obj = NodeCandidate(type="Agent", title="Jordan Lee Carter", content=text, provenance=prov)
        edge = EdgeCandidate(
            type="project_partner",
            src_ref=subj.candidate_id,
            dst_ref=obj.candidate_id,
            provenance=prov,
        )
        return ExtractionResult(node_candidates=[subj, obj], edge_candidates=[edge])


class _TraceableLLM:
    model = "fake-extraction"

    def complete(self, messages, *, temperature=0.0, max_tokens=1024, **kwargs):  # type: ignore[no-untyped-def]
        return (
            '{"nodes":['
            '{"type":"Agent","title":"Evan","content":"Evan discussed ROI."},'
            '{"type":"Concept","title":"ROI","content":"ROI is return on investment."}'
            '],"edges":[{"type":"discussed","src":"Evan","dst":"ROI"}]}'
        )


class _ConcurrentEmptyLLM:
    """Hold the first calls until at least nine overlap, then return empty JSON."""

    model = "concurrent-empty"
    api_base = "http://127.0.0.1:8123/v1"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._release = threading.Event()
        self.started = threading.Event()
        self._active = 0
        self.peak = 0

    def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        self.started.set()
        with self._lock:
            self._active += 1
            self.peak = max(self.peak, self._active)
            if self._active >= 9:
                self._release.set()
        try:
            if not self._release.wait(timeout=5):
                raise AssertionError("fewer than nine extraction calls overlapped")
            return '{"nodes":[],"edges":[]}'
        finally:
            with self._lock:
                self._active -= 1


def test_on_progress_invoked_with_monotonic_stages_and_bounded_blocks(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "note.md"
        note.write_text(_MULTI_BLOCK, encoding="utf-8")

        calls: list[tuple[str, int, int]] = []
        companion = Companion(vault, provider=StubLLM(), extractor=_PartnerExtractor())
        result = companion.remember(note, on_progress=lambda *a: calls.append(a))

        assert calls, "on_progress was never invoked"
        # Block-population calls have blocks_done within [0, blocks_total].
        for stage, done, total in calls:
            assert stage in _STAGE_ORDER, stage
            if stage in _BLOCK_POPULATION_STAGES:
                assert 0 <= done <= total, (stage, done, total)
            else:
                # Sub-stage keep-alive ticks: an item ordinal with an
                # explicitly undeclared total (ADR 0039 T9), never a block pair.
                assert done >= 0 and total in (0, done), (stage, done, total)
        # Stages advance monotonically (never regress).
        order = [_STAGE_ORDER[stage] for stage, _, _ in calls]
        assert order == sorted(order), order
        # The full pipeline ran: parsing through committing all appear.
        seen = {stage for stage, _, _ in calls}
        assert {"parsing", "extracting", "embedding", "dedup", "committing"} <= seen
        # blocks_total was discovered (multi-block note) and extracting reached it.
        total = max(t for s, _, t in calls if s in _BLOCK_POPULATION_STAGES)
        assert total >= 2
        assert ("extracting", total, total) in calls
        assert result.blocks_total == total
        assert result.nodes_extracted == 2
        assert result.edges_extracted == 1
        assert result.claims_minted == 1
    finally:
        vault.close()


def test_on_event_records_chunks_llm_dedup_gate_and_commit(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "note.md"
        note.write_text("# Standup\n\nEvan discussed ROI with the team.\n", encoding="utf-8")

        events: list[dict] = []
        result = Companion(vault, provider=_TraceableLLM()).remember(
            note,
            on_event=events.append,
        )

        kinds = [event["kind"] for event in events]
        assert "chunks" in kinds
        assert "llm_request" in kinds
        assert "llm_response" in kinds
        assert "extraction_result" in kinds
        assert "dedup_store_exact" in kinds
        assert "relation_curator_progress" in kinds
        assert "gate" in kinds
        assert "commit" in kinds

        chunks = next(event for event in events if event["kind"] == "chunks")
        assert chunks["payload"]["chunks"][0]["text"].startswith("# Standup")
        request = next(event for event in events if event["kind"] == "llm_request")
        assert request["payload"]["model"] == "fake-extraction"
        assert request["payload"]["messages"][0]["role"] == "system"
        assert request["payload"]["params"]["response_format"]["type"] == "json_schema"
        response = next(event for event in events if event["kind"] == "llm_response")
        assert "Evan" in response["payload"]["response"]
        commit = next(event for event in events if event["kind"] == "commit")
        assert commit["payload"]["nodes_extracted"] == result.nodes_extracted
        relation_progress = next(
            event for event in events if event["kind"] == "relation_curator_progress"
        )
        assert relation_progress["payload"]["reviewed"] == 1
        assert relation_progress["payload"]["total"] == 1
        assert relation_progress["payload"]["remaining"] == 0
        assert result.nodes_extracted == 2
        assert result.edges_extracted == 1
    finally:
        vault.close()


def test_extraction_fan_out_allows_more_than_eight_and_folds_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "parallel.md"
        note.write_text("# Parallel extraction\n", encoding="utf-8")
        (Path(vault.path) / "okto-neuron.yaml").write_text(
            "\n".join(
                [
                    "marginalia_yaml_version: 1",
                    "llm:",
                    # This fixture exercises fan-out MECHANICS, so it must
                    # declare the model under test and the backend capacity
                    # the clamp requires (the code default model is empty,
                    # so the fixture names its own model explicitly).
                    "  defaults:",
                    "    model: fan-out-test-model",
                    "  parallel_capable_models:",
                    "    - fan-out-test-model",
                    "  extraction:",
                    "    mode: baseline",
                    "    max_concurrent: 12",
                    "ingest:",
                    "  incremental: false",
                    "  subchunk: false",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            companion_module,
            "_extraction_units",
            lambda store, source, **_kwargs: [(None, f"chunk {index}") for index in range(12)],
        )

        provider = _ConcurrentEmptyLLM()
        callback_lock = threading.Lock()
        callback_active = 0
        callback_peak = 0
        events: list[dict] = []

        def on_event(event: dict) -> None:
            nonlocal callback_active, callback_peak
            with callback_lock:
                callback_active += 1
                callback_peak = max(callback_peak, callback_active)
            # Without Companion's event lock, concurrent tracing callbacks
            # overlap here and race the server's history persistence path.
            time.sleep(0.002)
            with callback_lock:
                events.append(event)
                callback_active -= 1

        result = Companion(vault, provider=provider).remember(
            note,
            on_event=on_event,
        )

        assert provider.peak > 8
        assert provider.peak <= 12
        assert callback_peak == 1
        assert result.blocks_total == 12
        schedule = next(event for event in events if event["kind"] == "extraction_schedule")
        assert schedule["payload"] == {
            "max_concurrent": 12,
            "effective_concurrent": 12,
            "scheduled_blocks": 12,
        }
        result_events = [event for event in events if event["kind"] == "extraction_result"]
        assert [event["payload"]["block"]["index"] for event in result_events] == list(range(12))
        request_contexts = {
            event["payload"]["block"]["index"] for event in events if event["kind"] == "llm_request"
        }
        assert request_contexts == set(range(12))
    finally:
        vault.close()


def test_active_extraction_adopts_higher_concurrency_without_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "hot-concurrency.md"
        note.write_text("# Hot concurrency\n", encoding="utf-8")
        config_path = Path(vault.path) / "okto-neuron.yaml"

        def write_config(limit: int) -> None:
            config_path.write_text(
                "\n".join(
                    [
                        "marginalia_yaml_version: 1",
                        "llm:",
                        # This fixture exercises fan-out MECHANICS, so it must
                        # declare the model under test and the backend
                        # capacity the clamp requires (the code default model
                        # is empty, so the fixture names its own model).
                        "  defaults:",
                        "    model: fan-out-test-model",
                        "  parallel_capable_models:",
                        "    - fan-out-test-model",
                        "  extraction:",
                        "    mode: baseline",
                        f"    max_concurrent: {limit}",
                        "ingest:",
                        "  incremental: false",
                        "  subchunk: false",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

        write_config(1)
        monkeypatch.setattr(
            companion_module,
            "_extraction_units",
            lambda store, source, **_kwargs: [(None, f"chunk {index}") for index in range(12)],
        )

        provider = _ConcurrentEmptyLLM()
        events: list[dict] = []

        def raise_limit_after_first_call_starts() -> None:
            if provider.started.wait(timeout=2):
                write_config(12)

        updater = threading.Thread(target=raise_limit_after_first_call_starts)
        updater.start()
        result = Companion(vault, provider=provider).remember(note, on_event=events.append)
        updater.join(timeout=2)

        assert not updater.is_alive()
        assert provider.started.is_set()
        assert provider.peak > 8
        assert result.blocks_total == 12
        changed = next(
            event for event in events if event["kind"] == "extraction_concurrency_changed"
        )
        assert changed["payload"] == {
            "previous_max_concurrent": 1,
            "max_concurrent": 12,
            "effective_concurrent": 12,
            "scheduled_blocks": 12,
        }
    finally:
        vault.close()


def test_restart_reuses_later_unit_that_finished_before_source_order_fold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later future is durable even when an earlier source unit is cancelled."""

    from okto_neuron.consolidate import NodeCandidate
    from okto_neuron.extract import ExtractionResult

    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "out-of-order.md"
        note.write_text("# Out of order\n", encoding="utf-8")
        (Path(vault.path) / "okto-neuron.yaml").write_text(
            "\n".join(
                [
                    "marginalia_yaml_version: 1",
                    "llm:",
                    # This fixture exercises fan-out MECHANICS, so it must
                    # declare the model under test and the backend capacity
                    # the clamp requires (the code default model is empty,
                    # so the fixture names its own model explicitly).
                    "  defaults:",
                    "    model: fan-out-test-model",
                    "  parallel_capable_models:",
                    "    - fan-out-test-model",
                    "  extraction:",
                    "    mode: baseline",
                    "    max_concurrent: 2",
                    "ingest:",
                    "  incremental: false",
                    "  subchunk: false",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            companion_module,
            "_extraction_units",
            lambda store, source, **_kwargs: [(None, "chunk 0"), (None, "chunk 1")],
        )

        cancelled = threading.Event()
        later_finished = threading.Event()

        class _OutOfOrderExtractor:
            def __init__(self) -> None:
                self.phase = 1
                self.calls: list[tuple[int, str]] = []

            def extract(self, text: str, *, provenance=None):  # type: ignore[no-untyped-def]
                self.calls.append((self.phase, text))
                if self.phase == 1 and text == "chunk 0":
                    assert later_finished.wait(timeout=2)
                    raise RememberCancelled()
                candidate = NodeCandidate(
                    type="Concept",
                    title=text.title(),
                    **({"provenance": provenance} if provenance is not None else {}),
                )
                if self.phase == 1:
                    later_finished.set()
                    cancelled.set()
                return ExtractionResult(node_candidates=[candidate])

        extractor = _OutOfOrderExtractor()
        companion = Companion(vault, provider=StubLLM(), extractor=extractor)
        with pytest.raises(RememberCancelled):
            companion.remember(note, should_cancel=cancelled.is_set)

        records = companion._candidate_ledger().records()
        succeeded = [
            row
            for row in records
            if row.get("kind") == "extraction_unit" and row.get("status") == "succeeded"
        ]
        assert len(succeeded) == 1

        extractor.phase = 2
        result = companion.remember(note)

        assert (2, "chunk 0") in extractor.calls
        assert (2, "chunk 1") not in extractor.calls
        assert result.outcome["units"]["reused"] == 1
        assert result.outcome["quality"] == "complete"
    finally:
        vault.close()


def test_source_span_is_revalidated_before_provider_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron.errors import IngestError
    from okto_neuron.extract import ExtractionResult

    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "changing.md"
        note.write_text("# Before\n\nOriginal bytes.\n", encoding="utf-8")
        (Path(vault.path) / "okto-neuron.yaml").write_text(
            "marginalia_yaml_version: 1\ningest:\n  incremental: false\n  subchunk: false\n",
            encoding="utf-8",
        )
        original_units = companion_module._extraction_units

        def mutate_after_anchoring(store, source, **kwargs):  # type: ignore[no-untyped-def]
            units = original_units(store, source, **kwargs)
            note.write_text("# After\n\nDifferent bytes.\n", encoding="utf-8")
            return units

        monkeypatch.setattr(companion_module, "_extraction_units", mutate_after_anchoring)

        class _NeverExtract:
            calls = 0

            def extract(self, text: str, *, provenance=None):  # type: ignore[no-untyped-def]
                self.calls += 1
                return ExtractionResult()

        extractor = _NeverExtract()
        companion = Companion(vault, provider=StubLLM(), extractor=extractor)
        with pytest.raises(IngestError, match="source changed during extraction"):
            companion.remember(note)

        assert extractor.calls == 0
        source_changed = [
            row
            for row in companion._candidate_ledger().records()
            if row.get("kind") == "extraction_unit" and row.get("status") == "source_changed"
        ]
        assert len(source_changed) == 1
    finally:
        vault.close()


def test_unit_replay_requires_exact_extraction_fingerprint(
    tmp_path: Path,
) -> None:
    from okto_neuron.consolidate import NodeCandidate
    from okto_neuron.extract import ExtractionResult

    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "fingerprint.md"
        note.write_text("# Fingerprint\n\nStable source.\n", encoding="utf-8")
        config_path = Path(vault.path) / "okto-neuron.yaml"

        def write_config(system_prompt: str | None = None) -> None:
            rows = [
                "marginalia_yaml_version: 1",
                "llm:",
                "  extraction:",
                "    mode: baseline",
            ]
            if system_prompt is not None:
                rows.append(f"    system_prompt: {system_prompt!r}")
            rows.extend(
                [
                    "ingest:",
                    "  incremental: true",
                    "  subchunk: false",
                    "",
                ]
            )
            config_path.write_text("\n".join(rows), encoding="utf-8")

        class _CountingExtractor:
            def __init__(self) -> None:
                self.calls = 0

            def extract(self, text: str, *, provenance=None):  # type: ignore[no-untyped-def]
                self.calls += 1
                candidate = NodeCandidate(
                    type="Concept",
                    title="Fingerprint",
                    embedding=[0.1] * 384,
                    **({"provenance": provenance} if provenance is not None else {}),
                )
                return ExtractionResult(node_candidates=[candidate])

        write_config()
        first = _CountingExtractor()
        Companion(vault, provider=StubLLM(), extractor=first).remember(note)
        assert first.calls == 1
        successful_unit = next(
            record
            for record in Companion(vault, provider=StubLLM())._candidate_ledger().records()
            if record.get("kind") == "extraction_unit" and record.get("status") == "succeeded"
        )
        assert "embedding" not in successful_unit["result"]["nodes"][0]
        assert "embedding_dim" not in successful_unit["result"]["nodes"][0]

        same_policy = _CountingExtractor()
        same_result = Companion(
            vault,
            provider=StubLLM(),
            extractor=same_policy,
        ).remember(note)
        assert same_policy.calls == 0
        assert same_result.outcome["units"]["reused"] == 1

        # A reader/schema drift must invalidate only the unusable cache row and
        # re-extract the unit. It must not wedge every future ingest on the old
        # payload or create an unresolved conflicting-success record.
        ledger = Companion(vault, provider=StubLLM())._candidate_ledger()
        records = ledger.records()
        for record in records:
            if record.get("kind") == "extraction_unit" and record.get("status") == "succeeded":
                record["result"] = {
                    "nodes": [
                        {
                            "type": "Concept",
                            "title": "Old schema",
                            "removed_contract_field": True,
                        }
                    ],
                    "edges": [],
                }
        ledger.path.write_text(
            "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
            encoding="utf-8",
        )

        repaired_policy = _CountingExtractor()
        repaired_result = Companion(
            vault,
            provider=StubLLM(),
            extractor=repaired_policy,
        ).remember(note)
        assert repaired_policy.calls == 1
        assert repaired_result.outcome["units"]["reused"] == 0
        assert any(
            record.get("kind") == "extraction_unit"
            and record.get("reason") == "stored_replay_incompatible"
            for record in ledger.records()
        )

        post_repair_policy = _CountingExtractor()
        post_repair_result = Companion(
            vault,
            provider=StubLLM(),
            extractor=post_repair_policy,
        ).remember(note)
        assert post_repair_policy.calls == 0
        assert post_repair_result.outcome["units"]["reused"] == 1

        write_config("changed extraction prompt")
        changed_policy = _CountingExtractor()
        changed_result = Companion(
            vault,
            provider=StubLLM(),
            extractor=changed_policy,
        ).remember(note)
        assert changed_policy.calls == 1
        assert changed_result.outcome["units"]["reused"] == 0
    finally:
        vault.close()


def test_transient_extraction_failure_retries_once_then_journals_success(
    tmp_path: Path,
) -> None:
    from okto_neuron.consolidate import NodeCandidate
    from okto_neuron.extract import ExtractionResult

    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "retry.md"
        note.write_text("# Retry\n\nTransient failure.\n", encoding="utf-8")

        class _RetryOnceExtractor:
            calls = 0

            def extract(self, text: str, *, provenance=None):  # type: ignore[no-untyped-def]
                self.calls += 1
                if self.calls == 1:
                    raise LLMProviderError("temporary timeout", category="timeout")
                return ExtractionResult(
                    node_candidates=[
                        NodeCandidate(
                            type="Concept",
                            title="Recovered",
                            **({"provenance": provenance} if provenance is not None else {}),
                        )
                    ]
                )

        extractor = _RetryOnceExtractor()
        result = Companion(vault, provider=StubLLM(), extractor=extractor).remember(note)

        assert extractor.calls == 2
        assert result.outcome["quality"] == "complete"
        attempts = [
            record
            for record in Companion(vault, provider=StubLLM())._candidate_ledger().records()
            if record.get("kind") == "extraction_unit"
        ]
        assert [(row["attempt"], row["status"]) for row in attempts] == [
            (1, "provider_failed"),
            (2, "succeeded"),
        ]
        assert attempts[0]["error_class"] == "timeout"
        assert attempts[0]["retry_disposition"] == "retried"
    finally:
        vault.close()


@pytest.mark.parametrize(
    ("category", "expected_calls", "expected_disposition"),
    [
        ("authentication", 1, "not_retryable"),
        ("unknown", 1, "not_retryable"),
        ("timeout", 2, "exhausted"),
    ],
)
def test_extraction_retry_policy_is_closed_and_bounded(
    tmp_path: Path,
    category: str,
    expected_calls: int,
    expected_disposition: str,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / f"{category}.md"
        note.write_text("# Failure\n\nProvider failure.\n", encoding="utf-8")

        class _AlwaysFails:
            calls = 0

            def extract(self, text: str, *, provenance=None):  # type: ignore[no-untyped-def]
                self.calls += 1
                raise LLMProviderError("normalized provider failure", category=category)

        extractor = _AlwaysFails()
        companion = Companion(vault, provider=StubLLM(), extractor=extractor)
        with pytest.raises(LLMUnavailableError):
            companion.remember(note)

        assert extractor.calls == expected_calls
        attempts = [
            record
            for record in companion._candidate_ledger().records()
            if record.get("kind") == "extraction_unit" and record.get("status") == "provider_failed"
        ]
        assert len(attempts) == expected_calls
        assert attempts[-1]["error_class"] == category
        assert attempts[-1]["retry_disposition"] == expected_disposition
    finally:
        vault.close()


def test_on_event_records_bedrock_missing_dependency_provider_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_find_spec = importlib.util.find_spec

    def _missing_boto3(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "boto3":
            return None
        return original_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", _missing_boto3)

    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "note.md"
        note.write_text(
            "# Bedrock\n\nDirect Bedrock should explain setup failures.\n", encoding="utf-8"
        )

        events: list[dict] = []
        # Fix 1 (issue #4 — loud total failure): this note is a single Block,
        # so the one attempted extraction failing with a provider error means
        # EVERY attempted block failed. remember() now raises
        # LLMUnavailableError instead of returning a success-shaped
        # RememberResult with provider_error quietly set — that silent
        # "success" was exactly the has_heading-only bug (only the
        # LLM-free structural claim survives, with no visible error). The
        # events sink (mutated by side effect during the run, before the
        # raise) still captures the extraction_provider_error event.
        with pytest.raises(LLMUnavailableError) as exc_info:
            Companion(
                vault,
                provider=LiteLLMProvider(_bedrock_resolved()),
            ).remember(note, on_event=events.append)

        provider_errors = [
            event for event in events if event["kind"] == "extraction_provider_error"
        ]
        assert len(provider_errors) == 1
        error = provider_errors[0]["payload"]["error"]
        assert "bedrock" in error.lower()
        assert "boto3" in error
        assert "okto-neuron[litellm,bedrock]" in error
        assert error in str(exc_info.value)
    finally:
        vault.close()


def test_remember_without_callback_unchanged(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "note.md"
        note.write_text(_MULTI_BLOCK, encoding="utf-8")
        result = Companion(vault, provider=StubLLM(), extractor=_PartnerExtractor()).remember(note)
        assert isinstance(result, RememberResult)
        assert result.document_id
    finally:
        vault.close()


class _ConstantEmbedder:
    """Returns an identical vector for every text, so any two same-type
    candidates land in the merge-judge's embedding band — used to force real
    (not synthetic) judge_within_batch pair-judging through a full
    ``Companion.remember()`` run."""

    dim = 384

    def embed(self, text: str) -> list[float]:  # noqa: ARG002 — constant on purpose
        return [1.0, *([0.0] * 383)]


class _BatchTrackingEmbedder:
    dim = 384

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, text: str) -> list[float]:  # noqa: ARG002
        return [1.0, *([0.0] * 383)]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.embed(text) for text in texts]


def test_remember_batches_candidate_and_relationship_claim_embeddings(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "note.md"
        note.write_text(_MULTI_BLOCK, encoding="utf-8")
        embedder = _BatchTrackingEmbedder()

        result = Companion(
            vault,
            provider=StubLLM(),
            extractor=_PartnerExtractor(),
            embedder=embedder,
        ).remember(note)

        assert result.nodes_extracted == 2
        assert result.claims_minted == 1
        assert [len(call) for call in embedder.calls] == [2, 1]
    finally:
        vault.close()


class _MultiConceptExtractor:
    """Emits several distinct same-type Concept candidates from ONE block, so
    the within-batch dedup pass judges multiple real pairs (paired with
    ``_ConstantEmbedder``, which puts every pair in the merge-judge's band)."""

    def __init__(self, count: int = 4) -> None:
        self._count = count

    def extract(self, text, *, provenance=None):  # type: ignore[no-untyped-def]
        from okto_neuron.consolidate import NodeCandidate
        from okto_neuron.core.schema import Provenance
        from okto_neuron.extract import ExtractionResult

        prov = provenance or Provenance()
        cands = [
            NodeCandidate(type="Concept", title=f"concept-{i}", content=text, provenance=prov)
            for i in range(self._count)
        ]
        return ExtractionResult(node_candidates=cands, edge_candidates=[])


class _CountingStubLLM(StubLLM):
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, *, temperature=0.0, max_tokens=1024, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().complete(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )


def test_tracing_provider_translates_cli_cancel_and_restores_thread_local() -> None:
    def should_cancel() -> bool:
        return False

    def outer_predicate() -> bool:
        return False

    class _CancelledProvider:
        model = "cancelled"

        def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            assert _current_call_cancel_predicate() is should_cancel
            raise LLMCallCancelled()

    previous = _set_call_cancel_predicate(outer_predicate)
    try:
        provider = _TracingLLMProvider(
            _CancelledProvider(),
            lambda kind, summary, payload: None,
            lambda: {},
            should_cancel=should_cancel,
        )
        with pytest.raises(RememberCancelled):
            provider.complete([Message("user", "stop")])
        assert _current_call_cancel_predicate() is outer_predicate
    finally:
        _set_call_cancel_predicate(previous)


# ── llm_request trace reports the EFFECTIVE request ─────────────────────────
# The inspector used to build its "params" from _TracingLLMProvider's OWN
# method arguments, which are the PRE-merge values. A role configured with a
# raw ``sampling_payload`` has those arguments discarded inside
# LiteLLMProvider.complete(), so the trace showed parameters that were never
# sent — LLMExtractor's class defaults (temperature=0.0, max_tokens=16000)
# instead of the operator's configured payload. "Show what is actually sent"
# is the whole point of the raw-payload feature's trace, and a misreporting
# observability surface is worse than none: it cost real debugging time and
# made a live run unverifiable.

_TRACE_CONTROL_KEYS = frozenset(
    {"api_base", "api_key", "drop_params", "extra_body", "max_retries", "messages", "model", "timeout"}
)


def _openai_resolved(**overrides) -> ResolvedLLM:
    """A loopback OpenAI-compatible role, the shape a self-hosted vault uses."""

    base = dict(
        provider="openai",
        api_base="http://127.0.0.1:8123/v1",
        model="Qwen3.8-27B",
        api_key_env=None,
        max_tokens=None,
        temperature=None,
        top_p=None,
        top_k=None,
        min_p=None,
        presence_penalty=None,
        enable_thinking=None,
    )
    base.update(overrides)
    return ResolvedLLM(**base)


def _capture_llm_requests(
    provider: object,
    events: list[dict],
    **complete_kwargs,
) -> str:
    """Run one traced completion, recording every emitted event."""

    traced = _TracingLLMProvider(
        provider,  # type: ignore[arg-type]
        lambda kind, summary, payload: events.append(
            {"kind": kind, "summary": summary, "payload": payload}
        ),
        lambda: {"index": 0},
    )
    return traced.complete([Message("user", "hello")], **complete_kwargs)


def _mock_litellm(monkeypatch: pytest.MonkeyPatch, calls: list[dict], **extra) -> None:
    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion, **extra))


def test_llm_request_trace_reports_sampling_payload_values_not_caller_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact reported defect: a role with a raw ``sampling_payload`` had
    its trace report LLMExtractor's class defaults (0 / 16000), which the
    provider then discarded. The event must report the payload's values, and
    must equal what litellm was actually called with."""

    calls: list[dict] = []
    _mock_litellm(monkeypatch, calls)
    payload = {
        "temperature": 0.2,
        "top_p": 0.8,
        "top_k": 20,
        "max_tokens": 32768,
        "chat_template_kwargs": {
            "enable_thinking": False,
            "preserve_thinking": False,
            "reasoning_effort": "none",
        },
    }
    events: list[dict] = []

    _capture_llm_requests(
        LiteLLMProvider(_openai_resolved(sampling_payload=payload)),
        events,
        # LLMExtractor's hardcoded class defaults, which the payload overrides.
        temperature=0.0,
        max_tokens=16000,
    )

    requests = [event for event in events if event["kind"] == "llm_request"]
    assert len(requests) == 1
    traced = requests[0]["payload"]
    assert traced["params"]["temperature"] == 0.2
    assert traced["params"]["max_tokens"] == 32768
    assert traced["params"]["top_p"] == 0.8
    assert traced["params_source"] == "effective"
    assert traced["sampling_payload_applied"] is True
    assert traced["extra_body"] == {
        "top_k": 20,
        "chat_template_kwargs": {
            "enable_thinking": False,
            "preserve_thinking": False,
            "reasoning_effort": "none",
        },
    }
    # The pre-merge arguments stay visible, so an operator can SEE that their
    # payload won rather than having to trust a merged blob.
    assert traced["requested_params"]["temperature"] == 0.0
    assert traced["requested_params"]["max_tokens"] == 16000
    # The load-bearing assertion: traced params ARE the wire params.
    assert traced["params"] == {
        name: value for name, value in calls[0].items() if name not in _TRACE_CONTROL_KEYS
    }
    assert traced["extra_body"] == calls[0]["extra_body"]


def test_llm_request_trace_reports_caller_values_when_no_sampling_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged behaviour for a role with no raw payload: the caller's own
    per-call sampler arguments are what gets sent, so they are what gets
    traced."""

    calls: list[dict] = []
    _mock_litellm(
        monkeypatch,
        calls,
        get_supported_openai_params=lambda **kwargs: ["max_tokens", "temperature", "top_p"],
    )
    events: list[dict] = []

    _capture_llm_requests(
        LiteLLMProvider(_openai_resolved(sampling_payload={})),
        events,
        temperature=0.31,
        max_tokens=4321,
    )

    traced = next(event for event in events if event["kind"] == "llm_request")["payload"]
    assert traced["params_source"] == "effective"
    assert traced["sampling_payload_applied"] is False
    assert traced["params"]["temperature"] == 0.31
    assert traced["params"]["max_tokens"] == 4321
    assert traced["requested_params"]["temperature"] == 0.31
    assert traced["params"] == {
        name: value for name, value in calls[0].items() if name not in _TRACE_CONTROL_KEYS
    }


def test_llm_request_trace_falls_back_to_caller_args_for_a_non_reporting_provider() -> None:
    """A provider that cannot report its assembled request (the CLI providers,
    StubLLM, test doubles) is traced exactly as before — and the event says
    which of the two it is rather than leaving the reader to guess."""

    class _Plain:
        model = "plain"

        def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            return "ok"

    events: list[dict] = []
    _capture_llm_requests(_Plain(), events, temperature=0.7, max_tokens=99)

    traced = next(event for event in events if event["kind"] == "llm_request")["payload"]
    assert traced["params_source"] == "requested"
    assert traced["params"]["temperature"] == 0.7
    assert traced["params"]["max_tokens"] == 99
    # Nothing is claimed about the wire that this wrapper cannot know.
    assert "extra_body" not in traced
    assert "omitted_params" not in traced
    assert "sampling_payload_applied" not in traced


def test_llm_request_trace_survives_the_step_label_wrapper_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production never wraps a provider directly: Companion._get_provider
    returns _StepLabelledProvider(LiteLLMProvider), and the tracing wrapper
    goes around THAT. If the middle wrapper stops forwarding the provider's
    ability to report, the trace silently degrades back to caller arguments in
    the real pipeline while a direct-wrap test keeps passing."""

    calls: list[dict] = []
    _mock_litellm(monkeypatch, calls)
    events: list[dict] = []

    _capture_llm_requests(
        _StepLabelledProvider(
            LiteLLMProvider(_openai_resolved(sampling_payload={"temperature": 0.2})),
            "extraction",
        ),
        events,
        temperature=0.0,
    )

    traced = next(event for event in events if event["kind"] == "llm_request")["payload"]
    assert traced["params_source"] == "effective"
    assert traced["params"]["temperature"] == 0.2


def test_llm_request_trace_never_leaks_the_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The assembled request carries the environment credential (and, for a
    keyless self-hosted endpoint, an injected placeholder) alongside the
    sampler params. The traced view is filtered by the same control-key set
    the parameter accounting uses, so no credential and no endpoint URL can
    reach the ingest history sidecar or the UI."""

    calls: list[dict] = []
    _mock_litellm(monkeypatch, calls)
    monkeypatch.setenv("OKTO_NEURON_TRACE_SECRET", "sk-secret-sentinel")
    events: list[dict] = []

    _capture_llm_requests(
        LiteLLMProvider(
            _openai_resolved(
                api_key_env="OKTO_NEURON_TRACE_SECRET",
                sampling_payload={"temperature": 0.2},
            )
        ),
        events,
    )

    # The real call still gets the credential — this is a trace scrub, not a
    # functional regression.
    assert calls[0]["api_key"] == "sk-secret-sentinel"
    traced = next(event for event in events if event["kind"] == "llm_request")["payload"]
    assert "api_key" not in traced["params"]
    assert "api_base" not in traced["params"]
    serialized = json.dumps(traced)
    assert "sk-secret-sentinel" not in serialized
    assert "api_key" not in serialized


def test_exactly_one_llm_request_event_per_complete_on_every_exit_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INVARIANT: one ``llm_request`` per ``complete()``, on success, on
    provider error, and on cancellation. Readers use
    ``next(e for e in events if e["kind"] == "llm_request")``, so zero events
    is a StopIteration and two silently doubles a trace."""

    def _requests(events: list[dict]) -> list[dict]:
        return [event for event in events if event["kind"] == "llm_request"]

    # 1. success, provider reports its effective request
    calls: list[dict] = []
    _mock_litellm(monkeypatch, calls)
    ok_events: list[dict] = []
    _capture_llm_requests(
        LiteLLMProvider(_openai_resolved(sampling_payload={"temperature": 0.2})), ok_events
    )
    assert len(_requests(ok_events)) == 1

    # 2. the backend rejects the request AFTER it was assembled and observed
    def raising_completion(**kwargs):
        raise RuntimeError("400 Bad Request: unknown parameter 'typical_p'")

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=raising_completion))
    error_events: list[dict] = []
    with pytest.raises(LLMProviderError):
        _capture_llm_requests(
            LiteLLMProvider(_openai_resolved(sampling_payload={"typical_p": 0.9})), error_events
        )
    assert len(_requests(error_events)) == 1
    assert [event["kind"] for event in error_events] == ["llm_request", "llm_error"]

    # 3. a reporting provider that fails BEFORE it assembles anything — the
    #    wrapper's fallback still owes exactly one request event.
    class _CancelsBeforeBuilding:
        model = "cancelled"
        traces_effective_request = True

        def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            raise LLMCallCancelled()

    cancel_events: list[dict] = []
    with pytest.raises(RememberCancelled):
        _capture_llm_requests(_CancelsBeforeBuilding(), cancel_events)
    assert len(_requests(cancel_events)) == 1
    assert _requests(cancel_events)[0]["payload"]["params_source"] == "requested"


def test_should_cancel_stops_after_current_dedup_request(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "note.md"
        note.write_text("# Concepts\n\nSeveral related concepts.\n", encoding="utf-8")
        provider = _CountingStubLLM()

        with pytest.raises(RememberCancelled):
            Companion(
                vault,
                provider=provider,
                extractor=_MultiConceptExtractor(count=4),
                embedder=_ConstantEmbedder(),
            ).remember(note, should_cancel=lambda: provider.calls >= 1)

        # Four similar concepts normally trigger six merge-judge requests. A
        # stop raised after the first response prevents every subsequent call.
        assert provider.calls == 1
    finally:
        vault.close()


def test_stage_events_and_log_line_on_every_transition(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Fix 3 — the live 7-hour ingest had no way to tell, from ingest-history
    events[] or the serve log, when a stage actually CHANGED: items sat at
    'dedup 100%' with frozen counts for the whole judge tail. Every stage
    boundary must now be exactly one 'stage' on_event PLUS one
    'remember stage=...' log line — never one per per-block progress tick."""
    caplog.set_level("INFO", logger="okto_neuron.companion")
    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "note.md"
        note.write_text(_MULTI_BLOCK, encoding="utf-8")

        events: list[dict] = []
        Companion(vault, provider=StubLLM(), extractor=_PartnerExtractor()).remember(
            note, on_event=events.append
        )

        stage_events = [e for e in events if e["kind"] == "stage"]
        stages_seen = [e["payload"]["stage"] for e in stage_events]
        # Each stage fires exactly once — never once per per-block tick.
        assert stages_seen == list(dict.fromkeys(stages_seen))
        assert {"parsing", "extracting", "embedding", "dedup", "committing"} <= set(stages_seen)
        for event in stage_events:
            assert set(event["payload"]) == {
                "stage",
                "blocks_done",
                "blocks_total",
                "elapsed_s",
            }
            assert event["summary"] == f"Stage → {event['payload']['stage']}"

        log_lines = [
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("remember stage=")
        ]
        assert len(log_lines) == len(stage_events)
        for stage in stages_seen:
            assert any(f"remember stage={stage} " in line for line in log_lines)
    finally:
        vault.close()


def test_dedup_progress_cadence_and_merge_judge_tracing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix 2 — the candidate ledger went silent for 30+ minutes during dedup
    while LLM calls were still firing, and the merge judge was never wrapped
    for tracing (every other role — extraction/curator/relation_curator —
    already was). Pins: dedup_progress fires at DEDUP_PROGRESS_EVENT_EVERY,
    and Merge Judge llm_request/llm_response events appear in the item
    inspector once on_event is set."""
    import okto_neuron.companion as companion_module

    monkeypatch.setattr(companion_module, "DEDUP_PROGRESS_EVENT_EVERY", 2)

    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "note.md"
        note.write_text("# Concepts\n\nSeveral related concepts.\n", encoding="utf-8")

        events: list[dict] = []
        Companion(
            vault,
            provider=StubLLM(),
            extractor=_MultiConceptExtractor(count=4),
            embedder=_ConstantEmbedder(),
        ).remember(note, on_event=events.append)

        # 4 same-type candidates, all in the constant embedder's band, none
        # ever merge (StubLLM's merge verdict is always "distinct") -> 1 + 2 +
        # 3 = 6 within-batch pairs judged (JUDGE_K=3 caps each comparison at
        # the top-3 survivors). At cadence 2 that's progress events at 2/4/6.
        dedup_progress = [e for e in events if e["kind"] == "dedup_progress"]
        assert [e["payload"]["pairs_judged"] for e in dedup_progress] == [2, 4, 6]

        merge_requests = [
            e
            for e in events
            if e["kind"] == "llm_request" and e["summary"].startswith("Merge Judge")
        ]
        merge_responses = [
            e
            for e in events
            if e["kind"] == "llm_response" and e["summary"].startswith("Merge Judge")
        ]
        assert len(merge_requests) == 6
        assert len(merge_responses) == 6
    finally:
        vault.close()


def _substage_calls(calls: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    """The dedup/curation keep-alive ticks (every stage outside the block
    population reports an item ordinal with an undeclared total)."""
    return [call for call in calls if call[0] not in _BLOCK_POPULATION_STAGES and call[2] == 0]


def _bridge_notifications(calls: list[tuple[str, int, int]]) -> list[tuple[str, int]]:
    """Replay ``_mcp_progress_bridge``'s ``(stage, done)`` coalescing rule.

    The bridge drops a call whose ``(stage, blocks_done)`` equals the PREVIOUS
    one, so a keep-alive whose ``done`` never advances collapses into a single
    notification and the client's idle timer still fires. This is the failure
    mode the sub-stage counters must not have.
    """
    sent: list[tuple[str, int]] = []
    for stage, done, _total in calls:
        key = (stage, done)
        if sent and sent[-1] == key:
            continue
        sent.append(key)
    return sent


def _remember_with_progress(
    vault: Vault,
    note: Path,
    calls: list[tuple[str, int, int]],
    *,
    on_progress: Any = None,
) -> RememberResult:
    return Companion(
        vault,
        provider=StubLLM(),
        extractor=_MultiConceptExtractor(count=12),
        embedder=_ConstantEmbedder(),
    ).remember(note, on_progress=on_progress or (lambda *a: calls.append(a)))


def test_dedup_and_curation_emit_advancing_throttled_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dedup/curation phases dominate a real ingest's runtime and used to
    emit exactly ONE on_progress call each, so a client aborted mid-phase on
    its 300s idle timer. They now tick, throttled, with ADVANCING ordinals."""
    monkeypatch.setattr(companion_module, "SUBSTAGE_PROGRESS_EVERY", 5)
    # Clock path disabled: this test pins the count-based floor only.
    monkeypatch.setattr(companion_module, "SUBSTAGE_PROGRESS_INTERVAL_S", 10_000.0)

    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "note.md"
        note.write_text("# Concepts\n\nSeveral related concepts.\n", encoding="utf-8")
        calls: list[tuple[str, int, int]] = []
        _remember_with_progress(vault, note, calls)

        ticks = _substage_calls(calls)
        by_stage: dict[str, list[int]] = {}
        for stage, done, _total in ticks:
            by_stage.setdefault(stage, []).append(done)

        # Both long phases report, not just one.
        assert set(by_stage) == {"dedup", "committing"}
        for stage, dones in by_stage.items():
            # More than one event per phase (the old behaviour was exactly one)…
            assert len(dones) > 1, (stage, dones)
            # …strictly advancing, so the bridge cannot coalesce them away…
            assert dones == sorted(set(dones)), (stage, dones)
            # …and spaced by the configured floor, never one per item.
            assert dones[0] == 1
            assert all(b - a >= 5 for a, b in zip(dones, dones[1:], strict=False)), dones

        # Ticks also cover the SERIAL pre-passes (per-candidate resolve() and
        # prompt building) that run before the first curation verdict — with
        # 12 candidates a drain-only instrumentation could not exceed 12+12.
        assert max(by_stage["committing"]) > 24

        # 12 same-type candidates: far more items than emissions.
        assert max(by_stage["dedup"]) >= 10
        assert len(by_stage["dedup"]) < max(by_stage["dedup"])

        # Coalescing check: replaying the MCP bridge's (stage, done) dedupe
        # keeps every sub-stage notification.
        sent = _bridge_notifications(calls)
        assert sum(1 for stage, _ in sent if stage == "dedup") == len(by_stage["dedup"])
        assert len([1 for stage, _ in sent if stage == "committing"]) >= len(
            by_stage["committing"]
        )
    finally:
        vault.close()


def test_substage_progress_emits_on_the_clock_when_items_are_slow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count floor alone cannot bound the silence — curation runs one
    unbounded serial LLM call per candidate by default. With the interval
    elapsed, EVERY item ticks, so the worst-case gap is one item's latency
    plus the interval rather than N items' latency."""
    monkeypatch.setattr(companion_module, "SUBSTAGE_PROGRESS_EVERY", 10**6)
    monkeypatch.setattr(companion_module, "SUBSTAGE_PROGRESS_INTERVAL_S", 0.0)

    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "note.md"
        note.write_text("# Concepts\n\nSeveral related concepts.\n", encoding="utf-8")
        calls: list[tuple[str, int, int]] = []
        _remember_with_progress(vault, note, calls)

        by_stage: dict[str, list[int]] = {}
        for stage, done, _total in _substage_calls(calls):
            by_stage.setdefault(stage, []).append(done)
        # Every single item ticked: the ordinals are 1..n with no gaps.
        for stage, dones in by_stage.items():
            assert dones == list(range(1, len(dones) + 1)), (stage, dones)
        assert len(by_stage["dedup"]) >= 10
    finally:
        vault.close()


def test_substage_progress_leaves_the_payload_identical(tmp_path: Path) -> None:
    """Keeping the call alive must not change what the call returns."""
    fields = (
        "committed",
        "queued",
        "blocks_total",
        "nodes_extracted",
        "edges_extracted",
        "claims_minted",
    )

    def _run(name: str, capture: list[tuple[str, int, int]] | None) -> dict[str, object]:
        vault = Vault.init(tmp_path / name)
        try:
            note = Path(vault.path) / "note.md"
            note.write_text("# Concepts\n\nSeveral related concepts.\n", encoding="utf-8")
            calls: list[tuple[str, int, int]] = []
            result = Companion(
                vault,
                provider=StubLLM(),
                extractor=_MultiConceptExtractor(count=12),
                embedder=_ConstantEmbedder(),
            ).remember(note, on_progress=(lambda *a: calls.append(a)) if capture is not None else None)
            if capture is not None:
                capture.extend(calls)
            return {field: getattr(result, field) for field in fields}
        finally:
            vault.close()

    captured: list[tuple[str, int, int]] = []
    with_progress = _run("with", captured)
    without_progress = _run("without", None)
    assert with_progress == without_progress
    assert _substage_calls(captured), "no sub-stage progress was emitted"


def test_failing_progress_callback_does_not_fail_the_ingest(tmp_path: Path) -> None:
    """Telemetry is not the product: a raising sink during the dedup/curation
    keep-alive must never abort a run that is doing real work."""
    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "note.md"
        note.write_text("# Concepts\n\nSeveral related concepts.\n", encoding="utf-8")
        seen: list[str] = []

        def _boom(stage: str, done: int, total: int) -> None:
            if stage not in _BLOCK_POPULATION_STAGES and total == 0:
                seen.append(stage)
                raise RuntimeError("progress sink exploded")

        result = Companion(
            vault,
            provider=StubLLM(),
            extractor=_MultiConceptExtractor(count=12),
            embedder=_ConstantEmbedder(),
        ).remember(note, on_progress=_boom)

        assert isinstance(result, RememberResult)
        assert result.document_id
        assert seen, "the sub-stage keep-alive never fired"
    finally:
        vault.close()
