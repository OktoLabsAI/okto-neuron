import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { access, mkdtemp, readFile, rm, stat } from 'node:fs/promises'
import http from 'node:http'
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { chromium } from 'playwright'

const frontendRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)))
const repoRoot = path.dirname(frontendRoot)
const fictionalApiKey = 'fictional-browser-smoke-key'
const alphaName = 'browser-alpha'
const betaName = 'browser-beta'

function deadline(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function reservePort() {
  const server = net.createServer()
  server.unref()
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  const address = server.address()
  assert(address && typeof address === 'object')
  const port = address.port
  await new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())))
  return port
}

async function eventually(check, message, timeoutMs = 20_000) {
  const started = Date.now()
  let lastError
  while (Date.now() - started < timeoutMs) {
    try {
      return await check()
    } catch (error) {
      lastError = error
      await deadline(100)
    }
  }
  throw new Error(`${message}${lastError ? `: ${lastError.message}` : ''}`)
}

async function stopChild(child) {
  if (!child || child.exitCode !== null) return
  const exited = once(child, 'exit')
  try {
    if (process.platform === 'win32') child.kill('SIGTERM')
    else process.kill(-child.pid, 'SIGTERM')
  } catch (error) {
    if (error.code !== 'ESRCH') throw error
  }
  if (await Promise.race([exited.then(() => true), deadline(10_000).then(() => false)])) return
  try {
    if (process.platform === 'win32') child.kill('SIGKILL')
    else process.kill(-child.pid, 'SIGKILL')
  } catch (error) {
    if (error.code !== 'ESRCH') throw error
  }
  await Promise.race([once(child, 'exit'), deadline(5_000)])
}

function startEmbeddingMock(port) {
  const calls = []
  const server = http.createServer(async (request, response) => {
    const chunks = []
    for await (const chunk of request) chunks.push(chunk)
    const url = new URL(request.url ?? '/', `http://127.0.0.1:${port}`)

    if (request.method === 'GET' && url.pathname === '/v1/models') {
      response.writeHead(200, { 'Content-Type': 'application/json' })
      response.end(JSON.stringify({ object: 'list', data: [{ id: 'fictional-embed-4' }] }))
      return
    }

    if (request.method === 'POST' && url.pathname === '/v1/embeddings') {
      const rawBody = Buffer.concat(chunks).toString('utf8')
      const authenticated = request.headers.authorization === `Bearer ${fictionalApiKey}`
      calls.push({ authenticated, body: rawBody })
      if (!authenticated) {
        response.writeHead(401, { 'Content-Type': 'application/json' })
        response.end(JSON.stringify({ error: { message: 'missing fictional credential' } }))
        return
      }
      // Return one vector PER INPUT, as a conformant OpenAI-compatible endpoint
      // must. The probe batches its inputs (ADR 0037), so a fixed single-vector
      // reply fails the count check with "returned 1 vectors for N inputs".
      let inputs = []
      try {
        const parsed = JSON.parse(rawBody)
        inputs = Array.isArray(parsed.input) ? parsed.input : [parsed.input]
      } catch {
        inputs = ['']
      }
      response.writeHead(200, { 'Content-Type': 'application/json' })
      response.end(JSON.stringify({
        object: 'list',
        data: inputs.map((_value, index) => ({
          object: 'embedding',
          index,
          embedding: [0.1, 0.2, 0.3, 0.4],
        })),
        model: 'fictional-embed-4',
        usage: { prompt_tokens: inputs.length, total_tokens: inputs.length },
      }))
      return
    }

    response.writeHead(404, { 'Content-Type': 'application/json' })
    response.end(JSON.stringify({ error: { message: 'not found' } }))
  })
  return { server, calls }
}

// The offline embedder is an APPLICATION default (ADR 0034 §4a), not a
// vault-creation input. Preseed it on the vault-neutral defaults surface so the
// smoke never downloads real embedding weights, then assert each created vault
// actually inherits it.
async function preseedStubEmbedder(baseUrl) {
  const response = await fetch(`${baseUrl}/api/v1/config/defaults`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ embedding: { provider: 'stub' } }),
  })
  assert.equal(response.status, 200, 'vault-neutral defaults must accept the stub embedder')

  const readback = await fetch(`${baseUrl}/api/v1/config/defaults`)
  assert.equal(readback.status, 200, 'vault-neutral defaults must be readable with no vault selected')
  const defaults = await readback.json()
  assert.equal(
    defaults.embedding?.provider,
    'stub',
    'application defaults must persist the stub embedder',
  )
}

