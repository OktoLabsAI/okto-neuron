#!/usr/bin/env bash
# new-dataset.sh — scaffold a new golden dataset skeleton.
# Usage: ./tests/golden/bin/new-dataset.sh <dataset-name>
#
# Creates tests/golden/datasets/<name>/ with empty inputs/, a dataset.yaml
# template, a 4-tier questions.yaml template, and CREATING.md (the audit trail).
# This is the repeatable, scalable process for minting a NEW golden dataset.
set -euo pipefail

NAME="${1:-}"
[[ -n "$NAME" ]] || { echo "usage: new-dataset.sh <dataset-name>" >&2; exit 64; }
[[ "$NAME" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || { echo "name must be kebab/snake lowercase" >&2; exit 64; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GOLDEN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEST="$GOLDEN_DIR/datasets/$NAME"
[[ -e "$DEST" ]] && { echo "already exists: $DEST" >&2; exit 1; }

mkdir -p "$DEST/inputs"

cat > "$DEST/dataset.yaml" <<YAML
# Golden dataset manifest — $NAME
name: $NAME
description: "ONE LINE: what this corpus is and what it tests."

# Where the source docs came from (audit trail).
source:
  origin: "portable description or environment variable for the source corpus"
  selected_on: "YYYY-MM-DD"
  rationale: "Why these ~15 files (content-rich, interconnected, no PII)."

# Harness settings.
settings:
  embedder: fastembed

# The input file list (relative to inputs/). Informational — the harness
# ingests every .md/.markdown/.txt under inputs/ regardless.
files: []
YAML

cat > "$DEST/questions.yaml" <<'YAML'
# Golden Q&A set. The harness reads `question`; settings.k is the single pinned k. The LLM judge
# reads `expected_answer`. Keep 12-16 questions across 4 tiers:
#   T1 single-fact recall · T2 single-doc synthesis ·
#   T3 multi-doc / multi-hop · T4 negative / absent ("not in the notes")
settings:
  k: 10

questions:
  - id: t1-example
    tier: T1
    question: "A single, concrete fact answerable from one file."
    expected_answer: "The exact expected answer."
    expected_source_paths: [relative/path/to/file.md]
    must_contain: [key-fact-1]

  - id: t4-absent-example
    tier: T4
    question: "Something deliberately NOT in the corpus."
    expected_answer: "Not in the notes / absent. The system should decline."
    expected_source_paths: []
    must_contain: []
YAML

cat > "$DEST/CREATING.md" <<MD
# Creating the "$NAME" golden dataset

Audit trail for how this dataset's ground truth was derived. Follow the process
in \`tests/golden/README.md\`.

## 1. Source corpus
- Origin:
- Selected on:
- Selection rationale (content-rich, self-contained, interconnected, no secrets/PII):

## 2. Files selected (~15)
| file (inputs/) | why included |
|---|---|

## 3. Ground-truth derivation
For each question: the exact answer, tier, expected_source_paths, must_contain,
and the byte-anchored evidence (file + the sentence/section it came from).

| id | tier | evidence (file → quoted fact) |
|---|---|---|

## 4. Validation
- [ ] Full QUESTION -> expected-answer set presented to the user
- [ ] User signed off (date):
- [ ] questions.yaml frozen
MD

echo "scaffolded: $DEST"
echo "next: copy ~15 source docs into $DEST/inputs/, fill dataset.yaml, then"
echo "      Claude reads the files and drafts questions.yaml + CREATING.md,"
echo "      user validates, then run: ./tests/golden/bin/run-golden.sh $NAME"
