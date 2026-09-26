#!/usr/bin/env python3
"""Real-models driver for scenario 56.

NO MOCKS. Opens the scenario's already-initialised vault, runs the REAL
Companion.remember pipeline (the scenario's explicitly configured live model
for extraction, fastembed for vectors), then asserts byte-anchored Claim
provenance and recall.

Prints machine-readable markers the .sh harness greps for, and exits non-zero
on any failure. Reads the vault path from $VAULT.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from okto_neuron._internal.infra import is_infra
from okto_neuron.companion import Companion
from okto_neuron.embed import get_provider as embed_provider
from okto_neuron.vault import Vault

PARTNER = "Jordan Lee Carter"
QUESTION = "who is the partner on Okto Neuron?"


def fail(msg: str) -> None:
    print(f"ACCEPTANCE_FAIL {msg}")
    sys.exit(1)


def main() -> None:
    vault_path = Path(os.environ["VAULT"]).resolve()
    note = vault_path / "notes" / "partner.md"
    if not note.exists():
        fail(f"partner note missing: {note}")

    embedder = embed_provider("fastembed")  # real fastembed vectors
    print(f"PROVIDERS embed_dim={embedder.dim}")  # type: ignore[attr-defined]

    vault = Vault.open(vault_path)
    try:
        vault.embedder = embedder  # type: ignore[attr-defined]  # semantic recall via the real embedder
    except Exception:
        pass

    # Let Companion resolve the real provider from the vault config.
    companion = Companion(vault, embedder=embedder)
    result = companion.remember(note)
    print(f"REMEMBER committed={result.committed} queued={result.queued}")

    raw = note.read_bytes()
    claims = list(vault.store.list_nodes(type="Claim"))  # type: ignore[attr-defined]
    print(f"CLAIMS_MINTED {len(claims)}")
    if len(claims) <= 1:
        fail(f"expected >1 Claim, got {len(claims)}")

    # (1) A Claim naming the partner, anchored with a byte range that hashes back.
    anchor_ok = False
    for claim in claims:
        if PARTNER not in claim.title:
            continue
        block = vault.store.get_node(claim.facets["block_id"])  # type: ignore[attr-defined]
        if block is None or block.type != "Block":
            continue
        bs = int(block.facets["byte_start"])
        be = int(block.facets["byte_end"])
        if be <= bs:
            continue
        digest = hashlib.sha256(raw[bs:be]).hexdigest()
        if digest == str(block.facets["content_hash"]):
            anchor_ok = True
            print(
                f"PARTNER_CLAIM_ANCHOR_OK title='{claim.title}' "
                f"bytes=[{bs}:{be}] hash={digest[:16]} slice='{raw[bs:be].decode(errors='replace')[:60].strip()}'"
            )
            break
    if not anchor_ok:
        fail("no partner Claim with a byte-range that hashes to source")

    # (2) recall surfaces the partner WITH byte-range provenance.
    hits = companion.recall(QUESTION, k=10)
    print(f"RECALL_HITS {len(hits)}")
    for i, h in enumerate(hits):
        print(f"RECALL_RANK {i} type={h.node.type} infra={is_infra(h.node)} title={h.node.title!r}")
    partner_hits = [h for h in hits if PARTNER in h.node.title]
    if not partner_hits:
        fail(f"recall did not surface partner; titles={[h.node.title for h in hits]}")
    hit = partner_hits[0]
    if hit.provenance.path != str(note.resolve()):
        fail(f"recall provenance path mismatch: {hit.provenance.path}")
    if hit.byte_end <= hit.byte_start:
        fail(f"recall provenance byte range invalid: [{hit.byte_start}:{hit.byte_end}]")
    print(
        f"RECALL_PARTNER_OK title='{hit.node.title}' type={hit.node.type} "
        f"path={Path(hit.provenance.path).name} bytes=[{hit.byte_start}:{hit.byte_end}]"
    )

    # (3) Full quality bar — the team's definition of done.
    #   #2 top-1 is the relationship Claim naming the partner, byte-anchored.
    top = hits[0]
    if not (top.node.type == "Claim" and PARTNER in top.node.title):
        fail(
            f"QUALITY_BAR top-1 must be the partner Claim, got type={top.node.type} title={top.node.title!r}"
        )
    else:
        digest = hashlib.sha256(raw[top.byte_start : top.byte_end]).hexdigest()
        if digest != top.content_hash:
            fail("QUALITY_BAR top-1 byte range does not hash back to source")
        print(
            f"QUALITY_BAR_TOP1_OK title={top.node.title!r} bytes=[{top.byte_start}:{top.byte_end}]"
        )
    #   #3 ZERO infra nodes in top-k.
    infra = [h.node.title for h in hits if is_infra(h.node)]
    if infra:
        fail(f"QUALITY_BAR infra nodes leaked into recall top-k: {infra}")
    else:
        print("QUALITY_BAR_NO_INFRA_OK")
    #   #4 ZERO raw Block nodes in untyped recall.
    blocks = [h.node.title for h in hits if h.node.type == "Block"]
    if blocks:
        fail(f"QUALITY_BAR raw Block nodes leaked into recall top-k: {blocks}")
    else:
        print("QUALITY_BAR_NO_BLOCK_OK")
    #   #5 single-entity note -> one node per entity.
    entity_types = {"Concept", "Agent", "InformationObject", "Place", "Activity"}
    counts: dict = {}
    for n in vault.store.list_nodes():  # type: ignore[attr-defined]
        if is_infra(n) or n.type not in entity_types:
            continue
        key = (n.type, (n.title or "").strip().lower())
        counts[key] = counts.get(key, 0) + 1
    dupes = {k: v for k, v in counts.items() if v > 1}
    if dupes:
        fail(f"QUALITY_BAR duplicate entity nodes: {dupes}")
    else:
        print("QUALITY_BAR_NO_DUPES_OK")

    print("QUALITY_BAR_OK")
    print("ACCEPTANCE_OK")
    vault.close()


if __name__ == "__main__":
    main()
