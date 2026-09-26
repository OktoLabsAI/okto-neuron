"""AuthorityRecord create/serialize round-trip; map derivations; reversibility."""

from __future__ import annotations

import json
import threading

import pytest

from okto_neuron.reconcile.authority import AuthorityIndex, AuthorityRecord


def _record() -> AuthorityRecord:
    return AuthorityRecord(
        cluster_id="c1",
        canonical_id="node-rich",
        canonical_name="Alex Rivera",
        member_ids=("node-rich", "node-m", "node-mh"),
        variants=("alex", "m. rivera"),
        exact_match_pairs=(("node-rich", "node-m"), ("node-rich", "node-mh")),
        verdict="same",
        confidence=0.93,
        provenance={"agent": "marginalia-reconcile", "lanes": ["embedding", "lexical"]},
    )


def test_record_json_round_trip():
    rec = _record()
    again = AuthorityRecord.from_json(rec.to_json())
    assert again == rec


def test_to_authority_node_is_skos_model_only():
    rec = _record()
    node = rec.to_authority_node()
    # canonical_name -> prefLabel, variants -> altLabel (SKOS mapping).
    assert node.canonical_name == "Alex Rivera"
    assert node.variants == ["alex", "m. rivera"]
    assert node.type == "Authority"


def test_upsert_and_index_file_shape(tmp_path):
    idx = AuthorityIndex(tmp_path / "authority")
    idx.upsert(_record())
    data = json.loads((tmp_path / "authority" / "index.json").read_text())
    assert data["schema_version"] == "authority.v1"
    assert data["predicate"] == "skos:exactMatch"
    assert len(data["records"]) == 1
    assert (tmp_path / "authority" / "index.json").stat().st_mode & 0o077 == 0
    # reload sees it
    assert len(AuthorityIndex(tmp_path / "authority").records()) == 1


def test_equivalence_map_member_to_canonical(tmp_path):
    idx = AuthorityIndex(tmp_path / "authority")
    idx.upsert(_record())
    m = idx.equivalence_map()
    assert m["node-m"] == "node-rich"
    assert m["node-mh"] == "node-rich"
    assert m["node-rich"] == "node-rich"  # canonical maps to itself


def test_equivalence_map_accepts_only_same_and_legacy_same_records(tmp_path):
    index_path = tmp_path / "authority" / "index.json"
    index_path.parent.mkdir()
    same = _record().to_json()
    distinct = {**same, "cluster_id": "distinct", "verdict": "distinct"}
    type_correction = {
        **same,
        "cluster_id": "type-correction",
        "canonical_id": "corrected",
        "member_ids": ["wrong-type"],
        "verdict": "type_correction",
    }
    legacy = {
        **same,
        "cluster_id": "legacy",
        "canonical_id": "legacy-canonical",
        "member_ids": ["legacy-member"],
    }
    legacy.pop("verdict")
    index_path.write_text(
        json.dumps(
            {"schema_version": "authority.v1", "records": [distinct, type_correction, legacy]}
        ),
        encoding="utf-8",
    )

    mapping = AuthorityIndex(index_path.parent).equivalence_map()

    assert mapping == {
        "legacy-member": "legacy-canonical",
        "legacy-canonical": "legacy-canonical",
    }


def test_alias_canonical_map_normalized(tmp_path):
    idx = AuthorityIndex(tmp_path / "authority")
    idx.upsert(_record())
    m = idx.alias_canonical_map()
    # normalized variant title -> canonical_name
    assert m["alex"] == "Alex Rivera"
    assert m["m. rivera"] == "Alex Rivera"


def test_alias_map_uses_exact_unicode_key_not_discovery_separator_key(tmp_path):
    idx = AuthorityIndex(tmp_path / "authority")
    rec = AuthorityRecord(
        cluster_id="unicode",
        canonical_id="canonical",
        canonical_name="Greek",
        member_ids=("canonical", "variant"),
        variants=("\u03aa\u0301", "Graph_Store"),
        exact_match_pairs=(("canonical", "variant"),),
        verdict="same",
    )
    idx.upsert(rec)

    aliases = idx.alias_canonical_map()

    assert aliases["\u0390"] == "Greek"
    assert aliases["graph_store"] == "Greek"
    assert "graph store" not in aliases


def test_remove_is_reversible(tmp_path):
    idx = AuthorityIndex(tmp_path / "authority")
    idx.upsert(_record())
    assert len(idx.records()) == 1
    idx.remove("c1")
    assert idx.records() == []
    # persisted removal: a fresh reader sees nothing
    assert AuthorityIndex(tmp_path / "authority").records() == []


def test_stale_instances_reread_under_lock_without_lost_upsert(tmp_path):
    left = AuthorityIndex(tmp_path / "authority")
    right = AuthorityIndex(tmp_path / "authority")
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def write(index: AuthorityIndex, record: AuthorityRecord) -> None:
        try:
            barrier.wait()
            index.upsert(record)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    second = AuthorityRecord.from_json(
        {**_record().to_json(), "cluster_id": "c2", "canonical_id": "node-2"}
    )
    threads = [
        threading.Thread(target=write, args=(left, _record())),
        threading.Thread(target=write, args=(right, second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert {record.cluster_id for record in AuthorityIndex(left.dir).records()} == {"c1", "c2"}


def test_failed_atomic_replace_preserves_previous_file_and_memory(tmp_path, monkeypatch):
    import okto_neuron.reconcile.authority as authority_module

    index = AuthorityIndex(tmp_path / "authority")
    first = _record()
    index.upsert(first)
    before = index.path.read_bytes()

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(authority_module, "_replace_with_windows_retry", fail_replace)
    changed = AuthorityRecord.from_json({**first.to_json(), "canonical_name": "Changed"})
    with pytest.raises(OSError, match="replace failed"):
        index.upsert(changed)

    assert index.path.read_bytes() == before
    assert index.records() == [first]
