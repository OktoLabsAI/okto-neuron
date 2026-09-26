"""Documentation infrastructure and high-risk interface truth checks."""

from __future__ import annotations

import json
import os
import re
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = runpy.run_path(str(ROOT / "docs" / "build_knowledge_base.py"))
GENERATED_PAGES = [
    ROOT / "docs" / "index.html",
    ROOT / "docs" / "knowledge-base" / "index.html",
    ROOT / "docs" / "rfc.html",
    ROOT / "docs" / "roadmap.html",
]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


_SAFE_GIT_ENV_KEYS = frozenset({"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"})


def _sanitized_git_env() -> dict[str, str]:
    """Environment for git commands run against throwaway fixture repos.

    Strips every ``GIT_*`` variable except a narrow allowlist of
    config-source safety toggles (``GIT_CONFIG_NOSYSTEM`` and friends), so
    a ``GIT_DIR``/``GIT_WORK_TREE`` — or a ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_*``/
    ``GIT_CONFIG_VALUE_*`` triple injecting ``core.worktree``/``core.bare`` —
    exported by an outer git process (e.g. a pre-commit hook that invokes
    pytest from a linked worktree) cannot redirect these commands at that
    real repository instead of the fixture under ``tmp_path``.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") or key in _SAFE_GIT_ENV_KEYS
    }


def _create_precommit_fixture_repo(root: Path, name: str) -> Path:
    """Build a throwaway git repo fixture for pre-commit hook tests.

    Every nested git command (and ``uv lock``) runs with a sanitized
    environment via ``_sanitized_git_env`` so it always targets ``repo``
    under ``root``, never a real repository whose ``GIT_DIR``/``GIT_WORK_TREE``
    leaked into the test process environment.
    """
    uv = shutil.which("uv")
    assert uv is not None

    repo = root / name
    (repo / ".githooks").mkdir(parents=True)
    (repo / "src" / "okto_neuron").mkdir(parents=True)
    (repo / ".githooks" / "pre-commit").write_text(_read(".githooks/pre-commit"), encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "hook-fixture"\nversion = "0.0.1"\n'
        'requires-python = ">=3.12"\ndependencies = []\n',
        encoding="utf-8",
    )
    (repo / "src" / "okto_neuron" / "__init__.py").write_text(
        '__version__ = "0.0.1"\n', encoding="utf-8"
    )
    git_env = _sanitized_git_env()
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "hook@example.invalid"],
        ["git", "config", "user.name", "Hook Fixture"],
        [uv, "lock"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "baseline"],
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True, text=True, env=git_env)
    return repo


def test_durable_source_registry_is_complete_and_unique() -> None:
    GENERATOR["validate_source_registry"]()
    registered = GENERATOR["registered_kb_sources"]()
    assert len(registered) == len(set(registered))
    assert set(registered) == GENERATOR["canonical_kb_sources"]()


def test_roadmap_registry_has_valid_references_and_statuses() -> None:
    GENERATOR["validate_roadmap"]()


def test_roadmap_registry_rejects_blank_ids(tmp_path: Path) -> None:
    (tmp_path / "roadmap.json").write_text(
        json.dumps(
            {
                "milestones": [
                    {"id": "v1", "label": "One", "status": "later"},
                ],
                "tracks": [
                    {
                        "id": " ",
                        "title": "Track",
                        "items": [
                            {
                                "id": None,
                                "title": "Item",
                                "status": "later",
                                "milestone": "v1",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    validator = GENERATOR["validate_roadmap"]
    original_docs = validator.__globals__["DOCS"]
    validator.__globals__["DOCS"] = tmp_path
    try:
        with pytest.raises(RuntimeError, match="missing or blank id"):
            validator()
    finally:
        validator.__globals__["DOCS"] = original_docs


def test_roadmap_renderer_escapes_track_icons(tmp_path: Path) -> None:
    injected = '<img src=x onerror="alert(1)">'
    (tmp_path / "roadmap.json").write_text(
        json.dumps(
            {
                "updated": "test",
                "milestones": [
                    {"id": "v1", "label": "One", "status": "later"},
                ],
                "tracks": [
                    {
                        "id": "track",
                        "title": "Track",
                        "icon": injected,
                        "items": [
                            {
                                "id": "item",
                                "title": "Item",
                                "detail": "Detail",
                                "status": "later",
                                "milestone": "v1",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    renderer = GENERATOR["build_roadmap_page"]
    original_docs = renderer.__globals__["DOCS"]
    renderer.__globals__["DOCS"] = tmp_path
    try:
        rendered = renderer()
    finally:
        renderer.__globals__["DOCS"] = original_docs
    assert injected not in rendered
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in rendered


def test_markdown_renderer_escapes_raw_html() -> None:
    rendered = GENERATOR["md"].render('<script data-test="raw">alert(1)</script>')
    assert "<script" not in rendered
    assert "&lt;script" in rendered


def test_linked_heading_slug_uses_visible_text_not_destination() -> None:
    tokens = GENERATOR["md"].parse("## [API reference](https://example.invalid/secret)\n")
    inline = next(token for token in tokens if token.type == "inline")
    heading = GENERATOR["_heading_text"](inline)
    assert heading == "API reference"
    assert GENERATOR["_github_heading_slug"](heading) == "api-reference"


def test_local_url_rebase_preserves_encoded_path_delimiters() -> None:
    rebase = GENERATOR["_rebase_local_url"]
    assert (
        rebase(
            "assets/a%23b%3Fc.png",
            source_rel="docs/source.md",
            output_rel="docs/knowledge-base/index.html",
        )
        == "../assets/a%23b%3Fc.png"
    )


def test_durable_markdown_sources_have_no_broken_local_links() -> None:
    GENERATOR["validate_markdown_local_links"]()


def test_generated_pages_have_no_broken_local_links() -> None:
    GENERATOR["validate_generated_local_links"](GENERATED_PAGES)


def test_generated_pages_match_their_live_sources_byte_for_byte() -> None:
    expected = {
        ROOT / "docs" / "index.html": GENERATOR["build_hub"](),
        ROOT / "docs" / "knowledge-base" / "index.html": GENERATOR["build_kb"](),
        ROOT / "docs" / "rfc.html": GENERATOR["build_doc_page"](
            "RFC.md", "RFC v1.0", "design bible · rendered from RFC.md"
        ),
        ROOT / "docs" / "roadmap.html": GENERATOR["build_roadmap_page"](),
    }
    stale = [
        path.relative_to(ROOT).as_posix()
        for path, content in expected.items()
        if path.read_text(encoding="utf-8") != content
    ]
    assert stale == [], (
        "generated documentation is stale; run "
        f"`uv run python docs/build_knowledge_base.py`: {stale}"
    )


def test_generated_link_gate_checks_cross_file_html_fragments(tmp_path: Path) -> None:
    page = tmp_path / "page.html"
    target = tmp_path / "target.html"
    target.write_text('<h1 id="present">Present</h1>', encoding="utf-8")
    validator = GENERATOR["validate_generated_local_links"]
    original_root = validator.__globals__["ROOT"]
    validator.__globals__["ROOT"] = tmp_path
    try:
        page.write_text('<a href="target.html#present">good</a>', encoding="utf-8")
        validator([page])

        page.write_text('<a href="target.html#missing">bad</a>', encoding="utf-8")
        with pytest.raises(RuntimeError, match="target.html#missing"):
            validator([page])
    finally:
        validator.__globals__["ROOT"] = original_root


def test_interface_docs_follow_the_live_seven_view_navigation() -> None:
    app = _read("frontend/src/App.tsx")
    labels = re.findall(r"\{ id: '[^']+', label: '([^']+)', icon:", app)
    expected = ["Query", "Add", "Logs", "Browse", "Graph", "Curation", "Config"]
    assert labels == expected

    for relative in ("README.md", "docs/web-ui.md", "frontend/README.md"):
        text = _read(relative)
        for label in expected:
            assert label in text, f"{relative} omits the live {label} view"
    assert "Three views" not in _read("frontend/README.md")


def test_frontend_vault_selection_isolates_view_state_and_query_history() -> None:
    app = _read("frontend/src/App.tsx")
    mock = _read("frontend/src/lib/mock.ts")
    query = _read("frontend/src/components/query/QueryView.tsx")
    web_ui = _read("docs/web-ui.md")

    # The API's `current` value is a compatibility fallback, not browser state.
    # Opening/reloading a tab always presents the manager until that tab makes
    # an explicit selection; even one registered vault is not auto-selected.
    assert "useApp.getState().selectedVaultPath" in app
    assert "body.current?.path" not in app
    assert "body.vaults.length === 1" not in app
    assert "switchVault:" not in mock
    assert "key={`${current?.path ?? 'no-vault'}:${dataVersion}`}" in app
    assert "THREAD_STORAGE_PREFIX = 'okto-neuron.query.thread.v2'" in query
    assert "encodeURIComponent(vaultPath)" in query
    assert "window.localStorage.removeItem(LEGACY_THREAD_STORAGE_KEY)" in query
    assert "useApp((state) => state.selectedVaultPath)" in query
    assert "browser-local and keyed by resolved vault path" in web_ui
    assert "remounts the visible view" in web_ui


def test_frontend_vault_copy_uses_selection_and_per_runtime_watch_semantics() -> None:
    app = _read("frontend/src/App.tsx")
    logs = _read("frontend/src/components/logs/IngestLogsView.tsx")
    types = _read("frontend/src/types/index.ts")
    service = _read("frontend/src/services/folder-watch-api.ts")

    assert "No vault selected" in app
    for relative, text in {
        "App.tsx": app,
        "IngestLogsView.tsx": logs,
        "types/index.ts": types,
        "folder-watch-api.ts": service,
    }.items():
        lowered = text.lower()
        assert "no active vault" not in lowered, relative
        assert "non-active vault" not in lowered, relative
        assert "single-drain" not in lowered, relative
    assert "const anyWatching" in logs
    assert "request-bound vault runtime's okto-neuron.yaml" in service


def test_mcp_ask_docs_preserve_the_default_block_path() -> None:
    runtime = _read("src/okto_neuron/server/runtime.py")
    companion = _read("src/okto_neuron/companion/__init__.py")
    readme = _read("README.md")
    web_ui = _read("docs/web-ui.md")
    readme_flat = " ".join(readme.split())
    web_ui_flat = " ".join(web_ui.split())

    # Flattened so the assertion pins the DEFAULTS (k=20, hops=1), not the
    # formatter's line wrapping of the signature.
    runtime_flat = " ".join(runtime.split())
    assert "def ask( question: str, k: int = 20, hops: int = 1," in runtime_flat
    assert "policy = AskRetrievalPolicy(hops=" in runtime
    assert "Default to the vault's configured retrieval mode (block)" in runtime
    assert "else bool(cfg.llm.ask.enable_subgraph)" in companion
    assert "source-block context by default" in readme_flat
    assert "Graph-native subgraph assembly is opt-in" in readme_flat
    assert "production default synthesizes over retrieved" in web_ui_flat
    assert "subgraph path remains opt-in" in web_ui_flat


def test_current_pack_docs_do_not_claim_a_runtime_manifest_plugin_surface() -> None:
    readme = _read("README.md")
    rfc = _read("RFC.md")
    understanding = _read("docs/understanding/index.html")
    generator = _read("docs/build_knowledge_base.py")
    registry = _read("src/okto_neuron/packs/registry.py")

    assert "pluggable type packs" not in readme
    assert "external manifest packs are not a production plug-in surface" in readme
    assert "community-extensible surface" not in rfc
    assert "exactly four graph-native tools" not in rfc
    assert "type packs</i> that extend these" not in understanding
    assert "support types 6 → 5" not in generator
    assert "if n not in BUILTIN" in registry


def test_mock_mode_is_explicit_outside_vite_development() -> None:
    mode = _read("frontend/src/lib/mode.ts")
    frontend_readme = _read("frontend/README.md")
    web_ui = _read("docs/web-ui.md")

    assert "if (import.meta.env.DEV) return true" in mode
    assert ".get('mock') === '1'" in mode
    assert "never a production fallback" in frontend_readme
    assert "`?mock=1`" in frontend_readme
    assert "`?mock=1`" in web_ui


def test_config_patch_applied_contract_has_no_stale_restart_variant() -> None:
    server = _read("src/okto_neuron/server/http.py")
    mock = _read("frontend/src/lib/mock.ts")
    frontend_types = _read("frontend/src/types/index.ts")
    service = _read("frontend/src/services/config-api.ts")
    frontend_readme = _read("frontend/README.md")
    web_ui = _read("docs/web-ui.md")

    assert 'applied = "live"' in server
    assert 'applied = "reembed"' in server
    assert 'applied = "restart"' not in server
    assert "export type AppliedKind = 'live' | 'reembed'" in frontend_types
    assert 'applied:"live"|"reembed"' in service
    assert "applied: live|reembed" in frontend_readme
    assert '`applied: "live"`' in web_ui
    assert '`applied: "reembed"`' in web_ui
    for field in ("embedding.provider", "embedding.model", "embedding.dimension"):
        assert field in mock


def test_roadmap_has_only_current_release_items_and_future_product_backlog() -> None:
    roadmap = json.loads(_read("docs/roadmap.json"))
    items = [
        (track["id"], item["id"], item["status"])
        for track in roadmap["tracks"]
        for item in track["items"]
    ]

    doing = [f"{track_id}/{item_id}" for track_id, item_id, status in items if status == "doing"]
    assert doing == [
        "corrective-release/current-public-0200-evidence",
        "core/pluggable-graph-backend",
        "graph-quality/adr0040-semantic-graph-quality",
    ]

    milestones = {entry["id"]: entry for entry in roadmap["milestones"]}
    assert milestones["v0.0.40"]["status"] == "done"
    assert "ineligible for promotion" in milestones["v0.0.40"]["target"]
    assert milestones["v0.0.41"]["status"] == "done"
    assert "ineligible for promotion" in milestones["v0.0.41"]["target"]
    assert milestones["v0.0.42"]["status"] == "done"
    assert "superseded by 0.0.43" in milestones["v0.0.42"]["target"]
    assert milestones["v0.0.43"]["status"] == "done"
    assert "superseded" in milestones["v0.0.43"]["target"]
    assert milestones["v0.0.44"]["status"] == "done"
    assert "superseded" in milestones["v0.0.44"]["target"]
    assert milestones["v0.0.45"]["status"] == "done"
    assert "superseded" in milestones["v0.0.45"]["target"]
    assert milestones["v0.0.46"]["status"] == "done"
    assert "superseded" in milestones["v0.0.46"]["target"]
    assert milestones["v0.0.47"]["status"] == "done"
    assert "superseded" in milestones["v0.0.47"]["target"]
    assert milestones["v0.0.48"]["status"] == "done"
    assert "superseded" in milestones["v0.0.48"]["target"]
    assert milestones["v0.0.49"]["status"] == "done"
    assert "superseded" in milestones["v0.0.49"]["target"]
    assert milestones["v0.0.50"]["status"] == "done"
    assert "prerelease" in milestones["v0.0.50"]["target"]
    assert "superseded" in milestones["v0.0.50"]["target"]
    assert milestones["v0.1.0"]["status"] == "done"
    assert "prerelease" in milestones["v0.1.0"]["target"]
    assert "superseded" in milestones["v0.1.0"]["target"]
    assert milestones["v0.2.0"]["status"] == "done"
    assert "prerelease" in milestones["v0.2.0"]["target"]
    assert "Windows" in milestones["v0.2.0"]["target"]
    assert "snapshot race" in milestones["v0.2.0"]["target"]
    assert milestones["corrective-release"]["status"] == "doing"

    release_items = {
        item["id"]: item
        for track in roadmap["tracks"]
        if track["id"] == "corrective-release"
        for item in track["items"]
    }
    assert release_items["artifact-functional-security-repair"]["milestone"] == "v0.0.41"
    assert release_items["artifact-functional-security-repair"]["status"] == "done"
    assert release_items["successor-publication-and-linux"]["milestone"] == "v0.0.41"
    assert release_items["successor-publication-and-linux"]["status"] == "done"
    assert "windows-and-promote" not in release_items
    successor = release_items["successor-authorization-and-release"]
    assert successor["milestone"] == "v0.0.42"
    assert successor["status"] == "done"
    for item_id, milestone in (
        ("current-public-0043-evidence", "v0.0.43"),
        ("current-public-0044-evidence", "v0.0.44"),
        ("current-public-0047-evidence", "v0.0.47"),
        ("current-public-0048-evidence", "v0.0.48"),
        ("current-public-0049-evidence", "v0.0.49"),
        ("current-public-0050-evidence", "v0.0.50"),
        ("current-public-0100-evidence", "v0.1.0"),
    ):
        assert release_items[item_id]["milestone"] == milestone
        assert release_items[item_id]["status"] == "done"
    current_public = release_items["current-public-0200-evidence"]
    assert current_public["milestone"] == "v0.2.0"
    assert current_public["status"] == "doing"

    # Public roadmap evidence is version and date only (S2): the exact source
    # identities, run ids and CI notes live in the internal release ledger.
    evidence_shape = re.compile(r"^[a-z0-9.-]+( · \d{4}-\d{2}-\d{2})?$")
    for track in roadmap["tracks"]:
        for item in track["items"]:
            assert evidence_shape.match(item["evidence"]), (item["id"], item["evidence"])
    raw = _read("docs/roadmap.json")
    assert "billing" not in raw.lower()
    assert "payments have failed" not in raw

    web_ui_items = {
        item["id"]: item
        for track in roadmap["tracks"]
        if track["id"] == "web-ui"
        for item in track["items"]
    }
    for item_id in ("application-daemon-multivault", "managed-provider-credentials"):
        assert web_ui_items[item_id]["status"] == "done"
        assert web_ui_items[item_id]["milestone"] == "corrective-release"

    unfinished_product_items = [
        (track_id, item_id, status)
        for track_id, item_id, status in items
        if status != "done"
        and item_id
        not in {
            "current-public-0200-evidence",
            "adr0040-semantic-graph-quality",
            "pluggable-graph-backend",
        }
    ]
    assert unfinished_product_items
    assert all(status in {"next", "later"} for _, _, status in unfinished_product_items)


def test_corrective_release_discloses_privacy_neutral_compatibility_breaks() -> None:
    budgets = _read("src/okto_neuron/budgets.py")
    cli = _read("src/okto_neuron/cli/__init__.py")
    readme = _read("README.md")
    rfc = _read("RFC.md")
    roadmap = json.loads(_read("docs/roadmap.json"))
    release_item = next(
        item
        for track in roadmap["tracks"]
        if track["id"] == "corrective-release"
        for item in track["items"]
        if item["id"] == "artifact-functional-security-repair"
    )

    assert "CORPUS_BUDGET_SECONDS" in budgets
    assert 'suite == "corpus"' in budgets
    assert '@app.command("pilot")' in cli
    assert 'f"pilot-log.{timestamp.date().isoformat()}.json"' in cli

    for text in (readme, rfc, release_item["detail"]):
        normalized = " ".join(text.split())
        assert "privacy-neutralizing" in normalized
        assert "CORPUS_BUDGET_SECONDS" in normalized
        assert "corpus" in normalized
        assert "pilot-log.YYYY-MM-DD.json" in normalized
        assert "must migrate" in normalized


def test_current_surfaces_do_not_advertise_retired_cli_commands() -> None:
    for relative in ("README.md", "RFC.md", "docs/web-ui.md"):
        text = _read(relative)
        assert re.search(r"\bkg\s+kg\b", text) is None, f"{relative} advertises nested kg"
        assert "kg migrate bridge-edges" not in text, f"{relative} advertises retired migration"












def test_readme_documents_direct_wheel_feature_boundaries() -> None:
    readme = _read("README.md")
    assert "From 0.3.0 the installer and its SHA-256 release manifest live in this repository" in " ".join(readme.split())
    assert "[serve]` is the preferred" in readme
    assert "`[mcp]` is an\nidentical compatibility alias" in readme
    assert "`[jsonld]`, which supplies RDFLib" in readme


def test_current_release_surfaces_do_not_overstate_or_retain_closed_drift() -> None:
    readme = _read("README.md")
    readme_normalized = " ".join(readme.split())
    rfc = _read("RFC.md")

    assert (
        "The direct-wheel contract below describes the released `0.2.0` wheel"
        in readme_normalized
    )
    assert (
        "does **not** retroactively describe the immutable public predecessor wheels"
        in readme_normalized
    )
    assert "The repaired base wheel supports" in readme

    surface_api = rfc.split("### 4.6 Surface APIs", 1)[1].split("### 4.7 Ingest pipeline", 1)[0]
    assert "examplecorp" not in surface_api.lower()
    assert "JP" not in surface_api

    for workflow_path in (
        ".github/workflows/eval-gate.yml",
        ".github/workflows/model-free-tests.yml",
    ):
        workflow = _read(workflow_path)
        assert "enable branch protection" not in workflow
        assert "exact recorded source SHA" in workflow


def test_eval_gate_avoids_anonymous_xet_for_the_frozen_model() -> None:
    workflow = _read(".github/workflows/eval-gate.yml")
    floor = workflow.split(
        "      - name: Deterministic floor — hard-recall@k + "
        "extraction-completeness (frozen vault)\n",
        1,
    )[1].split("      - name:", 1)[0]

    assert 'HF_HUB_DISABLE_XET: "1"' in floor
    assert "recall_floor.py gate" in floor


def test_release_identity_and_open_gates_agree_across_current_state_docs() -> None:
    # Public docs carry release history by version and date only (S2): no
    # private source SHAs, no hosted-CI billing text. The exact identities live
    # in the internal release ledger.
    distribution_sha = "6d9c1d1c604331773d06fadbe8770e04432bfb4c"

    readme_raw = _read("README.md")
    readme = " ".join(readme_raw.lower().split())
    rfc_raw = _read("RFC.md")
    rfc = " ".join(rfc_raw.lower().split())
    roadmap_raw = _read("docs/roadmap.json")
    roadmap = " ".join(roadmap_raw.lower().split())
    onboarding = " ".join(_read("docs/onboarding-plan.md").lower().split())

    for text in (readme_raw, rfc_raw, roadmap_raw):
        assert re.search(r"\bsource `?[0-9a-f]{7,40}\b", text) is None
    for text in (readme, rfc, roadmap):
        assert "billing" not in text
        assert "payments have failed" not in text
    assert "| 0.2.0 | 2026-09-23 | prerelease." in readme
    assert "| 0.0.43 | 2026-07-14 | the last release published as stable. |" in readme
    assert "| 0.0.41 | 2026-07-14 | prerelease, not eligible for promotion" in readme
    assert "| 0.0.40 | 2026-07-13 | not eligible for promotion. |" in readme
    assert "no published version has passed a real interactive windows powershell 5.1" in readme
    assert "no published version has yet passed a real interactive windows powershell 5.1" in rfc
    assert "permanently non-promotable" in rfc
    assert distribution_sha in onboarding

    for label, text in {"roadmap": roadmap, "onboarding": onboarding}.items():
        assert "0.0.41" in text, f"{label} lost the immutable 0.0.41 identity"
        assert "non-promotable" in text, f"{label} does not reject 0.0.41 promotion"
        assert "os.fchmod" in text, f"{label} omits the missing-fchmod defect"
        assert "locked pid payload byte" in text, f"{label} omits the locked-byte defect"
        assert "venv launcher pid" in text, f"{label} omits the launcher/runtime PID defect"
        assert "not release evidence" in text, f"{label} overstates the repair smoke"
        assert "0.0.42" in text, f"{label} does not name the authorized successor"
        assert "authorized" in text, f"{label} does not record release authorization"
        assert "only remaining release task" not in text, f"{label} retains the obsolete TODO"

    assert "windows-and-promote" not in roadmap


def test_current_durable_sources_do_not_contain_personalized_home_paths() -> None:
    # Internal agent instructions and research live in tests/test_docs_integrity_internal.py.
    candidates = [ROOT / "README.md", ROOT / "RFC.md"]
    candidates.extend((ROOT / "docs").rglob("*.md"))

    personalized = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/")
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in candidates
        if personalized.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_instruction_docs_describe_operational_exact_sha_enforcement() -> None:
    # The agent-instruction half of this check (CLAUDE.md, AGENTS.md,
    # .claude/commands) is in tests/test_docs_integrity_internal.py.
    paths = (".github/workflows/docs-gate.yml", ".githooks/pre-commit")
    combined = "\n".join(_read(path) for path in paths)

    assert "UN-BYPASSABLE" not in combined
    assert "Un-bypassable" not in combined

    for path in (".github/workflows/docs-gate.yml", ".githooks/pre-commit"):
        text = _read(path)
        for marker in (
            "frontend/README",
            "docs/plans/",
            "docs/backends/",
        ):
            assert marker in text, f"{path} omits durable source scope marker {marker}"
        assert "tests/test_docs_integrity.py" in text

    hook = _read(".githooks/pre-commit")
    assert "uv lock --check" in hook
    assert ".venv/Scripts/python.exe" in hook


def test_docs_gates_use_nul_safe_boolean_change_classification() -> None:
    workflow = _read(".github/workflows/docs-gate.yml")
    hook = _read(".githooks/pre-commit")

    assert '"--name-only", "-z"' in workflow
    assert "src_changed<<" not in workflow
    assert "docs_changed<<" not in workflow
    assert 'output.write(f"src_changed={str(bool(src_paths)).lower()}\\n")' in workflow
    assert "package_init_is_version_only_change" in workflow
    assert "version_line.sub(marker, before) == version_line.sub(marker, after)" in workflow

    assert "--name-only -z" in hook
    assert "package_init_is_version_only_change" in hook
    assert "pyproject.toml|uv.lock" in hook
    assert "path_is_validation_input" in hook
    assert "return 1" in hook
    assert '[ -z "$pyproj_v" ] || [ -z "$init_v" ]' in hook


def test_precommit_enforces_lock_and_narrow_version_only_exemption(
    tmp_path: Path,
) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    git_env = _sanitized_git_env()

    def create_repo(name: str) -> Path:
        return _create_precommit_fixture_repo(tmp_path, name)

    stale_lock = create_repo("stale-lock")
    (stale_lock / "pyproject.toml").write_text(
        '[project]\nname = "hook-fixture"\nversion = "0.0.1"\n'
        'requires-python = ">=3.12"\ndependencies = ["click>=8"]\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "pyproject.toml"], cwd=stale_lock, check=True, env=git_env)
    result = subprocess.run(
        ["bash", ".githooks/pre-commit"],
        cwd=stale_lock,
        capture_output=True,
        text=True,
        check=False,
        env=git_env,
    )
    assert result.returncode != 0
    assert "uv.lock does not match pyproject.toml" in result.stderr

    version_only = create_repo("version-only")
    (version_only / "pyproject.toml").write_text(
        '[project]\nname = "hook-fixture"\nversion = "0.0.2"\n'
        'requires-python = ">=3.12"\ndependencies = []\n',
        encoding="utf-8",
    )
    init_path = version_only / "src" / "okto_neuron" / "__init__.py"
    init_path.write_text('__version__ = "0.0.2"\n', encoding="utf-8")
    subprocess.run(
        [uv, "lock"], cwd=version_only, check=True, capture_output=True, text=True, env=git_env
    )
    subprocess.run(
        ["git", "add", "pyproject.toml", "uv.lock", "src/okto_neuron/__init__.py"],
        cwd=version_only,
        check=True,
        env=git_env,
    )
    (version_only / "notes.tmp").write_text("unrelated local state\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", ".githooks/pre-commit"],
        cwd=version_only,
        capture_output=True,
        text=True,
        check=False,
        env=git_env,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    readme_path = version_only / "README.md"
    readme_path.write_text("unstaged validation input\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", ".githooks/pre-commit"],
        cwd=version_only,
        capture_output=True,
        text=True,
        check=False,
        env=git_env,
    )
    assert result.returncode != 0
    assert "validation snapshot is only partially staged" in result.stderr
    readme_path.unlink()

    init_path.write_text(
        '__version__ = "0.0.2"\n\ndef runtime_change() -> None:\n    pass\n',
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "src/okto_neuron/__init__.py"], cwd=version_only, check=True, env=git_env
    )
    result = subprocess.run(
        ["bash", ".githooks/pre-commit"],
        cwd=version_only,
        capture_output=True,
        text=True,
        check=False,
        env=git_env,
    )
    assert result.returncode != 0
    assert "source is landing with no docs source update" in result.stderr


def test_precommit_fixture_repo_ignores_leaked_git_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GIT_DIR/GIT_WORK_TREE leaked into the test process environment — as
    happens when a pre-commit hook invokes pytest from a linked worktree —
    must not redirect _create_precommit_fixture_repo's nested git commands
    at that real repository instead of the fixture under tmp_path.
    """
    # Sanitized even for this setup: if the guard test itself runs under the
    # exact leak scenario it exists to catch (pytest invoked by a pre-commit
    # hook with GIT_DIR/GIT_WORK_TREE already exported), building the decoy
    # fixture must not corrupt whatever repo that ambient env points at.
    git_env = _sanitized_git_env()
    decoy = tmp_path / "decoy-real-repo"
    decoy.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(decoy)],
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    )
    for command in (
        ["git", "config", "user.email", "decoy@example.invalid"],
        ["git", "config", "user.name", "Decoy"],
    ):
        subprocess.run(command, cwd=decoy, check=True, capture_output=True, text=True, env=git_env)
    (decoy / "keep-me.txt").write_text("do not touch\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."], cwd=decoy, check=True, capture_output=True, text=True, env=git_env
    )
    subprocess.run(
        ["git", "commit", "-qm", "decoy initial"],
        cwd=decoy,
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    )
    before_log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=decoy,
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    ).stdout
    before_config = (decoy / ".git" / "config").read_text(encoding="utf-8")

    fixtures_root = tmp_path / "fixtures"
    fixtures_root.mkdir()

    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))

    repo = _create_precommit_fixture_repo(fixtures_root, "leak-guard")

    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)

    assert (repo / ".git").is_dir(), "fixture repo must own its own .git directory"
    fixture_log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    ).stdout
    assert "baseline" in fixture_log, "fixture commit must land inside tmp_path, not the decoy"

    after_log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=decoy,
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    ).stdout
    assert after_log == before_log, "decoy repo history must be untouched"
    assert "baseline" not in after_log, "fixture commit must not land in the decoy repo"
    assert (decoy / "keep-me.txt").exists(), "decoy repo working tree must be untouched"

    # The exact corruption signature from the original incident: an
    # unsanitized `git config user.email`/`user.name` mutates the decoy's
    # .git/config in place, even if a later step in the fixture build fails.
    after_config = (decoy / ".git" / "config").read_text(encoding="utf-8")
    assert after_config == before_config, "decoy .git/config must be untouched"
    assert "hook@example.invalid" not in after_config
    assert "Hook Fixture" not in after_config


