# Okto Neuron Web UI

Built SPA (React 18 + Vite + TS + Tailwind 3 + Zustand 5, graph viz via `@xyflow/react`),
served by Okto Neuron's Starlette server. Talks to the backend strictly via the
`/api/v1/*` REST contract documented in [`../docs/web-ui.md`](../docs/web-ui.md) and
implemented by `src/okto_neuron/server/http.py`, with one service module per domain
under `src/services/`.

The app opens on its vault manager. Each browser tab chooses its own vault and sends that
selection on every vault-scoped request; daemon defaults and sole-vault compatibility fallback
never select a browser tab implicitly. Creating a vault selects it locally, while deleting the
selected managed vault returns the tab to the manager. Background work remains bound to the
immutable runtime where it started.

## Seven views

- **Query** (`components/query`) — chat-style `ask` + `recall`; renders answer text,
  citations, and result hits with byte-range provenance `path[start:end]`.
- **Add** (`components/ingest`) — paste, upload, or enqueue a server-local folder through
  the restart-durable ingest queue.
- **Logs** (`components/logs`) — inspect queue events and durable candidate-ledger runs;
  this is observability, not a second ingest path.
- **Browse** (`components/kg`) — KG/database browser: filter nodes by the closed
  5 primitives + 6 support types, inspect a node (edges + claim provenance + byte
  ranges), 1-hop neighborhood graph.
- **Graph** (`components/kg`) — capped whole-vault overview with filters, stats, and
  click-to-expand neighborhoods.
- **Curation** (`components/curation`) — review-first control plane: Overview, a unified
  Review inbox, and Maintenance actions routed through the daemon-owned job queue.
- **Config** (`components/config`) — full LLM/embedder/gate/extraction control;
  `PATCH /api/v1/config` surfaces `applied: live|reembed`; hard warning when
  the embedder model changes on a populated vault (re-embed invalidation).

## Dev

```bash
npm ci
npm run dev          # http://localhost:5180, proxies /api -> 127.0.0.1:7777
# point at a different backend: VITE_API_TARGET=http://host:port npm run dev
```

The fetch base is the relative `/api/v1`, so the built app works on whatever loopback
origin serves it. Direct non-loopback server binding is disabled; use an SSH tunnel for
remote access.

### Mock mode

Mocks are a developer aid, never a production fallback. Vite development builds expose the
persisted **Mock data** control. A production build hides it unless the page is opened with
the explicit `?mock=1` query parameter; without that opt-in, even a stale `localStorage`
value cannot make fake data look like vault data. Shapes live in `src/lib/mock.ts`.

## Build (gate)

```bash
npm run build        # tsc -b && vite build  ->  ../frontend_dist/
```

Emits `frontend_dist/` (`index.html` plus hashed assets). `build.sh` runs this build, and
the Starlette server mounts the committed output after all API routes with an SPA fallback.

## Release browser smoke

The release smoke drives the real committed SPA with Playwright against a foreground Okto Neuron
daemon and a loopback OpenAI-compatible embedding stub. It owns a temporary HOME, all three ports,
two browser tabs, the daemon process group, and cleanup. Build the UI first; install Chromium once
on a new machine:

```bash
npx playwright install chromium
npm run build
npm run test:release-browser
```

It proves the zero-vault manager and cookie-free direct URL; independent tab-local vault selection
and `X-Okto-Neuron-Vault` request scoping; switching after a durable curation job without calling the
legacy blocking switch endpoint; managed embedding-key storage, a real authenticated loopback
probe, and secret-free vault YAML; and exact-name managed-vault deletion back to the manager.
Set `OKTO_NEURON_BROWSER_SMOKE_CLI=/path/to/marginalia` to exercise an installed artifact. The
release-artifact workflow points it at the exact wheel environment rather than the source venv.
