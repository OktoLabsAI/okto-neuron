import { useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  CheckCircle2,
  Clock3,
  FileText,
  Filter,
  GitBranch,
  Loader2,
  RefreshCw,
  Search,
  StopCircle,
  TerminalSquare,
} from 'lucide-react'
import { getIngestQueue, getIngestQueueItem, retryIngestQueueItem } from '@/services/ingest-api'
import {
  IngestOutcomeBadge,
  IngestOutcomeSummary,
} from '@/components/ingest/IngestOutcomeSummary'
import { getLedgerRun, getLedgerRuns, getLedgerSummary } from '@/services/ledger-api'
import { useFolderWatchStatus } from '@/hooks/useFolderWatchStatus'
import { isIngestActive, useApp } from '@/store/app'
import { Badge, ErrorBox } from '@/components/ui'
import { eventPayloadView } from '@/lib/ingest-events'
import type {
  IngestEvent,
  IngestItemStatus,
  IngestQueueItem,
  LedgerRecord,
  LedgerPendingCommitPreview,
  LedgerPendingRelationSample,
  LedgerRunDetailResponse,
  LedgerRunSummary,
  LedgerSummaryResponse,
} from '@/types'

type StatusFilter = IngestItemStatus | 'all'

const STATUS_LABELS: Record<IngestItemStatus, string> = {
  queued: 'Queued',
  processing: 'Processing',
  done: 'Done',
  error: 'Error',
  cancelled: 'Cancelled',
}

const STAGE_LABELS: Record<string, string> = {
  queued: 'Queued',
  parsing: 'Parsing',
  extracting: 'Extracting',
  embedding: 'LLM',
  dedup: 'Dedup',
  committing: 'Committing',
  done: 'Done',
  error: 'Error',
  cancelled: 'Cancelled',
}

const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: 'all', label: 'All status' },
  { value: 'processing', label: 'Processing' },
  { value: 'queued', label: 'Queued' },
  { value: 'done', label: 'Done' },
  { value: 'error', label: 'Error' },
  { value: 'cancelled', label: 'Cancelled' },
]

function stageLabel(stage?: string | null): string {
  if (!stage) return ''
  const key = stage.toLowerCase()
  return STAGE_LABELS[key] ?? key.replace(/[_-]+/g, ' ')
}

function stageProgressText(item: IngestQueueItem): string {
  const label = item.stage_progress_label?.trim()
  if (!label) return ''
  const done = metricValue(item.stage_progress_done)
  const total = metricValue(item.stage_progress_total)
  return total > 0 ? `${label} ${done}/${total}` : `${label} ${done}`
}

function eventTime(ts: number): string {
  const date = new Date(ts * 1000)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString()
}

function eventDate(ts: number): string {
  const date = new Date(ts * 1000)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleDateString()
}

function statusTone(status: IngestItemStatus): 'default' | 'accent' | 'warn' | 'danger' {
  if (status === 'done') return 'accent'
  if (status === 'processing') return 'warn'
  if (status === 'error') return 'danger'
  return 'default'
}

function StatusIcon({ status }: { status: IngestItemStatus }) {
  if (status === 'done') return <CheckCircle2 size={15} className="text-accent-400" />
  if (status === 'error') return <AlertCircle size={15} className="text-rose-400" />
  if (status === 'cancelled') return <StopCircle size={15} className="text-surface-500" />
  if (status === 'processing') return <Loader2 size={15} className="animate-spin text-amber-400" />
  return <Clock3 size={15} className="text-surface-500" />
}

// v0.0.28 incremental-ingest events (ADR 0022/0023/0024) ride in the normal
// events array but carry structured payloads better summarized than their raw
// backend `summary` string. Falls back to `event.summary` for any other kind.
function friendlyEventSummary(event: IngestEvent): string | null {
  const p = event.payload ?? {}
  if (event.kind === 'subchunk_partition') {
    const hunks = typeof p.hunks_extracted === 'number' ? p.hunks_extracted : 0
    return `sub-chunk: extracted ${hunks} hunk${hunks === 1 ? '' : 's'}`
  }
  if (event.kind === 'incremental_partition') {
    const skipped = typeof p.blocks_skipped === 'number' ? p.blocks_skipped : 0
    const extracted = typeof p.blocks_extracted === 'number' ? p.blocks_extracted : 0
    return `skipped ${skipped} unchanged block${skipped === 1 ? '' : 's'}, extracted ${extracted}`
  }
  if (event.kind === 'claims_reconciled') {
    const superseded = typeof p.claims_superseded === 'number' ? p.claims_superseded : 0
    const detached = typeof p.claims_detached === 'number' ? p.claims_detached : 0
    return `superseded ${superseded} · detached ${detached}`
  }
  return null
}

function EventKindBadge({ kind }: { kind: string }) {
  const key = kind.toLowerCase()
  const tone =
    key.includes('llm') ? 'violet' : key.includes('error') ? 'danger' : key.includes('commit') ? 'accent' : 'default'
  return <Badge tone={tone}>{kind}</Badge>
}

