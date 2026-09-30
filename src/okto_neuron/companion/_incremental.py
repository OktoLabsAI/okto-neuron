"""Incremental ingest — content-hash block skip (ADR 0023) and the substrate
for sub-chunk diff ingestion (ADR 0024).

The write path (`Companion.remember`) re-parses a file into fixed 12k-byte
Blocks and, today, runs one LLM extraction per non-empty Block — even when only
one line changed. This module supplies the *pre-extraction partition* that lets
`remember()` skip Blocks whose content the graph has already extracted.

Two grounding facts about the existing pipeline make this an additive filter,
not a redesign (both verified against the live code):

- A Block stores its own ``content_hash`` (== ``sha256_hex(raw_bytes)``) and the
  absolute ``byte_start``/``byte_end`` of its slice in the source file
  (``ingest/markdown.py``). So "is this Block still current?" is answered
  deterministically by recomputing ``sha256_hex(file_bytes[byte_start:byte_end])``
  and comparing to the stored hash — no guessing which of several same-index
  Blocks is the live one.
- LLM-minted Claim identity is ``semantic_claim_id(S,P,O)`` — content-hash
  *independent* (``consolidate/_claim_identity.py``). So re-extracting a changed
  Block never churns Claim ids; skipping merely avoids re-paying the LLM call.

`remember()` calls ``vault.add(source)`` *before* it asks for extraction units,
and ``add`` upserts new Blocks without removing stale ones (no delete API in the
GraphStore protocol). The partition therefore works off two snapshots:

- ``prior`` — captured BEFORE ``vault.add``: which ``content_hash`` values for
  this source already carried live LLM Claims.
- the post-add Block set — filtered to *current* Blocks (hash still matches the
  file bytes); orphaned prior Blocks are simply never handed to the extractor.

A current Block whose ``content_hash`` is in ``prior`` (already extracted) is
**skipped**; a new/changed Block is **extracted**. Whole-file no-op re-ingest
⇒ every current Block is in ``prior`` ⇒ zero extraction.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from okto_neuron._compat import getenv as _compat_getenv
from okto_neuron._internal.infra import is_infra
from okto_neuron.ingest.markdown import sha256_hex
from okto_neuron.semantic_surface import discovery_surface_key

_LOG = logging.getLogger("okto_neuron.companion")

if TYPE_CHECKING:
    from okto_neuron.core.schema import Node
    from okto_neuron.store.protocol import GraphStore


def _nodes_by_id(store: "GraphStore", node_ids) -> "dict[str, Node]":
    """One batched ``get_nodes`` read keyed by id; a missing id is simply absent,
    so ``.get(id)`` reproduces ``store.get_node(id)``'s ``None``."""
    ids = [node_id for node_id in node_ids if node_id]
    return {node.id: node for node in store.get_nodes(ids)} if ids else {}


def _claim_object_ids(claims) -> list[str]:
    """``O_id`` of every fetched Claim, for one batched object-title read."""
    return [
        str((claim.facets or {}).get("O_id"))
        for claim in claims
        if claim.type == "Claim" and (claim.facets or {}).get("O_id")
    ]


_INCREMENTAL_ENV = "OKTO_NEURON_INCREMENTAL_INGEST"
_SUBCHUNK_ENV = "OKTO_NEURON_SUBCHUNK_INGEST"

_TRUE = {"1", "true", "yes", "on"}


_FALSE = {"0", "false", "no", "off"}