async function assertVaultInheritsStubEmbedder(baseUrl, vaultPath, name) {
  const response = await fetch(`${baseUrl}/api/v1/config`, {
    headers: { 'X-Okto-Neuron-Vault': vaultPath },
  })
  assert.equal(response.status, 200, `config for ${name} must load`)
  const config = await response.json()
  assert.equal(
    config.embedding?.provider,
    'stub',
    `${name} must inherit the application default embedder`,
  )
}

async function createManagedVault(page, name) {
  const manager = page.locator('main')
  const nameInput = manager.getByPlaceholder('demo-notes', { exact: true })
  await nameInput.fill(name)

  // The per-vault embedder select was removed deliberately; pin its absence and
  // the explainer that replaced it so this gate tracks the current contract.
  // Checking for an absent `option[value="stub"]` is not reliable on its own:
  // M3 added a legitimate graph-backend `<select>` to this same
  // form, and a graph backend can itself be named "stub" (the dev-only
  // marginalia.graph_backends (pre-0.3.0 group name, still read) entry point at tests/fixtures/stub_backend_pkg,
  // installed into this smoke's daemon venv for backend-registry contract
  // tests) — so the old value-based check flags the backend picker as a false
  // positive whenever that fixture happens to be installed. Assert directly
  // on select cardinality and identity instead: the create form must expose
  // exactly one `<select>`, and it must be the "Backend" picker, not a
  // second, separate embedder selector.
  const formSelects = manager.locator('form select')
  assert.equal(
    await formSelects.count(),
    1,
    'vault manager must expose exactly one select (the graph-backend picker), not a separate per-vault embedder selector',
  )
  assert.equal(
    await manager.locator('form label', { hasText: 'Backend' }).count(),
    1,
    "vault manager's one select must be the graph-backend picker",
  )
  assert.equal(
    await manager.getByText('Uses the application defaults.', { exact: false }).count(),
    1,
    'vault manager must state that creation uses the application defaults',
  )

  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith('/api/v1/vaults') && response.request().method() === 'POST',
  )
  const createButton = manager.getByRole('button', { name: 'Create vault', exact: true })
  assert.equal(await createButton.count(), 1, 'vault manager must expose one create action')
  await createButton.click()
  const response = await responsePromise
  assert.equal(response.status(), 200, `creating ${name} must succeed`)
  const payload = await response.json()
  const created = payload.created ?? payload.vaults.find((vault) => vault.name === name)
  assert(created, `create response must identify ${name}`)
  return created
}

async function openConfigAndCaptureVault(page, expectedPath) {
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith('/api/v1/config') && response.request().method() === 'GET',
  )
  const configButton = page
    .locator('nav[aria-label="Primary"]')
    .getByRole('button', { name: 'Config', exact: true })
  assert.equal(await configButton.count(), 1, 'desktop navigation must expose one Config action')
  await configButton.click()
  const response = await responsePromise
  assert.equal(response.status(), 200, 'selected-vault config must load')
  assert.equal(
    response.request().headers()['x-okto-neuron-vault'],
    expectedPath,
    'config request must carry this tab\'s selected vault',
  )
  await page.getByRole('heading', { name: 'Config', exact: true }).waitFor()
}

