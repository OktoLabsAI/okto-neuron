import { lazy, Suspense, useCallback, useEffect, useState } from 'react'
import {
  AlertTriangle,
  Database,
  MessageSquareText,
  SlidersHorizontal,
  FlaskConical,
  Sparkles,
  Loader2,
  Share2,
  ShieldCheck,
  HardDrive,
  RefreshCw,
  Plus,
  Trash2,
  ScrollText,
  WifiOff,
} from 'lucide-react'
import { useApp, isIngestActive, type View } from '@/store/app'
import { mockModeAvailable } from '@/lib/mode'
import { useIngestPoller } from '@/hooks/useIngestPoller'
import { QueryView } from '@/components/query/QueryView'
import { ConfigPanel } from '@/components/config/ConfigPanel'
import { IngestView } from '@/components/ingest/IngestView'
import { IngestLogsView } from '@/components/logs/IngestLogsView'
import { CurationView } from '@/components/curation/CurationView'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { MigrationRequiredBanner } from '@/components/MigrationRequiredBanner'
import {
  createVault,
  deleteVault,
  getBackends,
  getVaults,
  getVaultReembedStatus,
  startVaultReembed,
} from '@/services/vault-api'
import type { BackendInfo, ReembedStatus, VaultInfo, VaultIssue, VaultListResponse } from '@/types'
import type { FormEvent } from 'react'

// Syntactic loopback check for a storage URI, mirroring the server-side gate
// (config/_vault.py's `_classify_storage_endpoint`) closely enough to drive
// the UI: an unparseable or non-loopback host means the "allow remote
// database endpoint" checkbox must show, since the server will 400 without
// `allow_remote_db` in that case anyway.
const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '::1', '[::1]', '0.0.0.0'])

function isLoopbackUri(uri: string): boolean {
  try {
    const host = new URL(uri).hostname.toLowerCase()
    return LOOPBACK_HOSTS.has(host)
  } catch {
    return false
  }
}

const KGBrowser = lazy(() =>
  import('@/components/kg/KGBrowser').then((module) => ({ default: module.KGBrowser })),
)
const GraphView = lazy(() =>
  import('@/components/kg/GraphView').then((module) => ({ default: module.GraphView })),
)

const NAV: { id: View; label: string; icon: typeof Database }[] = [
  { id: 'query', label: 'Query', icon: MessageSquareText },
  { id: 'ingest', label: 'Add', icon: Sparkles },
  { id: 'logs', label: 'Logs', icon: ScrollText },
  { id: 'browser', label: 'Browse', icon: Database },
  { id: 'graph', label: 'Graph', icon: Share2 },
  { id: 'curation', label: 'Curation', icon: ShieldCheck },
  { id: 'config', label: 'Config', icon: SlidersHorizontal },
]

