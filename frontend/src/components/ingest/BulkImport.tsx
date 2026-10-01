import { useEffect, useRef, useState } from 'react'
import {
  FolderInput,
  UploadCloud,
  CheckCircle2,
  AlertCircle,
  AlertTriangle,
  Loader2,
  FileText,
  Layers,
  MessageSquareText,
  GitBranch,
  Network,
  StopCircle,
} from 'lucide-react'
import {
  ingestFolder,
  ingestBatch,
  cancelIngest,
  getIngestQueueItem,
  retryIngestQueueItem,
} from '@/services/ingest-api'
import type { IngestEvent, IngestQueueItem, IngestQueueResponse, UploadFile } from '@/types'
import { useApp } from '@/store/app'
import { Spinner, ErrorBox, Badge } from '@/components/ui'
import { eventPayloadView } from '@/lib/ingest-events'
import { CurationProgress } from './CurationProgress'
import { IngestOutcomeBadge, IngestOutcomeSummary } from './IngestOutcomeSummary'

// Text-like sources only - Okto Neuron's trust root is markdown.
const TEXT_RE = /\.(md|markdown|txt)$/i

// Map a worker stage token to a human-readable label. Unknown stages are
// humanized (snake/kebab to spaced) so a new backend stage still reads cleanly.
const STAGE_LABELS: Record<string, string> = {
  queued: 'Queued',
  parsing: 'Parsing',
  extracting: 'Extracting',
  embedding: 'Embedding',
  dedup: 'Dedup',
  committing: 'Committing',
  done: 'Done',
  error: 'Error',
  cancelled: 'Cancelled',
}

const STAGE_ORDER: Record<string, number> = {
  queued: 0,
  parsing: 1,
  extracting: 2,
  embedding: 3,
  dedup: 4,
  committing: 5,
  done: 6,
  error: 6,
  cancelled: 6,
}

const PIPELINE_STEPS = [
  { id: 'parsing', label: 'Parse', icon: FileText },
  { id: 'extracting', label: 'Chunks', icon: Layers },
  { id: 'embedding', label: 'LLM', icon: MessageSquareText },
  { id: 'dedup', label: 'Dedup', icon: GitBranch },
  { id: 'committing', label: 'Commit', icon: Network },
]

function stageLabel(stage?: string | null): string | undefined {
  if (!stage) return undefined
  const key = stage.toLowerCase()
  return STAGE_LABELS[key] ?? key.replace(/[_-]+/g, ' ')
}

function stageProgressText(item: IngestQueueItem): string | undefined {
  const label = item.stage_progress_label?.trim()
  const done = item.stage_progress_done ?? 0
  const total = item.stage_progress_total ?? 0
  if (!label) return undefined
  return total > 0 ? `${label} ${done}/${total}` : `${label} ${done}`
}

function stageScore(item: IngestQueueItem): number {
  if (item.status === 'done') return STAGE_ORDER.done
  if (item.status === 'error') return STAGE_ORDER.error
  const key = (item.stage || item.status || 'queued').toLowerCase()
  return STAGE_ORDER[key] ?? STAGE_ORDER.queued
}

function StatusDot({ status }: { status: IngestQueueItem['status'] }) {
  if (status === 'done') return <CheckCircle2 size={15} className="text-accent-400" />
  if (status === 'error') return <AlertCircle size={15} className="text-rose-400" />
  if (status === 'cancelled') return <StopCircle size={15} className="text-surface-500" />
  if (status === 'processing') return <Loader2 size={15} className="animate-spin text-amber-400" />
  return <span className="inline-block h-[15px] w-[15px] rounded-full border border-surface-600" />
}

