"""The libyaml C loader is behaviour-identical to ``SafeLoader`` for config reads (refs #14)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from okto_neuron.config import _vault as vault_mod
from okto_neuron.config._vault import VaultConfig, clear_config_cache, vault_yaml_version

REPO = Path(__file__).resolve().parents[2]
HAS_LIBYAML = hasattr(yaml, "CSafeLoader")
needs_libyaml = pytest.mark.skipif(
    not HAS_LIBYAML, reason="PyYAML was built without libyaml (yaml.CSafeLoader absent)"
)

SYNTHETIC = {
    "duplicate-keys": "a: 1\nb: 2\na: 3\n",
    "anchors-aliases-merge": (
        "base: &base\n  x: 1\n  y: [1, 2]\nchild:\n  <<: *base\n  y: [3]\nlist: [*base, *base]\n"
    ),
    "multiline-literal": "text: |\n  line one\n  line two\n\n  after blank\nfolded: >\n  a\n  b\n\n  c\n",
    "multiline-strip-keep": "s: |-\n  x\nk: |+\n  y\n\n",
    "unicode": "name: caf\u00e9\nemoji: \U0001f600\nzh: \u4e2d\u6587\nesc: \"\\u00e9\\U0001F600\\x41\"\n",
    "scalars": (
        "t: yes\nf: no\non_: on\noff_: off\nn: ~\nempty:\noct: 0o14\nhex: 0x1F\n"
        "us: 1_000\ninf: .inf\nflt: 1.5e3\ndate: 2026-09-30\nts: 2026-09-30T10:00:00Z\n"
        "str: '123'\nq: \"a: b\"\n"
    ),
    "flow-and-nested": "a: {b: [1, {c: d}], e: ''}\nk: [a,\n  b]\n",
    "crlf": "a: 1\r\nb:\r\n  - x\r\n  - y\r\n",
    "comments": "# top\na: 1  # trailing\n# mid\nb: 2\n",
    "empty-document": "",
    "only-comment": "# nothing\n",
    "scalar-document": "just a string\n",
    "list-document": "- 1\n- 2\n",
    "version-key": "marginalia_yaml_version: 2\ninherits_application_defaults: true\n",
}

MALFORMED = {
    "unclosed-flow": "a: [unclosed\n",
    "bad-indent": "a:\n  b: 1\n c: 2\n",
    "tab-indent": "a:\n\tb: 1\n",
    "unknown-alias": "a: *nope\n",
    "unhashable-key": "? [a]\n: 1\n",
    "bad-escape": 'a: "\\q"\n',
    "unsafe-tag": "a: !!python/object:os.system {}\n",
    "stray-colon": "a: b: c\n",
    "unterminated-quote": "a: 'oops\n",
}


def _outcome(text: str, loader: type) -> tuple[str, object]:
    try:
        data = yaml.load(text, Loader=loader)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None) or getattr(error, "context_mark", None)
        return ("error", (type(error).__name__, mark.line if mark is not None else None))
    return ("ok", repr(data))


def _assert_same(text: str, label: str) -> None:
    py = _outcome(text, yaml.SafeLoader)
    c = _outcome(text, yaml.CSafeLoader)
    assert c == py, f"{label}: CSafeLoader {c!r} != SafeLoader {py!r}"


def _real_world_config_text() -> str:
    dumped = VaultConfig.default().model_dump(mode="json", exclude_none=True)
    dumped["marginalia_yaml_version"] = 2
    return yaml.safe_dump(dumped, sort_keys=False)


def _large_config_text(target_bytes: int = 87_000) -> str:
    """An 87 KB-class vault yaml: the real default plus many typed extra keys."""
    dumped = VaultConfig.default().model_dump(mode="json", exclude_none=True)
    dumped["marginalia_yaml_version"] = 2
    dumped["extra_blob"] = {
        f"key_{i}": {"text": f"value \u00e9 {i}", "nums": [i, i / 3, True, None], "nested": {"a": i}}
        for i in range(900)
    }
    text = yaml.safe_dump(dumped, sort_keys=False, allow_unicode=True)
    assert len(text.encode("utf-8")) >= target_bytes
    return text


def _fixture_yaml_files() -> list[Path]:
    roots = [REPO / "tests", REPO / "src"]
    files = [p for root in roots for p in root.rglob("*.y*ml") if p.suffix in {".yaml", ".yml"}]
    return sorted(files)


def _docs_yaml_blocks() -> list[tuple[str, str]]:
    fence = re.compile(r"```ya?ml\n(.*?)```", re.DOTALL)
    blocks = []
    for root in (REPO / "docs", REPO / "site-docs"):
        if not root.exists():
            continue
        for md in sorted(root.rglob("*.md*")):
            if not md.is_file():
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for index, match in enumerate(fence.finditer(text)):
                blocks.append((f"{md.relative_to(REPO)}#{index}", match.group(1)))
    return blocks


DOCS_BLOCKS = _docs_yaml_blocks() or [("none", "a: 1\n")]


@needs_libyaml
@pytest.mark.parametrize("label", sorted(SYNTHETIC))
def test_synthetic_documents_load_identically(label: str) -> None:
    _assert_same(SYNTHETIC[label], label)
    assert _outcome(SYNTHETIC[label], yaml.CSafeLoader)[0] == "ok"


@needs_libyaml
def test_duplicate_keys_keep_the_last_value_in_both() -> None:
    assert yaml.load(SYNTHETIC["duplicate-keys"], yaml.CSafeLoader) == {"a": 3, "b": 2}
    assert yaml.load(SYNTHETIC["duplicate-keys"], yaml.SafeLoader) == {"a": 3, "b": 2}


@needs_libyaml
@pytest.mark.parametrize("label", sorted(MALFORMED))
def test_malformed_documents_raise_yaml_error_in_both(label: str) -> None:
    py = _outcome(MALFORMED[label], yaml.SafeLoader)
    c = _outcome(MALFORMED[label], yaml.CSafeLoader)
    assert py[0] == "error", f"{label} unexpectedly parses under SafeLoader"
    assert c[0] == "error", f"{label} unexpectedly parses under CSafeLoader"
    # The C scanner reports through the same marked-error family; the line the loader
    # surfaces in ConfigParseError must not move.
    assert c[1][1] == py[1][1], f"{label}: error line differs {c[1]} vs {py[1]}"


@needs_libyaml
def test_real_default_config_round_trips() -> None:
    text = _real_world_config_text()
    _assert_same(text, "default config")
    assert yaml.load(text, yaml.CSafeLoader) == yaml.load(text, yaml.SafeLoader)


@needs_libyaml
def test_large_config_round_trips_and_validates_identically(tmp_path, monkeypatch) -> None:
    text = _large_config_text()
    _assert_same(text, "87KB-class config")
    assert yaml.load(text, yaml.CSafeLoader) == yaml.load(text, yaml.SafeLoader)

    vault = tmp_path / "v"
    vault.mkdir()
    (vault / "okto-neuron.yaml").write_text(text, encoding="utf-8")
    clear_config_cache()
    with_c = VaultConfig.load(vault).model_dump(mode="json")
    assert vault_yaml_version(vault) == 2

    clear_config_cache()
    monkeypatch.delattr(yaml, "CSafeLoader")
    without_c = VaultConfig.load(vault).model_dump(mode="json")
    assert without_c == with_c
    clear_config_cache()


@needs_libyaml
@pytest.mark.parametrize("path", _fixture_yaml_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_repo_yaml_files_load_identically(path: Path) -> None:
    _assert_same(path.read_text(encoding="utf-8"), str(path.relative_to(REPO)))


@needs_libyaml
@pytest.mark.parametrize(
    "label,text",
    DOCS_BLOCKS,
    ids=[label for label, _ in DOCS_BLOCKS],
)
def test_docs_example_blocks_load_identically(label: str, text: str) -> None:
    _assert_same(text, label)


def test_the_config_seam_uses_the_c_loader_when_present(monkeypatch) -> None:
    seen = []
    real = yaml.load

    def spy(stream, Loader):  # noqa: N803 - mirrors yaml.load's signature
        seen.append(Loader)
        return real(stream, Loader=Loader)

    monkeypatch.setattr(yaml, "load", spy)
    assert vault_mod._safe_load("a: 1\n") == {"a": 1}
    expected = yaml.CSafeLoader if HAS_LIBYAML else yaml.SafeLoader
    assert seen == [expected]


def test_the_config_seam_falls_back_to_safe_loader_without_libyaml(monkeypatch) -> None:
    seen = []
    real = yaml.load

    def spy(stream, Loader):  # noqa: N803
        seen.append(Loader)
        return real(stream, Loader=Loader)

    monkeypatch.setattr(yaml, "load", spy)
    monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
    assert vault_mod._safe_load("a: [1, 2]\n") == {"a": [1, 2]}
    assert seen == [yaml.SafeLoader]
    with pytest.raises(yaml.YAMLError):
        vault_mod._safe_load("a: [unclosed\n")


def test_load_errors_keep_their_line_numbers(tmp_path) -> None:
    from okto_neuron.errors import ConfigParseError

    vault = tmp_path / "v"
    vault.mkdir()
    (vault / "okto-neuron.yaml").write_text("a: 1\nb: [unclosed\n", encoding="utf-8")
    clear_config_cache()
    with pytest.raises(ConfigParseError) as caught:
        VaultConfig.load(vault)
    expected = _outcome("a: 1\nb: [unclosed\n", yaml.SafeLoader)[1][1]
    assert caught.value.line == expected + 1
