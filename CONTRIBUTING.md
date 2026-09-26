# Contributing to Okto Neuron

Thanks for your interest in contributing. Okto Neuron is a local-first
knowledge graph with a CLI (`okto-neuron`, plus the `kg` graph commands), a
web UI, and an MCP server, all in this repository.

## Before you start

- **Search existing issues and PRs** to avoid duplicate work.
- **For anything non-trivial** (new features, breaking changes, architecture
  changes), open an issue first to discuss the approach before writing code.
  Schema changes (the five primitives and six support types) need an ADR
  under `docs/adr/` before any code.
- **Small fixes** (typos, docs, obvious bugs) can go straight to a PR.

## Contributor License Agreement

By submitting a pull request to this repository, you agree to the terms in
[`CLA.md`](./CLA.md). No separate signature step is required; opening the
PR indicates agreement.

## Development setup

Python 3.12 and [uv](https://docs.astral.sh/uv/) are required. uv owns the
environment through `uv.lock`; there is no pip entry path.

```bash
git clone https://github.com/OktoLabsAI/okto-neuron.git
cd okto-neuron

uv sync
uv run okto-neuron serve    # REST + web UI on :7777, MCP on :8201
```

Frontend (only needed if you're changing the UI):

```bash
cd frontend
npm ci
npm run dev          # local dev server
npm run typecheck
npm run build        # writes ../frontend_dist, which the wheel ships
cd ..
```

Commit the rebuilt `frontend_dist/` together with any UI source change.

## Making a change

1. Create a branch off `main` (don't commit directly to `main`).
2. Keep the change focused: one logical change per PR.
3. Match the existing code style in the files you touch; don't reformat
   unrelated code.
4. Add or update tests for behavior you change.
5. Update the docs the change affects (`README.md`, `docs/`, `site-docs/`).
   The generated pages under `docs/` are rebuilt with
   `uv run python docs/build_knowledge_base.py`; never edit them by hand.

## Running tests

```bash
uv run ruff check src tests
uv run pytest \
  -m "not slow and not realmodel and not perf and not footprint and not acceptance_private_corpus and not acceptance_judge" \
  -q
uv run pytest tests/test_smoke.py::test_name -xvs   # one test
./bin/acceptance.sh                                 # end-to-end scenarios, no mocks
```

Markers such as `slow`, `realmodel`, `perf` and `footprint` are declared in
`pyproject.toml` and excluded from the default selection above; run them
explicitly when your change touches what they cover. `realmodel` needs a
configured model endpoint.

## Commit messages

Write clear, descriptive commit messages explaining *why*, not just *what*.
Reference related issues where relevant (e.g. `Fixes #123`).

## Pull requests

- Describe what changed and why.
- Link any related issue.
- Make sure ruff, the pytest selection above and (if you touched the
  frontend) `npm run typecheck` and `npm run build` pass locally before
  requesting review.
- A maintainer will review and may ask for changes. PRs that go stale
  without activity may be closed.

## Reporting bugs

Open a GitHub issue with:

- What you expected to happen vs. what actually happened.
- Steps to reproduce.
- Version (`okto-neuron --version`), OS, the graph backend, and how you
  installed it (one-line installer, PyPI, or source).

## Reporting security vulnerabilities

**Do not open a public issue for security vulnerabilities.** Follow the
process in [`SECURITY.md`](./SECURITY.md) instead.

## License

By contributing, you agree that your contributions will be licensed under
the same license as the project (see [`LICENSE`](./LICENSE)), subject to the
[CLA](./CLA.md).