async function exerciseEmbeddingCredential(page, home, mockBaseUrl, mockCalls) {
  // Config is now a tablist ("Configuration sections"); the Embedding card only
  // mounts once its tab is active. Everything inside it is unchanged.
  const tabs = page.getByRole('tablist', { name: 'Configuration sections' })
  assert.equal(await tabs.count(), 1, 'Config must expose one section tablist')
  const embeddingTab = tabs.getByRole('tab', { name: 'Embedding', exact: true })
  assert.equal(await embeddingTab.count(), 1, 'Config must expose one Embedding tab')
  await embeddingTab.click()
  await eventually(
    async () => assert.equal(await embeddingTab.getAttribute('aria-selected'), 'true'),
    'Embedding tab did not become selected',
  )

  const embedding = page.locator('section').filter({
    has: page.getByRole('heading', { name: 'Embedding', exact: true }),
  })
  await eventually(
    async () => assert.equal(await embedding.count(), 1),
    'Config must expose one Embedding section',
  )

  const providerField = embedding.getByText('Provider', { exact: true }).locator('..')
  const provider = providerField.locator('select')
  assert.equal(await provider.count(), 1, 'Embedding provider selector must be unique')
  await provider.selectOption('openai')

  const model = embedding.getByText('Model', { exact: true }).locator('..').locator('input')
  assert.equal(await model.count(), 1, 'Embedding model input must be unique')
  await model.fill('fictional-embed-4')

  const dimension = embedding.getByText('Dimension', { exact: true }).locator('..').locator('input')
  assert.equal(await dimension.count(), 1, 'Embedding dimension input must be unique')
  await dimension.fill('4')

  const baseUrl = embedding.getByPlaceholder('http://127.0.0.1:1234/v1', { exact: true })
  assert.equal(await baseUrl.count(), 1, 'Embedding Base URL input must be unique')
  await baseUrl.fill(mockBaseUrl)

  const apiKey = embedding.getByTestId('embedding-api-key')
  await apiKey.fill(fictionalApiKey)
  const credentialResponsePromise = page.waitForResponse(
    (response) => response.url().endsWith('/api/v1/credentials/provider')
      && response.request().method() === 'PUT',
  )
  await embedding.getByTestId('embedding-api-key-save').click()
  const credentialResponse = await credentialResponsePromise
  assert.equal(credentialResponse.status(), 200, 'managed embedding credential write must succeed')
  const credentialRequest = credentialResponse.request().postDataJSON()
  assert.deepEqual(
    { kind: credentialRequest.kind, provider: credentialRequest.provider, api_key: credentialRequest.api_key },
    { kind: 'embedding', provider: 'openai', api_key: fictionalApiKey },
    'the generic credential route must receive the embedding key',
  )
  const credential = await credentialResponse.json()
  assert.equal(credential.configured, true)
  assert.match(credential.api_key_env, /^OKTO_NEURON_PROVIDER_OPENAI_/)
  assert.equal(JSON.stringify(credential).includes(fictionalApiKey), false, 'credential response must not echo the key')
  await embedding.getByText(`Key stored as ${credential.api_key_env} and attached to this draft.`, {
    exact: true,
  }).waitFor()

  const envPath = path.join(home, '.okto-neuron', 'env')
  const envFile = await readFile(envPath, 'utf8')
  assert.equal((await stat(envPath)).mode & 0o777, 0o600, 'credential file must be owner-only')
  assert.match(envFile, new RegExp(`^${credential.api_key_env}=`, 'm'))
  assert.equal(envFile.includes(fictionalApiKey), true, 'fictional key must be persisted in isolated HOME')

  const testResponsePromise = page.waitForResponse(
    (response) => response.url().endsWith('/api/v1/embedding/test')
      && response.request().method() === 'POST',
  )
  const testButton = embedding.getByRole('button', { name: 'Test', exact: true })
  assert.equal(await testButton.count(), 1, 'Embedding section must expose one Test action')
  await testButton.click()
  const testResponse = await testResponsePromise
  assert.equal(testResponse.status(), 200, 'embedding probe route must respond')
  // `vectors` is the batched-probe count (ADR 0037): the probe embeds more than
  // one input and reports how many vectors came back, so a provider that silently
  // collapses a batch to one vector is caught here rather than at ingest time.
  assert.deepEqual(await testResponse.json(), {
    ok: true,
    models: ['fictional-embed-4'],
    dimension: 4,
    vectors: 2,
    error: null,
  })
  // The success banner now reports the batch shape rather than a model count.
  await embedding.getByText('Batch test passed', { exact: false }).waitFor()
  await embedding.getByText('2 vectors', { exact: false }).waitFor()
  await embedding.getByText('4 dimensions', { exact: false }).waitFor()
  assert.equal(mockCalls.length, 1, 'embedding probe must call the loopback provider once')
  assert.equal(mockCalls[0].authenticated, true, 'embedding probe must use the managed key')

  // Changing provider/model/dimension now invalidates stored vectors, so the save
  // action relabels to "Save & re-embed" and gates the write behind an explicit
  // confirmation. Drive that path and pin the guard: a destructive re-embed must
  // not be reachable in one unconfirmed click.
  const saveButton = page.getByRole('button', { name: 'Save & re-embed', exact: true })
  assert.equal(
    await page.getByRole('button', { name: 'Save changes', exact: true }).count(),
    0,
    'an embedding-space change must not offer a plain Save changes action',
  )
  assert.equal(await saveButton.count(), 1, 'Config must expose one Save & re-embed action')
  await saveButton.click()

  const confirmDialog = page.locator('div').filter({
    has: page.getByRole('heading', { name: 'Change embedding space?', exact: true }),
  })
  await page.getByRole('heading', { name: 'Change embedding space?', exact: true }).waitFor()
  const confirmButton = confirmDialog
    .getByRole('button', { name: 'Save & re-embed', exact: true })
    .last()
  const saveResponsePromise = page.waitForResponse(
    (response) => response.url().endsWith('/api/v1/config') && response.request().method() === 'PATCH',
  )
  await confirmButton.click()
  const saveResponse = await saveResponsePromise
  assert.equal(saveResponse.status(), 200, 'embedding draft must save')
  const savePayload = await saveResponse.json()
  assert.equal(savePayload.config.embedding.api_key_env, credential.api_key_env)
  assert.equal(savePayload.config.embedding.provider, 'openai')

  return credential.api_key_env
}