def test_required_workflows_refuse_lockfile_drift() -> None:
    workflows = (
        ".github/workflows/docs-gate.yml",
        ".github/workflows/eval-gate.yml",
        ".github/workflows/model-free-tests.yml",
        ".github/workflows/release-artifact-gate.yml",
    )
    for path in workflows:
        assert "uv lock --check" in _read(path), f"{path} can silently refresh uv.lock"

    # Both model-free jobs provision their own isolated runner.
    assert _read(".github/workflows/model-free-tests.yml").count("uv lock --check") == 2


def test_release_artifact_gate_runs_repository_browser_smoke_against_exact_wheel() -> None:
    workflow = _read(".github/workflows/release-artifact-gate.yml")
    package = json.loads(_read("frontend/package.json"))
    browser_smoke = _read("frontend/tests/release-smoke.mjs")

    assert package["scripts"]["test:release-browser"] == "node tests/release-smoke.mjs"
    assert (
        "OKTO_NEURON_BROWSER_SMOKE_CLI: ${{ runner.temp }}/okto-neuron-wheel-venv/bin/okto-neuron"
    ) in workflow
    assert "npm --prefix frontend run test:release-browser" in workflow
    assert "process.env.OKTO_NEURON_BROWSER_SMOKE_CLI || defaultCli" in browser_smoke
    assert "for (const inherited of Object.keys(daemonEnv))" in browser_smoke
    assert "inherited.startsWith('OKTO_NEURON_')" in browser_smoke
    assert "must receive only suite-owned Okto Neuron variables" in browser_smoke


