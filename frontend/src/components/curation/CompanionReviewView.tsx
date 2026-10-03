// Companion review queue (ADR 0009 P1) over the existing GET /review-queue +
// POST /resolve-review. Lists parked candidates from the companion contradiction
// gate; actions commit | discard | merge. SEPARATE from the reconcile
// review queue by design (ADR 0009 locked decision).
// A busy vault queues an action (202) instead of failing: the view tracks queued actions,
// polls GET /review-actions until each is final, and offers Cancel while one is queued.
import { useEffect, useMemo, useRef, useState } from 'react'
import { Bot, CheckSquare, RefreshCw, Square, Trash2 } from 'lucide-react'
import { Spinner, ErrorBox, Badge } from '@/components/ui'
import {
  cancelReviewAction,
  companionTriage,
  getJob,
  getReviewActions,
  getReviewQueue,
  resolveReview,
  resolveReviewBatch,
  type CurationJob,
  type CompanionReviewItem,
  type ReviewBatchAction,
  type ReviewAction,
} from '@/services/curation-api'
import { appendPage, REVIEW_PAGE_SIZE } from '@/lib/review-queue'
import { HttpError } from '@/services/http'
import {
  cancellable,
  describe,
  hasPending,
  isFinal,
  isQueuedAnswer,
  label,
  mergeListing,
  newlyFinal,
  pendingFor,
  POLL_MS,
  queuedActions,
  track,
  withTitles,
  type QueuedReviewAction,
  type TrackedReviewAction,
} from '@/lib/review-actions'

const ACTIONS: ReviewAction[] = ['commit', 'discard', 'merge']
const TYPES = ['Agent', 'Concept', 'Place', 'InformationObject', 'Activity']
const PAGE_SIZE = 100

type ConfidenceBucket = 'all' | 'zero' | 'lt05' | 'mid' | 'high'

function itemId(it: CompanionReviewItem): string {
  return String(it.candidate_id ?? it.id ?? '')
}

function itemKind(it: CompanionReviewItem): 'node' | 'relation' {
  return it.kind === 'relation' ? 'relation' : 'node'
}

function itemType(it: CompanionReviewItem): string {
  return typeof it.type === 'string' ? it.type : ''
}

function itemConfidence(it: CompanionReviewItem): number | null {
  return typeof it.confidence === 'number' ? it.confidence : null
}

function inBucket(it: CompanionReviewItem, bucket: ConfidenceBucket): boolean {
  if (bucket === 'all') return true
  const confidence = itemConfidence(it) ?? 0
  if (bucket === 'zero') return confidence === 0
  if (bucket === 'lt05') return confidence > 0 && confidence < 0.5
  if (bucket === 'mid') return confidence >= 0.5 && confidence <= 0.85
  return confidence > 0.85
}

function actionTone(status: string): 'default' | 'accent' | 'warn' | 'danger' {
  if (status === 'applied') return 'accent'
  if (status === 'failed') return 'danger'
  if (status === 'queued') return 'warn'
  return 'default'
}

function triageSummary(job: CurationJob | null): string {
  if (!job) return ''
  if (job.status === 'error') return job.error || 'triage failed'
  if (job.status !== 'done') return job.progress || job.status
  const result = job.result || {}
  const triaged = Number(result.triaged ?? 0)
  const committed = Number(result.committed ?? 0)
  const discarded = Number(result.discarded ?? 0)
  const kept = Number(result.kept ?? 0)
  const errors = Array.isArray(result.errors) ? result.errors.length : 0
  return `${triaged} triaged · ${committed} committed · ${discarded} discarded · ${kept} kept${errors ? ` · ${errors} errors` : ''}`
}

