"""ADR 0039/0040 layered semantic fingerprint contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron.companion import Companion
from okto_neuron.config import VaultConfig
from okto_neuron.consolidate import NodeCandidate
from okto_neuron.consolidate.ledger import CandidateLedger
from okto_neuron.consolidate.review_queue import ReviewQueue
from okto_neuron.predicates import (
    PredicateDecisionProvenance,
    PredicateEvidenceSample,
    PredicateRecord,
    PredicateRegistry,
    PredicateTypeSignature,
)
from okto_neuron.semantic_fingerprint import (
    CONTRACT_VERSIONS,
    _effective_semantic_fingerprint_triplet,
    build_semantic_fingerprint_payloads,
    invalidate_semantic_materialization,
    load_semantic_materialization,
    materialized_semantic_fingerprints,
    publish_semantic_materialization,
    semantic_materialization_path,
    semantic_fingerprints,
)


def _config_with(mutator) -> VaultConfig:  # type: ignore[no-untyped-def]
    payload = VaultConfig().model_dump(mode="python")
    mutator(payload)
    return VaultConfig.model_validate(payload)


def _predicate_record(label: str = "lives_in") -> PredicateRecord:
    return PredicateRecord(
        label=label,
        lifecycle="canonical",
        definition="The subject lives in the object place.",
        direction="subject_to_object",
        symmetric=False,
        signatures=(PredicateTypeSignature("Agent", "Place", 1),),
        support_count=1,
        samples=(PredicateEvidenceSample("claim-1", "source-1"),),
        confidence=0.9,
        provenance=PredicateDecisionProvenance(
            source="human",
            decision_id="decision-1",
            judge_model="",
            prompt_version="",
            semantic_policy_fingerprint="policy-v1",
            created_at="2026-07-16T12:00:00Z",
        ),
    )


def test_execution_policy_and_secrets_do_not_change_fingerprints(tmp_path: Path) -> None:
    baseline = semantic_fingerprints(VaultConfig(), tmp_path)

    def mutate(payload: dict) -> None:
        payload["embedding"].update(
            api_key_env="OKTO_NEURON_EMBED_KEY",
            allow_remote=True,
            batch_size=127,
            max_concurrent_batches=31,
        )
        payload["llm"].update(allow_remote=True)
        payload["llm"]["defaults"].update(
            api_base=(
                "http://127.0.0.1:8123/v1?key=do-not-hash&sig=also-secret"
                "&X-Amz-Signature=aws-secret&code=function-secret"
            ),
            api_key_env="OKTO_NEURON_LLM_KEY",
            # The old free-form `parameters` map this used to also exercise
            # (arbitrary execution/secret-shaped keys like
            # `request_timeout_s`/`max_retries`/`metadata.authorization_token`
            # getting scrubbed by `_semantic_parameters`) was removed
            # outright (decision A, 2026-09-15, no migration): `LLMDefaults`/
            # `StepLLM` no longer have a `parameters` field at all, so a
            # vault config can no longer place arbitrary content there —
            # `ResolvedLLM.parameters` is now populated only from the three
            # known-safe typed fields (repeat_penalty/reasoning_effort/
            # preserve_thinking), none of which are secret- or
            # execution-policy-shaped.
        )
        payload["llm"]["extraction"]["max_concurrent"] = 29
        payload["consolidation"].update(
            curation_max_concurrent=23,
            curation_call_timeout_s=777,
        )

    changed = _config_with(mutate)
    fingerprints = semantic_fingerprints(changed, tmp_path)

    assert fingerprints == baseline
    serialized = json.dumps(
        build_semantic_fingerprint_payloads(changed, tmp_path).__dict__,
        sort_keys=True,
    )
    assert "OKTO_NEURON_LLM_KEY" not in serialized
    assert "OKTO_NEURON_EMBED_KEY" not in serialized
    assert "do-not-hash" not in serialized
    assert "also-secret" not in serialized
    assert "aws-secret" not in serialized
    assert "function-secret" not in serialized
    assert "curation_max_concurrent" not in serialized


def test_generation_materialization_receipt_overrides_newer_ledger_history(
    tmp_path: Path,
) -> None:
    # D-92 made the empty-ledger fallback compute the vault's live-config
    # fingerprint triplet (``_effective_semantic_fingerprint_triplet``)
    # instead of failing closed to an all-``None`` triplet, so this needs a
    # real, initialized vault on disk (a bare ``tmp_path`` has no
    # ``okto-neuron.yaml`` for ``VaultConfig.load`` to read) -- see
    # ``tests/server/test_curation_rebuild.py::
    # test_run_rebuild_model_free_still_publishes_rollback_receipt`` for the
    # server-level version of this same contract.
    vault = Vault.init(tmp_path / "vault")
    try:
        vault_path = Path(vault.path)
        fingerprints = {
            "config": f"sha256:{'1' * 64}",
            "extraction": f"sha256:{'2' * 64}",
            "semantic_policy": f"sha256:{'3' * 64}",
        }

        written = publish_semantic_materialization(
            vault_path,
            graph_generation="generation-a",
            fingerprints=fingerprints,
            source="rollback",
        )

        assert (
            load_semantic_materialization(
                semantic_materialization_path(vault_path),
                expected_graph_generation="generation-a",
            )
            == written
        )
        assert (
            materialized_semantic_fingerprints(
                vault_path,
                graph_generation="generation-a",
            )
            == fingerprints
        )

        # No receipt exists for "generation-b" (a receipt never leaks across
        # generations) and the candidate ledger has zero completed runs, so
        # this must fall through to the vault's current live-config triplet
        # (D-92) -- not to generation-a's receipt, and not to an all-``None``
        # triplet (that contract is now scoped to a genuinely ambiguous
        # ledger, i.e. completed runs that disagree; see the
        # ``len(values) != 1`` branch).
        live_triplet = _effective_semantic_fingerprint_triplet(vault_path)
        other_generation = materialized_semantic_fingerprints(
            vault_path,
            graph_generation="generation-b",
        )
        assert other_generation == live_triplet
        assert other_generation != fingerprints
        assert all(value is not None for value in other_generation.values())

        invalidate_semantic_materialization(vault_path)
        assert not semantic_materialization_path(vault_path).exists()
    finally:
        vault.close()


def test_materialization_receipt_rejects_partial_or_unhashed_fingerprints(
    tmp_path: Path,
) -> None:
    for fingerprints in (
        {"config": f"sha256:{'1' * 64}"},
        {
            "config": f"sha256:{'1' * 64}",
            "extraction": f"sha256:{'2' * 64}",
            "semantic_policy": "policy-v1",
        },
    ):
        with pytest.raises(ValueError, match="semantic materialization"):
            publish_semantic_materialization(
                tmp_path,
                graph_generation="generation-a",
                fingerprints=fingerprints,
                source="rebuild",
            )


def test_model_prompt_and_chunk_policy_change_both_semantic_layers(tmp_path: Path) -> None:
    baseline = semantic_fingerprints(VaultConfig(), tmp_path)

    changed_configs = [
        _config_with(lambda payload: payload["llm"]["defaults"].update(model="other-model")),
        _config_with(
            lambda payload: payload["llm"]["extraction"].update(
                system_prompt="Extract only source-grounded facts."
            )
        ),
        _config_with(
            lambda payload: payload["ingest"].update(
                chunk_size_bytes=8_000,
                chunk_overlap_bytes=400,
            )
        ),
    ]

    for changed in changed_configs:
        fingerprints = semantic_fingerprints(changed, tmp_path)
        assert fingerprints.config_fingerprint != baseline.config_fingerprint
        assert fingerprints.extraction_fingerprint != baseline.extraction_fingerprint
        assert fingerprints.semantic_policy_fingerprint != baseline.semantic_policy_fingerprint


def test_semantic_api_base_query_is_retained_and_changes_fingerprints(
    tmp_path: Path,
) -> None:
    first = _config_with(
        lambda payload: payload["llm"]["defaults"].update(
            api_base="http://127.0.0.1:8123/v1?api-version=2026-01-01"
        )
    )
    second = _config_with(
        lambda payload: payload["llm"]["defaults"].update(
            api_base="http://127.0.0.1:8123/v1?api-version=2026-07-16"
        )
    )

    payloads = build_semantic_fingerprint_payloads(first, tmp_path)
    api_base = payloads.config["llm"]["steps"]["extraction"]["connection"]["api_base"]
    assert api_base.endswith("?api-version=2026-01-01")
    assert semantic_fingerprints(first, tmp_path) != semantic_fingerprints(second, tmp_path)


def test_downstream_threshold_does_not_change_extraction_layer(tmp_path: Path) -> None:
    baseline = semantic_fingerprints(VaultConfig(), tmp_path)
    changed = _config_with(
        lambda payload: payload["consolidation"].update(auto_commit_threshold=0.91)
    )

    fingerprints = semantic_fingerprints(changed, tmp_path)

    assert fingerprints.config_fingerprint != baseline.config_fingerprint
    assert fingerprints.extraction_fingerprint == baseline.extraction_fingerprint
    assert fingerprints.semantic_policy_fingerprint != baseline.semantic_policy_fingerprint


def test_curation_batch_size_changes_full_policy_but_not_extraction(tmp_path: Path) -> None:
    baseline = semantic_fingerprints(VaultConfig(), tmp_path)
    changed = _config_with(lambda payload: payload["consolidation"].update(curation_batch_size=19))

    fingerprints = semantic_fingerprints(changed, tmp_path)

    assert fingerprints.config_fingerprint != baseline.config_fingerprint
    assert fingerprints.extraction_fingerprint == baseline.extraction_fingerprint
    assert fingerprints.semantic_policy_fingerprint != baseline.semantic_policy_fingerprint


@pytest.mark.parametrize(
    "field",
    ["type_adjudication_enabled", "relation_curator_enabled"],
)
def test_semantic_stage_ablation_changes_policy_but_not_extraction(
    tmp_path: Path,
    field: str,
) -> None:
    baseline = semantic_fingerprints(VaultConfig(), tmp_path)
    changed = _config_with(lambda payload: payload["consolidation"].update({field: False}))

    fingerprints = semantic_fingerprints(changed, tmp_path)
    payloads = build_semantic_fingerprint_payloads(changed, tmp_path)

    assert fingerprints.config_fingerprint != baseline.config_fingerprint
    assert fingerprints.extraction_fingerprint == baseline.extraction_fingerprint
    assert fingerprints.semantic_policy_fingerprint != baseline.semantic_policy_fingerprint
    policy_key = "type_adjudication" if field == "type_adjudication_enabled" else "relation_curator"
    assert payloads.semantic_policy[policy_key]["enabled"] is False


def test_superseded_audit_policy_changes_full_policy_but_not_extraction(
    tmp_path: Path,
) -> None:
    baseline = semantic_fingerprints(VaultConfig(), tmp_path)
    changed = _config_with(
        lambda payload: payload["consolidation"].update(
            audit_superseded_nodes_with_llm=True,
            audit_superseded_relations_with_llm=True,
        )
    )

    fingerprints = semantic_fingerprints(changed, tmp_path)

    assert fingerprints.config_fingerprint != baseline.config_fingerprint
    assert fingerprints.extraction_fingerprint == baseline.extraction_fingerprint
    assert fingerprints.semantic_policy_fingerprint != baseline.semantic_policy_fingerprint


def test_pack_and_embedding_changes_move_only_their_semantic_layers(tmp_path: Path) -> None:
    baseline = semantic_fingerprints(VaultConfig(), tmp_path)
    pack_change = semantic_fingerprints(
        _config_with(lambda payload: payload.update(packs=["core", "sdlc"])),
        tmp_path,
    )
    embedding_change = semantic_fingerprints(
        _config_with(
            lambda payload: payload["embedding"].update(
                model="other-embedding-model",
                dimension=768,
            )
        ),
        tmp_path,
    )

    assert pack_change.config_fingerprint != baseline.config_fingerprint
    assert pack_change.extraction_fingerprint != baseline.extraction_fingerprint
    assert pack_change.semantic_policy_fingerprint != baseline.semantic_policy_fingerprint
    assert embedding_change.config_fingerprint != baseline.config_fingerprint
    assert embedding_change.extraction_fingerprint == baseline.extraction_fingerprint
    assert embedding_change.semantic_policy_fingerprint != baseline.semantic_policy_fingerprint


def test_decision_sidefiles_change_only_full_policy(tmp_path: Path) -> None:
    baseline = semantic_fingerprints(VaultConfig(), tmp_path)
    paths = (
        tmp_path / ".marginalia" / "predicates" / "aliases.json",
        tmp_path / ".marginalia" / "predicates" / "registry.json",
        tmp_path / ".marginalia" / "authority" / "index.json",
        tmp_path / ".marginalia" / "authority" / "decisions.json",
    )

    previous = baseline
    for index, path in enumerate(paths):
        if path.name == "registry.json":
            PredicateRegistry(tmp_path).upsert(_predicate_record())
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'{{"fixture":{index}}}', encoding="utf-8")
        changed = semantic_fingerprints(VaultConfig(), tmp_path)
        assert changed.config_fingerprint == baseline.config_fingerprint
        assert changed.extraction_fingerprint == baseline.extraction_fingerprint
        assert changed.semantic_policy_fingerprint != previous.semantic_policy_fingerprint
        previous = changed


def test_payload_carries_versioned_semantic_contracts(tmp_path: Path) -> None:
    payloads = build_semantic_fingerprint_payloads(VaultConfig(), tmp_path)
    empty_registry_hash = payloads.semantic_policy["decision_sidefiles"]["predicate_registry"][
        "content_hash"
    ]

    assert payloads.semantic_policy["contracts"] == dict(sorted(CONTRACT_VERSIONS.items()))
    assert payloads.semantic_policy["decision_sidefiles"] == {
        "authority": {"schema": "authority.v1", "content_hash": "absent"},
        "identity_decisions": {
            "schema": "identity_decisions.v1",
            "content_hash": "absent",
        },
        "predicate_aliases": {
            "schema": "predicate_aliases.v1",
            "content_hash": "absent",
        },
        "predicate_registry": {
            "schema": "predicate_registry.v1",
            "content_hash": empty_registry_hash,
        },
    }
    assert empty_registry_hash.startswith("sha256:")
    assert payloads.extraction["contracts"] == {
        "extraction_primitives": "extraction_primitives.v2",
        "node_identity": "node_identity.v1",
    }
    assert payloads.semantic_policy["type_adjudication"]["provider_step"] == "curator"
    assert payloads.semantic_policy["type_adjudication"]["system_prompt"].startswith("sha256:")


def test_predicate_registry_policy_hash_excludes_evidence_but_not_semantics(
    tmp_path: Path,
) -> None:
    registry = PredicateRegistry(tmp_path)
    baseline = semantic_fingerprints(VaultConfig(), tmp_path)
    record = _predicate_record()
    registry.upsert(record)
    semantic = semantic_fingerprints(VaultConfig(), tmp_path)

    registry.upsert(
        record.__class__(
            **{
                **record.__dict__,
                "support_count": 2,
                "signatures": (PredicateTypeSignature("Agent", "Place", 2),),
                "samples": (PredicateEvidenceSample("claim-2", "source-2"),),
                "confidence": 0.5,
            }
        )
    )
    evidence_only = semantic_fingerprints(VaultConfig(), tmp_path)
    registry.upsert(
        record.__class__(
            **{
                **record.__dict__,
                "definition": "The subject has a different relation to the object.",
            }
        )
    )
    changed_definition = semantic_fingerprints(VaultConfig(), tmp_path)

    assert semantic.semantic_policy_fingerprint != baseline.semantic_policy_fingerprint
    assert evidence_only.semantic_policy_fingerprint == semantic.semantic_policy_fingerprint
    assert changed_definition.semantic_policy_fingerprint != semantic.semantic_policy_fingerprint


def test_absent_and_explicitly_empty_registry_have_same_policy_hash(tmp_path: Path) -> None:
    absent = semantic_fingerprints(VaultConfig(), tmp_path)
    path = tmp_path / ".marginalia" / "predicates" / "registry.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"schema_version":"predicate_registry.v1","records":[]}\n',
        encoding="utf-8",
    )

    explicit_empty = semantic_fingerprints(VaultConfig(), tmp_path)

    assert explicit_empty.semantic_policy_fingerprint == absent.semantic_policy_fingerprint


def test_builtin_projection_is_stable_and_pack_seed_is_explicit(tmp_path: Path) -> None:
    registry = PredicateRegistry(tmp_path)
    empty = semantic_fingerprints(VaultConfig(), tmp_path)

    registry.seed_builtins()
    core = semantic_fingerprints(VaultConfig(), tmp_path)
    registry.seed_builtins()
    core_again = semantic_fingerprints(VaultConfig(), tmp_path)
    registry.seed_builtins(("sdlc",))
    with_pack = semantic_fingerprints(VaultConfig(), tmp_path)

    assert core.semantic_policy_fingerprint == empty.semantic_policy_fingerprint
    assert core_again.semantic_policy_fingerprint == core.semantic_policy_fingerprint
    assert with_pack.semantic_policy_fingerprint != core.semantic_policy_fingerprint


def test_candidate_ledger_persists_all_three_fingerprints(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    fingerprints = semantic_fingerprints(VaultConfig(), tmp_path)

    run_id = ledger.start_run(
        document_id="doc",
        source="source.md",
        blocks_total=2,
        model="model",
        semantic_policy_fingerprint=fingerprints.semantic_policy_fingerprint,
        config_fingerprint=fingerprints.config_fingerprint,
        extraction_fingerprint=fingerprints.extraction_fingerprint,
    )

    record = next(row for row in ledger.records() if row["run_id"] == run_id)
    assert record["semantic_policy_fingerprint"] == fingerprints.semantic_policy_fingerprint
    assert record["config_fingerprint"] == fingerprints.config_fingerprint
    assert record["extraction_fingerprint"] == fingerprints.extraction_fingerprint


def test_manual_review_run_start_persists_all_three_fingerprints(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "vault")
    try:
        candidate = NodeCandidate(type="Concept", title="queued candidate")
        marginalia_dir = Path(vault.path) / ".marginalia"
        ReviewQueue(marginalia_dir, vault.store).enqueue(candidate, "low_confidence")

        Companion(vault).resolve_review(candidate.candidate_id, "discard")

        started = next(
            row
            for row in CandidateLedger(marginalia_dir).records()
            if row["kind"] == "ingest_run" and row["state"] == "started"
        )
        for field in (
            "semantic_policy_fingerprint",
            "config_fingerprint",
            "extraction_fingerprint",
        ):
            assert started[field].startswith("sha256:")
    finally:
        vault.close()