// ─────────────────────────────────────────────────────────────────────────────
// ADR 0039 Phase 5 gate — "The UI renders and tests distinct labels/actions
// for cancelled, done + partial, error + integrity_failed, and done +
// complete without opening raw JSON" (docs/adr/0039-ingest-technical-
// correctness.md:1119-1123), plus the CurationProgress integrity-error row
// (T9, tests/server/test_ingest_queue_integrity.py pins the server side).
//
// Per plan-10 item (c): extends this existing Playwright smoke suite rather
// than introducing a new component-test runner (vitest). Route interception
// of the read-only /api/v1/ingest-queue* and /api/v1/ledger/summary
// projections is a rendering fixture, not a mock of the system under test —
// it runs against the real running daemon and the real built bundle; only
// the queue/ledger payloads are canned so all four outcome states plus the
// integrity-error progress row are producible without corrupting a graph.
// ─────────────────────────────────────────────────────────────────────────────

function ingestOutcomeGateFixtures() {
  const units = (over) => ({
    scheduled: 5,
    attempted: 5,
    succeeded: 5,
    reused: 0,
    failed: 0,
    empty_after_retry: 0,
    skipped: 0,
    source_changed: 0,
    cancelled: 0,
    ...over,
  })

  const items = [
    {
      id: 'gate-complete-1',
      name: 'gate-complete.md',
      path: '/vault/gate-complete.md',
      status: 'done',
      committed: 3,
      queued: 0,
      error: null,
      stage: 'done',
      blocks_total: 3,
      blocks_done: 3,
      nodes: 3,
      edges: 2,
      claims: 4,
      outcome: { quality: 'complete', units: units(), retryable: false },
    },
    {
      id: 'gate-partial-1',
      name: 'gate-partial.md',
      path: '/vault/gate-partial.md',
      status: 'done',
      committed: 2,
      queued: 0,
      error: null,
      stage: 'done',
      blocks_total: 5,
      blocks_done: 5,
      nodes: 3,
      edges: 2,
      claims: 3,
      outcome: {
        quality: 'partial',
        units: units({ succeeded: 3, failed: 2 }),
        failed_units: [
          { unit_id: 'u1', retryable: true },
          { unit_id: 'u2', retryable: true },
        ],
        retryable: true,
      },
    },
    {
      id: 'gate-integrity-1',
      name: 'gate-integrity.md',
      path: '/vault/gate-integrity.md',
      status: 'error',
      committed: 0,
      queued: 0,
      error: 'integrity check failed',
      stage: 'error',
      outcome: {
        quality: 'integrity_failed',
        units: units({ succeeded: 0, failed: 0 }),
        retryable: false,
        integrity: { status: 'failed', audit_id: 'audit-gate-1', graph_generation: 'gen-7' },
      },
    },
    {
      id: 'gate-cancelled-1',
      name: 'gate-cancelled.md',
      path: '/vault/gate-cancelled.md',
      status: 'cancelled',
      committed: 0,
      queued: 2,
      error: null,
      stage: 'cancelled',
    },
    {
      // Keeps summary.active true so BulkImport polls and CurationProgress mounts.
      id: 'gate-processing-1',
      name: 'gate-processing.md',
      path: '/vault/gate-processing.md',
      status: 'processing',
      committed: 0,
      queued: 1,
      error: null,
      stage: 'extracting',
      blocks_total: 4,
      blocks_done: 1,
    },
  ]

  const queue = {
    status: 'ok',
    summary: {
      total: items.length,
      queued: 0,
      processing: 1,
      done: 2,
      error: 1,
      cancelled: 1,
      active: true,
      cancel_requested: false,
    },
    items,
  }

  const ledger = {
    status: 'ok',
    run: {
      run_id: 'gate-run-1',
      state: 'running',
      started_at: null,
      completed_at: null,
      document_id: 'gate-doc-1',
    },
    counts: { candidates: 10, candidate_rows: 10, comparisons: 10, commit_plans: 0, commit_records: 0 },
    progress: {
      // Deliberately done > total: this is the ADR 0039 T9 integrity-error
      // row (server never clamps it) that CurationProgress must surface,
      // not hide behind a reassuring 100% bar.
      node_curator: {
        done: 12,
        total: 10,
        remaining: 0,
        fraction: 1.2,
        progress_integrity_error: {
          code: 'population_overflow',
          population: 'node_candidates',
          done: 12,
          total: 10,
          overflow: 2,
        },
      },
      relation_curator: { done: 3, total: 10, remaining: 7, fraction: 0.3 },
    },
  }

  return { queue, ledger }
}