def test_windows_managed_credentials_gate_uses_dpapi_across_fresh_processes() -> None:
    workflow = _read(".github/workflows/release-artifact-gate.yml")
    windows_job = workflow.split("  windows-managed-credentials:\n", 1)[1]

    assert "runs-on: windows-latest" in windows_job
    assert "Verify exact-wheel DPAPI round-trip in fresh processes" in windows_job
    assert "uv build --wheel" in windows_job
    assert "uv pip install --python $python $wheel" in windows_job
    assert windows_job.count("& $python -c `") == 2
    assert "write_user_env_secret" in windows_job
    assert "load_user_env_file" in windows_job
    assert '$stored.Contains("$envName=dpapi-v1:")' in windows_job
    assert "$stored.Contains($secret)" in windows_job
    assert 'Remove-Item "Env:$envName" -ErrorAction SilentlyContinue' in windows_job
    assert windows_job.count('Test-Path "Env:$envName"') == 2
    assert "fresh writer process failed" in windows_job
    assert "fresh reader process failed to recover the DPAPI credential" in windows_job


def test_windows_artifact_gate_runs_exact_wheel_daemon_lifecycle() -> None:
    workflow = _read(".github/workflows/release-artifact-gate.yml")
    windows_job = workflow.split("  windows-managed-credentials:\n", 1)[1]

    assert "Verify exact-wheel Windows daemon lifecycle" in windows_job
    assert "shell: powershell" in windows_job
    assert '"$($wheels[0].FullName)[serve]"' in windows_job
    assert "& $okto-neuron serve --daemon --no-open" in windows_job
    assert "& $okto-neuron status --json --timeout 30" in windows_job
    assert "& $okto-neuron stop --timeout 30" in windows_job