function Metric({ label, value, tone = 'default' }: { label: string; value: string | number; tone?: 'default' | 'accent' | 'warn' }) {
  const toneClass =
    tone === 'accent'
      ? 'text-accent-300'
      : tone === 'warn'
        ? 'text-amber-300'
        : 'text-surface-100'
  return (
    <div className="min-w-0 rounded-lg border border-surface-800 bg-surface-950/40 px-3 py-2">
      <div className={`truncate font-mono text-lg leading-6 ${toneClass}`}>{value}</div>
      <div className="truncate text-[10px] uppercase tracking-wide text-surface-500">{label}</div>
    </div>
  )
}

function EventInspector({
  item,
  loading,
  error,
}: {
  item?: IngestQueueItem
  loading: boolean
  error?: string
}) {
  if (!item) return null
  const events = item.events ?? []
  return (
    <div className="flex min-h-0 flex-col gap-3 rounded-lg border border-surface-800 bg-surface-900/40 px-4 py-4">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-medium text-surface-100">Inspect</div>
          <div className="truncate text-xs text-surface-500" title={item.path}>
            {item.name}
          </div>
        </div>
        {loading && <Loader2 size={15} className="animate-spin text-surface-500" />}
      </div>
      {error && <div className="text-xs text-rose-300">{error}</div>}
      {events.length === 0 ? (
        <div className="rounded-lg border border-surface-800 bg-surface-950/40 px-3 py-3 text-xs text-surface-500">
          No detailed events recorded for this item yet.
        </div>
      ) : (
        <div className="flex max-h-96 flex-col gap-2 overflow-y-auto pr-1">
          {events.map((event, index) => (
            <EventCard key={`${event.ts}:${event.kind}:${index}`} event={event} />
          ))}
        </div>
      )}
    </div>
  )
}

function EventCard({ event }: { event: IngestEvent }) {
  const time = new Date(event.ts * 1000).toLocaleTimeString()
  const view = eventPayloadView(event.payload)
  return (
    <details className="rounded-lg border border-surface-800 bg-surface-950/50" open={event.kind === 'llm_request' || event.kind === 'llm_response'}>
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-xs">
        <span className="rounded border border-surface-700 px-1.5 py-0.5 font-mono text-[10px] uppercase text-accent-300">
          {event.kind}
        </span>
        <span className="min-w-0 flex-1 truncate text-surface-200">{event.summary}</span>
        <span className="shrink-0 font-mono text-[10px] text-surface-600">{time}</span>
      </summary>
      {view.note && (
        <div className="border-t border-surface-800 px-3 py-1.5 text-[11px] text-amber-300">{view.note}</div>
      )}
      <pre className="max-h-72 overflow-auto border-t border-surface-800 px-3 py-3 text-[11px] leading-relaxed text-surface-300">
        {view.text}
      </pre>
    </details>
  )
}

function ChunkRibbon({ item }: { item: IngestQueueItem }) {
  const total = item.blocks_total ?? 0
  const done = item.status === 'done' ? total : item.blocks_done ?? 0
  const segments = total > 0 ? Math.min(total, 24) : 8
  const filled = total > 0 ? Math.round((done / total) * segments) : 0
  return (
    <div className="flex h-9 items-end gap-1">
      {Array.from({ length: segments }).map((_, i) => {
        const active = item.status === 'processing' && total > 0 && i === filled
        const complete = i < filled || item.status === 'done'
        return (
          <span
            key={i}
            className={`block min-w-0 flex-1 rounded-t-sm transition-all duration-500 ${
              complete
                ? 'bg-accent-500'
                : active
                  ? 'animate-pulse bg-amber-400'
                  : 'bg-surface-800'
            }`}
            style={{ height: `${14 + ((i % 5) + 1) * 4}px` }}
          />
        )
      })}
    </div>
  )
}

