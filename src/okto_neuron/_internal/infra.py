"""Infrastructure/provenance node marker — the one predicate both the retriever
(Track A) and the resolver/dedup (Track B) bind on. Infra nodes are graph
plumbing (provenance Agent/Activity), NOT content; they must never surface in
recall/ask/browser content views, and must never be treated as entity
duplicates."""

from __future__ import annotations

INFRA_FACET_KEY = "infra"
# Merge into a node's facets at mint time:  facets={**facets, **INFRA_FACET}
INFRA_FACET = {INFRA_FACET_KEY: True}

# Low-salience structural anchor marker. A node carries this when it was
# auto-promoted purely so a literal-fact Claim has a live subject anchor (e.g. a
# folder/phase/file label that the node curator queued rather than committed —
# see companion.remember Fix A). Such a node is a real Concept (so it stays in
# the dedup/resolver path, UNLIKE infra plumbing), but it must NOT surface as an
# entity in recall/ask/subgraph retrieval. This is a deliberately SEPARATE
# signal from ``infra``: infra == provenance plumbing (also dedup-exempt);
# low-salience == content-bearing anchor that is just not a retrieval entity.
#
# Trust caveat (mirrors ``infra``): the key is un-namespaced and frontmatter
# meta keys become top-level facets (ingest/markdown.py), so a user could set it
# by hand. The blast radius is self-inflicted (hides one's own node from recall)
# — not a privilege escalation — identical to the existing ``infra`` caveat.
LOW_SALIENCE_FACET_KEY = "_salience"
LOW_SALIENCE_FACET = {LOW_SALIENCE_FACET_KEY: "low"}


def is_low_salience(node: object) -> bool:
    """True iff ``node`` is a low-salience structural anchor (Fix A).

    Filtered from recall/ask/subgraph retrieval but still a first-class node in
    the graph and the dedup/resolver path. Durable mint-time facet only; never
    matches on title/content."""
    facets = getattr(node, "facets", None) or {}
    return facets.get(LOW_SALIENCE_FACET_KEY) == "low"


SUPERSEDED_FACET_KEY = "_superseded"


def is_superseded(node: object) -> bool:
    """True iff ``node`` is a Claim that a later, corrected Claim has superseded
    (ADR 0024: a fact's value changed, e.g. "due Jun 26" → "due Jul 1"). The old
    Claim is dated (``valid_until``) and filtered from recall, but never deleted —
    its ``supersedes`` edge keeps the history walkable. Distinct from a *detached*
    Claim (source line removed with no replacement), which STAYS in recall."""
    facets = getattr(node, "facets", None) or {}
    return bool(facets.get(SUPERSEDED_FACET_KEY))


def llm_infra_ids(model_id: str) -> frozenset[str]:
    """The two deterministic LLM-extraction provenance ids for a model."""
    from okto_neuron.ingest.markdown import sha256_hex

    return frozenset(
        {
            sha256_hex("agent", "llm", model_id),
            sha256_hex("activity", "llm-extraction", model_id),
        }
    )


def _system_infra_ids() -> frozenset[str]:
    from okto_neuron.ingest import EXTRACTION_ACTIVITY_ID, SYSTEM_AGENT_ID

    return frozenset({SYSTEM_AGENT_ID, EXTRACTION_ACTIVITY_ID})


def is_infra(node: object, *, extra_ids: frozenset[str] = frozenset()) -> bool:
    """True iff ``node`` is provenance/infrastructure plumbing.

    Primary signal: the durable mint-time facet. Fallback: the deterministic
    system id-set (+ any caller-supplied model-derived ids). Both signals are
    durable; we never match on title/content."""
    facets = getattr(node, "facets", None) or {}
    if facets.get(INFRA_FACET_KEY) is True:
        return True
    node_id = getattr(node, "id", None)
    return node_id in (_system_infra_ids() | extra_ids)


__all__ = [
    "INFRA_FACET_KEY",
    "INFRA_FACET",
    "LOW_SALIENCE_FACET_KEY",
    "LOW_SALIENCE_FACET",
    "is_infra",
    "is_low_salience",
    "is_superseded",
    "SUPERSEDED_FACET_KEY",
    "llm_infra_ids",
]