function metricValue(value?: number | null): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function selectedFallback(items: IngestQueueItem[], current?: string | null): string | null {
  if (current && items.some((item) => item.id === current)) return current
  return (
    items.find((item) => item.status === 'processing')?.id ??
    [...items].reverse().find((item) => item.status === 'error')?.id ??
    [...items].reverse().find((item) => item.status === 'done')?.id ??
    items.find((item) => item.status === 'queued')?.id ??
    items[0]?.id ??
    null
  )
}

export function IngestLogsView() {
  const queue = useApp((s) => s.ingestQueue)
  const setQueue = useApp((s) => s.setIngestQueue)
  const setView = useApp((s) => s.setView)
  const mode = useApp((s) => s.logsMode)
  const setMode = useApp((s) => s.setLogsMode)
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [eventKindFilter, setEventKindFilter] = useState('all')
  const [query, setQuery] = useState('')
  const [errorsOnly, setErrorsOnly] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<IngestQueueItem | null>(null)
  const [selectedEventIndex, setSelectedEventIndex] = useState(0)
  const [loadingQueue, setLoadingQueue] = useState(false)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [retryingId, setRetryingId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const items = queue?.items ?? []
  const selectedItem = items.find((item) => item.id === selectedId) ?? null
  const selectedDetail = detail?.id === selectedId ? detail : selectedItem
  const events = selectedDetail?.events ?? []

  const eventKinds = useMemo(() => {
    const kinds = new Set<string>()
    for (const event of events) kinds.add(event.kind)
    return ['all', ...Array.from(kinds).sort()]
  }, [events])

  const filteredItems = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return items.filter((item) => {
      if (statusFilter !== 'all' && item.status !== statusFilter) return false
      if (errorsOnly && item.status !== 'error') return false
      if (!needle) return true
      return (
        item.name.toLowerCase().includes(needle) ||
        item.path.toLowerCase().includes(needle) ||
        (item.error ?? '').toLowerCase().includes(needle) ||
        (item.last_event?.summary ?? '').toLowerCase().includes(needle)
      )
    })
  }, [errorsOnly, items, query, statusFilter])

  const filteredEvents = useMemo(() => {
    return events.filter((event) => eventKindFilter === 'all' || event.kind === eventKindFilter)
  }, [eventKindFilter, events])

  const selectedEvent = filteredEvents[selectedEventIndex] ?? filteredEvents[0] ?? null

  useEffect(() => {
    setSelectedId((current) => selectedFallback(items, current))
  }, [items])

  useEffect(() => {
    setSelectedEventIndex(0)
  }, [eventKindFilter, selectedId])

  useEffect(() => {
    if (!selectedItem) {
      setDetail(null)
      return
    }
    let cancelled = false
    setLoadingDetail(true)
    setError(null)
    void (async () => {
      try {
        const body = await getIngestQueueItem(selectedItem.id)
        if (!cancelled) setDetail(body.item)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Log detail failed')
      } finally {
        if (!cancelled) setLoadingDetail(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [selectedItem?.id, selectedItem?.event_count, selectedItem?.status])

  const refresh = async () => {
    setLoadingQueue(true)
    setError(null)
    try {
      setQueue(await getIngestQueue())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Log refresh failed')
    } finally {
      setLoadingQueue(false)
    }
  }

  const retryItem = async (item: IngestQueueItem) => {
    if (retryingId) return
    setRetryingId(item.id)
    setError(null)
    try {
      setQueue(await retryIngestQueueItem(item.id))
      setDetail(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ingest retry failed')
    } finally {
      setRetryingId(null)
    }
  }

  const summary = queue?.summary
  const terminal = summary ? summary.done + summary.error + summary.cancelled : 0
  const total = summary?.total ?? 0
  const pct = total > 0 ? Math.round((terminal / total) * 100) : 0

  return (
    <section className="flex h-full flex-col bg-surface-950">
      <header className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-surface-800 px-6 py-4">
        <div className="min-w-0">
          <h1 className="text-base font-semibold text-surface-100">Logs</h1>
          <p className="truncate text-xs text-surface-500">
            {mode === 'queue' ? 'Ingest events and artifacts' : 'Candidate ledger and commit lineage'}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex rounded-lg border border-surface-700 bg-surface-900 p-1">
            <button
              type="button"
              onClick={() => setMode('queue')}
              className={`h-8 rounded-md px-3 text-xs ${
                mode === 'queue' ? 'bg-surface-700 text-surface-100' : 'text-surface-400 hover:text-surface-200'
              }`}
            >
              Queue
            </button>
            <button
              type="button"
              onClick={() => setMode('ledger')}
              className={`h-8 rounded-md px-3 text-xs ${
                mode === 'ledger' ? 'bg-surface-700 text-surface-100' : 'text-surface-400 hover:text-surface-200'
              }`}
            >
              Ledger
            </button>
          </div>
          {mode === 'queue' && (
            <button
              type="button"
              onClick={() => void refresh()}
              disabled={loadingQueue}
              className="inline-flex h-9 items-center gap-2 rounded-lg border border-surface-700 bg-surface-900 px-3 text-sm text-surface-300 hover:bg-surface-800 disabled:opacity-50"
            >
              <RefreshCw size={15} className={loadingQueue ? 'animate-spin' : ''} />
              Refresh
            </button>
          )}
        </div>
      </header>

      {mode === 'ledger' ? (
        <LedgerInspector />
      ) : (
      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-auto px-6 py-5">
        {error && <ErrorBox message={error} />}

        <div className="grid gap-3 md:grid-cols-5">
          <SummaryTile label="Total" value={summary?.total ?? 0} />
          <SummaryTile label="Queued" value={summary?.queued ?? 0} />
          <SummaryTile label="Processing" value={summary?.processing ?? 0} tone="warn" />
          <SummaryTile label="Done" value={summary?.done ?? 0} tone="accent" />
          <SummaryTile label="Errors" value={summary?.error ?? 0} tone="danger" />
        </div>

        <div className="h-2 shrink-0 overflow-hidden rounded-full bg-surface-800">
          <div className="h-full rounded-full bg-accent-500 transition-all duration-500" style={{ width: `${pct}%` }} />
        </div>

        <FolderWatchPanel />

        {items.length === 0 ? (
          <div className="flex min-h-0 flex-1 items-center justify-center rounded-lg border border-surface-800 bg-surface-900/40">
            <div className="flex flex-col items-center gap-3 px-6 py-10 text-center">
              <TerminalSquare size={24} className="text-surface-600" />
              <div className="text-sm text-surface-400">No ingest records</div>
              <button
                type="button"
                onClick={() => setView('ingest')}
                className="rounded-lg border border-surface-700 px-3 py-1.5 text-xs text-surface-300 hover:bg-surface-800"
              >
                Add knowledge
              </button>
            </div>
          </div>
        ) : (
          <div className="grid min-h-[560px] flex-1 gap-4 xl:grid-cols-[360px_minmax(360px,1fr)_minmax(360px,0.9fr)]">
            <div className="flex min-h-0 flex-col rounded-lg border border-surface-800 bg-surface-900/40">
              <div className="space-y-3 border-b border-surface-800 p-3">
                <div className="flex items-center gap-2">
                  <div className="relative min-w-0 flex-1">
                    <Search size={14} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-surface-600" />
                    <input
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      placeholder="Search files"
                      className="h-9 w-full rounded-lg border border-surface-700 bg-surface-950 pl-8 pr-3 text-xs text-surface-100 outline-none placeholder:text-surface-600 focus:border-accent-600"
                    />
                  </div>
                  <Filter size={15} className="text-surface-500" />
                </div>
                <div className="grid grid-cols-[1fr_auto] gap-2">
                  <select
                    value={statusFilter}
                    onChange={(event) => setStatusFilter(event.target.value as StatusFilter)}
                    className="h-9 min-w-0 rounded-lg border border-surface-700 bg-surface-950 px-2 text-xs text-surface-200 outline-none focus:border-accent-600"
                  >
                    {STATUS_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                  <label className="flex h-9 items-center gap-2 rounded-lg border border-surface-700 bg-surface-950 px-2 text-xs text-surface-400">
                    <input
                      type="checkbox"
                      checked={errorsOnly}
                      onChange={(event) => setErrorsOnly(event.target.checked)}
                      className="h-3.5 w-3.5 accent-rose-500"
                    />
                    Errors
                  </label>
                </div>
              </div>

              <div className="min-h-0 flex-1 overflow-y-auto p-2">
                {filteredItems.length === 0 ? (
                  <div className="px-3 py-8 text-center text-xs text-surface-500">No matching files</div>
                ) : (
                  filteredItems.map((item) => {
                    const selected = item.id === selectedId
                    const blocksTotal = metricValue(item.blocks_total)
                    const blocksDone = item.status === 'done' ? blocksTotal : metricValue(item.blocks_done)
                    const blockText = blocksTotal > 0 ? `${blocksDone}/${blocksTotal}` : stageLabel(item.stage) || STATUS_LABELS[item.status]
                    const currentProgress = item.status === 'processing' ? stageProgressText(item) : ''
                    const stageTotal = metricValue(item.stage_progress_total)
                    const stageDone = metricValue(item.stage_progress_done)
                    const progressTotal = stageTotal > 0 ? stageTotal : blocksTotal
                    const progressDone = stageTotal > 0 ? stageDone : blocksDone
                    return (
                      <button
                        key={item.id}
                        type="button"
                        onClick={() => setSelectedId(item.id)}
                        className={`mb-1 flex w-full flex-col gap-2 rounded-lg px-3 py-2 text-left transition-colors ${
                          selected ? 'bg-surface-800 text-surface-100' : 'hover:bg-surface-800/60'
                        }`}
                      >
                        <div className="flex min-w-0 items-center gap-2">
                          <StatusIcon status={item.status} />
                          <span className="min-w-0 flex-1 truncate text-sm text-surface-200" title={item.path}>
                            {item.name}
                          </span>
                          <Badge tone={statusTone(item.status)}>{STATUS_LABELS[item.status]}</Badge>
                          <IngestOutcomeBadge item={item} />
                        </div>
                        <div className="grid grid-cols-[1fr_auto] gap-2 text-[11px] text-surface-500">
                          <span className="truncate">
                            {currentProgress || (item.last_event
                              ? (friendlyEventSummary(item.last_event) ?? item.last_event.summary)
                              : blockText)}
                          </span>
                          <span>{item.event_count ?? 0} events</span>
                        </div>
                        {item.status === 'processing' && progressTotal > 0 && (
                          <div className="h-1 overflow-hidden rounded-full bg-surface-800">
                            <div
                              className="h-full rounded-full bg-amber-400 transition-all duration-500"
                              style={{ width: `${Math.min(100, Math.round((progressDone / progressTotal) * 100))}%` }}
                            />
                          </div>
                        )}
                      </button>
                    )
                  })
                )}
              </div>
            </div>

            <div className="flex min-h-0 flex-col rounded-lg border border-surface-800 bg-surface-900/40">
              <div className="flex shrink-0 items-start justify-between gap-3 border-b border-surface-800 p-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 text-sm font-medium text-surface-100">
                    <FileText size={15} className="text-surface-500" />
                    <span className="truncate">{selectedDetail?.name ?? 'File'}</span>
                  </div>
                  <div className="mt-1 truncate text-xs text-surface-500" title={selectedDetail?.path}>
                    {selectedDetail?.path ?? ''}
                  </div>
                </div>
                {loadingDetail && <Loader2 size={15} className="animate-spin text-surface-500" />}
              </div>

              {selectedDetail && (
                <>
                  <div className="grid shrink-0 grid-cols-2 gap-2 border-b border-surface-800 p-4 md:grid-cols-5">
                    <SummaryTile compact label="Chunks" value={metricValue(selectedDetail.blocks_total)} />
                    <SummaryTile compact label="Nodes" value={metricValue(selectedDetail.nodes)} />
                    <SummaryTile compact label="Edges" value={metricValue(selectedDetail.edges)} />
                    <SummaryTile compact label="Claims" value={metricValue(selectedDetail.claims)} tone="accent" />
                    <SummaryTile compact label="Review" value={selectedDetail.queued} tone={selectedDetail.queued > 0 ? 'warn' : 'default'} />
                  </div>
                  <IngestOutcomeSummary
                    item={selectedDetail}
                    onRetry={(item) => void retryItem(item)}
                    retrying={retryingId === selectedDetail.id}
                  />
                </>
              )}

              <div className="flex shrink-0 items-center gap-2 border-b border-surface-800 p-3">
                <select
                  value={eventKindFilter}
                  onChange={(event) => setEventKindFilter(event.target.value)}
                  className="h-9 min-w-0 rounded-lg border border-surface-700 bg-surface-950 px-2 text-xs text-surface-200 outline-none focus:border-accent-600"
                >
                  {eventKinds.map((kind) => (
                    <option key={kind} value={kind}>
                      {kind === 'all' ? 'All events' : kind}
                    </option>
                  ))}
                </select>
              </div>

              <div className="min-h-0 flex-1 overflow-y-auto p-3">
                {filteredEvents.length === 0 ? (
                  <div className="px-3 py-8 text-center text-xs text-surface-500">No events</div>
                ) : (
                  <div className="space-y-2">
                    {filteredEvents.map((event, index) => {
                      const selected = selectedEvent === event
                      return (
                        <button
                          key={`${event.ts}:${event.kind}:${index}`}
                          type="button"
                          onClick={() => setSelectedEventIndex(index)}
                          className={`w-full rounded-lg border px-3 py-2 text-left transition-colors ${
                            selected
                              ? 'border-accent-600/50 bg-accent-950/20'
                              : 'border-surface-800 bg-surface-950/40 hover:bg-surface-800/60'
                          }`}
                        >
                          <div className="mb-2 flex min-w-0 items-center gap-2">
                            <EventKindBadge kind={event.kind} />
                            <span className="ml-auto shrink-0 font-mono text-[10px] text-surface-600">
                              {eventTime(event.ts)}
                            </span>
                          </div>
                          <div className="line-clamp-2 text-xs leading-relaxed text-surface-300">
                            {friendlyEventSummary(event) ?? event.summary}
                          </div>
                        </button>
                      )
                    })}
                  </div>
                )}
              </div>
            </div>

            <div className="flex min-h-0 flex-col rounded-lg border border-surface-800 bg-surface-900/40">
              <div className="shrink-0 border-b border-surface-800 p-4">
                <div className="flex items-center gap-2 text-sm font-medium text-surface-100">
                  <TerminalSquare size={15} className="text-surface-500" />
                  Payload
                </div>
                {selectedEvent && (
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-surface-500">
                    <EventKindBadge kind={selectedEvent.kind} />
                    <span>{eventDate(selectedEvent.ts)}</span>
                    <span className="font-mono">{eventTime(selectedEvent.ts)}</span>
                  </div>
                )}
              </div>
              {selectedEvent ? (
                <div className="min-h-0 flex-1 overflow-auto">
                  <div className="border-b border-surface-800 px-4 py-3 text-sm text-surface-200">
                    {selectedEvent.summary}
                  </div>
                  {(() => {
                    const view = eventPayloadView(selectedEvent.payload)
                    return (
                      <>
                        {view.note && (
                          <div className="border-b border-surface-800 px-4 py-2 text-[11px] text-amber-300">
                            {view.note}
                          </div>
                        )}
                        <pre className="min-h-full overflow-auto p-4 text-[11px] leading-relaxed text-surface-300">
                          {view.text}
                        </pre>
                      </>
                    )
                  })()}
                </div>
              ) : (
                <div className="flex min-h-0 flex-1 items-center justify-center px-4 py-8 text-xs text-surface-500">
                  No payload selected
                </div>
              )}
            </div>
          </div>
        )}
      </div>
      )}
    </section>
  )
}

function FolderWatchPanel() {
  const status = useFolderWatchStatus()
  if (!status) return null
  const entries = Object.entries(status.vaults)
  const watched = entries.filter(([, v]) => v.enabled && v.roots.length > 0)
  if (watched.length === 0) return null
  // Each vault runtime watches independently. The aggregate badge reports
  // "paused" only when every configured runtime is temporarily paused.
  const anyWatching = watched.some(([, v]) => !v.paused_reason)

  return (
    <div className="rounded-lg border border-surface-800 bg-surface-900/40 p-4">
      <div className="mb-2 flex items-center gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-surface-400">Folder watch</h3>
        {anyWatching ? <Badge tone="accent">watching</Badge> : <Badge tone="warn">paused</Badge>}
      </div>
      <div className="flex flex-col gap-3">
        {watched.map(([vaultPath, v]) => (
          <div key={vaultPath} className="rounded-lg border border-surface-800 bg-surface-950/40 p-3 text-xs">
            <div className="flex flex-wrap items-center gap-2 text-surface-400">
              <span className="font-mono text-[11px] text-surface-500" title={vaultPath}>
                {vaultPath.split('/').pop() || vaultPath}
              </span>
              {v.paused_reason ? (
                <span title={v.paused_reason}>
                  <Badge tone="warn">paused</Badge>
                </span>
              ) : (
                <Badge tone="accent">watching</Badge>
              )}
              <span>{v.roots.length} root{v.roots.length === 1 ? '' : 's'}</span>
              <span>
                {v.watched_file_count} file{v.watched_file_count === 1 ? '' : 's'} tracked
                {Boolean(v.skipped_non_text) && ` · ${v.skipped_non_text} non-text skipped`}
              </span>
              {v.last_poll_ts && (
                <span className="ml-auto text-surface-600">
                  last poll {new Date(v.last_poll_ts * 1000).toLocaleTimeString()}
                </span>
              )}
            </div>
            {v.pending.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {v.pending.map((p) => (
                  <Badge key={p.name} tone="warn">
                    {p.name} · settling {p.settling_for_s.toFixed(1)}s
                  </Badge>
                ))}
              </div>
            )}
            {v.recent_ingests.length > 0 && (
              <div className="mt-2 truncate text-surface-500">
                recent: {v.recent_ingests.slice(-3).map((r) => r.name).join(', ')}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

function SummaryTile({
  label,
  value,
  tone = 'default',
  compact = false,
}: {
  label: string
  value: number
  tone?: 'default' | 'accent' | 'warn' | 'danger'
  compact?: boolean
}) {
  const toneClass =
    tone === 'accent'
      ? 'text-accent-300'
      : tone === 'warn'
        ? 'text-amber-300'
        : tone === 'danger'
          ? 'text-rose-300'
          : 'text-surface-100'
  return (
    <div className={`rounded-lg border border-surface-800 bg-surface-900/50 ${compact ? 'px-3 py-2' : 'px-4 py-3'}`}>
      <div className={`truncate font-mono ${compact ? 'text-base' : 'text-xl'} ${toneClass}`}>{value}</div>
      <div className="truncate text-[10px] uppercase tracking-wide text-surface-500">{label}</div>
    </div>
  )
}

function LedgerInspector() {
  const ingestActive = useApp(isIngestActive)
  const [runs, setRuns] = useState<LedgerRunSummary[]>([])
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [detail, setDetail] = useState<LedgerRunDetailResponse | null>(null)
  const [pendingSummary, setPendingSummary] = useState<LedgerSummaryResponse | null>(null)
  const [selectedRecordIndex, setSelectedRecordIndex] = useState(0)
  const [query, setQuery] = useState('')
  const [loadingRuns, setLoadingRuns] = useState(false)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refreshRuns = async (showLoading = true) => {
    if (showLoading) setLoadingRuns(true)
    setError(null)
    try {
      const body = await getLedgerRuns(100)
      setRuns(body.runs)
      setSelectedRunId((current) => current && body.runs.some((run) => run.run_id === current) ? current : body.runs[0]?.run_id ?? null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ledger refresh failed')
    } finally {
      if (showLoading) setLoadingRuns(false)
    }
  }

  useEffect(() => {
    void refreshRuns()
  }, [])

  const selectedRun = runs.find((item) => item.run_id === selectedRunId) ?? null
  const ledgerLive = ingestActive || selectedRun?.state === 'started'

  useEffect(() => {
    if (!selectedRunId) {
      setDetail(null)
      setPendingSummary(null)
      return
    }
    let cancelled = false
    setLoadingDetail(true)
    setError(null)
    void (async () => {
      try {
        const [body, summary] = await Promise.all([
          getLedgerRun(selectedRunId),
          getLedgerSummary(selectedRunId),
        ])
        if (!cancelled) {
          setDetail(body)
          setPendingSummary(summary)
          setSelectedRecordIndex(0)
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Ledger detail failed')
      } finally {
        if (!cancelled) setLoadingDetail(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [selectedRunId])

  useEffect(() => {
    if (!selectedRunId || !ledgerLive) return
    let cancelled = false
    const refreshSummary = async () => {
      try {
        const summary = await getLedgerSummary(selectedRunId)
        if (!cancelled) setPendingSummary(summary)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Ledger summary failed')
      }
    }
    void refreshSummary()
    const timer = window.setInterval(() => {
      void refreshSummary()
    }, 4000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [ledgerLive, selectedRunId])

  useEffect(() => {
    if (!ledgerLive) return
    const timer = window.setInterval(() => {
      void refreshRuns(false)
    }, 2000)
    return () => window.clearInterval(timer)
  }, [ledgerLive])

  const filteredRuns = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return runs
    return runs.filter((run) =>
      [run.name, run.source, run.document_id, run.model, run.state, run.run_id]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(needle)),
    )
  }, [query, runs])

  const records = detail?.records ?? []
  const selectedRecord = records[selectedRecordIndex] ?? records[0] ?? null
  const run = detail?.run ?? selectedRun
  const summary = run?.summary ?? {}
  const pendingPreview = pendingSummary?.pending_commit_preview ?? null
  const committed = numberFrom(summary.committed)
  const queued = numberFrom(summary.queued)
  const claimsMinted = numberFrom(summary.claims_minted)

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-auto px-6 py-5">
      {error && <ErrorBox message={error} />}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="relative w-full max-w-md">
          <Search size={14} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-surface-600" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search ledger runs"
            className="h-9 w-full rounded-lg border border-surface-700 bg-surface-950 pl-8 pr-3 text-xs text-surface-100 outline-none placeholder:text-surface-600 focus:border-accent-600"
          />
        </div>
        <button
          type="button"
          onClick={() => void refreshRuns()}
          disabled={loadingRuns}
          className="inline-flex h-9 items-center gap-2 rounded-lg border border-surface-700 bg-surface-900 px-3 text-sm text-surface-300 hover:bg-surface-800 disabled:opacity-50"
        >
          <RefreshCw size={15} className={loadingRuns ? 'animate-spin' : ''} />
          Refresh
        </button>
        {ledgerLive && (
          <Badge tone="warn">
            live
          </Badge>
        )}
      </div>

      {runs.length === 0 ? (
        <div className="flex min-h-0 flex-1 items-center justify-center rounded-lg border border-surface-800 bg-surface-900/40">
          <div className="flex flex-col items-center gap-3 px-6 py-10 text-center">
            <GitBranch size={24} className="text-surface-600" />
            <div className="text-sm text-surface-400">No candidate ledger records yet</div>
          </div>
        </div>
      ) : (
        <div className="grid min-h-[560px] flex-1 gap-4 xl:grid-cols-[360px_minmax(380px,1fr)_minmax(360px,0.9fr)]">
          <div className="flex min-h-0 flex-col rounded-lg border border-surface-800 bg-surface-900/40">
            <div className="border-b border-surface-800 p-3 text-xs font-medium uppercase tracking-wide text-surface-500">
              Runs
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-2">
              {filteredRuns.length === 0 ? (
                <div className="px-3 py-8 text-center text-xs text-surface-500">No matching runs</div>
              ) : (
                filteredRuns.map((item) => {
                  const selected = item.run_id === selectedRunId
                  return (
                    <button
                      key={item.run_id}
                      type="button"
                      onClick={() => setSelectedRunId(item.run_id)}
                      className={`mb-1 flex w-full flex-col gap-2 rounded-lg px-3 py-2 text-left transition-colors ${
                        selected ? 'bg-surface-800 text-surface-100' : 'hover:bg-surface-800/60'
                      }`}
                    >
                      <div className="flex min-w-0 items-center gap-2">
                        <GitBranch size={15} className="shrink-0 text-surface-500" />
                        <span className="min-w-0 flex-1 truncate text-sm text-surface-200" title={item.source ?? item.run_id}>
                          {item.name ?? item.run_id.slice(0, 10)}
                        </span>
                        <Badge tone={item.state === 'completed' ? 'accent' : 'warn'}>{item.state}</Badge>
                      </div>
                      <div className="grid grid-cols-[1fr_auto] gap-2 text-[11px] text-surface-500">
                        <span className="truncate">{item.model ?? 'unknown model'}</span>
                        <span>{item.counts.candidates} candidates</span>
                      </div>
                      <div className="text-[11px] text-surface-600">{formatIso(item.started_at)}</div>
                    </button>
                  )
                })
              )}
            </div>
          </div>

          <div className="flex min-h-0 flex-col rounded-lg border border-surface-800 bg-surface-900/40">
            <div className="flex shrink-0 items-start justify-between gap-3 border-b border-surface-800 p-4">
              <div className="min-w-0">
                <div className="flex items-center gap-2 text-sm font-medium text-surface-100">
                  <FileText size={15} className="text-surface-500" />
                  <span className="truncate">{run?.name ?? 'Ledger run'}</span>
                </div>
                <div className="mt-1 truncate text-xs text-surface-500" title={run?.source ?? undefined}>
                  {run?.source ?? ''}
                </div>
              </div>
              {loadingDetail && <Loader2 size={15} className="animate-spin text-surface-500" />}
            </div>

            <div className="grid shrink-0 grid-cols-2 gap-2 border-b border-surface-800 p-4 md:grid-cols-5">
              <SummaryTile compact label="Candidates" value={run?.counts.candidates ?? 0} />
              <SummaryTile compact label="Decisions" value={run?.counts.comparisons ?? 0} />
              <SummaryTile compact label="Committed" value={committed} tone="accent" />
              <SummaryTile compact label="Queued" value={queued} tone={queued > 0 ? 'warn' : 'default'} />
              <SummaryTile compact label="Claims" value={claimsMinted} tone="accent" />
            </div>

            {pendingPreview && <PendingKgPreview preview={pendingPreview} />}

            <div className="min-h-0 flex-1 overflow-y-auto p-3">
              {records.length === 0 ? (
                <div className="px-3 py-8 text-center text-xs text-surface-500">No run records</div>
              ) : (
                <div className="space-y-2">
                  {records.map((record, index) => {
                    const selected = selectedRecord === record
                    return (
                      <button
                        key={`${record.ts}:${record.kind}:${index}`}
                        type="button"
                        onClick={() => setSelectedRecordIndex(index)}
                        className={`w-full rounded-lg border px-3 py-2 text-left transition-colors ${
                          selected
                            ? 'border-accent-600/50 bg-accent-950/20'
                            : 'border-surface-800 bg-surface-950/40 hover:bg-surface-800/60'
                        }`}
                      >
                        <div className="mb-2 flex min-w-0 items-center gap-2">
                          <EventKindBadge kind={record.kind} />
                          {record.state && <Badge tone={record.state === 'committed' || record.state === 'completed' ? 'accent' : 'default'}>{record.state}</Badge>}
                          <span className="ml-auto shrink-0 font-mono text-[10px] text-surface-600">
                            {formatIsoTime(record.ts)}
                          </span>
                        </div>
                        <div className="line-clamp-2 text-xs leading-relaxed text-surface-300">
                          {recordLabel(record)}
                        </div>
                      </button>
                    )
                  })}
                </div>
              )}
            </div>
          </div>

          <div className="flex min-h-0 flex-col rounded-lg border border-surface-800 bg-surface-900/40">
            <div className="shrink-0 border-b border-surface-800 p-4">
              <div className="flex items-center gap-2 text-sm font-medium text-surface-100">
                <TerminalSquare size={15} className="text-surface-500" />
                Record
              </div>
              {selectedRecord && (
                <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-surface-500">
                  <EventKindBadge kind={selectedRecord.kind} />
                  <span>{formatIso(selectedRecord.ts)}</span>
                </div>
              )}
            </div>
            {selectedRecord ? (
              <div className="min-h-0 flex-1 overflow-auto">
                <div className="border-b border-surface-800 px-4 py-3 text-sm text-surface-200">
                  {recordLabel(selectedRecord)}
                </div>
                <pre className="min-h-full overflow-auto p-4 text-[11px] leading-relaxed text-surface-300">
                  {JSON.stringify(selectedRecord, null, 2)}
                </pre>
              </div>
            ) : (
              <div className="flex min-h-0 flex-1 items-center justify-center px-4 py-8 text-xs text-surface-500">
                No record selected
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function PendingKgPreview({ preview }: { preview: LedgerPendingCommitPreview }) {
  const nodeSamples = preview.nodes.sample_candidates_by_verdict?.commit ?? []
  const fallbackNodeTitles = preview.nodes.sample_titles_by_verdict.commit ?? []
  const relationSamples = preview.relations.sample_relations_by_verdict?.commit ?? []
  const acceptedRelationSamples = relationSamples.filter(
    (relation) => relation.terminal_action === 'create_edge_or_claim',
  )
  const predicateEntries = Object.entries(preview.relations.accepted_predicates)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 8)

  return (
    <div className="shrink-0 border-b border-surface-800 bg-surface-950/30 p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <div className="text-xs font-medium uppercase tracking-wide text-surface-500">Pending KG preview</div>
          <div className="mt-1 text-xs text-surface-400">
            {preview.nodes.accepted_for_write} accepted nodes · {preview.relations.accepted_for_write} accepted relations
          </div>
        </div>
        <Badge tone={preview.relations.accepted_for_write > 0 ? 'accent' : 'warn'}>
          pre-commit
        </Badge>
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <div className="min-w-0 rounded-lg border border-surface-800 bg-surface-900/50 p-3">
          <div className="mb-2 text-[10px] uppercase tracking-wide text-surface-500">Sample accepted nodes</div>
          {nodeSamples.length > 0 ? (
            <div className="space-y-1.5">
              {nodeSamples.slice(0, 8).map((node) => (
                <div key={node.candidate_id} className="flex min-w-0 items-center gap-2 text-xs">
                  <Badge tone="default">{node.type}</Badge>
                  <span className="truncate text-surface-200" title={node.title}>{node.title}</span>
                </div>
              ))}
            </div>
          ) : fallbackNodeTitles.length > 0 ? (
            <div className="space-y-1.5">
              {fallbackNodeTitles.slice(0, 8).map((title) => (
                <div key={title} className="truncate text-xs text-surface-200" title={title}>{title}</div>
              ))}
            </div>
          ) : (
            <div className="text-xs text-surface-500">No accepted node samples yet.</div>
          )}
        </div>

        <div className="min-w-0 rounded-lg border border-surface-800 bg-surface-900/50 p-3">
          <div className="mb-2 text-[10px] uppercase tracking-wide text-surface-500">Sample accepted relations</div>
          {acceptedRelationSamples.length > 0 ? (
            <div className="space-y-2">
              {acceptedRelationSamples.slice(0, 6).map((relation) => (
                  <div key={relation.candidate_id} className="min-w-0 text-xs">
                    <div className="truncate text-surface-200" title={relation.subject.title ?? relation.subject.ref}>
                      {relation.subject.title || relation.subject.ref}
                    </div>
                    <div className="flex min-w-0 items-center gap-2 pl-3 text-[11px] text-surface-500">
                      <span className="shrink-0 font-mono text-accent-300">{relation.predicate}</span>
                      <span className="min-w-0 truncate" title={relationObjectLabel(relation)}>
                        {relationObjectLabel(relation)}
                      </span>
                    </div>
                  </div>
                ))}
            </div>
          ) : predicateEntries.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {predicateEntries.map(([predicate, count]) => (
                <span
                  key={predicate}
                  className="rounded border border-surface-700 px-1.5 py-0.5 font-mono text-[11px] text-surface-300"
                >
                  {predicate} {count}
                </span>
              ))}
            </div>
          ) : (
            <div className="text-xs text-surface-500">No accepted relation samples yet.</div>
          )}
        </div>
      </div>
    </div>
  )
}

function relationObjectLabel(relation: LedgerPendingRelationSample): string {
  const object = relation.object
  if (!object) return ''
  if ('literal' in object) return String(object.literal)
  return object.title || object.ref || ''
}

function numberFrom(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function formatIso(value?: string | null): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return `${date.toLocaleDateString()} ${date.toLocaleTimeString()}`
}

function formatIsoTime(value?: string | null): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleTimeString()
}

function recordLabel(record: LedgerRecord): string {
  if (record.kind === 'candidate') {
    return `${record.candidate_kind ?? 'candidate'} ${record.candidate_id ?? ''} ${record.state ?? ''}`.trim()
  }
  if (record.kind === 'comparison') {
    return `${record.method ?? 'comparison'} -> ${record.verdict ?? 'verdict'}${record.target_ref ? ` (${record.target_ref})` : ''}`
  }
  if (record.kind === 'commit_plan') {
    return `commit plan ${record.plan_id ?? ''} (${record.operations?.length ?? 0} operations)`
  }
  if (record.kind === 'commit_record') {
    return `commit record ${record.plan_id ?? ''}`
  }
  if (record.kind === 'ingest_run') {
    return `ingest run ${record.state ?? ''}`.trim()
  }
  return record.kind
}
