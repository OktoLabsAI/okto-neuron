"""Contract for the Marginalia -> Okto Neuron compatibility layer (0.3.0).

Written before the mechanical rename so each legacy name has a pinned behaviour:
env fallback with one warning, the home resolver that never moves vaults, both
config file names, both HTTP names, process detection and both entry-point
groups.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import warnings

import pytest

from okto_neuron import _compat as compat


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in ("OKTO_NEURON_ENDPOINT", "MARGINALIA_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    compat.reset_legacy_warnings_for_tests()
    return home


# -- env ---------------------------------------------------------------------


def test_new_env_name_is_read_without_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OKTO_NEURON_ENDPOINT", "http://127.0.0.1:1")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert compat.getenv("OKTO_NEURON_ENDPOINT") == "http://127.0.0.1:1"


def test_legacy_env_name_is_read_with_exactly_one_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MARGINALIA_ENDPOINT", "http://127.0.0.1:2")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert compat.getenv("OKTO_NEURON_ENDPOINT") == "http://127.0.0.1:2"
        assert compat.getenv("OKTO_NEURON_ENDPOINT") == "http://127.0.0.1:2"
    legacy = [w for w in caught if issubclass(w.category, compat.LegacyNameWarning)]
    assert len(legacy) == 1
    assert "MARGINALIA_ENDPOINT" in str(legacy[0].message)
    assert "OKTO_NEURON_ENDPOINT" in str(legacy[0].message)


def test_legacy_warning_is_visible_by_default() -> None:
    # FutureWarning subclasses are shown to end users under the default filters,
    # unlike DeprecationWarning.
    assert issubclass(compat.LegacyNameWarning, FutureWarning)


def test_new_env_name_wins_over_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OKTO_NEURON_ENDPOINT", "new")
    monkeypatch.setenv("MARGINALIA_ENDPOINT", "old")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert compat.getenv("OKTO_NEURON_ENDPOINT") == "new"
        assert compat.getenv("OKTO_NEURON_ENDPOINT") == "new"
    assert len([w for w in caught if issubclass(w.category, compat.LegacyNameWarning)]) == 1


def test_unset_env_returns_default() -> None:
    assert compat.getenv("OKTO_NEURON_ENDPOINT") is None
    assert compat.getenv("OKTO_NEURON_ENDPOINT", "d") == "d"


def test_getenv_rejects_a_legacy_spelling() -> None:
    with pytest.raises(ValueError):
        compat.getenv("MARGINALIA_ENDPOINT")


# -- app home (D5) -------------------------------------------------------------


def test_fresh_install_uses_new_home_only(_isolated: Path) -> None:
    assert compat.app_home() == _isolated / ".okto-neuron"
    assert compat.default_vault_roots() == [(_isolated / ".okto-neuron" / "vaults").resolve()]
    assert compat.app_file("okto-neuron.toml", "marginalia.toml") == (
        _isolated / ".okto-neuron" / "okto-neuron.toml"
    )


def test_upgraded_install_keeps_legacy_vault_root_first(_isolated: Path) -> None:
    (_isolated / ".marginalia" / "vaults" / "work").mkdir(parents=True)
    roots = compat.default_vault_roots()
    assert roots[0] == (_isolated / ".marginalia" / "vaults").resolve()
    assert (_isolated / ".okto-neuron" / "vaults").resolve() in roots


def test_app_file_falls_back_to_the_legacy_file_until_migrated(_isolated: Path) -> None:
    legacy = _isolated / ".marginalia"
    legacy.mkdir()
    (legacy / "marginalia.toml").write_text("marginalia_toml_version = 1\n")
    assert compat.app_file("okto-neuron.toml", "marginalia.toml") == legacy / "marginalia.toml"
    compat.migrate_legacy_app_home()
    assert compat.app_file("okto-neuron.toml", "marginalia.toml") == (
        _isolated / ".okto-neuron" / "okto-neuron.toml"
    )


def test_migration_copies_app_files_and_never_moves_vaults(_isolated: Path) -> None:
    legacy = _isolated / ".marginalia"
    vault = legacy / "vaults" / "work"
    (vault / ".marginalia").mkdir(parents=True)
    (vault / "marginalia.yaml").write_text("name: work\n")
    (legacy / "marginalia.toml").write_text("marginalia_toml_version = 1\n")
    (legacy / "env").write_text("MARGINALIA_OPENAI_API_KEY=x\n")
    (legacy / "providers.yaml").write_text("{}\n")
    (legacy / "daemon-1.token").write_text("t")

    summary = compat.migrate_legacy_app_home()

    new = _isolated / ".okto-neuron"
    assert summary["status"] == "migrated"
    assert sorted(summary["copied"]) == ["env", "okto-neuron.toml", "providers.yaml"]
    assert (new / "okto-neuron.toml").read_text() == "marginalia_toml_version = 1\n"
    assert (new / "env").read_text() == "MARGINALIA_OPENAI_API_KEY=x\n"
    # The originals and the vault stay exactly where they were.
    assert (legacy / "marginalia.toml").exists()
    assert (vault / "marginalia.yaml").read_text() == "name: work\n"
    assert not (new / "vaults").exists()
    assert not (new / "daemon-1.token").exists()
    pointer = (legacy / "MOVED_TO").read_text()
    assert pointer.splitlines()[0] == str(new)
    assert "NOT moved" in pointer


def test_migration_is_idempotent_and_keeps_existing_destination_files(_isolated: Path) -> None:
    legacy = _isolated / ".marginalia"
    legacy.mkdir()
    (legacy / "env").write_text("OLD=1\n")
    new = _isolated / ".okto-neuron"
    (new / "grafx").mkdir(parents=True)  # the MVP's directory must be left alone
    (new / "grafx" / "data").write_text("mvp")
    (new / "env").write_text("NEW=1\n")

    first = compat.migrate_legacy_app_home()
    second = compat.migrate_legacy_app_home()

    assert first["kept"] == ["env"] and first["copied"] == []
    assert second["kept"] == ["env"]
    assert (new / "env").read_text() == "NEW=1\n"
    assert (new / "grafx" / "data").read_text() == "mvp"


def test_migration_without_legacy_home_is_a_no_op(_isolated: Path) -> None:
    assert compat.migrate_legacy_app_home()["status"] == "no_legacy_home"
    assert not (_isolated / ".okto-neuron").exists()


# -- vault config file ---------------------------------------------------------


def test_new_vault_gets_new_config_name(tmp_path: Path) -> None:
    assert compat.vault_config_path(tmp_path).name == "okto-neuron.yaml"


def test_existing_legacy_vault_config_is_used_in_place(tmp_path: Path) -> None:
    (tmp_path / "marginalia.yaml").write_text("x: 1\n")
    assert compat.vault_config_path(tmp_path) == tmp_path / "marginalia.yaml"
    assert compat.existing_vault_config(tmp_path) == tmp_path / "marginalia.yaml"


def test_new_vault_config_wins_when_both_exist(tmp_path: Path) -> None:
    (tmp_path / "marginalia.yaml").write_text("x: 1\n")
    (tmp_path / "okto-neuron.yaml").write_text("x: 2\n")
    assert compat.vault_config_path(tmp_path) == tmp_path / "okto-neuron.yaml"


# -- HTTP -----------------------------------------------------------------------


def test_both_vault_headers_are_accepted() -> None:
    assert compat.vault_header_value({"x-okto-neuron-vault": "a"}) == "a"
    assert compat.vault_header_value({"x-marginalia-vault": "b"}) == "b"
    assert compat.vault_header_value({"x-okto-neuron-vault": "a", "x-marginalia-vault": "b"}) == "a"
    assert compat.vault_header_value({}) is None


def test_version_payload_emits_both_names() -> None:
    payload = compat.version_payload("0.3.0")
    assert payload == {"okto_neuron_version": "0.3.0", "marginalia_version": "0.3.0"}
    assert compat.version_from_payload({"marginalia_version": "0.2.0"}) == "0.2.0"
    assert compat.version_from_payload(payload) == "0.3.0"


# -- process detection ---------------------------------------------------------


@pytest.mark.parametrize(
    "argv0",
    [
        "/home/u/.local/bin/okto-neuron",
        "okto-neuron.exe",
        r"C:\Users\u\.local\bin\OKTO-NEURON.EXE",
        "/home/u/.local/bin/marginalia",
        r"C:\Users\u\.local\bin\marginalia.exe",
    ],
)
def test_console_script_detection(argv0: str) -> None:
    assert compat.is_console_script(argv0)


def test_console_script_detection_rejects_other_programs() -> None:
    assert not compat.is_console_script("/usr/bin/python3")
    assert not compat.is_console_script("neuron")
    assert compat.CLI_MODULE_NAMES == {"okto_neuron.cli", "marginalia.cli"}


@pytest.mark.parametrize(
    "command",
    [
        ["/opt/py/bin/python3", "-m", "marginalia.cli", "serve", "--port", "8123"],
        ["/opt/py/bin/python3", "-m", "okto_neuron.cli", "serve", "--port=8123"],
        ["/home/u/.local/bin/marginalia", "serve", "--port", "8123"],
    ],
)
def test_legacy_and_new_serve_command_lines_are_recognised(command: list[str]) -> None:
    from okto_neuron.server.lifecycle import _legacy_serve_port

    assert _legacy_serve_port(command) == 8123


# -- entry points --------------------------------------------------------------


def test_both_entry_point_groups_are_read_new_first(monkeypatch: pytest.MonkeyPatch) -> None:
    from importlib.metadata import EntryPoint, EntryPoints

    groups = {
        "okto_neuron.graph_backends": EntryPoints(
            [EntryPoint("shared", "new_pkg:Store", "okto_neuron.graph_backends")]
        ),
        "marginalia.graph_backends": EntryPoints(
            [
                EntryPoint("shared", "old_pkg:Store", "marginalia.graph_backends"),
                EntryPoint("legacy_only", "old_pkg:Other", "marginalia.graph_backends"),
            ]
        ),
    }
    monkeypatch.setattr(compat, "entry_points", lambda group: groups[group])
    found = {
        ep.name: ep.value
        for ep in compat.iter_entry_points(
            compat.GRAPH_BACKENDS_GROUP, compat.LEGACY_GRAPH_BACKENDS_GROUP
        )
    }
    assert found == {"shared": "new_pkg:Store", "legacy_only": "old_pkg:Other"}
    selected = list(
        compat.iter_entry_points(
            compat.GRAPH_BACKENDS_GROUP, compat.LEGACY_GRAPH_BACKENDS_GROUP, name="legacy_only"
        )
    )
    assert [ep.value for ep in selected] == ["old_pkg:Other"]


# -- grep gate: every "marginalia" literal in src is a listed, reasoned exception


def _string_literals(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((node.lineno, node.value))
    return found


def test_no_unlisted_marginalia_string_literal_in_src() -> None:
    src_root = Path(compat.__file__).resolve().parent
    patterns = [re.compile(pattern) for pattern in compat.KEPT_LEGACY_LITERALS]
    violations: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        if path.name == "_compat.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, value in _string_literals(tree):
            covered: set[int] = set()
            for pattern in patterns:
                for match in pattern.finditer(value):
                    covered.update(range(match.start(), match.end()))
            for match in re.finditer("marginalia", value, flags=re.IGNORECASE):
                if not set(range(match.start(), match.end())) <= covered:
                    snippet = value[max(0, match.start() - 30) : match.end() + 30]
                    violations.append(f"{path.relative_to(src_root)}:{lineno}: ...{snippet!r}...")
                    break
    assert not violations, "unlisted marginalia literals:\n" + "\n".join(violations)


def test_importlib_metadata_lookups_use_the_new_distribution_name() -> None:
    src_root = Path(compat.__file__).resolve().parent
    pattern = re.compile(r"""(version|distribution|metadata)\(\s*["']marginalia["']""")
    hits = [
        str(path.relative_to(src_root))
        for path in src_root.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == []


# -- credentials named in config ------------------------------------------------


def test_secret_env_reads_stored_legacy_names_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARGINALIA_OPENAI_API_KEY", "old-key")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert compat.secret_env("MARGINALIA_OPENAI_API_KEY") == "old-key"


def test_secret_env_new_default_name_falls_back_to_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OKTO_NEURON_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("MARGINALIA_OPENAI_API_KEY", "old-key")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert compat.secret_env("OKTO_NEURON_OPENAI_API_KEY") == "old-key"
    assert any(issubclass(w.category, compat.LegacyNameWarning) for w in caught)
    monkeypatch.setenv("OKTO_NEURON_OPENAI_API_KEY", "new-key")
    assert compat.secret_env("OKTO_NEURON_OPENAI_API_KEY") == "new-key"


# -- CLI alias ---------------------------------------------------------------------


def test_legacy_cli_alias_warns_and_runs_the_same_app() -> None:
    from click.testing import CliRunner

    from okto_neuron import __version__
    from okto_neuron.cli import app, legacy_app

    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.splitlines() == [f"okto-neuron {__version__}", compat.BRAND_LINE]

    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", "from okto_neuron.cli import legacy_app; legacy_app()", "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "the 'marginalia' command is now 'okto-neuron'" in proc.stderr
    assert proc.stdout.splitlines()[0] == f"okto-neuron {__version__}"
    assert callable(legacy_app)


def test_cli_prints_legacy_env_warnings_as_one_plain_line() -> None:
    import subprocess
    import sys

    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "MARGINALIA_ENDPOINT": "http://127.0.0.1:9"}
    proc = subprocess.run(
        [sys.executable, "-c", "from okto_neuron.cli import app; app()", "status", "--timeout", "1"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    lines = [line for line in proc.stderr.splitlines() if "MARGINALIA_ENDPOINT" in line]
    assert lines == [
        "warning: MARGINALIA_ENDPOINT is deprecated; set OKTO_NEURON_ENDPOINT instead. "
        "The old name is read until 0.5."
    ], proc.stderr
    assert "_compat_getenv(" not in proc.stderr