function PipelineMicroscope({ item }: { item?: IngestQueueItem }) {
  if (!item) return null
  const score = stageScore(item)
  const label = item.status === 'processing' ? stageLabel(item.stage) : stageLabel(item.stage) ?? item.status
  const detailedProgress = item.status === 'processing' ? stageProgressText(item) : undefined
  const chunksTotal = item.blocks_total ?? 0
  const chunksDone = item.status === 'done' ? chunksTotal : item.blocks_done ?? 0
  const chunkValue = chunksTotal > 0 ? `${chunksDone}/${chunksTotal}` : 0

  return (
    <div className="flex flex-col gap-4 rounded-lg border border-surface-800 bg-surface-900/40 px-4 py-4">
      <div className="flex items-start gap-3">
        <StatusDot status={item.status} />
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-surface-100" title={item.path}>
            {item.name}
          </div>
          <div className="text-xs text-surface-500">
            {detailedProgress ?? label}
          </div>
        </div>
        <Badge tone={item.status === 'error' ? 'danger' : item.status === 'processing' ? 'warn' : 'accent'}>
          {item.status}
        </Badge>
      </div>

      <div className="grid grid-cols-5 gap-2">
        {PIPELINE_STEPS.map((step) => {
          const Icon = step.icon
          const stepScore = STAGE_ORDER[step.id]
          const active = item.status === 'processing' && score === stepScore
          const complete = score > stepScore
          return (
            <div
              key={step.id}
              className={`flex h-20 flex-col items-center justify-center gap-2 rounded-lg border px-2 text-center transition-colors ${
                active
                  ? 'border-amber-400/70 bg-amber-950/20 text-amber-200'
                  : complete
                    ? 'border-accent-700/50 bg-accent-950/20 text-accent-200'
                    : 'border-surface-800 bg-surface-950/40 text-surface-500'
              }`}
            >
              <Icon size={17} />
              <span className="text-[11px] font-medium">{step.label}</span>
            </div>
          )
        })}
      </div>

      <ChunkRibbon item={item} />

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        <Metric label="Chunks" value={chunkValue} tone={item.status === 'processing' ? 'warn' : 'default'} />
        <Metric label="Nodes" value={item.nodes ?? 0} />
        <Metric label="Edges" value={item.edges ?? 0} />
        <Metric label="Claims" value={item.claims ?? 0} tone="accent" />
        <Metric label="Review" value={item.queued} tone={item.queued > 0 ? 'warn' : 'default'} />
      </div>
    </div>
  )
}

