"""Authoritative YAML boundary for the black-box Golden harness.

PyYAML is a core Okto Neuron dependency.  Quality evidence must never be parsed
through a shape-specific approximation because question and quote text feeds
byte-exact gates and grading decisions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


UNSUPPORTED_CAPABILITIES = frozenset({"valid_time_queries"})


class GoldenYamlError(RuntimeError):
    """The authoritative YAML parser is unavailable or rejected the document."""


def question_validation_errors(document: Any) -> list[str]:
    """Validate the shared Golden question contract without scoring anything."""

    if not isinstance(document, dict):
        return ["questions document must be a map"]
    questions = document.get("questions")
    if not isinstance(questions, list):
        return ["questions must be a list"]
    if not questions:
        return ["questions must contain at least one question"]

    errors: list[str] = []
    settings = document.get("settings") or {}
    if not isinstance(settings, dict):
        errors.append("settings must be a map")
        settings = {}
    default_k = settings.get("k", 10)
    if isinstance(default_k, bool) or not isinstance(default_k, int) or default_k <= 0:
        errors.append("settings.k must be a positive integer")
        default_k = 10

    seen_ids: set[str] = set()
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            errors.append(f"question at index {index} must be a map")
            continue
        raw_id = question.get("id")
        qid = str(raw_id).strip() if raw_id is not None else ""
        if not qid:
            errors.append(f"question at index {index} requires a non-empty id")
            qid = f"index-{index}"
        elif qid in seen_ids:
            errors.append(f"duplicate question id: {qid}")
        seen_ids.add(qid)

        question_k = question.get("k", default_k)
        if isinstance(question_k, bool) or not isinstance(question_k, int) or question_k <= 0:
            errors.append(f"{qid}: k must be a positive integer")
        elif question_k != default_k:
            errors.append(f"{qid}: k={question_k} differs from pinned settings.k={default_k}")

        unsupported_capability = question.get("unsupported_capability")
        if (
            unsupported_capability is not None
            and unsupported_capability not in UNSUPPORTED_CAPABILITIES
        ):
            errors.append(
                f"{qid}: unsupported_capability must be one of "
                + ", ".join(sorted(UNSUPPORTED_CAPABILITIES))
            )

        if bool(question.get("negative_control")):
            continue
        targets = question.get("gold_targets")
        if not isinstance(targets, list) or not targets:
            errors.append(f"{qid}: non-negative question requires at least one gold_target")
    return errors


def load_yaml_text(text: str, *, source: str = "<memory>") -> Any:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise GoldenYamlError(
            "PyYAML is required for Golden YAML; run through the uv-managed Okto Neuron environment"
        ) from exc
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise GoldenYamlError(f"invalid YAML in {source}: {exc}") from exc


def load_yaml(path: Path) -> Any:
    return load_yaml_text(path.read_text(encoding="utf-8"), source=str(path))


__all__ = [
    "GoldenYamlError",
    "UNSUPPORTED_CAPABILITIES",
    "load_yaml",
    "load_yaml_text",
    "question_validation_errors",
]