def test_source_ci_records_lint_and_workflow_syntax_evidence() -> None:
    workflow = _read(".github/workflows/model-free-tests.yml")
    assert "uv run ruff check src tests" in workflow
    assert "github.com/rhysd/actionlint/cmd/actionlint@v1.7.12" in workflow
    assert "actions/setup-go@4a3601121dd01d1626a1e23e37211e3254c1c06c" in workflow
    assert 'go-version: "1.25.0"' in workflow


def test_required_workflows_pin_third_party_actions_to_commits() -> None:
    workflows = (
        ".github/workflows/docs-gate.yml",
        ".github/workflows/eval-gate.yml",
        ".github/workflows/model-free-tests.yml",
        ".github/workflows/release-artifact-gate.yml",
    )
    mutable_action = re.compile(
        r"^\s*uses:\s*[^#\s]+@(v\d+|main|master)\s*(?:#.*)?$",
        re.MULTILINE,
    )
    for path in workflows:
        assert mutable_action.search(_read(path)) is None, f"{path} uses a mutable action ref"






def test_understanding_verification_stamp_matches_its_guide() -> None:
    guide = _read("docs/understanding/README.md")
    page = _read("docs/understanding/index.html")
    guide_date = re.search(r"verified against the live code on (\d{4}-\d{2}-\d{2})", guide)
    page_date = re.search(r"verified vs code · (\d{4}-\d{2}-\d{2})", page)
    assert guide_date is not None
    assert page_date is not None
    assert guide_date.group(1) == page_date.group(1)
    assert "ADR 0019 · proposed" not in page
    assert "the next lever is a confidence-based coverage gate" not in page
    assert "122/142" in page
    assert "6.24× fewer context tokens" in page
    assert "Every configured vault owns an immutable runtime and drain worker" in page
    assert "the v1 drain worker targets the <b>active</b> vault only" not in page