async function runIngestOutcomeGateSection(page) {
  const fixtures = ingestOutcomeGateFixtures()

  const handleIngestQueue = async (route) => {
    const url = new URL(route.request().url())
    const idMatch = url.pathname.match(/\/api\/v1\/ingest-queue\/([^/]+)$/)
    if (idMatch && route.request().method() === 'GET') {
      const item = fixtures.queue.items.find((it) => it.id === decodeURIComponent(idMatch[1]))
      return route.fulfill({ json: { status: 'ok', item } })
    }
    if (url.pathname === '/api/v1/ingest-queue' && route.request().method() === 'GET') {
      return route.fulfill({ json: fixtures.queue })
    }
    return route.continue()
  }
  const handleLedgerSummary = (route) => route.fulfill({ json: fixtures.ledger })

  await page.route('**/api/v1/ingest-queue*', handleIngestQueue)
  await page.route('**/api/v1/ledger/summary*', handleLedgerSummary)
  try {
    await page.getByRole('button', { name: 'Add', exact: true }).click()
    await eventually(async () => {
      assert.equal(await page.getByText('gate-complete.md', { exact: true }).count(), 1)
    }, 'ingest outcome gate fixture queue did not render')

    // 1. done + complete: label present, no retry action.
    await page.getByText('gate-complete.md', { exact: true }).click()
    await eventually(async () => {
      const panel = page.locator('div[data-outcome-quality="complete"]')
      assert.equal(await panel.count(), 1)
    }, 'complete outcome panel did not render')
    assert.equal(
      await page.getByRole('button', { name: /^Retry/ }).count(),
      0,
      'a complete outcome must not offer a retry action',
    )

    // 2. done + partial: label, amber icon, failed-unit count, retry enabled.
    await page.getByText('gate-partial.md', { exact: true }).click()
    await eventually(async () => {
      const panel = page.locator('div[data-outcome-quality="partial"]')
      assert.equal(await panel.count(), 1)
    }, 'partial outcome panel did not render')
    assert.equal(
      await page.locator('svg.lucide-triangle-alert.text-amber-400').count(),
      1,
      'partial outcome must show the amber warning icon',
    )
    const partialRetry = page.getByRole('button', { name: 'Retry 2 failed units', exact: true })
    await partialRetry.waitFor()
    assert.equal(await partialRetry.isDisabled(), false, 'partial outcome retry must be enabled')

    // 3. error + integrity_failed: label, rose icon, retry blocked, integrity status text.
    await page.getByText('gate-integrity.md', { exact: true }).click()
    await eventually(async () => {
      const panel = page.locator('div[data-outcome-quality="integrity_failed"]')
      assert.equal(await panel.count(), 1)
    }, 'integrity_failed outcome panel did not render')
    assert.equal(
      await page.locator('svg.lucide-triangle-alert.text-rose-400').count(),
      1,
      'integrity_failed outcome must show the rose danger icon',
    )
    assert.equal(
      await page.getByRole('button', { name: /^Retry/ }).count(),
      0,
      'integrity_failed must not offer a retry action (classified non-retryable)',
    )
    assert.equal(await page.getByText('failed', { exact: true }).count() >= 1, true)

    // 4. cancelled: distinct label + stop icon, no retry.
    await page.getByText('gate-cancelled.md', { exact: true }).click()
    await eventually(async () => {
      assert.equal(await page.getByText('Cancelled', { exact: true }).count(), 1)
    }, 'cancelled item did not render its distinct Cancelled label')
    assert.equal(
      await page.locator('svg.lucide-circle-stop, svg.lucide-stop-circle').count() >= 1,
      true,
      'cancelled item must show the stop icon',
    )
    assert.equal(
      await page.getByRole('button', { name: /^Retry/ }).count(),
      0,
      'a cancelled item must not offer a retry action',
    )

    // 5. CurationProgress integrity-error row: renders instead of a clamped bar.
    await eventually(async () => {
      assert.equal(await page.getByText('population_overflow', { exact: true }).count(), 1)
      assert.equal(await page.getByText('12/10', { exact: false }).count() >= 1, true)
    }, 'CurationProgress integrity-error row did not render')
    assert.equal(
      await page.getByText('reported more done than the declared', { exact: false }).count(),
      1,
      'integrity-error row must explain the overflow rather than showing a healthy bar',
    )
  } finally {
    await page.unroute('**/api/v1/ingest-queue*', handleIngestQueue)
    await page.unroute('**/api/v1/ledger/summary*', handleLedgerSummary)
  }

  console.log('- ADR 0039 Phase 5 gate: complete/partial/integrity_failed/cancelled outcomes + CurationProgress integrity-error row all render distinctly')
}

