import { AlertTriangle, Loader2, RotateCcw } from 'lucide-react'
import { Badge } from '@/components/ui'
import type {
  IngestOutcomeQuality,
  IngestQueueItem,
  IngestTechnicalOutcome,
} from '@/types'

export type DisplayOutcomeQuality = IngestOutcomeQuality | 'unknown'
type OutcomeSource = {
  outcome?: IngestTechnicalOutcome | null
}

const QUALITY_PRESENTATION: Record<
  DisplayOutcomeQuality,
  { label: string; description: string; tone: 'default' | 'accent' | 'warn' | 'danger' }
> = {
  complete: {
    label: 'Complete',
    description: 'All required extraction units completed.',
    tone: 'accent',
  },
  partial: {
    label: 'Completed with warnings',
    description: 'Useful output was stored, but one or more required units did not complete.',
    tone: 'warn',
  },
  failed: {
    label: 'Failed',
    description: 'No complete technical result was produced.',
    tone: 'danger',
  },
  integrity_failed: {
    label: 'Integrity failed',
    description: 'The graph failed its post-write integrity check.',
    tone: 'danger',
  },
  not_applicable: {
    label: 'Not applicable',
    description: 'This operation did not run the extraction pipeline.',
    tone: 'default',
  },
  empty: {
    label: 'Empty',
    description: 'The extractor ran cleanly but found no entity-grade content; the document was still stored.',
    tone: 'default',
  },
  unknown: {
    label: 'Outcome unknown',
    description: 'This item predates technical outcome reporting or has not finished yet.',
    tone: 'default',
  },
}

export function ingestOutcomeQuality(item: OutcomeSource): DisplayOutcomeQuality {
  const quality = item.outcome?.quality
  return quality && quality in QUALITY_PRESENTATION ? quality : 'unknown'
}

export function ingestFailedUnitCount(item: OutcomeSource): number {
  const units = item.outcome?.units
  const counted =
    (units?.failed ?? 0) +
    (units?.empty_after_retry ?? 0) +
    (units?.source_changed ?? 0) +
    (units?.cancelled ?? 0)
  return Math.max(item.outcome?.failed_units?.length ?? 0, counted)
}

export function ingestFailureClasses(item: OutcomeSource): string[] {
  const classes = new Set(
    (item.outcome?.failed_units ?? [])
      .map((unit) => unit.error_class?.trim())
      .filter((value): value is string => Boolean(value)),
  )
  const units = item.outcome?.units
  const outcomeClass = item.outcome?.error_class?.trim()
  if (outcomeClass) classes.add(outcomeClass)
  if ((units?.empty_after_retry ?? 0) > 0) classes.add('empty_after_retry')
  if ((units?.source_changed ?? 0) > 0) classes.add('source_changed')
  if ((units?.cancelled ?? 0) > 0) classes.add('cancelled')
  if (classes.size === 0 && ingestFailedUnitCount(item) > 0) classes.add('unclassified')
  return [...classes].sort()
}

export function ingestOutcomeRetryable(item: IngestQueueItem): boolean {
  const quality = ingestOutcomeQuality(item)
  if (typeof item.outcome?.retryable === 'boolean') return item.outcome.retryable
  const failedUnits = item.outcome?.failed_units ?? []
  if (failedUnits.some((unit) => unit.retryable === true)) return true
  if (failedUnits.some((unit) => typeof unit.retryable === 'boolean')) return false
  if (quality === 'unknown') {
    return item.status === 'error' || (item.status === 'done' && Boolean(item.provider_error))
  }
  if (quality === 'failed' && item.status === 'error' && item.outcome?.failed_units === undefined) {
    return true
  }
  return quality === 'partial' && Boolean(item.provider_error)
}

export function IngestOutcomeBadge({ item }: { item: OutcomeSource }) {
  const quality = ingestOutcomeQuality(item)
  const presentation = QUALITY_PRESENTATION[quality]
  return (
    <span data-outcome-quality={quality}>
      <Badge tone={presentation.tone}>{presentation.label}</Badge>
    </span>
  )
}