def _env_or_cfg(env_name: str, cfg_value: bool) -> bool:
    """Two-way env override (F10): truthy value forces ON, falsy value forces
    OFF, unset/unknown defers to the vault config (default ON)."""
    raw = _compat_getenv(env_name, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return cfg_value


def incremental_enabled(cfg=None) -> bool:
    """ADR 0023 per-block content-hash skip. Config-driven, DEFAULT ON since
    the 2026-07-02 remediation (``ingest.incremental`` in okto-neuron.yaml);
    ``OKTO_NEURON_INCREMENTAL_INGEST`` is a two-way override."""
    return _env_or_cfg(
        _INCREMENTAL_ENV, bool(getattr(cfg, "incremental", True)) if cfg is not None else True
    )


def subchunk_enabled(cfg=None) -> bool:
    """ADR 0024 sub-chunk diff ingestion. Implies (depends on) the Layer 1
    block skip. Config-driven, DEFAULT ON (``ingest.subchunk``);
    ``OKTO_NEURON_SUBCHUNK_INGEST`` is a two-way override."""
    return _env_or_cfg(
        _SUBCHUNK_ENV, bool(getattr(cfg, "subchunk", True)) if cfg is not None else True
    )


def _resolve(source: str | PathLike[str]) -> str | None:
    try:
        return str(Path(source).expanduser().resolve(strict=False))
    except (OSError, ValueError, TypeError):
        return None


@dataclass(frozen=True)
class PriorClaimInfo:
    """The live LLM Claims derived from one prior Block, keyed for skip and (in
    ADR 0024) detach/supersede decisions."""

    block_id: str
    content_hash: str
    claim_ids: tuple[str, ...]
    block_index: int = -1
    # The prior Block's stored (stripped) text — the OLD side of the sub-chunk
    # diff (ADR 0024). Empty for the skip-only Layer 1 path.
    content: str = ""


@dataclass(frozen=True)
class PriorSnapshot:
    """State of a source's Blocks/Claims captured BEFORE ``vault.add`` overwrites
    them. ``by_hash`` maps a Block ``content_hash`` to the live LLM Claims derived
    from it; ``hashes_with_claims`` is the fast skip set; ``by_index`` is the
    prior Block at each position (for the sub-chunk diff)."""

    by_hash: dict[str, PriorClaimInfo] = field(default_factory=dict)
    hashes_with_claims: frozenset[str] = field(default_factory=frozenset)
    by_index: dict[int, PriorClaimInfo] = field(default_factory=dict)


def _llm_claims_for_block(store: "GraphStore", block_id: str) -> list[str]:
    """Ids of live, LLM-minted Claims derived from ``block_id`` (via the
    ``prov:wasDerivedFrom`` edge). Deterministic markdown Claims (system agent /
    no ``model_id``) are excluded — they are re-minted for free by ``vault.add``
    and must not, on their own, suppress LLM extraction of a Block."""
    out: list[str] = []
    edges = list(store.list_edges(dst=block_id, type="prov:wasDerivedFrom"))
    claims = _nodes_by_id(store, [edge.src for edge in edges])
    for edge in edges:
        claim = claims.get(edge.src)
        if claim is None or claim.type != "Claim":
            continue
        if is_infra(claim):
            continue
        facets = claim.facets or {}
        # LLM provenance signal: a model fingerprint is stamped only on
        # LLM-minted Claims (markdown deterministic Claims leave it None).
        if not facets.get("model_id"):
            continue
        # NB: superseded/detached Claims still COUNT here — the question this
        # answers is "was this exact block content already extracted?" (the skip
        # decision). A block whose facts were later superseded was still
        # extracted; re-extracting identical bytes would only re-mint and
        # re-supersede them. So skip stays idempotent across edit sequences.
        out.append(claim.id)
    return out


def capture_prior_snapshot(
    store: "GraphStore",
    source: str | PathLike[str],
    *,
    chunk_size_bytes: int = 12_000,
    chunk_overlap_bytes: int = 0,
) -> PriorSnapshot:
    """Snapshot, for ``source``'s current Blocks, which ``content_hash`` values
    already carry live LLM Claims. Call this BEFORE ``vault.add(source)``."""
    resolved = _resolve(source)
    if resolved is None:
        return PriorSnapshot()
    by_hash: dict[str, PriorClaimInfo] = {}
    by_index: dict[int, PriorClaimInfo] = {}
    from okto_neuron.ingest.markdown import block_uses_chunking_policy

    for node in store.list_nodes(type="Block"):
        if node.facets.get("source_path") != resolved:
            continue
        if not block_uses_chunking_policy(
            node.facets,
            chunk_size_bytes=chunk_size_bytes,
            chunk_overlap_bytes=chunk_overlap_bytes,
        ):
            continue
        content_hash = str(node.facets.get("content_hash") or "")
        if not content_hash:
            continue
        claim_ids = _llm_claims_for_block(store, node.id)
        block_index = int(node.facets.get("block_index") or 0)
        info = PriorClaimInfo(
            block_id=node.id,
            content_hash=content_hash,
            claim_ids=tuple(claim_ids),
            block_index=block_index,
            content=str(node.content or ""),
        )
        # ``by_index`` carries EVERY prior Block (the sub-chunk diff needs the old
        # text even for Blocks with no claims yet). First writer per index wins;
        # an orphaned stale Block at the same index is ignored (the live one was
        # written first in deterministic order is not guaranteed, so prefer the
        # one whose claims exist, else any).
        existing = by_index.get(block_index)
        if existing is None or (not existing.claim_ids and info.claim_ids):
            by_index[block_index] = info
        if not claim_ids:
            continue
        # ``by_hash`` only tracks hashes that already produced live LLM Claims —
        # the Layer 1 skip set. First writer wins (hashes unique per slice).
        by_hash.setdefault(content_hash, info)
    return PriorSnapshot(
        by_hash=by_hash,
        hashes_with_claims=frozenset(by_hash),
        by_index=by_index,
    )


@dataclass(frozen=True)
class IncrementalPlan:
    """The partition handed back to ``remember()``. ``extract_anchors`` are the
    Block ids whose text must be extracted; ``skipped`` retained as-is."""

    extract_block_ids: frozenset[str]
    skipped_block_ids: frozenset[str]
    orphan_block_ids: frozenset[str]

    @property
    def counts(self) -> dict[str, int]:
        return {
            "blocks_extracted": len(self.extract_block_ids),
            "blocks_skipped": len(self.skipped_block_ids),
            "blocks_orphaned": len(self.orphan_block_ids),
        }


def _read_bytes(resolved: str | None) -> bytes | None:
    if resolved is None:
        return None
    try:
        return Path(resolved).read_bytes()
    except OSError:
        return None


def is_current_block(file_bytes: bytes, byte_start: int, byte_end: int, content_hash: str) -> bool:
    """A Block is *current* iff its stored ``content_hash`` still equals the hash
    of the file bytes it claims to span. Stale (orphaned) Blocks left behind by a
    prior ingest fail this and are never extracted."""
    if byte_start < 0 or byte_end > len(file_bytes) or byte_end < byte_start:
        return False
    return sha256_hex(file_bytes[byte_start:byte_end]) == content_hash


def plan_extraction(
    store: "GraphStore",
    source: str | PathLike[str],
    prior: PriorSnapshot,
    *,
    chunk_size_bytes: int = 12_000,
    chunk_overlap_bytes: int = 0,
) -> IncrementalPlan:
    """Partition the source's post-add Blocks into extract / skip / orphan.

    - **orphan** — stored Block from a different chunking policy, or whose hash
      no longer matches the file bytes. Never extracted.
    - **skip** — current Block whose ``content_hash`` already carried live LLM
      Claims (``prior``). Extraction avoided; existing Claims retained.
    - **extract** — current Block that is new or whose content changed.
    """
    resolved = _resolve(source)
    file_bytes = _read_bytes(resolved)
    extract: set[str] = set()
    skip: set[str] = set()
    orphan: set[str] = set()
    from okto_neuron.ingest.markdown import block_uses_chunking_policy

    if resolved is None:
        return IncrementalPlan(frozenset(), frozenset(), frozenset())

    # Finding 3.23 (TOCTOU): this read is independent of the one `vault.add()`
    # already did (moments earlier, in the same `remember()` call) to anchor
    # the current Blocks — there is no lock or shared buffer between the two.
    # If the file changed on disk in between (a concurrent edit, e.g. under
    # folder-watch), `file_bytes` here reflects bytes `vault.add()` never
    # anchored anything from, and comparing THOSE bytes against Blocks anchored
    # from the OLDER read would misjudge currency — wrongly orphaning a Block
    # that WAS current against what was actually ingested. Cross-check against
    # the Document's stored `sha256` facet (written by `vault.add()` from the
    # exact bytes it read): a mismatch means the file moved under us, so treat
    # per-block currency as unknown this round rather than orphaning on
    # unrelated bytes — self-correcting, the next ingest re-reads and re-checks.
    if file_bytes is not None:
        document_id = sha256_hex("document", resolved)
        document_node = store.get_node(document_id)
        stored_sha256 = (document_node.facets or {}).get("sha256") if document_node else None
        if stored_sha256 and sha256_hex(file_bytes) != stored_sha256:
            file_bytes = None
    for node in store.list_nodes(type="Block"):
        if node.facets.get("source_path") != resolved:
            continue
        facets = node.facets or {}
        if not block_uses_chunking_policy(
            facets,
            chunk_size_bytes=chunk_size_bytes,
            chunk_overlap_bytes=chunk_overlap_bytes,
        ):
            orphan.add(node.id)
            continue
        content_hash = str(facets.get("content_hash") or "")
        byte_start = int(facets.get("byte_start") or 0)
        byte_end = int(facets.get("byte_end") or 0)
        if file_bytes is not None and not is_current_block(
            file_bytes, byte_start, byte_end, content_hash
        ):
            orphan.add(node.id)
            continue
        if content_hash and content_hash in prior.hashes_with_claims:
            skip.add(node.id)
        else:
            extract.add(node.id)
    return IncrementalPlan(
        extract_block_ids=frozenset(extract),
        skipped_block_ids=frozenset(skip),
        orphan_block_ids=frozenset(orphan),
    )


# ── ADR 0024: sub-chunk diff ─────────────────────────────────────────────────
#
# Facet vocabulary for the memory-accretes model (user decision 2026-06-30):
#  - removal (a source line is deleted, no replacement): the Claim it produced is
#    NOT erased — it stays live in recall, but its provenance is stamped
#    ``_detached: True`` + ``valid_as_of: <date>`` (the source no longer carries
#    it; it was at least true as of that date). A durable annotation is also
#    written to the vault so ``kg rebuild`` re-derives the detachment.
#  - correction (a fact's value changes — same subject+predicate, new object):
#    the OLD Claim is ``_superseded: True`` + ``valid_until: <date>`` and filtered
#    from recall; the NEW Claim is minted live, with a ``supersedes`` edge.
_SUPERSEDED_KEY = "_superseded"
_DETACHED_KEY = "_detached"
_VALID_AS_OF_KEY = "valid_as_of"
_VALID_UNTIL_KEY = "valid_until"


@dataclass(frozen=True)
class Hunk:
    """A changed fragment of a Block — the unit a sub-chunk re-ingest extracts in
    place of the whole 12k window. Byte offsets are ABSOLUTE in the new file; the
    parent Block id is carried separately by the caller."""

    text: str
    byte_start: int
    byte_end: int
    content_hash: str


def _line_spans(raw: bytes) -> list[tuple[int, int, str]]:
    """``(byte_start, byte_end, rstripped_text)`` for every line of ``raw``
    (newline included in the byte range). Used to map changed lines back to byte
    offsets for anchoring."""
    out: list[tuple[int, int, str]] = []
    pos = 0
    n = len(raw)
    while pos < n:
        nl = raw.find(b"\n", pos)
        end = n if nl == -1 else nl + 1
        out.append((pos, end, raw[pos:end].decode("utf-8", errors="replace").rstrip()))
        pos = end
    return out


def diff_to_hunks(old_text: str, new_raw: bytes, new_byte_start: int) -> list[Hunk]:
    """Line-level diff of the OLD Block text against the NEW raw Block bytes,
    returning only the changed (``replace``/``insert``) fragments as byte-anchored
    Hunks. ``equal`` runs are never re-extracted (zero churn); pure ``delete``
    runs produce no Hunk (their claims are handled by the detach pass).

    The diff runs on LINE boundaries (matching the chunker's own line invariant)
    and anchors over RAW bytes (anchors index raw; ``.content`` is stripped).
    Comparison is on rstripped line text so trailing-whitespace-only churn at the
    window edges does not spuriously re-extract.
    """
    import difflib

    old_lines = [ln.rstrip() for ln in old_text.splitlines()]
    new_spans = _line_spans(new_raw)
    new_lines = [span[2] for span in new_spans]
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    hunks: list[Hunk] = []
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag not in ("replace", "insert") or j2 <= j1:
            continue
        start = new_spans[j1][0]
        end = new_spans[j2 - 1][1]
        raw_slice = new_raw[start:end]
        text = raw_slice.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        hunks.append(
            Hunk(
                text=text,
                byte_start=new_byte_start + start,
                byte_end=new_byte_start + end,
                content_hash=sha256_hex(raw_slice),
            )
        )
    return hunks


@dataclass(frozen=True)
class SubchunkUnit:
    """A narrowed extraction unit: the parent Block id is unchanged; the byte
    range / hash / text are the changed Hunk's. ``whole`` is True when the Block
    had no usable prior (brand-new or unmatched) and is extracted in full."""

    block_id: str
    byte_start: int
    byte_end: int
    content_hash: str
    text: str
    whole: bool


def subchunk_units_for_block(
    block_id: str,
    block_index: int,
    new_byte_start: int,
    new_raw: bytes,
    new_text: str,
    prior: PriorSnapshot,
) -> list[SubchunkUnit]:
    """Narrow one CHANGED current Block to its changed Hunks. Falls back to a
    single whole-Block unit when there is no prior text at this index (a genuinely
    new Block) so correctness never depends on a clean diff."""
    old = prior.by_index.get(block_index)
    if old is None or not old.content:
        return [
            SubchunkUnit(
                block_id=block_id,
                byte_start=new_byte_start,
                byte_end=new_byte_start + len(new_raw),
                content_hash=sha256_hex(new_raw),
                text=new_text,
                whole=True,
            )
        ]
    hunks = diff_to_hunks(old.content, new_raw, new_byte_start)
    if not hunks:
        # No new bytes. Two very different situations land here:
        #   - the prior Block was successfully extracted (it carries live LLM
        #     Claims): an edit elsewhere in the document left this window equal
        #     or only deleted from it, so there is genuinely nothing new to
        #     extract. Returning [] is the whole point of the narrowing pass.
        #   - the prior Block carries NO live LLM Claims: it was committed by
        #     ``vault.add`` but extraction never succeeded for it (e.g. the
        #     provider was down on the first run). "No new hunks" must NOT be
        #     read as "already done" — that would make a failed extraction
        #     permanent, since ``plan_extraction`` correctly put this Block in
        #     ``extract`` and narrowing would silently drop it with no ledger
        #     row and no skip record. Fall back to the whole-Block unit, the
        #     same shape as the "no prior at this index" branch above.
        # ``claim_ids`` is the claim-gated set built by ``_llm_claims_for_block``
        # (deterministic markdown Claims excluded) — the same source of truth as
        # ``prior.hashes_with_claims``, already keyed per index.
        if old.claim_ids:
            return []
        return [
            SubchunkUnit(
                block_id=block_id,
                byte_start=new_byte_start,
                byte_end=new_byte_start + len(new_raw),
                content_hash=sha256_hex(new_raw),
                text=new_text,
                whole=True,
            )
        ]
    return [
        SubchunkUnit(
            block_id=block_id,
            byte_start=h.byte_start,
            byte_end=h.byte_end,
            content_hash=h.content_hash,
            text=h.text,
            whole=False,
        )
        for h in hunks
    ]


def _claim_live_block_ids(store: "GraphStore", claim_id: str) -> set[str]:
    """Block ids this Claim is currently derived from (its ``prov:wasDerivedFrom``
    edges)."""
    return {edge.dst for edge in store.list_edges(src=claim_id, type="prov:wasDerivedFrom")}


def _claim_source_paths(store: "GraphStore", claim_id: str) -> set[str]:
    """Resolved ``source_path`` facets of the Blocks this Claim is derived from.

    The document-lineage key for the supersede bound (Task 5): a Claim "belongs
    to" the document(s) whose Blocks it is anchored on. Empty when no anchoring
    Block carries a ``source_path`` (so two path-less claims never falsely match)."""
    paths: set[str] = set()
    for bid in _claim_live_block_ids(store, claim_id):
        block = store.get_node(bid)
        if block is None:
            continue
        p = (block.facets or {}).get("source_path")
        if p:
            paths.add(str(p))
    return paths


def _object_identity(facets: dict) -> str:
    """A comparable identity for a Claim's object — entity id if present, else the
    literal. Used to tell a *correction* (same subject+predicate, different object)
    from a pure *removal*."""
    o_id = facets.get("O_id")
    if o_id:
        return f"id:{o_id}"
    return f"lit:{facets.get('O_literal')!r}"


def _fact_label(store: "GraphStore", facets: dict) -> str:
    """Human-readable 'Subject — predicate — object' for the correction judge."""
    s_id = str(facets.get("S_id") or "")
    s_node = store.get_node(s_id)
    subject = s_node.title if s_node is not None else s_id
    obj = facets.get("O_literal")
    if obj is None and facets.get("O_id"):
        o_node = store.get_node(str(facets.get("O_id")))
        obj = o_node.title if o_node is not None else facets.get("O_id")
    return f"{subject} — {facets.get('P')} — {obj}"


def make_correction_judge(provider, resolved):
    """Build a graph-aware correction judge over the vault's judge LLM provider.

    Returns ``judge(new_fact: str, old_candidates: list[str]) -> int`` — the
    index of the OLD candidate corrected/updated by the NEW fact (same real-world
    attribute, changed value), or ``-1`` for none. Conservative: any provider or
    parse failure returns ``-1`` (→ detach, never a false supersede).

    This is the "check what is already written" step the exact subject+predicate
    match cannot do when extraction picks a different subject for the new value
    (e.g. 'Atlas Project'→'Atlas Milestone')."""
    from okto_neuron.llm import LLMProviderError, Message, complete_with_retry

    system = (
        "You decide whether a NEW fact is a CORRECTION (an update of the same "
        "real-world attribute to a new value) of an OLD fact. Same attribute, "
        "changed value = correction. A different attribute, or a genuinely "
        "different real-world thing, is NOT a correction. Be conservative: when "
        'unsure, answer none. Reply with STRICT JSON: {"index": <int>} where '
        "index is the 0-based position of the corrected fact, or -1 for none."
    )

    def judge(new_fact: str, candidates: list[str]) -> int:
        if not candidates:
            return -1
        listing = "\n".join(f"[{i}] {c}" for i, c in enumerate(candidates))
        user = (
            f"NEW fact now present in the edited document:\n  {new_fact}\n\n"
            f"OLD facts whose source lines were removed:\n{listing}\n\n"
            "Which OLD fact, if any, is corrected/updated by the NEW fact? "
            'Reply JSON {"index": n}.'
        )
        try:
            # ADR 0039 D5: one retry on a transient provider failure before
            # falling back to "no correction"; the retry reaches the ingest
            # tally through the tracing wrapper's ``note_retry`` hook.
            reply = complete_with_retry(
                provider,
                [Message("system", system), Message("user", user)],
                step="correction_judge",
                temperature=getattr(resolved, "temperature", 0.0) or 0.0,
                max_tokens=getattr(resolved, "max_tokens", 2000) or 2000,
                enable_thinking=getattr(resolved, "enable_thinking", False),
            )
        except LLMProviderError:
            return -1
        import json
        import re

        match = re.search(r'"index"\s*:\s*(-?\d+)', reply or "")
        if not match:
            try:
                parsed = json.loads(reply)
            except (ValueError, TypeError, json.JSONDecodeError):
                return -1
            # The 35B judge sometimes replies with a bare scalar ("2") instead
            # of {"index": 2} (esp. a truncated finish_reason=length reply). A
            # bare int IS the index; a dict carries it under "index"; anything
            # else (list/str/None/bool) → no correction. bool is an int
            # subclass, so exclude it before the int leg or True→index 1.
            if isinstance(parsed, bool):
                return -1
            if isinstance(parsed, int):
                idx = parsed
            elif isinstance(parsed, dict):
                try:
                    idx = int(parsed.get("index", -1))
                except (ValueError, TypeError):
                    return -1
            else:
                return -1
            return idx if 0 <= idx < len(candidates) else -1
        try:
            idx = int(match.group(1))
        except ValueError:
            return -1
        return idx if 0 <= idx < len(candidates) else -1

    return judge


def _apply_supersede(store: "GraphStore", old_claim, new_id: str, valid_as_of: str) -> None:
    """Date the OLD Claim (``_superseded`` + ``valid_until``, filtered from recall)
    and write a ``supersedes`` edge new→old so history stays walkable."""
    from okto_neuron.core.schema import Edge, Provenance

    store.add_node(
        old_claim.model_copy(
            update={
                "facets": {
                    **(old_claim.facets or {}),
                    _SUPERSEDED_KEY: True,
                    _VALID_UNTIL_KEY: valid_as_of,
                }
            }
        )
    )
    if new_id and new_id != old_claim.id:
        store.add_edge(
            Edge(
                id=sha256_hex("edge", new_id, "supersedes", old_claim.id),
                type="supersedes",
                src=new_id,
                dst=old_claim.id,
                provenance=Provenance(
                    source="companion-remember", rule_id="adr0022-lever5-supersede"
                ),
            )
        )


def _apply_detach(store: "GraphStore", old_claim, valid_as_of: str) -> None:
    """Stamp the OLD Claim detached (``_detached`` + ``valid_as_of``) — kept in
    recall (knowledge not erased), just marked source-absent."""
    store.add_node(
        old_claim.model_copy(
            update={
                "facets": {
                    **(old_claim.facets or {}),
                    _DETACHED_KEY: True,
                    _VALID_AS_OF_KEY: valid_as_of,
                }
            }
        )
    )


_STOPWORDS = frozenset({"the", "a", "an", "of", "and", "to", "is", "are", "in", "on", "for"})


def _subject_tokens(store: "GraphStore", facets: dict) -> set[str]:
    s_id = str(facets.get("S_id") or "")
    node = store.get_node(s_id)
    title = (node.title if node is not None else s_id) or ""
    return {
        token for token in discovery_surface_key(title).split() if token and token not in _STOPWORDS
    }


def _correction_candidate_claim_ids(
    store: "GraphStore",
    orphan_block_ids: frozenset[str],
    current_block_ids: frozenset[str],
) -> frozenset[str]:
    """Claims whose prior source evidence was removed by this exact edit.

    The correction judge is an incremental edit tool, not a vault-wide
    contradiction sweep. First ingestion has no orphan Blocks and therefore no
    possible corrected prior assertion. For an edit, admit only model Claims
    anchored to an orphaned prior Block and exclude any Claim still supported by
    a current Block. Cross-document comparison belongs to reconciliation.
    """
    candidates: set[str] = set()
    for block_id in sorted(orphan_block_ids):
        for claim_id in _llm_claims_for_block(store, block_id):
            if _claim_live_block_ids(store, claim_id) & current_block_ids:
                continue
            candidates.add(claim_id)
    return frozenset(candidates)


def supersede_contradicted(
    store: "GraphStore",
    new_claim_ids: frozenset[str],
    existing_claim_ids: frozenset[str],
    *,
    valid_as_of: str,
    correction_judge=None,
    max_candidates: int = 8,
    source_path: str | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """INTEGRAL correction pass (ADR 0022 Lever 5) — runs on EVERY ingestion.

    For each Claim minted THIS run (``new_claim_ids``), find a pre-existing Claim
    (``existing_claim_ids``) it CORRECTS — same real-world attribute, changed
    value — and supersede the old one (new wins; old dated + filtered from recall;
    ``supersedes`` edge new→old). Extraction drifts subject AND predicate AND
    object phrasing between runs, so detection is not exact (S,P): a cheap lexical
    prefilter (shared subject id OR same-document subject-title token) bounds candidates, then
    the LLM judge confirms semantically ("check what is already written"). This is
    the user-chosen default (2026-06-30): auto-supersede on contradiction.

    DOCUMENT-LINEAGE BOUND (Task 5): a vault-wide token prefilter let an edit to
    one document supersede a fact sourced ONLY from an unrelated document (weak
    subject-title overlap + an aggressive judge = knowledge silently hidden). A
    judge-confirmed correction is auto-applied ONLY when it stays inside the
    document being edited (``source_path`` — the old Claim is anchored on the same
    file) OR the two Claims share a *resolved* subject id (``S_id``). Otherwise it
    is a CROSS-DOCUMENT proposal: not applied, returned in ``deferred`` for the
    caller to surface for triage. ``source_path`` is the resolved path of the
    source being ingested; ``None`` disables the same-document leg (S_id only).

    Returns ``(applied, deferred)`` — each ``[(old_id, new_id), …]``. Idempotent
    and conservative (judge biases 'no correction' when unsure; the bound only
    ever narrows what is auto-applied, never widens it)."""
    if not new_claim_ids or not existing_claim_ids or (should_stop and should_stop()):
        return [], []
    # Index caller-scoped prior claims. Production passes only Claims whose
    # evidence was removed by this source edit; the path set keeps the helper
    # defensive for direct callers.
    existing: list[tuple[str, dict, set[str], set[str]]] = []
    for cid in sorted(existing_claim_ids):
        if should_stop and should_stop():
            return [], []
        node = store.get_node(cid)
        if node is None or node.type != "Claim":
            continue
        f = node.facets or {}
        if not f.get("model_id") or f.get(_SUPERSEDED_KEY) or f.get(_DETACHED_KEY):
            continue
        existing.append((cid, f, _subject_tokens(store, f), _claim_source_paths(store, cid)))

    superseded: list[tuple[str, str]] = []
    deferred: list[tuple[str, str]] = []
    already: set[str] = set()
    for new_id in sorted(new_claim_ids):
        if should_stop and should_stop():
            break
        new_node = store.get_node(new_id)
        if new_node is None or new_node.type != "Claim":
            continue
        nf = new_node.facets or {}
        if not nf.get("model_id"):
            continue
        new_obj = _object_identity(nf)
        new_toks = _subject_tokens(store, nf)
        new_label = _fact_label(store, nf)
        # Candidate olds: shared subject id OR shared subject-title token, with a
        # DIFFERENT object value, not already superseded this run.
        new_asserted = str(nf.get("asserted_at") or "")
        cands: list[tuple[str, str, bool]] = []  # (existing_id, label, existing_is_newer)
        for cid, of, toks, paths in existing:
            if cid in already or cid == new_id:
                continue
            if _object_identity(of) == new_obj:
                continue  # same value → corroboration, not correction
            # RECENCY GATE (F8 + ADR 0040 D12): when BOTH Claims carry an
            # explicit, byte-grounded source timestamp, it is authoritative for
            # their relative order. Equal explicit timestamps are ambiguous and
            # therefore never auto-supersede. Otherwise retain the established
            # source-file recency gate so old backups cannot correct fresher
            # knowledge. Traversal order, filenames, and Claim ids are never
            # used as temporal tie-breakers.
            new_source_asserted = str(nf.get("source_asserted_at") or "")
            old_source_asserted = str(of.get("source_asserted_at") or "")
            old_asserted = str(of.get("asserted_at") or "")
            existing_is_newer = False
            if new_source_asserted and old_source_asserted:
                if old_source_asserted == new_source_asserted:
                    continue
                existing_is_newer = old_source_asserted > new_source_asserted
            if (
                not (new_source_asserted and old_source_asserted)
                and new_asserted
                and old_asserted
                and old_asserted > new_asserted
            ):
                continue
            shared_subject = bool(nf.get("S_id")) and of.get("S_id") == nf.get("S_id")
            same_document = source_path is not None and source_path in paths
            if shared_subject or (same_document and (toks & new_toks)):
                cands.append((cid, _fact_label(store, of), existing_is_newer))
            if len(cands) >= max_candidates:
                break
        if not cands:
            continue
        if correction_judge is None:
            continue
        # BEST-EFFORT (non-dict-reply family): the correction judge is an LLM call.
        # It biases toward -1 on its own failures, but an unexpected raise
        # (non-dict JSON reply, transport crash) must not abort the whole
        # corrections pass — claims for THIS run are already committed. Log
        # with claim context, skip this one, keep applying the rest.
        if should_stop and should_stop():
            break
        from okto_neuron.llm import LLMCallCancelled, _set_call_cancel_predicate

        previous_cancel = _set_call_cancel_predicate(should_stop)
        try:
            try:
                idx = correction_judge(new_label, [label for _id, label, _newer in cands])
            finally:
                _set_call_cancel_predicate(previous_cancel)
        except LLMCallCancelled:
            break
        except Exception:  # noqa: BLE001 — one bad judge call is not fatal
            _LOG.exception(
                "ADR-0022 correction judge raised (new_claim=%s source_path=%s); "
                "skipping this correction, ingest continues",
                new_id,
                source_path,
            )
            continue
        if should_stop and should_stop():
            break
        if 0 <= idx < len(cands):
            old_id, _old_label, existing_is_newer = cands[idx]
            old_node = store.get_node(old_id)
            if old_node is None:
                continue
            of = old_node.facets or {}
            # DOCUMENT-LINEAGE BOUND (Task 5). Auto-apply only when the
            # correction stays in-document (old Claim anchored on the source
            # being edited) OR the two Claims share a RESOLVED subject id.
            # A cross-document, token-only match is deferred, never applied —
            # so an edit to one file can't hide a fact sourced only elsewhere.
            shared_subject = bool(nf.get("S_id")) and of.get("S_id") == nf.get("S_id")
            same_document = source_path is not None and source_path in _claim_source_paths(
                store, old_id
            )
            if not (shared_subject or same_document):
                deferred.append((old_id, new_id))
                continue
            # BEST-EFFORT (ADR 0022 Lever 5): claims for THIS run are already
            # committed before this correction pass runs. A store mutation that
            # blows up on ONE supersede must not abort the whole ingest/rebuild —
            # log the real error with claim context, skip this correction, and
            # keep applying the rest. Corrections that succeed are preserved.
            try:
                if existing_is_newer:
                    # Reverse-order materialization: the incoming Claim is the
                    # historically older assertion. A confirmed contradiction
                    # must leave the already-present newer Claim live and mark
                    # the incoming Claim stale; merely refusing to let the old
                    # Claim supersede the new one would leave both active.
                    _apply_supersede(store, new_node, old_id, valid_as_of)
                else:
                    _apply_supersede(store, old_node, new_id, valid_as_of)
            except Exception:  # noqa: BLE001 — post-commit correction is best-effort
                _LOG.exception(
                    "ADR-0022 supersede failed (old_claim=%s new_claim=%s "
                    "source_path=%s); skipping this correction, ingest continues",
                    old_id,
                    new_id,
                    source_path,
                )
                continue
            if existing_is_newer:
                superseded.append((new_id, old_id))
            else:
                already.add(old_id)
                superseded.append((old_id, new_id))
    return superseded, deferred


def _normalized_lines(text: str) -> frozenset[str]:
    """Rewrap-invariant content units for the survival/presence checks.

    Prose paragraphs are re-joined and split into sentences: deleting one
    sentence REWRAPS every following physical line, so line-granularity
    misread survivors as removed (observed live: a one-sentence deletion
    mass-detached every sibling claim). Paragraph boundaries (blank lines)
    are respected so headings/bullets stay their own units."""
    units: set[str] = set()
    for para in re.split(r"\n\s*\n", text):
        joined = " ".join(ln.strip() for ln in para.splitlines() if ln.strip())
        if not joined:
            continue
        for sent in re.split(r"(?<=[.!?;:])\s+", joined):
            stripped = sent.strip()
            if stripped:
                units.add(stripped)
    return frozenset(units)


@dataclass(frozen=True)
class OrphanDiff:
    """Line-level diff of ONE orphaned Block against the source's CURRENT
    content (union of current Blocks' text). Built from the STORE nodes' own
    ``content`` — never from ``prior.by_index``, whose first-writer-per-index
    entry can be a different block than the orphan being judged.

    Recorded even at zero removals: an orphan whose every line survived is
    pure re-chunking churn (the same text moved to a neighbouring Block) and
    must detach nothing."""

    block_id: str
    removed_lines: frozenset[str]  # lines present in the orphan, absent now
    survived_lines: frozenset[str]  # lines still present in current content


def build_orphan_diffs(
    store: "GraphStore",
    orphan_block_ids: frozenset[str],
    current_lines: frozenset[str],
) -> dict[str, OrphanDiff]:
    """One :class:`OrphanDiff` per orphan block (missing/empty blocks yield an
    all-empty diff, which detaches nothing).

    ``current_lines`` is derived by the caller from the SOURCE FILE BYTES —
    the trust root — never from freshly-written Block nodes: a mid-remember()
    store read of a just-upserted Block's content proved unreliable on the
    daemon path (observed live: empty current content ⇒ survived=∅ ⇒ every
    sibling claim mass-detached and reverts never resurrected)."""
    diffs: dict[str, OrphanDiff] = {}
    blocks = _nodes_by_id(store, orphan_block_ids)
    for bid in orphan_block_ids:
        node = blocks.get(bid)
        lines = _normalized_lines(str(node.content or "")) if node is not None else frozenset()
        diffs[bid] = OrphanDiff(
            block_id=bid,
            removed_lines=frozenset(lines - current_lines),
            survived_lines=frozenset(lines & current_lines),
        )
    return diffs


def _claim_anchored_lines(claim_facets: dict, block_node) -> frozenset[str]:
    """The orphan-block lines a Claim is actually anchored to.

    Resolved from the claim's ``source_span`` byte range sliced out of the
    block's stored content (block ``byte_start`` rebases file offsets). Falls
    back to the WHOLE block's lines when the span is missing or unusable —
    the strictest survival test, so a fallback can only under-detach."""
    block_facets = (block_node.facets or {}) if block_node is not None else {}
    content = str(block_node.content or "") if block_node is not None else ""
    span = claim_facets.get("source_span")
    if isinstance(span, dict):
        b_start = 0
        try:
            b_start = int(block_facets.get("byte_start") or 0)
            c_start = int(span.get("byte_start") or -1)
            c_end = int(span.get("byte_end") or -1)
        except (TypeError, ValueError):
            c_start = c_end = -1
        if 0 <= c_start < c_end:
            raw = content.encode("utf-8")
            lo = max(0, c_start - b_start)
            hi = min(len(raw), c_end - b_start)
            if lo < hi:
                anchored = _normalized_lines(raw[lo:hi].decode("utf-8", errors="ignore"))
                if anchored:
                    return anchored
    return _normalized_lines(content)


def _attribute_claim_lines(
    claim_facets: dict,
    anchored: frozenset[str],
    *,
    object_title: str | None = None,
) -> frozenset[str]:
    """The subset of a claim's anchored lines that actually CARRY the claim.

    Block-wide spans can't discriminate between sibling facts, but a literal
    claim's value appears verbatim in its source line — match ``O_literal``
    (falling back to the object node title via ``O_id`` being unavailable
    here, then to the full anchored set). Markdown heading lines are excluded
    from the fallback: they survive nearly any edit and carry no fact."""
    literal = claim_facets.get("O_literal")
    if not (isinstance(literal, str) and literal.strip()):
        # Entity-object claims: the object node's TITLE is the discriminating
        # needle (review finding: without it, entity-relation deletions never
        # detached — the all-lines fallback always found a survivor).
        literal = object_title
    if isinstance(literal, str) and literal.strip():
        needle = literal.strip().lower()
        hits = frozenset(ln for ln in anchored if needle in ln.lower())
        if hits:
            return hits
        # Composite literals rarely match verbatim ("launch date 2026-09-15"
        # vs the source's "launch date OF 2026-09-15"), and prose WRAPS — the
        # value and its label often sit on different physical lines. Attribute
        # by HIGH-ENTROPY tokens first (digit-bearing or long: dates, ports,
        # versions, ids — near-unique in a block), each sufficient alone;
        # otherwise require all significant tokens on the same line.
        tokens = [
            t
            for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", needle)
            if len(t) >= 4 or any(ch.isdigit() for ch in t)
        ]
        high_entropy = [t for t in tokens if len(t) >= 8 or any(ch.isdigit() for ch in t)]
        if high_entropy:
            hits = frozenset(ln for ln in anchored if any(t in ln.lower() for t in high_entropy))
            if hits:
                return hits
        if tokens:
            hits = frozenset(ln for ln in anchored if all(t in ln.lower() for t in tokens))
            if hits:
                return hits
    content_lines = frozenset(ln for ln in anchored if not ln.lstrip().startswith("#"))
    return content_lines or anchored


def detach_orphan_removals(
    store: "GraphStore",
    orphan_block_ids: frozenset[str],
    current_block_ids: frozenset[str],
    *,
    valid_as_of: str,
    current_lines: frozenset[str] | None = None,
) -> list[str]:
    """SUB-CHUNK removal pass (ADR 0024 memory-accretes): for LLM Claims left on
    orphaned (now-absent) Blocks that are NOT re-corroborated by a current Block
    and NOT already superseded (the integral corrections pass ran first), stamp
    ``_detached``/``valid_as_of`` while KEEPING them in recall. Returns the
    detached Claim ids (for the durable annotation). Idempotent.

    LINE-SURVIVAL GUARD (F9): a claim is detached only when its anchored lines
    actually DISAPPEARED from the source's current content. Re-chunking churn
    (the same line living in a re-sliced Block) previously detached live facts
    — now any surviving anchored line keeps the claim."""
    if current_lines is None:
        # Fallback for direct callers: derive from current block nodes.
        acc: set[str] = set()
        current_blocks = _nodes_by_id(store, current_block_ids)
        for bid in current_block_ids:
            node = current_blocks.get(bid)
            if node is not None:
                acc |= _normalized_lines(str(node.content or ""))
        current_lines = frozenset(acc)
    diffs = build_orphan_diffs(store, orphan_block_ids, current_lines)
    detached: list[str] = []
    seen: set[str] = set()
    orphan_blocks = _nodes_by_id(store, orphan_block_ids)
    for block_id in orphan_block_ids:
        block_node = orphan_blocks.get(block_id)
        diff = diffs.get(block_id)
        # One claim read and one object read per block (was one get_node per
        # edge plus one per object). Claims written by ``_apply_detach`` in an
        # earlier block are never re-judged (``seen``), and object nodes are only
        # read for their title, which a detach stamp never changes.
        edges = list(store.list_edges(dst=block_id, type="prov:wasDerivedFrom"))
        claims = _nodes_by_id(store, [edge.src for edge in edges])
        objects = _nodes_by_id(store, _claim_object_ids(claims.values()))
        for edge in edges:
            claim = claims.get(edge.src)
            if claim is None or claim.type != "Claim" or claim.id in seen:
                continue
            seen.add(claim.id)
            facets = claim.facets or {}
            if not facets.get("model_id"):
                # Deterministic markdown claims (has_tag/has_heading/links_to)
                # are NOT left to `kg rebuild` any more (finding 3.5): every
                # `ingest_document` call unconditionally supersedes a
                # document's stale deterministic claims against the fresh set
                # it just parsed (`ingest/__init__.py`,
                # `_retire_stale_deterministic_claims`), before this ADR 0024
                # pass ever runs. That is also the semantically correct verb
                # for them — a removed tag/heading/link is now definitively
                # FALSE, so it is superseded (dropped from recall), not
                # detached (kept live) the way this LLM-claim pass treats an
                # ambiguous line removal. This gate therefore stays: it is
                # simply never reached for a deterministic claim any more
                # (already superseded, or never orphaned in the first place).
                continue
            if facets.get(_DETACHED_KEY) or facets.get(_SUPERSEDED_KEY):
                continue
            if _claim_live_block_ids(store, claim.id) & current_block_ids:
                continue  # still corroborated by present content
            anchored = _claim_anchored_lines(facets, block_node)
            if diff is None or not anchored:
                continue  # nothing to judge against — keep (under-detach)
            obj_node = objects.get(str(facets.get("O_id") or "")) if facets.get("O_id") else None
            object_title = (obj_node.title or None) if obj_node is not None else None
            # Claim spans anchor at extraction-unit granularity (often the
            # WHOLE block), so a naive "any anchored line removed" rule nukes
            # every sibling claim when one line of a multi-fact paragraph is
            # edited (observed live: 1-line edit detached all 9 claims).
            # ATTRIBUTION: narrow to the lines that actually carry this
            # claim's object literal when possible; only when ALL of the
            # claim's attributed lines disappeared is it a removal. Pure
            # re-chunking churn (nothing removed) still detaches nothing.
            attributed = _attribute_claim_lines(facets, anchored, object_title=object_title)
            if not (attributed & diff.removed_lines):
                continue  # the claim's own line(s) still exist — keep
            if attributed & diff.survived_lines:
                continue  # partially survived — keep (conservative)
            _apply_detach(store, claim, valid_as_of)
            detached.append(claim.id)
    return detached


def _superseders_all_stale(store: "GraphStore", claim_id: str) -> bool:
    """True when every claim superseding ``claim_id`` is itself stale
    (superseded/detached) or gone — the precondition for reversing a
    supersede. While the WINNING correction is live, the loser stays
    superseded."""
    saw_any = False
    for edge in store.list_edges(dst=claim_id, type="supersedes"):
        saw_any = True
        winner = store.get_node(edge.src)
        if winner is None:
            continue
        wf = winner.facets or {}
        if not (wf.get(_SUPERSEDED_KEY) or wf.get(_DETACHED_KEY)):
            return False  # the correction is still in force
    # No supersedes edge at all (defensive): treat as NOT safely reversible.
    return saw_any


def resurrect_reverted_claims(
    store: "GraphStore",
    current_block_ids: frozenset[str],
    *,
    asserted_at: str,
    current_lines: frozenset[str] | None = None,
) -> list[dict]:
    """REVERT pass (F7 deterministic leg): a Claim stamped ``_superseded`` /
    ``_detached`` whose anchoring Block is CURRENT again (the source reverted
    to the old content, so the deterministic Block id matches file bytes once
    more) is resurrected — the lifecycle facets are stripped and it returns to
    recall.

    RECENCY GATE: resurrection happens only when the source's ``asserted_at``
    (file mtime at ingest) is >= the stamp date — an old backup restored over
    the vault must NOT resurrect facts that were corrected later.

    This deterministic leg is load-bearing: on a pure revert the Layer-1
    content-hash skip prevents re-extraction, so the mint-path merge leg never
    sees the claim. Returns one record per resurrected claim;
    ``was_superseded`` marks reverse-supersedes (the correction this claim
    lost to may now be the stale one — surfaced as ``stale_source`` for the
    companion inbox).

    REVERT EVIDENCE, not block-currency (adversarial-review critical
    finding): an accreting doc keeps every block current forever, so mere
    currency would un-supersede corrections in the SAME run that made them.
    Required instead: (a) the claim's own attributed lines are PRESENT in the
    anchoring current Block's content, and (b) for ``_superseded`` claims,
    every superseding claim is itself stale (the correction was undone — the
    detach pass runs FIRST in ``_reconcile_removals``)."""
    out: list[dict] = []
    seen: set[str] = set()
    current_blocks = _nodes_by_id(store, current_block_ids)
    for block_id in current_block_ids:
        block_node = current_blocks.get(block_id)
        # Presence is judged against the SOURCE FILE units (trust root) when
        # supplied — a freshly-upserted Block's content read mid-remember()
        # proved unreliable on the daemon path. NOTE: ``current_block_ids``
        # here should include the source's ORPHAN blocks too — a PARTIAL
        # revert (one sentence restored while other edits persist) re-anchors
        # nothing, so the claim still hangs off its old orphan block; the
        # file-presence check is what decides (observed live in phase4h).
        block_lines = current_lines
        if block_lines is None:
            block_lines = (
                _normalized_lines(str(block_node.content or ""))
                if block_node is not None
                else frozenset()
            )
        # Batched per block, as in ``detach_orphan_removals``: a claim resurrected
        # (rewritten) in an earlier block is ``seen`` and never re-read here.
        edges = list(store.list_edges(dst=block_id, type="prov:wasDerivedFrom"))
        claims = _nodes_by_id(store, [edge.src for edge in edges])
        objects = _nodes_by_id(store, _claim_object_ids(claims.values()))
        for edge in edges:
            claim = claims.get(edge.src)
            if claim is None or claim.type != "Claim" or claim.id in seen:
                continue
            seen.add(claim.id)
            facets = dict(claim.facets or {})
            if not facets.get("model_id"):
                continue
            was_superseded = bool(facets.get(_SUPERSEDED_KEY))
            was_detached = bool(facets.get(_DETACHED_KEY))
            if not (was_superseded or was_detached):
                continue
            stamp = str(facets.get(_VALID_UNTIL_KEY) or facets.get(_VALID_AS_OF_KEY) or "")
            if stamp and asserted_at and asserted_at < stamp:
                continue  # stale source — keep the correction in force
            anchored = _claim_anchored_lines(facets, block_node)
            obj_node = objects.get(str(facets.get("O_id") or "")) if facets.get("O_id") else None
            object_title = (obj_node.title or None) if obj_node is not None else None
            attributed = _attribute_claim_lines(facets, anchored, object_title=object_title)
            if not attributed or not (attributed <= block_lines):
                continue  # the claim's text is not (fully) back — no revert
            if was_superseded and not _superseders_all_stale(store, claim.id):
                continue  # the winning correction is still live
            for key in (_SUPERSEDED_KEY, _DETACHED_KEY, _VALID_UNTIL_KEY, _VALID_AS_OF_KEY):
                facets.pop(key, None)
            store.add_node(claim.model_copy(update={"facets": facets}))
            out.append({"claim_id": claim.id, "was_superseded": was_superseded})
    return out


def write_detachment_annotations(
    store: "GraphStore",
    vault_path: str | PathLike[str],
    source: str | PathLike[str],
    claim_ids: list[str],
    *,
    valid_as_of: str,
) -> str | None:
    """Persist a DURABLE record of each detached Claim under the vault so a future
    ``kg rebuild`` can re-derive the detachment (preserving ADR 0007 trust-root
    soundness: retained knowledge stays inside the trust root, not only in the
    derived graph). One JSONL line per detached Claim, append-only, keyed by the
    source whose edit removed the line. Returns the artifact path (or None)."""
    if not claim_ids:
        return None
    import json

    resolved = _resolve(source) or str(source)
    out_dir = Path(vault_path) / ".marginalia" / "detached"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    artifact = out_dir / (sha256_hex(resolved)[:16] + ".jsonl")
    lines: list[str] = []
    for claim_id in claim_ids:
        claim = store.get_node(claim_id)
        if claim is None:
            continue
        facets = claim.facets or {}
        lines.append(
            json.dumps(
                {
                    "claim_id": claim_id,
                    "S_id": facets.get("S_id"),
                    "P": facets.get("P"),
                    "O_id": facets.get("O_id"),
                    "O_literal": facets.get("O_literal"),
                    "title": claim.title,
                    "source_path": resolved,
                    "valid_as_of": valid_as_of,
                    "reason": "source-line-removed",
                },
                sort_keys=True,
            )
        )
    if not lines:
        return None
    try:
        with artifact.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        return None
    return str(artifact)


__all__ = [
    "incremental_enabled",
    "subchunk_enabled",
    "PriorSnapshot",
    "PriorClaimInfo",
    "IncrementalPlan",
    "capture_prior_snapshot",
    "plan_extraction",
    "is_current_block",
    "Hunk",
    "diff_to_hunks",
    "SubchunkUnit",
    "subchunk_units_for_block",
    "supersede_contradicted",
    "OrphanDiff",
    "build_orphan_diffs",
    "detach_orphan_removals",
    "resurrect_reverted_claims",
    "make_correction_judge",
    "write_detachment_annotations",
    "_DETACHED_KEY",
    "_SUPERSEDED_KEY",
    "_VALID_AS_OF_KEY",
    "_VALID_UNTIL_KEY",
]