export default function App() {
  const { view, setView, mock, toggleMock, dataVersion } = useApp()
  const clearIngestQueue = useApp((s) => s.clearIngestQueue)
  const bumpData = useApp((s) => s.bumpData)
  const selectNode = useApp((s) => s.selectNode)
  const ingestActive = useApp(isIngestActive)
  const ingestSummary = useApp((s) => s.ingestQueue?.summary)
  const queueVaultPath = useApp((s) => s.ingestQueue?.vault?.path ?? null)
  const connectionStatus = useApp((s) => s.connectionStatus)
  const connectionMessage = useApp((s) => s.connectionMessage)
  const selectedVaultPath = useApp((s) => s.selectedVaultPath)
  const setSelectedVault = useApp((s) => s.setSelectedVault)
  const [vaults, setVaults] = useState<VaultInfo[]>([])
  const [current, setCurrent] = useState<VaultInfo | null>(null)
  const [vaultWarning, setVaultWarning] = useState<VaultIssue | null>(null)
  const [vaultLoading, setVaultLoading] = useState(false)
  const [vaultError, setVaultError] = useState<string | null>(null)
  const [repairingPath, setRepairingPath] = useState<string | null>(null)
  const [repairStatus, setRepairStatus] = useState<ReembedStatus | null>(null)
  const [repairError, setRepairError] = useState<string | null>(null)

  const resetForVault = useCallback(() => {
    clearIngestQueue()
    selectNode(null)
    bumpData()
  }, [bumpData, clearIngestQueue, selectNode])

  const applyVaults = useCallback((body: VaultListResponse, preferredPath?: string | null) => {
    // The daemon's `current` field is only the compatibility fallback for
    // unscoped CLI/API/MCP callers. A browser tab must never inherit it (or a
    // sole-vault fallback) as its own selection. Read the latest tab state at
    // response time so a slow list refresh cannot undo a newer local switch.
    const previousPath = useApp.getState().selectedVaultPath
    const desiredPath = preferredPath === undefined
      ? previousPath
      : preferredPath
    const chosen = body.vaults.find((vault) => vault.path === desiredPath)
      ?? null
    const normalized = body.vaults.map((vault) => ({
      ...vault,
      current: vault.path === chosen?.path,
    }))
    const nextCurrent = chosen
      ? normalized.find((vault) => vault.path === chosen.path) ?? null
      : null
    setVaults(normalized)
    setCurrent(nextCurrent)
    setSelectedVault(nextCurrent?.path ?? null)
    setVaultWarning(body.warning ?? null)
    if ((nextCurrent?.path ?? null) !== previousPath) resetForVault()
  }, [resetForVault, setSelectedVault])

  const refreshVaults = useCallback(async (silent = false) => {
    if (!silent) {
      setVaultLoading(true)
      setVaultError(null)
    }
    try {
      applyVaults(await getVaults())
    } catch (err) {
      if (!silent) setVaultError(err instanceof Error ? err.message : 'Vault list failed')
    } finally {
      if (!silent) setVaultLoading(false)
    }
  }, [applyVaults])

  useEffect(() => {
    void refreshVaults()
  }, [mock, refreshVaults])

  useEffect(() => {
    if (!queueVaultPath || queueVaultPath === current?.path) return
    void refreshVaults(true)
  }, [current?.path, queueVaultPath, refreshVaults])

  const onSwitchVault = async (path: string) => {
    if (!path || path === selectedVaultPath) return
    setVaultError(null)
    const chosen = vaults.find((vault) => vault.path === path) ?? null
    if (!chosen) {
      setVaultError('Vault is no longer available. Refresh the list and try again.')
      return
    }
    setVaults((entries) => entries.map((vault) => ({
      ...vault,
      current: vault.path === chosen.path,
    })))
    setCurrent({ ...chosen, current: true })
    setSelectedVault(chosen.path)
    resetForVault()
  }

  const onCreateVault = async (
    name: string,
    backend?: string,
    acceptExperimental?: boolean,
    storage?: { uri?: string; credentialEnv?: string; database?: string },
    allowRemoteDb?: boolean,
  ) => {
    const trimmed = name.trim()
    if (!trimmed) return
    setVaultLoading(true)
    setVaultError(null)
    try {
      const body = await createVault(
        backend
          ? {
              name: trimmed,
              backend,
              ...(acceptExperimental ? { accept_experimental: true } : {}),
              ...(storage?.uri ? { storage_uri: storage.uri } : {}),
              ...(storage?.credentialEnv ? { storage_credential_env: storage.credentialEnv } : {}),
              ...(storage?.database ? { storage_database: storage.database } : {}),
              ...(allowRemoteDb ? { allow_remote_db: true } : {}),
            }
          : { name: trimmed },
      )
      const createdPath = body.created?.path
        ?? body.vaults.find((vault) => vault.name === trimmed)?.path
        ?? null
      applyVaults(body, createdPath)
    } catch (err) {
      setVaultError(err instanceof Error ? err.message : 'Vault create failed')
    } finally {
      setVaultLoading(false)
    }
  }

  const onDeleteVault = async (vault: VaultInfo, confirmName: string): Promise<boolean> => {
    setVaultLoading(true)
    setVaultError(null)
    try {
      const body = await deleteVault(vault.id, confirmName)
      const nextPath = selectedVaultPath === vault.path
        ? null
        : selectedVaultPath
      applyVaults(body, nextPath)
      return true
    } catch (err) {
      setVaultError(err instanceof Error ? err.message : 'Vault deletion failed')
      return false
    } finally {
      setVaultLoading(false)
    }
  }

  const onRepairVault = async (vault: string) => {
    if (!vault || repairingPath) return
    setRepairingPath(vault)
    setRepairError(null)
    setRepairStatus({ status: 'ok', running: true, phase: 'starting' })
    try {
      const body = await startVaultReembed(vault)
      setRepairStatus(body.reembed)
    } catch (err) {
      setRepairingPath(null)
      setRepairError(err instanceof Error ? err.message : 'Re-embed failed')
    }
  }

  useEffect(() => {
    if (!repairingPath) return
    let cancelled = false
    let timer: number | undefined
    const poll = async () => {
      try {
        const status = await getVaultReembedStatus(repairingPath)
        if (cancelled) return
        setRepairStatus(status)
        if (!status.running && status.phase === 'complete') {
          setRepairingPath(null)
          await refreshVaults()
          return
        }
        if (!status.running && status.phase === 'failed') {
          setRepairingPath(null)
          setRepairError('Re-embed failed')
          return
        }
      } catch (err) {
        if (cancelled) return
        setRepairingPath(null)
        setRepairError(err instanceof Error ? err.message : 'Re-embed status failed')
        return
      }
      timer = window.setTimeout(poll, 1500)
    }
    void poll()
    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [repairingPath])

  const hasVault = current !== null
  const showMockControls = mockModeAvailable()

  return (
    <div className="flex h-full">
      {hasVault && <IngestPollerMount />}
      {/* Sidebar */}
      <nav
        aria-label="Primary"
        className="hidden w-56 shrink-0 flex-col border-r border-surface-800 bg-surface-900 md:flex"
      >
        <div className="flex items-center gap-2 px-5 py-5">
          <div className="h-7 w-7 rounded-lg bg-gradient-to-br from-accent-500 to-violet-500" />
          <div>
            <div className="text-sm font-semibold leading-tight">Okto Neuron</div>
            <div className="text-[11px] text-surface-500">knowledge graph</div>
          </div>
        </div>
        <VaultSelector
          vaults={vaults}
          current={current}
          loading={vaultLoading}
          error={vaultError}
          warning={vaultWarning}
          repairingPath={repairingPath}
          repairStatus={repairStatus}
          repairError={repairError}
          onRepair={onRepairVault}
          onRefresh={refreshVaults}
          onSwitch={onSwitchVault}
          onCreate={onCreateVault}
          onDelete={onDeleteVault}
        />
        <div className="flex flex-col gap-1 px-3">
          {NAV.map(({ id, label, icon: Icon }) => {
            const disabled = !hasVault && id !== 'config'
            return (
            <button
              key={id}
              onClick={() => setView(id)}
              disabled={disabled}
              className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors ${
                view === id
                  ? 'bg-accent-700/30 text-accent-300 ring-1 ring-accent-600/40'
                  : 'text-surface-400 hover:bg-surface-800 hover:text-surface-200'
              }`}
              title={disabled ? 'Select a vault first' : label}
              aria-current={view === id ? 'page' : undefined}
            >
              <Icon size={17} />
              {label}
            </button>
            )
          })}
        </div>
        {hasVault && ingestActive && ingestSummary && (
          <button
            onClick={() => setView('logs')}
            className="mx-3 mt-auto flex items-center gap-2 rounded-lg border border-amber-600/40 bg-amber-950/20 px-3 py-2 text-xs text-amber-300 hover:bg-amber-950/40"
            title="An ingest job is running - click to inspect logs"
          >
            <Loader2 size={14} className="animate-spin" />
            <span className="flex-1 text-left">
              Ingesting {ingestSummary.processing + ingestSummary.done}/{ingestSummary.total}
            </span>
          </button>
        )}
        {showMockControls && <div className={`${ingestActive ? '' : 'mt-auto'} border-t border-surface-800 p-3`}>
          <label className="flex cursor-pointer items-center gap-2 rounded-lg px-3 py-2 text-xs text-surface-400 hover:bg-surface-800">
            <FlaskConical size={15} className={mock ? 'text-amber-400' : 'text-surface-500'} />
            <span className="flex-1">Mock data</span>
            <input
              type="checkbox"
              checked={mock}
              onChange={(e) => toggleMock(e.target.checked)}
              className="h-4 w-4 accent-accent-500"
            />
          </label>
          {mock && (
            <p className="px-3 pt-1 text-[10px] leading-snug text-amber-400/70">
              Serving documented mock shapes — switch off to hit the live API.
            </p>
          )}
        </div>}
        <div
          className={`${(hasVault && ingestActive && ingestSummary) || showMockControls ? '' : 'mt-auto'} px-5 pb-4 pt-3 text-[10px] text-surface-500`}
          data-testid="brand-footer"
        >
          Okto Neuron by Okto Labs
        </div>
      </nav>

      {/* Main — wrapped so a render error in any view never unmounts the shell. */}
      <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <div className="shrink-0 border-b border-surface-800 bg-surface-900 md:hidden">
          <div className="flex items-center gap-2 px-3 py-3">
            <div className="h-7 w-7 shrink-0 rounded-lg bg-gradient-to-br from-accent-500 to-violet-500" />
            <div className="min-w-0 flex-1">
              <div className="text-sm font-semibold leading-tight">Okto Neuron</div>
              <div className="truncate text-[11px] text-surface-500">
                {current?.name ?? 'Choose a vault'}
              </div>
            </div>
            {showMockControls && (
              <label className="flex items-center gap-1.5 text-[11px] text-surface-400">
                <FlaskConical size={14} className={mock ? 'text-amber-400' : 'text-surface-500'} />
                Mock
                <input
                  type="checkbox"
                  checked={mock}
                  onChange={(event) => toggleMock(event.target.checked)}
                  className="h-4 w-4 accent-accent-500"
                />
              </label>
            )}
          </div>
          <VaultSelector
            vaults={vaults}
            current={current}
            loading={vaultLoading}
            error={vaultError}
            warning={vaultWarning}
            repairingPath={repairingPath}
            repairStatus={repairStatus}
            repairError={repairError}
            onRepair={onRepairVault}
            onRefresh={refreshVaults}
            onSwitch={onSwitchVault}
            onCreate={onCreateVault}
            onDelete={onDeleteVault}
          />
          <nav aria-label="Primary mobile" className="overflow-x-auto px-3 pb-3">
            <div className="flex min-w-max gap-1">
              {NAV.map(({ id, label, icon: Icon }) => {
                const disabled = !hasVault && id !== 'config'
                return (
                <button
                  key={id}
                  type="button"
                  onClick={() => setView(id)}
                  disabled={disabled}
                  className={`flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs transition-colors ${
                    view === id
                      ? 'bg-accent-700/30 text-accent-300 ring-1 ring-accent-600/40'
                      : 'text-surface-400 hover:bg-surface-800 hover:text-surface-200'
                  }`}
                  title={disabled ? 'Select a vault first' : label}
                  aria-current={view === id ? 'page' : undefined}
                >
                  <Icon size={15} />
                  {label}
                </button>
                )
              })}
            </div>
          </nav>
          {hasVault && ingestActive && ingestSummary && (
            <button
              type="button"
              onClick={() => setView('logs')}
              className="mx-3 mb-3 flex items-center gap-2 rounded-lg border border-amber-600/40 bg-amber-950/20 px-3 py-2 text-xs text-amber-300"
            >
              <Loader2 size={14} className="animate-spin" />
              Ingesting {ingestSummary.processing + ingestSummary.done}/{ingestSummary.total}
            </button>
          )}
        </div>
        <MigrationRequiredBanner />
        {connectionStatus === 'offline' && (
          <div
            role="alert"
            className="flex shrink-0 flex-wrap items-center gap-3 border-b border-rose-700/60 bg-rose-950/80 px-4 py-2.5 text-sm text-rose-100 sm:flex-nowrap"
          >
            <WifiOff size={16} className="shrink-0" />
            <span className="min-w-0 flex-1">
              {connectionMessage ?? 'The Okto Neuron daemon is unavailable.'}
            </span>
            <button
              type="button"
              onClick={() => void refreshVaults()}
              className="inline-flex h-8 items-center gap-2 rounded-md border border-rose-500/50 px-3 text-xs font-medium hover:bg-rose-900/60"
            >
              <RefreshCw size={13} /> Retry
            </button>
          </div>
        )}
        <div className="min-h-0 flex-1 overflow-hidden">
          <ErrorBoundary resetKey={`${view}:${dataVersion}:${current?.path ?? 'none'}`}>
            <Suspense
              key={`${current?.path ?? 'no-vault'}:${dataVersion}`}
              fallback={<ViewLoading />}
            >
              {view === 'config' ? (
                <ConfigPanel />
              ) : !hasVault ? (
                <VaultManager
                  vaults={vaults}
                  loading={vaultLoading}
                  error={vaultError}
                  warning={vaultWarning}
                  repairingPath={repairingPath}
                  repairStatus={repairStatus}
                  repairError={repairError}
                  onRepair={onRepairVault}
                  onRefresh={refreshVaults}
                  onSwitch={onSwitchVault}
                  onCreate={onCreateVault}
                  onDelete={onDeleteVault}
                />
              ) : (
                <>
                  {view === 'query' && <QueryView />}
                  {view === 'ingest' && <IngestView />}
                  {view === 'logs' && <IngestLogsView />}
                  {view === 'browser' && <KGBrowser />}
                  {view === 'graph' && <GraphView />}
                  {view === 'curation' && <CurationView />}
                </>
              )}
            </Suspense>
          </ErrorBoundary>
        </div>
      </main>
    </div>
  )
}

function ViewLoading() {
  return (
    <div className="grid h-full place-items-center text-sm text-surface-400" role="status">
      <span className="inline-flex items-center gap-2">
        <Loader2 size={16} className="animate-spin" /> Loading view...
      </span>
    </div>
  )
}

function IngestPollerMount() {
  useIngestPoller()
  return null
}

interface VaultControlsProps {
  vaults: VaultInfo[]
  current: VaultInfo | null
  loading: boolean
  error: string | null
  warning: VaultIssue | null
  repairingPath: string | null
  repairStatus: ReembedStatus | null
  repairError: string | null
  onRepair: (vault: string) => Promise<void>
  onRefresh: () => Promise<void>
  onSwitch: (path: string) => Promise<void>
  onCreate: (
    name: string,
    backend?: string,
    acceptExperimental?: boolean,
    storage?: { uri?: string; credentialEnv?: string; database?: string },
    allowRemoteDb?: boolean,
  ) => Promise<void>
  onDelete: (vault: VaultInfo, confirmName: string) => Promise<boolean>
}

function VaultSelector({
  vaults,
  current,
  loading,
  error,
  warning,
  repairingPath,
  repairStatus,
  repairError,
  onRefresh,
  onSwitch,
  onCreate,
  onDelete,
}: VaultControlsProps) {
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [deleting, setDeleting] = useState<VaultInfo | null>(null)

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!name.trim()) return
    await onCreate(name)
    setName('')
    setCreating(false)
  }

  return (
    <div className="px-3 pb-3">
      <div className="flex items-center gap-2">
        <div className="flex min-w-0 flex-1 items-center gap-2 rounded-lg border border-surface-800 bg-surface-950 px-2 py-1.5">
          <HardDrive size={15} className="shrink-0 text-surface-500" />
          <select
            value={current?.path ?? ''}
            disabled={loading || vaults.length === 0}
            onChange={(event) => void onSwitch(event.target.value)}
            className="min-w-0 flex-1 bg-transparent text-xs text-surface-200 outline-none disabled:text-surface-600"
            title={current?.path ?? 'Vault'}
          >
            {!current && vaults.length > 0 && <option value="">Select vault</option>}
            {vaults.length === 0 && <option value="">No vaults</option>}
            {vaults.map((vault) => (
              <option key={vault.path} value={vault.path}>
                {vault.issue ? `${vault.name} (needs repair)` : vault.name}
              </option>
            ))}
          </select>
        </div>
        <button
          type="button"
          onClick={() => void onRefresh()}
          disabled={loading}
          className="grid h-8 w-8 place-items-center rounded-lg border border-surface-800 bg-surface-950 text-surface-400 hover:bg-surface-800 hover:text-surface-200 disabled:opacity-50"
          title="Refresh vaults"
        >
          <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
        </button>
        <button
          type="button"
          onClick={() => setCreating((value) => !value)}
          disabled={loading}
          className="grid h-8 w-8 place-items-center rounded-lg border border-surface-800 bg-surface-950 text-surface-400 hover:bg-surface-800 hover:text-surface-200 disabled:opacity-50"
          title="Create vault"
        >
          <Plus size={14} />
        </button>
        <button
          type="button"
          onClick={() => current && setDeleting(current)}
          disabled={loading || !current?.deletable}
          className="grid h-8 w-8 place-items-center rounded-lg border border-surface-800 bg-surface-950 text-surface-400 hover:border-red-900 hover:bg-red-950/30 hover:text-red-300 disabled:opacity-35"
          title={
            current?.deletable
              ? `Delete ${current.name}`
              : (current?.delete_reason ?? 'Select a vault inside a configured vault root')
          }
        >
          <Trash2 size={14} />
        </button>
      </div>
      {creating && (
        <form onSubmit={(event) => void submit(event)} className="mt-2 flex gap-2">
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="vault name"
            className="min-w-0 flex-1 rounded-lg border border-surface-800 bg-surface-950 px-2 py-1.5 text-xs text-surface-100 outline-none placeholder:text-surface-600 focus:border-accent-600"
          />
          <button
            type="submit"
            disabled={loading || !name.trim()}
            className="rounded-lg bg-accent-600 px-2 text-xs font-medium text-white hover:bg-accent-500 disabled:opacity-50"
          >
            Add
          </button>
        </form>
      )}
      {warning && (
        <VaultWarning
          issue={warning}
          compact
          repairing={repairingPath === warning.path}
          repairStatus={repairStatus}
          repairError={repairError}
        />
      )}
      {error && <div className="mt-1 px-1 text-[10px] leading-snug text-red-300">{error}</div>}
      {deleting && (
        <DeleteVaultDialog
          vault={deleting}
          loading={loading}
          onCancel={() => setDeleting(null)}
          onConfirm={async (confirmName) => {
            if (await onDelete(deleting, confirmName)) setDeleting(null)
          }}
        />
      )}
    </div>
  )
}

function VaultManager({
  vaults,
  loading,
  error,
  warning,
  repairingPath,
  repairStatus,
  repairError,
  onRepair,
  onRefresh,
  onSwitch,
  onCreate,
  onDelete,
}: Omit<VaultControlsProps, 'current'>) {
  const [name, setName] = useState('')
  // No literal-backend fallback: the create form stays unusable (submit
  // disabled below) until GET /api/v1/backends actually resolves and picks
  // the real first entry -- a hardcoded "ladybug" default would silently
  // create the wrong backend if the product default ever changes again.
  const [backend, setBackend] = useState('')
  const [backends, setBackends] = useState<BackendInfo[]>([])
  const [backendsLoaded, setBackendsLoaded] = useState(false)
  const [backendsError, setBackendsError] = useState<string | null>(null)
  const [acceptExperimental, setAcceptExperimental] = useState(false)
  const [deleting, setDeleting] = useState<VaultInfo | null>(null)
  const [storageUri, setStorageUri] = useState('')
  const [storageCredentialEnv, setStorageCredentialEnv] = useState('')
  const [storageDatabase, setStorageDatabase] = useState('')
  const [allowRemoteDb, setAllowRemoteDb] = useState(false)

  useEffect(() => {
    let cancelled = false
    getBackends()
      .then((available) => {
        if (cancelled) return
        setBackends(available)
        setBackendsLoaded(true)
        setBackendsError(null)
        if (available.length > 0) {
          setBackend((current) =>
            available.some((option) => option.name === current) ? current : available[0].name,
          )
        }
      })
      .catch((err) => {
        if (cancelled) return
        setBackendsError(err instanceof Error ? err.message : 'Failed to load graph backends')
      })
    return () => {
      cancelled = true
    }
  }, [])

  const selectedCapabilities = backends.find((option) => option.name === backend)?.capabilities ?? null
  const isExperimental = selectedCapabilities?.experimental === true
  const requiresNetwork = selectedCapabilities?.requires_network === true
  const isRemoteStorageUri = requiresNetwork && storageUri.trim() !== '' && !isLoopbackUri(storageUri.trim())

  const onBackendChange = (nextBackend: string) => {
    setBackend(nextBackend)
    setAcceptExperimental(false)
    setAllowRemoteDb(false)
  }

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!name.trim() || !backendsLoaded || !backend) return
    if (isExperimental && !acceptExperimental) return
    if (isRemoteStorageUri && !allowRemoteDb) return
    await onCreate(
      name,
      backend,
      isExperimental ? acceptExperimental : undefined,
      requiresNetwork
        ? {
            uri: storageUri.trim() || undefined,
            credentialEnv: storageCredentialEnv.trim() || undefined,
            database: storageDatabase.trim() || undefined,
          }
        : undefined,
      isRemoteStorageUri ? allowRemoteDb : undefined,
    )
    setName('')
    setAcceptExperimental(false)
    setStorageUri('')
    setStorageCredentialEnv('')
    setStorageDatabase('')
    setAllowRemoteDb(false)
  }

  return (
    <section className="h-full overflow-auto bg-surface-950">
      <div className="mx-auto flex min-h-full w-full max-w-5xl flex-col justify-center px-8 py-10">
        <div className="mb-8 flex items-center justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold text-surface-100">Vaults</h1>
            <p className="mt-1 text-sm text-surface-500">No vault selected</p>
          </div>
          <button
            type="button"
            onClick={() => void onRefresh()}
            disabled={loading}
            className="inline-flex items-center gap-2 rounded-lg border border-surface-800 bg-surface-900 px-3 py-2 text-sm text-surface-300 hover:bg-surface-800 disabled:opacity-50"
          >
            <RefreshCw size={15} className={loading ? 'animate-spin' : ''} />
            Refresh
          </button>
        </div>

        {warning && (
          <VaultWarning
            issue={warning}
            repairing={repairingPath === warning.path}
            repairStatus={repairStatus}
            repairError={repairError}
            onRepair={onRepair}
          />
        )}

        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
          <div className="rounded-lg border border-surface-800 bg-surface-900">
            <div className="border-b border-surface-800 px-4 py-3 text-sm font-medium text-surface-200">
              Existing
            </div>
            <div className="divide-y divide-surface-800">
              {vaults.length === 0 ? (
                <div className="px-4 py-8 text-sm text-surface-500">No vaults found</div>
              ) : (
                vaults.map((vault) => (
                  <div key={vault.path} className="flex items-center gap-2 pr-3">
                    <button
                      type="button"
                      onClick={() => void onSwitch(vault.path)}
                      disabled={loading}
                      className="flex min-w-0 flex-1 items-center gap-3 px-4 py-3 text-left hover:bg-surface-800 disabled:opacity-50"
                    >
                      <HardDrive size={18} className="shrink-0 text-surface-500" />
                      <span className="min-w-0 flex-1">
                        <span className="flex min-w-0 items-center gap-2">
                          <span className="truncate text-sm font-medium text-surface-100">
                            {vault.name}
                          </span>
                          <span className="shrink-0 rounded border border-surface-700 px-1.5 py-0.5 text-[10px] uppercase text-surface-500">
                            {vault.backend}
                          </span>
                          {vault.issue && (
                            <span className="shrink-0 rounded border border-amber-700/60 bg-amber-950/30 px-1.5 py-0.5 text-[10px] uppercase text-amber-300">
                              repair
                            </span>
                          )}
                          {!vault.managed && (
                            <span className="shrink-0 rounded border border-surface-700 px-1.5 py-0.5 text-[10px] uppercase text-surface-500">
                              external
                            </span>
                          )}
                        </span>
                        <span className="block truncate text-xs text-surface-500">
                          {vault.path}
                        </span>
                        {vault.issue && (
                          <span className="mt-1 block truncate text-xs text-amber-300">
                            Embedding width changed; re-embed required.
                          </span>
                        )}
                      </span>
                    </button>
                    <button
                      type="button"
                      onClick={() => setDeleting(vault)}
                      disabled={loading || !vault.deletable}
                      title={vault.deletable ? `Delete ${vault.name}` : vault.delete_reason ?? 'Protected vault'}
                      className="grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-surface-800 text-surface-500 hover:border-red-900 hover:bg-red-950/30 hover:text-red-300 disabled:opacity-30"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                ))
              )}
            </div>
          </div>

          <form onSubmit={(event) => void submit(event)} className="rounded-lg border border-surface-800 bg-surface-900 p-4">
            <div className="mb-4 text-sm font-medium text-surface-200">Create</div>
            <label className="mb-3 block">
              <span className="mb-1 block text-xs text-surface-500">Name</span>
              <input
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="demo-notes"
                className="w-full rounded-lg border border-surface-700 bg-surface-950 px-3 py-2 text-sm text-surface-100 outline-none placeholder:text-surface-600 focus:border-accent-600"
              />
            </label>
            <label className="mb-3 block">
              <span className="mb-1 block text-xs text-surface-500">Backend</span>
              <select
                value={backend}
                onChange={(event) => onBackendChange(event.target.value)}
                className="w-full rounded-lg border border-surface-700 bg-surface-950 px-3 py-2 text-sm text-surface-100 outline-none focus:border-accent-600"
              >
                {backends.map((option) => (
                  <option key={option.name} value={option.name}>
                    {option.name}
                    {option.capabilities?.experimental ? ' (experimental)' : ''}
                  </option>
                ))}
              </select>
            </label>
            <p className="mb-4 text-xs leading-relaxed text-surface-500">
              Uses the application defaults. You can override them after selecting the vault.
            </p>
            {requiresNetwork && (
              <div className="mb-4 space-y-3 rounded-lg border border-surface-800 bg-surface-950/50 p-3">
                <label className="block">
                  <span className="mb-1 block text-xs text-surface-500">Storage URI</span>
                  <input
                    value={storageUri}
                    onChange={(event) => setStorageUri(event.target.value)}
                    placeholder="bolt://host:7687"
                    className="w-full rounded-lg border border-surface-700 bg-surface-950 px-3 py-2 text-sm text-surface-100 outline-none placeholder:text-surface-600 focus:border-accent-600"
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs text-surface-500">Credential env var</span>
                  <input
                    value={storageCredentialEnv}
                    onChange={(event) => setStorageCredentialEnv(event.target.value)}
                    placeholder="OKTO_NEURON_NEO4J_PASSWORD"
                    className="w-full rounded-lg border border-surface-700 bg-surface-950 px-3 py-2 text-sm text-surface-100 outline-none placeholder:text-surface-600 focus:border-accent-600"
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs text-surface-500">Database (optional)</span>
                  <input
                    value={storageDatabase}
                    onChange={(event) => setStorageDatabase(event.target.value)}
                    placeholder="neo4j"
                    className="w-full rounded-lg border border-surface-700 bg-surface-950 px-3 py-2 text-sm text-surface-100 outline-none placeholder:text-surface-600 focus:border-accent-600"
                  />
                </label>
                {isRemoteStorageUri && (
                  <label className="flex cursor-pointer items-start gap-2 text-[11px] text-surface-300">
                    <input
                      type="checkbox"
                      checked={allowRemoteDb}
                      onChange={(event) => setAllowRemoteDb(event.target.checked)}
                      className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-accent-600"
                    />
                    <span>
                      Allow remote database endpoint. Vault-derived context may leave this
                      machine.
                    </span>
                  </label>
                )}
              </div>
            )}
            {isExperimental && (
              <div className="mb-4 rounded-lg border border-amber-700/50 bg-amber-950/20 px-3 py-2.5 text-amber-100">
                <div className="flex items-start gap-2">
                  <AlertTriangle size={15} className="mt-0.5 shrink-0 text-amber-300" />
                  <div className="min-w-0">
                    <div className="text-xs font-medium">{backend} is experimental</div>
                    <p className="mt-1 text-[11px] leading-relaxed text-amber-200/80">
                      Its on-disk format is pre-alpha and may change without a migration path.
                      It ships under the Elastic License 2.0 plus an Okto Labs Addendum, which is
                      not OSI-approved.
                    </p>
                    <label className="mt-2 flex cursor-pointer items-start gap-2 text-[11px] text-amber-100">
                      <input
                        type="checkbox"
                        checked={acceptExperimental}
                        onChange={(event) => setAcceptExperimental(event.target.checked)}
                        className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-amber-500"
                      />
                      <span>I accept the pre-alpha format and non-OSI license.</span>
                    </label>
                  </div>
                </div>
              </div>
            )}
            <button
              type="submit"
              disabled={
                loading ||
                !backendsLoaded ||
                !backend ||
                !name.trim() ||
                (isExperimental && !acceptExperimental) ||
                (isRemoteStorageUri && !allowRemoteDb)
              }
              className="inline-flex w-full items-center justify-center gap-2 rounded-lg bg-accent-600 px-3 py-2 text-sm font-medium text-white hover:bg-accent-500 disabled:opacity-50"
            >
              {loading ? <Loader2 size={15} className="animate-spin" /> : <Plus size={15} />}
              {backendsLoaded ? 'Create vault' : 'Loading backends...'}
            </button>
            {backendsError && (
              <div className="mt-3 text-xs leading-snug text-red-300">
                Failed to load graph backends: {backendsError}
              </div>
            )}
            {error && <div className="mt-3 text-xs leading-snug text-red-300">{error}</div>}
          </form>
        </div>
        {deleting && (
          <DeleteVaultDialog
            vault={deleting}
            loading={loading}
            onCancel={() => setDeleting(null)}
            onConfirm={async (confirmName) => {
              if (await onDelete(deleting, confirmName)) setDeleting(null)
            }}
          />
        )}
      </div>
    </section>
  )
}

function DeleteVaultDialog({
  vault,
  loading,
  onCancel,
  onConfirm,
}: {
  vault: VaultInfo
  loading: boolean
  onCancel: () => void
  onConfirm: (confirmName: string) => Promise<void>
}) {
  const [confirmation, setConfirmation] = useState('')
  const matches = confirmation === vault.name

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="delete-vault-title"
      className="fixed inset-0 z-50 grid place-items-center bg-black/70 px-4"
    >
      <div className="w-full max-w-lg rounded-xl border border-red-900/70 bg-surface-950 p-5 shadow-2xl">
        <div className="flex items-start gap-3">
          <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-red-950 text-red-300">
            <Trash2 size={18} />
          </div>
          <div className="min-w-0">
            <h2 id="delete-vault-title" className="text-base font-semibold text-surface-100">
              Delete {vault.name} permanently
            </h2>
            <p className="mt-1 text-sm leading-relaxed text-surface-400">
              This removes the entire vault directory. It cannot be undone.
              Watched external folders are never removed.
            </p>
          </div>
        </div>
        <div className="mt-4 rounded-lg border border-surface-800 bg-surface-900 px-3 py-2 font-mono text-xs text-surface-400">
          {vault.path}
        </div>
        <label className="mt-4 block">
          <span className="mb-1 block text-xs text-surface-400">
            Type <span className="font-mono text-surface-200">{vault.name}</span> to confirm
          </span>
          <input
            autoFocus
            value={confirmation}
            onChange={(event) => setConfirmation(event.target.value)}
            className="w-full rounded-lg border border-surface-700 bg-surface-900 px-3 py-2 text-sm text-surface-100 outline-none focus:border-red-700"
          />
        </label>
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={loading}
            className="rounded-lg border border-surface-700 px-3 py-2 text-sm text-surface-300 hover:bg-surface-800 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void onConfirm(confirmation)}
            disabled={loading || !matches}
            className="inline-flex items-center gap-2 rounded-lg bg-red-700 px-3 py-2 text-sm font-medium text-white hover:bg-red-600 disabled:opacity-40"
          >
            {loading ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />}
            Delete vault
          </button>
        </div>
      </div>
    </div>
  )
}

function VaultWarning({
  issue,
  compact = false,
  repairing = false,
  repairStatus,
  repairError,
  onRepair,
}: {
  issue: VaultIssue
  compact?: boolean
  repairing?: boolean
  repairStatus?: ReembedStatus | null
  repairError?: string | null
  onRepair?: (vault: string) => Promise<void>
}) {
  const progress =
    repairStatus?.phase === 'complete'
      ? `complete — ${repairStatus.recomputed ?? repairStatus.nodes_done ?? 0} vectors re-embedded`
      : repairStatus?.phase === 'embedding'
        ? `embedding ${repairStatus.nodes_done ?? 0}/${repairStatus.nodes_total ?? 0}`
        : repairStatus?.phase && repairStatus.phase !== 'idle'
          ? `${repairStatus.phase}…`
          : null

  return (
    <div
      className={`rounded-lg border border-amber-700/50 bg-amber-950/20 text-amber-100 ${
        compact ? 'mt-2 px-2 py-2' : 'mb-5 px-4 py-3'
      }`}
    >
      <div className="flex items-start gap-2">
        <AlertTriangle size={compact ? 14 : 17} className="mt-0.5 shrink-0 text-amber-300" />
        <div className="min-w-0">
          <div className={compact ? 'text-[11px] font-medium' : 'text-sm font-medium'}>
            Vault needs re-embed
          </div>
          <div className={compact ? 'mt-1 text-[10px] leading-snug text-amber-200/80' : 'mt-1 text-xs leading-relaxed text-amber-200/80'}>
            {issue.detail}
          </div>
          {issue.remedy && !compact && (
            <div className="mt-2 rounded border border-amber-800/60 bg-surface-950/40 px-2 py-1.5 font-mono text-[11px] text-amber-100">
              {issue.remedy}
            </div>
          )}
          {!compact && onRepair && (
            <div className="mt-3 flex flex-wrap items-center gap-3">
              <button
                type="button"
                onClick={() => void onRepair(issue.path)}
                disabled={repairing}
                className="inline-flex items-center gap-2 rounded-lg bg-amber-500 px-3 py-2 text-xs font-semibold text-surface-950 hover:bg-amber-400 disabled:opacity-60"
              >
                <RefreshCw size={14} className={repairing ? 'animate-spin' : ''} />
                {repairing ? 'Re-embedding...' : 'Re-embed vault'}
              </button>
              {progress && <span className="text-xs text-amber-200/80">{progress}</span>}
              {repairError && <span className="text-xs text-red-300">{repairError}</span>}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