export function CompanionReviewView() {
  const [items, setItems] = useState<CompanionReviewItem[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [total, setTotal] = useState<number | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [tracked, setTracked] = useState<TrackedReviewAction[]>([])
  const trackedRef = useRef<TrackedReviewAction[]>(tracked)
  trackedRef.current = tracked
  const [pollTick, setPollTick] = useState(0)
  const [typeFilter, setTypeFilter] = useState('all')
  const [confidenceFilter, setConfidenceFilter] = useState<ConfidenceBucket>('all')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [confirmAction, setConfirmAction] = useState<ReviewBatchAction | null>(null)
  const [batchOutcome, setBatchOutcome] = useState<string | null>(null)
  const [page, setPage] = useState(0)
  const [triageBusy, setTriageBusy] = useState(false)
  const [activeTriageJobId, setActiveTriageJobId] = useState<string | null>(null)
  const [activeTriageJob, setActiveTriageJob] = useState<CurationJob | null>(null)
  const triagePollRef = useRef<number | null>(null)

  async function refresh() {
    setLoading(true)
    setError(null)
    try {
      // First page only; "Load more" appends. A refresh (after an action) restarts from page 1.
      const resp = await getReviewQueue({ limit: REVIEW_PAGE_SIZE })
      setItems(resp.items)
      setNextCursor(resp.next_cursor ?? null)
      setTotal(resp.total)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function loadMore() {
    if (!nextCursor || loadingMore) return
    setLoadingMore(true)
    setError(null)
    try {
      const resp = await getReviewQueue({ limit: REVIEW_PAGE_SIZE, cursor: nextCursor })
      setItems((prev) => appendPage(prev, resp.items))
      setNextCursor(resp.next_cursor ?? null)
      setTotal(resp.total)
    } catch (e) {
      // 409 review_queue_migration_required carries the remedy in its message; 400 means a stale cursor.
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoadingMore(false)
    }
  }

  useEffect(() => {
    refresh()
    // Actions queued earlier (another tab, or before a reload) are still being applied.
    getReviewActions({ status: ['queued'], limit: 500 })
      .then((resp) =>
        setTracked((prev) => track(prev, resp.items.map((a) => ({ ...a, holderText: null, title: null })))),
      )
      .catch(() => {
        /* the review queue still works without the list */
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Poll while an action is queued; a final outcome changes the review queue, so reload it.
  useEffect(() => {
    if (!hasPending(tracked)) return
    let cancelled = false
    const timer = window.setTimeout(async () => {
      try {
        const listing = await getReviewActions({ limit: 500 })
        if (cancelled) return
        const before = trackedRef.current
        const next = mergeListing(before, listing.items)
        if (next !== before) setTracked(next)
        if (newlyFinal(before, next).length) await refresh()
      } catch {
        /* keep polling */
      }
      if (!cancelled) setPollTick((t) => t + 1)
    }, POLL_MS)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tracked, pollTick])

  useEffect(() => {
    const ids = new Set(items.filter((item) => itemKind(item) === 'node').map(itemId).filter(Boolean))
    setSelected((prev) => new Set([...prev].filter((id) => ids.has(id))))
    // Actions listed at load time get their title once the item is in the list.
    setTracked((prev) => withTitles(prev, titleOf))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items])

  function titleOf(candidateId: string): string | null {
    const title = items.find((it) => itemId(it) === candidateId)?.title
    return title === undefined || title === null || title === '' ? null : String(title)
  }

  useEffect(() => {
    setPage(0)
    setConfirmAction(null)
  }, [typeFilter, confidenceFilter, items.length])

  useEffect(() => {
    if (!activeTriageJobId) return
    let cancelled = false
    async function tick() {
      try {
        const { job } = await getJob(activeTriageJobId!)
        if (cancelled) return
        setActiveTriageJob(job)
        if (job.status === 'done' || job.status === 'error') {
          setActiveTriageJobId(null)
          await refresh()
          return
        }
      } catch {
        /* keep polling */
      }
      triagePollRef.current = window.setTimeout(tick, 1500)
    }
    tick()
    return () => {
      cancelled = true
      if (triagePollRef.current) window.clearTimeout(triagePollRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTriageJobId])

  const filteredItems = useMemo(
    () =>
      items.filter((it) => {
        if (typeFilter !== 'all' && itemType(it) !== typeFilter) return false
        return inBucket(it, confidenceFilter)
      }),
    [items, typeFilter, confidenceFilter],
  )
  const filteredIds = useMemo(
    () => filteredItems.filter((item) => itemKind(item) === 'node').map(itemId).filter(Boolean),
    [filteredItems],
  )
  const nodeCount = useMemo(
    () => items.filter((item) => itemKind(item) === 'node').length,
    [items],
  )
  const pageCount = Math.max(1, Math.ceil(filteredItems.length / PAGE_SIZE))
  const visibleItems = filteredItems.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)
  const allFilteredSelected =
    filteredIds.length > 0 && filteredIds.every((id) => selected.has(id))
  const selectedCount = selected.size

  async function act(id: string, action: ReviewAction) {
    setBusy(`${id}:${action}`)
    setError(null)
    try {
      const resp = await resolveReview(id, action)
      if (isQueuedAnswer(resp)) setTracked((prev) => track(prev, queuedActions(resp, titleOf)))
      else await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  function replaceTracked(action: QueuedReviewAction) {
    setTracked((prev) =>
      prev.map((a) => (a.id === action.id ? { ...a, ...action, holderText: a.holderText, title: a.title } : a)),
    )
  }

  async function cancelQueued(actionId: string) {
    setError(null)
    try {
      const resp = await cancelReviewAction(actionId)
      replaceTracked(resp.action)
    } catch (e) {
      // 409 not_cancellable carries the action as it is now (being applied, or already final).
      const current =
        e instanceof HttpError ? (e.body as { action?: QueuedReviewAction } | null)?.action : null
      if (current) replaceTracked(current)
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  function clearFinished() {
    setTracked((prev) => prev.filter((a) => !isFinal(a.status)))
  }

  function toggleSelected(id: string) {
    setConfirmAction(null)
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function toggleAllFiltered() {
    setConfirmAction(null)
    setSelected((prev) => {
      const next = new Set(prev)
      if (allFilteredSelected) {
        filteredIds.forEach((id) => next.delete(id))
      } else {
        filteredIds.forEach((id) => next.add(id))
      }
      return next
    })
  }

  async function runBatch(action: ReviewBatchAction) {
    if (selectedCount === 0) return
    if (confirmAction !== action) {
      setConfirmAction(action)
      return
    }
    setBusy(`batch:${action}`)
    setError(null)
    setBatchOutcome(null)
    try {
      const resp = await resolveReviewBatch([...selected], action)
      const queued = queuedActions(resp, titleOf)
      if (queued.length) setTracked((prev) => track(prev, queued))
      setBatchOutcome(
        `${resp.resolved} resolved · ${resp.skipped} skipped${resp.errors.length ? ` · ${resp.errors.length} errors` : ''}${queued.length ? ` · ${queued.length} queued (the vault is busy; applied when it is free)` : ''}`,
      )
      setSelected(new Set())
      setConfirmAction(null)
      if (!isQueuedAnswer(resp) || resp.resolved > 0) await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  async function runTriage() {
    setTriageBusy(true)
    setError(null)
    setBatchOutcome(null)
    try {
      const resp = await companionTriage()
      setActiveTriageJob(resp.job)
      setActiveTriageJobId(resp.job.id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setTriageBusy(false)
    }
  }

  const triageActive =
    activeTriageJob?.status === 'queued' || activeTriageJob?.status === 'running'

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-base font-medium text-surface-200">Companion review queue</h2>
            <Badge tone={(total ?? items.length) > 0 ? 'warn' : 'default'}>
              {total ?? items.length} total
            </Badge>
            {filteredItems.length !== items.length && (
              <Badge>{filteredItems.length} filtered</Badge>
            )}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            onClick={runTriage}
            disabled={triageBusy || triageActive || nodeCount === 0}
            className="flex items-center gap-2 rounded-lg bg-cyan-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-cyan-600 disabled:opacity-50"
          >
            <Bot size={14} />
            {triageBusy || triageActive ? 'Triaging' : 'Triage with judge'}
          </button>
          <button
            onClick={refresh}
            className="flex items-center gap-2 rounded-lg border border-surface-700 px-3 py-1.5 text-sm text-surface-300 hover:bg-surface-800"
          >
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
            Refresh
          </button>
        </div>
      </div>

      {error && <ErrorBox message={error} />}
      {tracked.length > 0 && (
        <div
          role="status"
          className="space-y-1.5 rounded-lg border border-amber-900/60 bg-amber-950/20 px-3 py-2 text-xs"
        >
          <div className="flex items-center justify-between gap-3">
            <span className="font-medium text-amber-300">Review actions</span>
            {tracked.some((a) => isFinal(a.status)) && (
              <button
                onClick={clearFinished}
                className="rounded-md border border-surface-700 px-2 py-0.5 text-surface-400 hover:bg-surface-800"
              >
                Clear finished
              </button>
            )}
          </div>
          {tracked.map((a) => {
            return (
              <div key={a.id} className="flex flex-wrap items-center gap-2">
                <Badge tone={actionTone(a.status)}>{a.status}</Badge>
                <span className="text-surface-300">{label(a)}</span>
                <span className="text-surface-400">{describe(a)}</span>
                {cancellable(a) && (
                  <button
                    onClick={() => cancelQueued(a.id)}
                    className="rounded-md border border-amber-700/60 px-2 py-0.5 text-amber-200 hover:bg-amber-900/40"
                  >
                    Cancel
                  </button>
                )}
              </div>
            )
          })}
        </div>
      )}
      {(activeTriageJob || batchOutcome) && (
        <div className="rounded-lg border border-surface-800 bg-surface-950 px-3 py-2 text-xs text-surface-400">
          {activeTriageJob && (
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={activeTriageJob.status === 'error' ? 'danger' : activeTriageJob.status === 'done' ? 'accent' : 'warn'}>
                {activeTriageJob.status}
              </Badge>
              <span>{triageSummary(activeTriageJob)}</span>
            </div>
          )}
          {batchOutcome && <div>{batchOutcome}</div>}
        </div>
      )}
      {loading && items.length === 0 && <Spinner label="Loading queue…" />}
      {!loading && items.length === 0 && !error && (
        <p className="text-sm text-surface-500">Queue is empty.</p>
      )}

      {items.length > 0 && (
        <div className="space-y-3 rounded-lg border border-surface-800 bg-surface-950 p-3">
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 text-xs text-surface-500">
              Type
              <select
                value={typeFilter}
                onChange={(event) => setTypeFilter(event.target.value)}
                className="rounded-md border border-surface-700 bg-surface-900 px-2 py-1 text-xs text-surface-200"
              >
                <option value="all">All</option>
                {TYPES.map((type) => (
                  <option key={type} value={type}>
                    {type}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-2 text-xs text-surface-500">
              Confidence
              <select
                value={confidenceFilter}
                onChange={(event) => setConfidenceFilter(event.target.value as ConfidenceBucket)}
                className="rounded-md border border-surface-700 bg-surface-900 px-2 py-1 text-xs text-surface-200"
              >
                <option value="all">All</option>
                <option value="zero">0</option>
                <option value="lt05">&lt; 0.5</option>
                <option value="mid">0.5-0.85</option>
                <option value="high">&gt; 0.85</option>
              </select>
            </label>
            <button
              onClick={toggleAllFiltered}
              disabled={filteredIds.length === 0}
              className="flex items-center gap-2 rounded-md border border-surface-700 px-2.5 py-1 text-xs text-surface-300 hover:bg-surface-800 disabled:opacity-50"
            >
              {allFilteredSelected ? <CheckSquare size={14} /> : <Square size={14} />}
              Select filtered
            </button>
            <span className="text-xs text-surface-500">{selectedCount} selected</span>
          </div>

          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => runBatch('commit')}
              disabled={selectedCount === 0 || busy !== null}
              className="flex items-center gap-2 rounded-md border border-emerald-700/70 px-2.5 py-1 text-xs text-emerald-300 hover:bg-emerald-950/30 disabled:opacity-50"
            >
              <CheckSquare size={14} />
              {confirmAction === 'commit' ? `Confirm commit ${selectedCount}` : 'Commit selected'}
            </button>
            <button
              onClick={() => runBatch('discard')}
              disabled={selectedCount === 0 || busy !== null}
              className="flex items-center gap-2 rounded-md border border-rose-800/70 px-2.5 py-1 text-xs text-rose-300 hover:bg-rose-950/30 disabled:opacity-50"
            >
              <Trash2 size={14} />
              {confirmAction === 'discard' ? `Confirm discard ${selectedCount}` : 'Discard selected'}
            </button>
            {confirmAction && (
              <button
                onClick={() => setConfirmAction(null)}
                className="rounded-md border border-surface-700 px-2.5 py-1 text-xs text-surface-400 hover:bg-surface-800"
              >
                Cancel
              </button>
            )}
          </div>
        </div>
      )}

      <div className="space-y-2">
        {visibleItems.map((it) => {
          const id = itemId(it)
          const pending = pendingFor(tracked, id)
          const confidence = itemConfidence(it)
          const relation = itemKind(it) === 'relation'
          const evidence = it.source_evidence
          const proposal = it.pinned_proposal
          const admission = proposal?.admission as Record<string, unknown> | undefined
          const gateInput = proposal?.gate_input as Record<string, unknown> | undefined
          const grounding = gateInput?.grounding as Record<string, unknown> | undefined
          return (
            <div
              key={id}
              className="rounded-lg border border-surface-800 bg-surface-900 px-4 py-3"
            >
              <div className="flex gap-3">
                {relation ? (
                  <Badge tone="warn">relation</Badge>
                ) : (
                  <input
                    type="checkbox"
                    checked={selected.has(id)}
                    onChange={() => toggleSelected(id)}
                    className="mt-1 h-4 w-4 rounded border-surface-700 bg-surface-950"
                  />
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    {it.type ? <Badge tone="violet">{String(it.type)}</Badge> : null}
                    {confidence !== null && <Badge>conf {confidence.toFixed(2)}</Badge>}
                    {pending && (
                      <Badge tone="warn">
                        {pending.applying ? 'applying' : 'queued'}: {pending.action}
                      </Badge>
                    )}
                    {it.reason ? (
                      <span className="text-xs text-surface-500">{String(it.reason)}</span>
                    ) : null}
                  </div>
                  <p className="mt-1 break-words text-sm text-surface-200">
                    {String(it.title ?? id)}
                  </p>
                  <p className="break-all font-mono text-xs text-surface-600">{id}</p>
                  {relation && (
                    <div className="mt-2 grid gap-1 rounded-md border border-surface-800 bg-surface-950 p-2 text-xs text-surface-400 sm:grid-cols-2">
                      <span>Admission: {String(admission?.state ?? 'unknown')} · {String(admission?.reason ?? 'unknown')}</span>
                      <span>Predicate: {String(admission?.predicate ?? it.type ?? 'unknown')}</span>
                      {grounding && (
                        <span className="sm:col-span-2">
                          Grounding: subject {String(grounding.subject_supported ?? false)} · predicate {String(grounding.predicate_supported ?? false)} · object {String(grounding.object_supported ?? false)} · direction {String(grounding.direction_supported ?? false)}
                        </span>
                      )}
                      <span className="sm:col-span-2 text-amber-300">Read-only: relation application remains operator-orchestrated.</span>
                    </div>
                  )}
                  {evidence?.excerpt && (
                    <blockquote className="mt-2 border-l-2 border-surface-700 pl-3 text-xs text-surface-400">
                      {evidence.excerpt}{evidence.excerpt_truncated ? '…' : ''}
                    </blockquote>
                  )}
                  {evidence?.source_path && (
                    <p className="mt-1 break-all font-mono text-[11px] text-surface-600">
                      {evidence.source_path}{evidence.byte_start != null ? `:${evidence.byte_start}-${evidence.byte_end ?? '?'}` : ''}
                    </p>
                  )}
                  {!relation && (
                    <div className="mt-2 flex flex-wrap gap-2">
                      {ACTIONS.map((a) => (
                        <button
                          key={a}
                          onClick={() => act(id, a)}
                          disabled={busy !== null || pending !== null}
                          className="rounded-md border border-surface-700 px-2.5 py-1 text-xs text-surface-300 hover:bg-surface-800 disabled:opacity-50"
                        >
                          {busy === `${id}:${a}` ? '...' : a}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </div>
          )
        })}
      </div>

      {filteredItems.length > PAGE_SIZE && (
        <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-surface-500">
          <span>
            Showing {page * PAGE_SIZE + 1}-{Math.min((page + 1) * PAGE_SIZE, filteredItems.length)} of {filteredItems.length}
          </span>
          <div className="flex gap-2">
            <button
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={page === 0}
              className="rounded-md border border-surface-700 px-2.5 py-1 text-surface-300 hover:bg-surface-800 disabled:opacity-50"
            >
              Previous
            </button>
            <span className="px-2.5 py-1">
              {page + 1} / {pageCount}
            </span>
            <button
              onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
              disabled={page >= pageCount - 1}
              className="rounded-md border border-surface-700 px-2.5 py-1 text-surface-300 hover:bg-surface-800 disabled:opacity-50"
            >
              Next
            </button>
          </div>
        </div>
      )}

      {nextCursor && (
        <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-surface-500">
          <span>
            Loaded {items.length}
            {total !== null ? ` of ${total}` : ''}
          </span>
          <button
            onClick={loadMore}
            disabled={loadingMore || loading}
            className="rounded-md border border-surface-700 px-2.5 py-1 text-surface-300 hover:bg-surface-800 disabled:opacity-50"
          >
            {loadingMore ? '...' : 'Load more'}
          </button>
        </div>
      )}

      {!loading && items.length > 0 && filteredItems.length === 0 && (
        <p className="text-sm text-surface-500">No candidates match the current filters.</p>
      )}
    </div>
  )
}