export function IngestOutcomeSummary({
  item,
  onRetry,
  retrying = false,
}: {
  item: IngestQueueItem
  onRetry?: (item: IngestQueueItem) => void
  retrying?: boolean
}) {
  const quality = ingestOutcomeQuality(item)
  const presentation = QUALITY_PRESENTATION[quality]
  const failed = ingestFailedUnitCount(item)
  const failureClasses = ingestFailureClasses(item)
  const units = item.outcome?.units
  const integrity = item.outcome?.integrity
  const hasIntegrityEvidence = Boolean(
    integrity?.status || integrity?.audit_id || integrity?.graph_generation,
  )
  const retryable = ingestOutcomeRetryable(item)

  return (
    <div className="space-y-3 border-b border-surface-800 p-4" data-outcome-quality={quality}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            {quality === 'partial' || quality === 'failed' || quality === 'integrity_failed' ? (
              <AlertTriangle size={15} className={quality === 'partial' ? 'text-amber-400' : 'text-rose-400'} />
            ) : null}
            <span className="text-xs font-medium uppercase tracking-wide text-surface-400">
              Technical outcome
            </span>
            <IngestOutcomeBadge item={item} />
          </div>
          <p className="mt-1 text-xs leading-relaxed text-surface-500">{presentation.description}</p>
        </div>
        {onRetry && retryable && (
          <button
            type="button"
            onClick={() => onRetry(item)}
            disabled={retrying}
            className="inline-flex h-8 items-center gap-2 rounded-lg border border-amber-600/50 bg-amber-950/20 px-3 text-xs font-medium text-amber-200 hover:bg-amber-950/40 disabled:opacity-50"
          >
            {retrying ? <Loader2 size={13} className="animate-spin" /> : <RotateCcw size={13} />}
            {retrying ? 'Retrying...' : `Retry${failed > 0 ? ` ${failed} failed unit${failed === 1 ? '' : 's'}` : ''}`}
          </button>
        )}
      </div>

      {quality !== 'unknown' && units && (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <OutcomeMetric label="Scheduled" value={units.scheduled ?? 0} />
          <OutcomeMetric label="Succeeded" value={units.succeeded ?? 0} />
          <OutcomeMetric label="Failed units" value={failed} danger={failed > 0} />
          <OutcomeMetric label="Skipped" value={units.skipped ?? 0} />
        </div>
      )}

      {failureClasses.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-surface-500">
          <span>Failure classes:</span>
          {failureClasses.map((failureClass) => (
            <Badge key={failureClass} tone="danger">{failureClass.replace(/[_-]+/g, ' ')}</Badge>
          ))}
          {item.outcome?.failed_units_truncated && <span>additional failed units omitted</span>}
        </div>
      )}

      {hasIntegrityEvidence && integrity && (
        <div className="rounded-lg border border-surface-800 bg-surface-950/40 px-3 py-2 text-[11px] text-surface-500">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium uppercase tracking-wide text-surface-400">
              Integrity audit
            </span>
            {integrity.status && (
              <Badge
                tone={
                  integrity.status === 'verified'
                    ? 'accent'
                    : integrity.status === 'failed' || integrity.status === 'incomplete'
                      ? 'danger'
                      : 'warn'
                }
              >
                {integrity.status}
              </Badge>
            )}
          </div>
          <dl className="mt-2 grid gap-1 sm:grid-cols-[auto_minmax(0,1fr)]">
            {integrity.audit_id && (
              <>
                <dt>audit id</dt>
                <dd className="break-all font-mono text-surface-300">{integrity.audit_id}</dd>
              </>
            )}
            {integrity.graph_generation && (
              <>
                <dt>graph generation</dt>
                <dd className="break-all font-mono text-surface-300">
                  {integrity.graph_generation}
                </dd>
              </>
            )}
          </dl>
        </div>
      )}
    </div>
  )
}

function OutcomeMetric({ label, value, danger = false }: { label: string; value: number; danger?: boolean }) {
  return (
    <div className="rounded-lg border border-surface-800 bg-surface-950/40 px-3 py-2">
      <div className={`font-mono text-sm ${danger ? 'text-rose-300' : 'text-surface-200'}`}>{value}</div>
      <div className="text-[10px] uppercase tracking-wide text-surface-600">{label}</div>
    </div>
  )
}
