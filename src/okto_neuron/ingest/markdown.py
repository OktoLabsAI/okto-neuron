"""Markdown ingest — CommonMark blocks plus deterministic v0 claims."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from okto_neuron.core.schema import Item
from okto_neuron.schema.support import Block, BlockKind, Claim, Document

_FRONT_TEXT = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_TAG = re.compile(r"(?:^|\s)#([\w/-]+)")
_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
_ATX_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_SEP = "\x1f"
# The fixed durable-copy directory (server/_ingest_queue.py's durable_copy_path)
# a watched-folder/CLI add mirrors a source under before it is ever parsed: the
# real path a user organized is `<root-key>/<relpath>` under this pair, not
# directly under the vault. See _document_title().
_DURABLE_SOURCES_PARTS = (".marginalia", "sources")
# durable_copy_path's root-key directory is exactly
# sha256(str(resolved root))[:16] -- 16 lowercase hex chars, never a real
# directory name a user would choose. Distinguishes a rooted watched-folder
# copy (sources/<root-key>/<relpath>) from the no-root flat fallback
# (sources/<stem>-<hash8><suffix>, no extra directory at all).
_ROOT_KEY_RE = re.compile(r"^[0-9a-f]{16}$")
# Historical defaults remain public to the ingest/config boundary. Keep the
# private alias because curation excerpt tests import it as a compatibility pin.
# Changed from 12_000 to 6_000 on 2026-09-12: three repeats/config on the real
# LOTR-slice daemon path measured mean fact_recall 0.4045 (sd 0.0148) at 6000/0
# vs. 0.2362 (sd 0.0056) at 12000/0 -- a 0.1683 gap, 11-30x either config's own
# noise, that never overlaps across six runs (internal chunk-size study,
# 2026-09-11).
DEFAULT_CHUNK_SIZE_BYTES = 6_000
DEFAULT_CHUNK_OVERLAP_BYTES = 0
_WINDOW_BYTES = DEFAULT_CHUNK_SIZE_BYTES
# The fixed partition every pre-ADR-0038 Block was actually written under,
# before chunking became configurable. NOT the current default above -- those
# Blocks are historical and their true size never changes, regardless of what
# DEFAULT_CHUNK_SIZE_BYTES/_OVERLAP_BYTES are set to today. Used only as the
# facet fallback in block_uses_chunking_policy() below; do not repoint this at
# the live default constants, or legacy no-facet Blocks get misclassified
# every time the shipped default changes.
_LEGACY_CHUNK_SIZE_BYTES = 12_000
_LEGACY_CHUNK_OVERLAP_BYTES = 0
# A ratio approaching 1.0 makes each window barely advance past the last,
# causing near-exponential block-count blowup (measured: 0.95 ratio produced a
# 20x block count vs. 0.0 on a 2.4MB fixture; short-line content is worse).
# 0.5 keeps the doubling effect of ordinary overlap use linear-ish while
# rejecting the pathological end of the range.
_MAX_CHUNK_OVERLAP_RATIO = 0.5


@dataclass(frozen=True)
class ParsedBlock:
    block: Block
    raw: bytes
    text: str


@dataclass(frozen=True)
class ParsedClaim:
    claim: Claim
    title: str
    text: str
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MarkdownIngest:
    document: Document
    item: Item
    blocks: list[ParsedBlock]
    claims: list[ParsedClaim]
    metadata: dict[str, Any]
    chunk_size_bytes: int
    chunk_overlap_bytes: int


def sha256_hex(*parts: object) -> str:
    h = hashlib.sha256()
    for i, part in enumerate(parts):
        if i:
            h.update(_SEP.encode("utf-8"))
        if isinstance(part, bytes):
            h.update(part)
        else:
            h.update(str(part).encode("utf-8"))
    return h.hexdigest()


def _extract_frontmatter(raw: bytes) -> tuple[dict[str, Any], int]:
    if not raw.startswith(b"---\n"):
        return {}, 0
    end = raw.find(b"\n---\n", 4)
    if end == -1:
        return {}, 0
    fm_end = end + len(b"\n---\n")
    try:
        meta = yaml.safe_load(raw[4:end].decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError):
        meta = {}
    if not isinstance(meta, dict):
        return {}, fm_end
    return meta, fm_end


def _safe_block_path(path: Path, vault_root: Path | None) -> str:
    """The path recorded on a Block, and hashed into its id.

    Vault-relative when ``vault_root`` is known — this is the normal case for
    every production caller (``ingest_document`` always has a vault root).
    Two files sharing a basename in different directories then get distinct
    Block ids, because the full relative path (not just the filename) feeds
    the id hash.

    Falls back to the bare filename ONLY when no vault root is available (a
    direct ``parse_markdown()`` call outside a vault context, e.g. a script or
    test) or when ``path`` doesn't resolve under ``vault_root``. This fallback
    is a deliberate compatibility path, not the normal case: it can still
    collide across same-named files in different directories, exactly as the
    unconditional bare-filename behaviour did before this fix.
    """
    if vault_root is not None:
        try:
            rel = path.resolve(strict=False).relative_to(vault_root.resolve(strict=False))
            return rel.as_posix()
        except ValueError:
            pass
    return path.name


def _document_title(meta: Mapping[str, Any], path: Path, vault_root: Path | None) -> str:
    """The Document node's display title.

    An explicit frontmatter ``title`` always wins. Otherwise the title is the
    document's path relative to the INGEST ROOT it was added under, without
    the file extension -- e.g. ``"projects/alpha/notes/sprint-06/SPRINT"``
    instead of the bare ``"SPRINT"`` every same-named file in a different
    directory used to collide on (one live vault had 69 Document nodes but
    only 56 distinct titles). The Document's *id* was already unique (hashed
    from the resolved absolute path), so this only fixes the display name.

    A watched-folder/CLI ``add`` never ingests straight from where the user's
    file actually lives: ``durable_copy_path`` (``server/_ingest_queue.py``)
    mirrors it first under ``<vault>/.marginalia/sources/<root-key>/<relpath>``,
    where ``root-key`` is ``sha256(str(resolved ingest root))[:16]``. So the
    path relative to ``vault_root`` carries that durable-copy scaffolding, not
    the tree the user organized -- strip it back off:

    1. Drop a leading ``.marginalia/sources`` pair when present (the fixed
       durable-copy directory name).
    2. Drop one more leading segment only when it is exactly 16 lowercase hex
       characters -- ``durable_copy_path``'s ``root_key`` shape, which a real
       directory name never happens to be. This also means the no-root flat
       fallback (``sources/<stem>-<hash8><suffix>``, no root-key directory at
       all) and an upload's mirrored ``webkitRelativePath`` tree (no root-key
       prefix either) are left alone, since neither has that segment.

    What remains IS the original relative tree, because durable_copy_path
    mirrors it byte-for-byte under the root-key directory.

    Falls back to the bare file stem when no ``vault_root`` is available or
    the path doesn't resolve under it (a direct ``parse_markdown()`` call
    outside a vault context, or ``allow_external_sources``) -- the same
    compatibility fallback :func:`_safe_block_path` uses.
    """
    explicit = meta.get("title")
    if explicit:
        return str(explicit)
    if vault_root is not None:
        try:
            rel = path.resolve(strict=False).relative_to(vault_root.resolve(strict=False))
        except ValueError:
            rel = None
        if rel is not None and rel.parts:
            parts = list(rel.parts)
            if tuple(parts[:2]) == _DURABLE_SOURCES_PARTS:
                parts = parts[2:]
                if len(parts) > 1 and _ROOT_KEY_RE.match(parts[0]):
                    parts = parts[1:]
            if parts:
                return Path(*parts).with_suffix("").as_posix()
    return path.stem


def _new_block(
    *,
    path: Path,
    block_index: int,
    byte_start: int,
    byte_end: int,
    block_kind: BlockKind,
    raw: bytes,
    text: str,
    vault_root: Path | None = None,
) -> ParsedBlock:
    block_hash = sha256_hex(raw)
    safe_path = _safe_block_path(path, vault_root)
    block = Block(
        id=sha256_hex(safe_path, block_hash, block_index),
        path=safe_path,
        block_index=block_index,
        byte_start=byte_start,
        byte_end=byte_end,
        block_kind=block_kind,
        content_hash=block_hash,
    )
    return ParsedBlock(block=block, raw=raw, text=text)


def block_uses_chunking_policy(
    facets: Mapping[str, Any], *, chunk_size_bytes: int, chunk_overlap_bytes: int
) -> bool:
    """Whether a stored Block belongs to the effective source partition.

    Blocks written before ADR 0038 have no policy facets and represent the
    historical 12,000-byte, zero-overlap partition.
    """

    try:
        stored_size = int(facets.get("chunk_size_bytes", _LEGACY_CHUNK_SIZE_BYTES))
        stored_overlap = int(facets.get("chunk_overlap_bytes", _LEGACY_CHUNK_OVERLAP_BYTES))
    except (TypeError, ValueError):
        return False
    return stored_size == chunk_size_bytes and stored_overlap == chunk_overlap_bytes


def _commonmark_blocks(
    path: Path,
    raw: bytes,
    body_start: int,
    *,
    chunk_size_bytes: int,
    chunk_overlap_bytes: int,
    vault_root: Path | None = None,
) -> list[ParsedBlock]:
    """Slice the document body into configurable byte windows — one Block each,
    with exact absolute byte anchors.

    We do NOT trust headings, frontmatter, or any markdown structure (author
    documents may have none, or wrong ones). The only reliable thing is "give
    the extractor a coherent chunk of text it can work with." Windows break
    only on line boundaries, never mid-line, so multibyte characters stay
    intact and byte anchors line up with whole lines. A line longer than the
    cap becomes its own oversized window rather than being split.
    """
    body = raw[body_start:]
    blocks: list[ParsedBlock] = []
    block_index = 0
    line_spans: list[tuple[int, int]] = []
    pos = 0
    while pos < len(body):
        newline = body.find(b"\n", pos)
        line_end = len(body) if newline == -1 else newline + 1
        line_spans.append((pos, line_end))
        pos = line_end

    def flush(start: int, end: int, idx: int) -> int:
        block_raw = body[start:end]
        if not block_raw.strip():
            return idx
        blocks.append(
            _new_block(
                path=path,
                block_index=idx,
                byte_start=body_start + start,
                byte_end=body_start + end,
                block_kind=BlockKind.paragraph,
                raw=block_raw,
                text=block_raw.decode("utf-8", errors="replace").strip(),
                vault_root=vault_root,
            )
        )
        return idx + 1

    start_line = 0
    while start_line < len(line_spans):
        window_start = line_spans[start_line][0]
        end_line = start_line
        while end_line < len(line_spans):
            candidate_end = line_spans[end_line][1]
            if end_line > start_line and candidate_end - window_start > chunk_size_bytes:
                break
            end_line += 1
            if candidate_end - window_start >= chunk_size_bytes:
                break

        window_end = line_spans[end_line - 1][1]
        block_index = flush(window_start, window_end, block_index)
        if end_line >= len(line_spans):
            break

        if chunk_overlap_bytes == 0:
            start_line = end_line
            continue

        overlap_floor = window_end - chunk_overlap_bytes
        next_line = end_line
        for line_index in range(start_line + 1, end_line):
            if line_spans[line_index][0] >= overlap_floor:
                next_line = line_index
                break
        start_line = next_line
    return blocks


def _headings_in_text(text: str) -> list[str]:
    headings: list[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _ATX_HEADING.match(line)
        if not match:
            continue
        heading = match.group(1).strip()
        if heading.endswith("#"):
            heading = heading.rstrip("#").rstrip()
        if heading:
            headings.append(heading)
    return headings


def _make_claim(
    *,
    block: Block,
    subject_id: str,
    predicate: str,
    object_literal: str,
    extraction_activity_id: str,
    agent_id: str,
    title: str,
    text: str,
    tags: list[str] | None = None,
) -> ParsedClaim:
    claim_id = sha256_hex(block.content_hash, subject_id, predicate, object_literal)
    claim = Claim(
        id=claim_id,
        S_id=subject_id,
        P=predicate,
        O_literal=object_literal,
        confidence=1.0,
        block_id=block.id,
        extraction_activity_id=extraction_activity_id,
        agent_id=agent_id,
    )
    return ParsedClaim(claim=claim, title=title, text=text, tags=tags or [])


def parse_markdown(
    path: str | Path,
    *,
    extraction_activity_id: str,
    agent_id: str,
    chunk_size_bytes: int = DEFAULT_CHUNK_SIZE_BYTES,
    chunk_overlap_bytes: int = DEFAULT_CHUNK_OVERLAP_BYTES,
    vault_root: str | Path | None = None,
) -> MarkdownIngest:
    if chunk_size_bytes <= 0:
        raise ValueError("chunk_size_bytes must be greater than zero")
    if chunk_overlap_bytes < 0 or chunk_overlap_bytes >= chunk_size_bytes:
        raise ValueError(
            "chunk_overlap_bytes must be non-negative and smaller than chunk_size_bytes"
        )
    if chunk_overlap_bytes / chunk_size_bytes > _MAX_CHUNK_OVERLAP_RATIO:
        raise ValueError(
            "chunk_overlap_bytes must not exceed "
            f"{_MAX_CHUNK_OVERLAP_RATIO:.0%} of chunk_size_bytes "
            f"(got chunk_overlap_bytes={chunk_overlap_bytes}, "
            f"chunk_size_bytes={chunk_size_bytes}, ratio="
            f"{chunk_overlap_bytes / chunk_size_bytes:.2f}); an overlap ratio "
            "approaching 1.0 causes near-exponential block-count amplification "
            "(re-scanning nearly the same window on every step)"
        )
    p = Path(path)
    root = Path(vault_root).resolve(strict=False) if vault_root is not None else None
    raw = p.read_bytes()
    meta, body_start = _extract_frontmatter(raw)
    body = raw[body_start:].decode("utf-8", errors="replace")
    title = _document_title(meta, p, root)
    file_hash = sha256_hex(raw)
    document_id = sha256_hex("document", str(p.resolve()))
    document = Document(
        id=document_id,
        uri=str(p.resolve()),
        media_type="text/markdown",
        byte_length=len(raw),
        sha256=file_hash,
        discovered_at=datetime.now(timezone.utc),
    )
    item = Item(
        id=document_id,
        title=title,
        content=body,
        tags=sorted({str(t) for t in meta.get("tags") or []}),
        path=str(p.resolve()),
        mimetype="text/markdown",
        facets={k: v for k, v in meta.items() if k not in {"title", "tags"}},
    )
    blocks = _commonmark_blocks(
        p,
        raw,
        body_start,
        chunk_size_bytes=chunk_size_bytes,
        chunk_overlap_bytes=chunk_overlap_bytes,
        vault_root=root,
    )
    if body_start:
        frontmatter_raw = raw[:body_start]
        blocks.insert(
            0,
            _new_block(
                path=p,
                block_index=0,
                byte_start=0,
                byte_end=body_start,
                block_kind=BlockKind.code_block,
                raw=frontmatter_raw,
                text=frontmatter_raw.decode("utf-8", errors="replace").strip(),
                vault_root=root,
            ),
        )
        blocks = [
            ParsedBlock(
                block=block.block.model_copy(
                    update={
                        "block_index": i,
                        "id": sha256_hex(block.block.path, block.block.content_hash, i),
                    }
                ),
                raw=block.raw,
                text=block.text,
            )
            for i, block in enumerate(blocks)
        ]

    claims: list[ParsedClaim] = []
    frontmatter_block = blocks[0].block if body_start and blocks else None
    for tag in sorted({str(t) for t in meta.get("tags") or []}):
        if frontmatter_block:
            claims.append(
                _make_claim(
                    block=frontmatter_block,
                    subject_id=document_id,
                    predicate="has_tag",
                    object_literal=tag,
                    extraction_activity_id=extraction_activity_id,
                    agent_id=agent_id,
                    title=f"tag: {tag}",
                    text=f"{title} has tag {tag}",
                    tags=[tag],
                )
            )

    # Overlapping windows (ADR 0038, operator-tunable) re-scan the same bytes
    # more than once, so a single real #tag/[[wikilink]] mention can fall
    # inside several windows. has_tag/links_to are document-level booleans
    # ("this document has tag X" / "this document links to Y") — one real
    # mention must mint exactly one Claim regardless of how many overlapping
    # windows re-detect it, so dedupe per (document, predicate, object) across
    # the whole per-block scan. has_heading is deliberately EXCLUDED: heading
    # anchors are positional (a heading claim is the byte-anchored provenance
    # target for its own section), so repeats across blocks stay distinct.
    seen_inline_tags: set[str] = set()
    seen_links: set[str] = set()
    for block in blocks:
        for heading in sorted(set(_headings_in_text(block.text))):
            claims.append(
                _make_claim(
                    block=block.block,
                    subject_id=document_id,
                    predicate="has_heading",
                    object_literal=heading,
                    extraction_activity_id=extraction_activity_id,
                    agent_id=agent_id,
                    title=f"heading: {heading}",
                    text=f"{title} has heading {heading}",
                )
            )
        for tag in sorted(set(_TAG.findall(block.text))):
            if tag in seen_inline_tags:
                continue
            seen_inline_tags.add(tag)
            claims.append(
                _make_claim(
                    block=block.block,
                    subject_id=document_id,
                    predicate="has_tag",
                    object_literal=tag,
                    extraction_activity_id=extraction_activity_id,
                    agent_id=agent_id,
                    title=f"tag: {tag}",
                    text=f"{title} has tag {tag}",
                    tags=[tag],
                )
            )
        for target in sorted(set(_WIKILINK.findall(block.text))):
            link = target.strip()
            if link in seen_links:
                continue
            seen_links.add(link)
            claims.append(
                _make_claim(
                    block=block.block,
                    subject_id=document_id,
                    predicate="links_to",
                    object_literal=link,
                    extraction_activity_id=extraction_activity_id,
                    agent_id=agent_id,
                    title=f"wikilink: {link}",
                    text=f"{title} links to {link}",
                )
            )
    return MarkdownIngest(
        document=document,
        item=item,
        blocks=blocks,
        claims=claims,
        metadata=meta,
        chunk_size_bytes=chunk_size_bytes,
        chunk_overlap_bytes=chunk_overlap_bytes,
    )


def load_markdown(path: str | Path) -> tuple[Item, list[str], list[str]]:
    """Return (Item node, wiki-link targets, tags).

    The Item carries title/content/facets; the caller resolves wikilinks to
    Authorities and writes mention edges.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    meta: dict = {}
    body = text
    m = _FRONT_TEXT.match(text)
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        body = text[m.end() :]

    title = str(meta.get("title") or p.stem)
    tags = list(meta.get("tags") or []) + _TAG.findall(body)
    links = _WIKILINK.findall(body)

    item = Item(
        title=title,
        content=body,
        tags=sorted(set(tags)),
        path=str(p.resolve()),
        mimetype="text/markdown",
        facets={k: v for k, v in meta.items() if k not in {"title", "tags"}},
    )
    return item, links, tags