async function main() {
  await access(path.join(repoRoot, 'frontend_dist', 'index.html'))

  const tempRoot = await mkdtemp(path.join(os.tmpdir(), 'okto-neuron-browser-release-'))
  const home = path.join(tempRoot, 'home')
  const restPort = await reservePort()
  const mcpPort = await reservePort()
  const providerPort = await reservePort()
  const baseUrl = `http://127.0.0.1:${restPort}`
  const mockBaseUrl = `http://127.0.0.1:${providerPort}/v1`
  const defaultCli = process.platform === 'win32'
    ? path.join(repoRoot, '.venv', 'Scripts', 'okto-neuron.exe')
    : path.join(repoRoot, '.venv', 'bin', 'okto-neuron')
  const cli = process.env.OKTO_NEURON_BROWSER_SMOKE_CLI || defaultCli

  let browser
  let daemon
  let daemonOutput = ''
  const embeddingMock = startEmbeddingMock(providerPort)

  try {
    await access(cli)
    embeddingMock.server.listen(providerPort, '127.0.0.1')
    await once(embeddingMock.server, 'listening')

    const daemonEnv = { ...process.env }
    for (const inherited of Object.keys(daemonEnv)) {
      if ((inherited.startsWith('OKTO_NEURON_') || inherited.startsWith('MARGINALIA_'))) delete daemonEnv[inherited]
    }
    Object.assign(daemonEnv, {
      HOME: home,
      XDG_CACHE_HOME: path.join(home, '.cache'),
      XDG_CONFIG_HOME: path.join(home, '.config'),
      XDG_DATA_HOME: path.join(home, '.local', 'share'),
      OKTO_NEURON_NO_OPEN: '1',
      NO_PROXY: '127.0.0.1,localhost',
      no_proxy: '127.0.0.1,localhost',
      DO_NOT_TRACK: '1',
      LITELLM_TELEMETRY: 'false',
      PYTHONUNBUFFERED: '1',
    })
    assert.deepEqual(
      Object.keys(daemonEnv).filter((key) => key.startsWith('OKTO_NEURON_') || key.startsWith('MARGINALIA_')),
      ['OKTO_NEURON_NO_OPEN'],
      'browser smoke daemon must receive only suite-owned Okto Neuron variables',
    )

    daemon = spawn(
      cli,
      [
        'serve',
        '--foreground',
        '--no-open',
        '--host',
        '127.0.0.1',
        '--port',
        String(restPort),
        '--mcp-port',
        String(mcpPort),
      ],
      {
        cwd: repoRoot,
        detached: process.platform !== 'win32',
        env: daemonEnv,
        stdio: ['ignore', 'pipe', 'pipe'],
      },
    )
    for (const stream of [daemon.stdout, daemon.stderr]) {
      stream.on('data', (chunk) => {
        daemonOutput = `${daemonOutput}${chunk.toString('utf8')}`.slice(-64_000)
      })
    }

    await eventually(async () => {
      if (daemon.exitCode !== null) throw new Error(`daemon exited ${daemon.exitCode}`)
      const response = await fetch(`${baseUrl}/api/v1/vaults`)
      assert.equal(response.status, 200)
      const body = await response.json()
      assert.deepEqual(body.vaults, [])
      return body
    }, 'isolated zero-vault daemon did not become ready', 30_000)

    await preseedStubEmbedder(baseUrl)

    const rootResponse = await fetch(`${baseUrl}/`)
    assert.equal(rootResponse.status, 200, 'built SPA root must load')
    assert.equal(rootResponse.headers.get('set-cookie'), null, 'direct UI must not mint an auth cookie')

    browser = await chromium.launch({ headless: true })
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
    const pageA = await context.newPage()
    const compatibilitySwitches = []
    pageA.on('request', (request) => {
      if (new URL(request.url()).pathname === '/api/v1/vaults/switch') {
        compatibilitySwitches.push(request.url())
      }
    })

    await pageA.goto(baseUrl, { waitUntil: 'networkidle' })
    await pageA.getByRole('heading', { name: 'Vaults', exact: true }).waitFor()
    assert.equal(await pageA.getByText('No vault selected', { exact: true }).count(), 1)
    assert.equal(await pageA.getByText('No vaults found', { exact: true }).count(), 1)
    assert.deepEqual(await context.cookies(baseUrl), [], 'zero-vault UI must not require a browser cookie')

    const alpha = await createManagedVault(pageA, alphaName)
    await assertVaultInheritsStubEmbedder(baseUrl, alpha.path, alphaName)
    const sidebarA = pageA.locator('nav[aria-label="Primary"]')
    const selectorA = sidebarA.locator('select')
    await eventually(async () => assert.equal(await selectorA.inputValue(), alpha.path), 'first tab did not select alpha')

    const pageB = await context.newPage()
    await pageB.goto(baseUrl, { waitUntil: 'networkidle' })
    await pageB.getByRole('heading', { name: 'Vaults', exact: true }).waitFor()
    assert.equal(await pageB.getByText('No vault selected', { exact: true }).count(), 1)
    const existingVaultsB = pageB.getByText('Existing', { exact: true }).locator('..')
    assert.equal(await existingVaultsB.getByText(alphaName, { exact: true }).count(), 1)
    const beta = await createManagedVault(pageB, betaName)
    await assertVaultInheritsStubEmbedder(baseUrl, beta.path, betaName)
    const selectorB = pageB.locator('nav[aria-label="Primary"] select')
    await eventually(async () => assert.equal(await selectorB.inputValue(), beta.path), 'second tab did not select beta')
    assert.equal(await selectorA.inputValue(), alpha.path, 'second tab must not change first tab selection')

    const refreshResponsePromise = pageA.waitForResponse(
      (response) => response.url().endsWith('/api/v1/vaults') && response.request().method() === 'GET',
    )
    await sidebarA.getByTitle('Refresh vaults', { exact: true }).click()
    assert.equal((await refreshResponsePromise).status(), 200)
    await eventually(
      async () => assert.equal(await selectorA.locator(`option[value="${beta.path}"]`).count(), 1),
      'first tab did not discover beta',
    )
    assert.equal(await selectorA.inputValue(), alpha.path, 'refresh must preserve first tab selection')

    await openConfigAndCaptureVault(pageA, alpha.path)
    await openConfigAndCaptureVault(pageB, beta.path)

    const apiKeyEnv = await exerciseEmbeddingCredential(pageB, home, mockBaseUrl, embeddingMock.calls)

    // Confirming the embedding-space change starts a real full re-embed, which
    // fences beta: vault-scoped requests answer 409 until it drains. Wait for the
    // vault to become readable again so the later switch assertions measure the
    // switch, not the maintenance window.
    await eventually(async () => {
      const response = await fetch(`${baseUrl}/api/v1/config`, {
        headers: { 'X-Okto-Neuron-Vault': beta.path },
      })
      assert.equal(response.status, 200, `beta still fenced by re-embed (${response.status})`)
    }, 'beta re-embed did not drain', 60_000)

    const betaConfig = await readFile(path.join(beta.path, 'okto-neuron.yaml'), 'utf8')
    assert.equal(betaConfig.includes(fictionalApiKey), false, 'vault YAML must never contain the API key')
    assert.equal(betaConfig.includes(apiKeyEnv), true, 'vault YAML must retain only the managed env reference')

    const curationJob = await pageA.evaluate(async (vaultPath) => {
      const response = await fetch('/api/v1/reconcile/propose', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Okto-Neuron-Vault': vaultPath,
        },
        body: '{}',
      })
      return { status: response.status, body: await response.json() }
    }, alpha.path)
    assert.equal(curationJob.status, 200, 'alpha curation job must be accepted')
    assert.equal(curationJob.body.job.kind, 'reconcile-propose')

    const jobsPath = path.join(alpha.path, '.marginalia', 'curation-jobs.json')
    await eventually(async () => {
      const persisted = JSON.parse(await readFile(jobsPath, 'utf8'))
      assert(persisted.jobs.some((job) => job.id === curationJob.body.job.id))
    }, 'alpha curation job was not persisted')

    const betaConfigResponsePromise = pageA.waitForResponse(
      (response) => response.url().endsWith('/api/v1/config')
        && response.request().method() === 'GET'
        && response.request().headers()['x-okto-neuron-vault'] === beta.path,
    )
    await selectorA.selectOption(beta.path)
    assert.equal((await betaConfigResponsePromise).status(), 200)
    await eventually(async () => assert.equal(await selectorA.inputValue(), beta.path), 'alpha tab did not switch to beta')
    assert.equal(compatibilitySwitches.length, 0, 'browser switching must never call the blocking compatibility route')
    assert.equal(await pageA.getByRole('dialog').count(), 0, 'vault switch must not show a blocking dialog')

    const deleteButton = sidebarA.getByTitle(`Delete ${betaName}`, { exact: true })
    assert.equal(await deleteButton.count(), 1, 'selected managed vault must expose Delete')
    await deleteButton.click()
    const dialog = pageA.getByRole('dialog')
    await dialog.getByRole('heading', { name: `Delete ${betaName} permanently`, exact: true }).waitFor()
    const confirmation = dialog.getByRole('textbox')
    const confirmDelete = dialog.getByRole('button', { name: 'Delete vault', exact: true })
    await confirmation.fill('wrong-name')
    assert.equal(await confirmDelete.isDisabled(), true, 'delete must stay guarded for a wrong name')
    await confirmation.fill(betaName)
    assert.equal(await confirmDelete.isEnabled(), true, 'exact vault name must enable delete')

    const deleteResponsePromise = pageA.waitForResponse(
      (response) => response.url().endsWith(`/api/v1/vaults/${beta.id}`)
        && response.request().method() === 'DELETE',
    )
    await confirmDelete.click()
    const deleteResponse = await deleteResponsePromise
    assert.equal(deleteResponse.status(), 200, 'managed vault delete must succeed')
    const deletePayload = await deleteResponse.json()
    assert.equal(deletePayload.vaults.some((vault) => vault.id === beta.id), false)
    // Config is an application surface (ADR 0034 §4a), so deleting the selected
    // vault while Config is open no longer bounces the tab to the vault manager:
    // Config stays mounted and falls back to application defaults. Pin that, plus
    // the de-selection itself.
    await eventually(
      async () => assert.equal(await selectorA.locator(`option[value="${beta.path}"]`).count(), 0),
      'deleted vault must leave the selector',
    )
    await pageA.getByRole('heading', { name: 'Config', exact: true }).waitFor()
    await pageA
      .getByText('Application defaults inherited by new vaults and vaults without local overrides.', {
        exact: true,
      })
      .waitFor()
    assert.equal(
      await pageA.getByRole('button', { name: 'Save changes', exact: true }).count(),
      0,
      'vault-scoped save must not be offered with no vault selected',
    )
    await assert.rejects(access(beta.path), 'deleted managed vault directory must be gone')

    // Beta's deletion left no vault selected (Config fell back to application
    // defaults, per the assertions above). Re-select alpha so the outcome-gate
    // section below has a vault and its Add tab is enabled.
    await selectorA.selectOption(alpha.path)
    await eventually(async () => assert.equal(await selectorA.inputValue(), alpha.path), 'could not re-select alpha for the outcome gate section')

    await runIngestOutcomeGateSection(pageA)

    await context.close()
    await browser.close()
    browser = undefined

    console.log('browser release smoke passed')
    console.log('- built SPA opened directly at a zero-vault manager with no auth cookie')
    console.log('- two tabs kept independent vault selections and scoped API headers')
    console.log('- switching remained local while a durable curation job existed')
    console.log('- embedding key storage, authenticated loopback probe, and secret-free config passed')
    console.log('- exact-name managed-vault deletion de-selected the vault and left Config on application defaults')
  } catch (error) {
    if (daemonOutput) process.stderr.write(`\nOkto Neuron daemon output:\n${daemonOutput}\n`)
    throw error
  } finally {
    if (browser) await browser.close().catch(() => {})
    await stopChild(daemon).catch(() => {})
    await new Promise((resolve) => embeddingMock.server.close(() => resolve()))
    await rm(tempRoot, { recursive: true, force: true })
  }
}

await main()
