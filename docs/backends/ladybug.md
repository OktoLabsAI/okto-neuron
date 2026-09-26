# Ladybug backend

Ladybug was Okto Neuron's original default `GraphStore` backend: a
single-writer, server-side-schema embedded graph store, persisted as one
`graph.lbug` file per vault. It requires no network access and never needed
a `--accept-experimental` flag. An owner decision made Okto Grafx the
default backend instead (see `docs/backends/grafx.md`); Ladybug remains a
fully supported, selectable backend, not deprecated or removed.

Ladybug is also still the resolved backend for any *legacy* vault: a
`okto-neuron.yaml` with no `storage` key at all (predating any backend being
pinnable) always resolves to `ladybug`, regardless of what a fresh vault's
default is. `kg init` with no `--backend` flag on a genuinely new vault also
still produces this legacy, storage-key-less shape — it is the one creation
surface that does not pin the new `grafx` default explicitly (see
`docs/backends/grafx.md`'s "Selecting it" section).

## Selecting it

Ladybug is no longer the default, so name it explicitly:

```bash
okto-neuron init --backend ladybug
kg init --backend ladybug
```

## Operational shape

- **On-disk file.** A Ladybug vault has a single `graph.lbug` file at the
  vault root — the artifact acceptance scenarios check directly (existence
  and minimum size) as part of their pass criteria.
- **Concurrency model.** Single-writer: writes are serialized, so there is no
  optimistic-conflict retry loop to configure (unlike the Grafx backend's
  `storage.retry`).
- **Checkpoint.** `checkpoint()` performs real WAL-merge work
  (`checkpoint_is_noop=False` in `BackendCapabilities`).
- **Rebuild/heal/reembed/rollback.** All curation verbs, including REST
  rollback, are fully supported against Ladybug's single-file swap.

See `docs/backends/grafx.md` for how the default Grafx backend differs.

## Visibility

Every surface that lists vaults shows each vault's resolved graph backend,
using this same legacy absent-`storage`-key-resolves-to-`ladybug` rule:

- `okto-neuron vault list` (plain text and `--json`) and `okto-neuron status`
  print a `Backend:`/`[backend]` value per vault.
- `GET /api/v1/vaults` and `GET /api/v1/status` include a `"backend"` field
  on each vault entry (and `GET /api/v1/status`'s per-scope payload).
- The web UI's vault manager shows a backend badge next to each vault name.

All four surfaces resolve the backend through one shared function,
`vault_registry.resolve_vault_backend()`, which itself delegates to
`store/vault.py`'s `_read_pinned_backends()` — the same resolver every vault
open path already reads the pin through — so the legacy-default rule is
defined in exactly one place.