export function BulkImport() {
  const [path, setPath] = useState('')
  const [recursive, setRecursive] = useState(true)
  const [scanning, setScanning] = useState(false)
  const [error, setError] = useState<string | undefined>()
  // Dedup/skip counts from the most recent folder scan - surfaced even when
  // the scan enqueued nothing new (e.g. every file was already queued), so
  // an all-refreshed scan reads as "done" rather than as a silent no-op.
  const [scanSummary, setScanSummary] = useState<
    | Pick<
        IngestQueueResponse,
        'enqueued' | 'refreshed' | 'skipped_non_text' | 'skipped_excluded' | 'skipped_empty' | 'skipped'
      >
    | undefined
  >()
  const [dragging, setDragging] = useState(false)
  const [selectedId, setSelectedId] = useState<string | undefined>()
  const [stopping, setStopping] = useState(false)
  const [detail, setDetail] = useState<IngestQueueItem | undefined>()
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | undefined>()
  const [retryingId, setRetryingId] = useState<string | undefined>()
  const dirRef = useRef<HTMLInputElement>(null)

  // The queue is global state now - the app-level poller (useIngestPoller)
  // keeps it live regardless of which view is mounted. We read it here and
  // seed it with the immediate enqueue response so the UI updates instantly.
  const queue = useApp((s) => s.ingestQueue)
  const setQueue = useApp((s) => s.setIngestQueue)
  const setView = useApp((s) => s.setView)
  const setLogsMode = useApp((s) => s.setLogsMode)
  const connectionStatus = useApp((s) => s.connectionStatus)

  const openLedger = () => {
    setLogsMode('ledger')
    setView('logs')
  }

  const scanFolder = async () => {
    const p = path.trim()
    if (!p || scanning) return
    setScanning(true)
    setError(undefined)
    setScanSummary(undefined)
    try {
      const resp = await ingestFolder(p, recursive)
      setQueue(resp)
      setScanSummary({
        enqueued: resp.enqueued,
        refreshed: resp.refreshed,
        skipped_non_text: resp.skipped_non_text,
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'scan failed')
    } finally {
      setScanning(false)
    }
  }

  const readFiles = async (files: File[]): Promise<UploadFile[]> => {
    const text = files.filter((f) => TEXT_RE.test(f.name))
    return Promise.all(
      text.map(async (f) => ({
        // webkitRelativePath keeps folder structure in the source name.
        filename: (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name,
        content: await f.text(),
      })),
    )
  }

  const sendUploads = async (files: File[]) => {
    setError(undefined)
    setScanSummary(undefined)
    const payload = await readFiles(files)
    if (payload.length === 0) {
      setError('No .md / .markdown / .txt files found in that drop.')
      return
    }
    try {
      setScanning(true)
      const resp = await ingestBatch(payload)
      setQueue(resp)
      // The server - not this component's TEXT_RE - owns source selection now.
      // Surface what it refused, or a folder of dot-directory scaffolding gets
      // queued with nothing to show for it.
      setScanSummary({
        enqueued: resp.enqueued,
        refreshed: resp.refreshed,
        skipped_non_text: resp.skipped_non_text,
        skipped_excluded: resp.skipped_excluded,
        skipped_empty: resp.skipped_empty,
        skipped: resp.skipped,
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'upload failed')
    } finally {
      setScanning(false)
    }
  }

  const stopBulk = async () => {
    if (stopping) return
    setStopping(true)
    setError(undefined)
    try {
      setQueue(await cancelIngest())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'stop failed')
    } finally {
      setStopping(false)
    }
  }

  const retryItem = async (item: IngestQueueItem) => {
    if (retryingId) return
    setRetryingId(item.id)
    setError(undefined)
    try {
      setQueue(await retryIngestQueueItem(item.id))
      setDetail(undefined)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'retry failed')
    } finally {
      setRetryingId(undefined)
    }
  }

  const onDrop = async (e: React.DragEvent) => {
    e.preventDefault()
    setDragging(false)
    const files = Array.from(e.dataTransfer.files)
    if (files.length) void sendUploads(files)
  }

  const s = queue?.summary
  const queueUnavailable = connectionStatus === 'offline'
  const stopPending = Boolean(stopping || s?.cancel_requested)
  const pct = s && s.total > 0 ? Math.round(((s.done + s.error + s.cancelled) / s.total) * 100) : 0
  const hasQueue = Boolean(queue && s && s.total > 0)
  const selectedItem = queue?.items.find((it) => it.id === selectedId)
  const focusedItem =
    selectedItem ??
    queue?.items.find((it) => it.status === 'processing') ??
    [...(queue?.items ?? [])]
      .reverse()
      .find((it) => it.status === 'done' || it.status === 'error' || it.status === 'cancelled') ??
    queue?.items.find((it) => it.status === 'queued')

  useEffect(() => {
    if (!focusedItem) {
      setDetail(undefined)
      return
    }
    let cancelled = false
    setDetailLoading(true)
    setDetailError(undefined)
    void (async () => {
      try {
        const body = await getIngestQueueItem(focusedItem.id)
        if (!cancelled) setDetail(body.item)
      } catch (e) {
        if (!cancelled) setDetailError(e instanceof Error ? e.message : 'detail failed')
      } finally {
        if (!cancelled) setDetailLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [focusedItem?.id, focusedItem?.event_count, focusedItem?.status])

  return (
    <div className={`mx-auto grid w-full gap-5 ${hasQueue ? 'max-w-6xl xl:grid-cols-[minmax(380px,0.85fr)_minmax(480px,1fr)]' : 'max-w-3xl'}`}>
      <div className="flex flex-col gap-5">
        {/* Server-side folder path */}
        <div className="flex flex-col gap-2 rounded-lg border border-surface-800 bg-surface-900/40 px-4 py-4">
          <div className="flex items-center gap-2 text-sm font-medium text-surface-100">
            <FolderInput size={16} className="text-accent-400" /> Import a folder from this machine
          </div>
          <p className="text-xs text-surface-500">
            The server reads files in place (no copy). Loopback-only - this runs on the box serving the vault.
          </p>
          <div className="flex items-center gap-2">
            <input
              value={path}
              onChange={(e) => setPath(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && void scanFolder()}
              placeholder="/absolute/path/to/folder"
              className="flex-1 rounded-lg border border-surface-700 bg-surface-900 px-3 py-2 font-mono text-sm text-surface-100 placeholder:text-surface-600 focus:border-accent-600 focus:outline-none"
            />
            <button
              onClick={() => void scanFolder()}
              disabled={!path.trim() || scanning}
              className="flex h-10 items-center gap-2 rounded-lg bg-accent-600 px-4 text-sm font-medium text-white hover:bg-accent-500 disabled:opacity-40"
            >
              Import folder
            </button>
          </div>
          <label className="flex items-center gap-2 text-xs text-surface-400">
            <input
              type="checkbox"
              checked={recursive}
              onChange={(e) => setRecursive(e.target.checked)}
              className="accent-accent-600"
            />
            Recurse into subfolders
          </label>
        </div>

        {/* Browser drag-drop */}
        <div
          onDragOver={(e) => {
            e.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => void onDrop(e)}
          className={`flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed px-4 py-8 text-center transition-colors ${
            dragging ? 'border-accent-500 bg-accent-950/20' : 'border-surface-700 bg-surface-900/30'
          }`}
        >
          <UploadCloud size={22} className="text-surface-500" />
          <div className="text-sm text-surface-200">Drag a folder or files here</div>
          <div className="text-xs text-surface-600">or</div>
          <input
            ref={dirRef}
            type="file"
            multiple
            // @ts-expect-error - non-standard but widely supported folder picker
            webkitdirectory=""
            className="hidden"
            onChange={(e) => {
              const files = Array.from(e.target.files ?? [])
              if (files.length) void sendUploads(files)
              e.target.value = ''
            }}
          />
          <button
            onClick={() => dirRef.current?.click()}
            className="rounded-lg border border-surface-700 px-3 py-1.5 text-xs text-surface-300 hover:bg-surface-800"
          >
            Choose a folder
          </button>
        </div>

        {scanning && !(s && s.total > 0) && (
          <div className="rounded-lg border border-surface-800 bg-surface-900/60 px-4 py-4">
            <Spinner label="scanning..." />
          </div>
        )}
        {error && <ErrorBox message={error} />}
        {/* Dedup/skip summary from the last folder scan. Shown even when the
            scan enqueued nothing new (all-refreshed), so it reads as done -
            not as a failed/no-op scan. */}
        {!scanning &&
          scanSummary &&
          (Boolean(scanSummary.refreshed) ||
            Boolean(scanSummary.skipped_non_text) ||
            Boolean(scanSummary.skipped_excluded) ||
            Boolean(scanSummary.skipped_empty)) && (
          <div className="flex flex-col gap-1.5 rounded-lg border border-surface-800 bg-surface-900/40 px-4 py-2.5 text-xs text-surface-300">
            <div className="flex items-center gap-2">
              <CheckCircle2 size={14} className="shrink-0 text-accent-400" />
              <span>
                Scan complete: {scanSummary.enqueued ?? 0} queued
                {Boolean(scanSummary.refreshed) && `, ${scanSummary.refreshed} already queued (refreshed)`}
                {Boolean(scanSummary.skipped_excluded) && `, ${scanSummary.skipped_excluded} excluded by source policy`}
                {Boolean(scanSummary.skipped_non_text) && `, ${scanSummary.skipped_non_text} non-text skipped`}
                {Boolean(scanSummary.skipped_empty) && `, ${scanSummary.skipped_empty} empty skipped`}
              </span>
            </div>
            {/* Name what was refused. A count alone repeats the failure that
                started this: a selection nobody could see. */}
            {Boolean(scanSummary.skipped?.length) && (
              <details className="pl-6">
                <summary className="cursor-pointer text-surface-500 hover:text-surface-300">
                  Show skipped files
                </summary>
                <ul className="mt-1 max-h-40 space-y-0.5 overflow-y-auto font-mono text-[11px] text-surface-500">
                  {scanSummary.skipped?.map((sk) => (
                    <li key={`${sk.filename}:${sk.reason}`}>
                      {sk.filename} <span className="text-surface-600">({sk.reason})</span>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        )}
      </div>

      {/* Live queue - only once something has actually been enqueued. The
          global poller may seed an empty queue; don't render a blank panel. */}
      {queue && s && s.total > 0 && (
        <div className="flex min-w-0 flex-col gap-5">
          <PipelineMicroscope item={focusedItem} />
          {focusedItem && (
            <div className="overflow-hidden rounded-lg border border-surface-800 bg-surface-900/40">
              <IngestOutcomeSummary
                item={detail ?? focusedItem}
                onRetry={(item) => void retryItem(item)}
                retrying={retryingId === focusedItem.id}
              />
            </div>
          )}
          <CurationProgress active={Boolean(s?.processing && s.processing > 0)} />
          <EventInspector
            item={detail ?? focusedItem}
            loading={detailLoading}
            error={detailError}
          />
          <div className="flex flex-col gap-3 rounded-lg border border-surface-800 bg-surface-900/40 px-4 py-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2 text-sm text-surface-100">
                {queueUnavailable && s.active ? (
                  <AlertTriangle size={15} className="text-rose-400" />
                ) : s.active ? (
                  <Loader2 size={15} className="animate-spin text-amber-400" />
                ) : (
                  <CheckCircle2 size={15} className="text-accent-400" />
                )}
                {queueUnavailable && s.active
                  ? 'Status unavailable'
                  : stopPending
                    ? 'Stopping...'
                    : s.active
                      ? 'Importing...'
                      : 'Done'}
              </div>
              <div className="flex flex-wrap gap-2 text-xs">
                <Badge>{s.total} total</Badge>
                {s.done > 0 && <Badge tone="accent">{s.done} done</Badge>}
                {s.processing > 0 && <Badge tone="warn">{s.processing} processing</Badge>}
                {s.queued > 0 && <Badge>{s.queued} queued</Badge>}
                {s.error > 0 && <Badge tone="danger">{s.error} failed</Badge>}
                {s.cancelled > 0 && <Badge>{s.cancelled} cancelled</Badge>}
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              {s.active && (
                <button
                  onClick={() => void stopBulk()}
                  disabled={stopPending || queueUnavailable}
                  title={queueUnavailable ? 'Reconnect to the daemon before sending a stop request' : undefined}
                  className="inline-flex h-9 w-fit items-center gap-2 rounded-lg border border-amber-600/50 bg-amber-950/20 px-3 text-xs font-medium text-amber-200 hover:bg-amber-950/40 disabled:opacity-50"
                >
                  {stopPending ? <Loader2 size={14} className="animate-spin" /> : <StopCircle size={14} />}
                  {stopPending ? 'Stopping bulk ingest...' : 'Stop bulk ingest'}
                </button>
              )}
              <button
                type="button"
                onClick={openLedger}
                className="inline-flex h-9 w-fit items-center gap-2 rounded-lg border border-accent-600/50 bg-accent-950/20 px-3 text-xs font-medium text-accent-200 hover:bg-accent-950/40"
              >
                <GitBranch size={14} />
                Inspect candidate ledger
              </button>
            </div>

            {stopPending && (
              <div className="text-xs text-amber-300" role="status" aria-live="polite">
                Stop requested. Stopping current model work or finishing an atomic commit safely.
              </div>
            )}

            {queueUnavailable && s.active && (
              <div className="text-xs text-rose-300" role="status">
                This queue snapshot is stale. Okto Neuron cannot confirm whether ingestion is still running.
              </div>
            )}

            {queue.truncated && (
              <div className="text-[11px] text-amber-400">
                Capped at the enqueue limit - some files were not queued.
              </div>
            )}

            {/* Progress bar */}
            <div
              className="h-2 w-full overflow-hidden rounded-full bg-surface-800"
              role="progressbar"
              aria-label="Bulk ingest progress"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={pct}
            >
              <div
                className="h-full rounded-full bg-accent-500 transition-all duration-500"
                style={{ width: `${pct}%` }}
              />
            </div>

            {/* Per-file rows */}
            <div className="flex max-h-72 flex-col gap-1 overflow-y-auto">
              {queue.items.map((it) => {
                const total = it.blocks_total ?? 0
                const done = it.status === 'done' ? total : it.blocks_done ?? 0
                const showBlocks = total > 0 && (it.status === 'processing' || it.status === 'done')
                const blockPct = showBlocks ? Math.min(100, Math.round((done / total) * 100)) : 0
                const detailedProgress = it.status === 'processing' ? stageProgressText(it) : undefined
                const label = detailedProgress ?? (it.status === 'processing' ? stageLabel(it.stage) : undefined)
                const stageTotal = it.stage_progress_total ?? 0
                const stageDone = it.stage_progress_done ?? 0
                const progressPct = stageTotal > 0
                  ? Math.min(100, Math.round((stageDone / stageTotal) * 100))
                  : blockPct
                const selected = focusedItem?.id === it.id
                return (
                  <button
                    key={it.id}
                    type="button"
                    onClick={() => setSelectedId(it.id)}
                    className={`flex flex-col gap-1 rounded-lg px-2 py-1.5 text-left transition-colors ${
                      selected ? 'bg-surface-800/80' : 'hover:bg-surface-800/50'
                    }`}
                  >
                    <div className="flex min-w-0 items-center gap-2 text-sm">
                      <StatusDot status={it.status} />
                      <span className="truncate text-surface-200" title={it.path}>
                        {it.name}
                      </span>
                      <IngestOutcomeBadge item={it} />
                      <span className="ml-auto shrink-0 font-mono text-[11px] text-surface-500">
                        {it.status === 'done'
                          ? `${it.nodes ?? 0} nodes / ${it.edges ?? 0} edges / ${it.claims ?? 0} claims`
                          : it.status === 'error'
                            ? (it.error ?? 'error')
                            : it.status === 'cancelled'
                              ? 'cancelled'
                            : detailedProgress
                              ? detailedProgress
                              : showBlocks
                              ? `${done}/${total} chunks`
                              : it.status}
                      </span>
                      {it.event_count ? (
                        <span className="shrink-0 rounded border border-surface-700 px-1.5 py-0.5 text-[10px] text-surface-500">
                          {it.event_count} events
                        </span>
                      ) : null}
                    </div>
                    {it.status === 'processing' && (label || showBlocks) && (
                      <div className="flex items-center gap-2 pl-[23px]">
                        {label && (
                          <span className="shrink-0 text-[11px] text-amber-400">{label}</span>
                        )}
                        {(stageTotal > 0 || showBlocks) && (
                          <div className="h-1 flex-1 overflow-hidden rounded-full bg-surface-800">
                            <div
                              className="h-full rounded-full bg-amber-400 transition-all duration-500"
                              style={{ width: `${progressPct}%` }}
                            />
                          </div>
                        )}
                      </div>
                    )}
                  </button>
                )
              })}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
