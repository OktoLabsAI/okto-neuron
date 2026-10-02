// Review actions the daemon queued because the vault was busy (202 from resolve-review or the
// batch route). Pure helpers, free of '@/…' imports so `node --test tests/review-actions.test.mjs`
// can load them directly.
//
// A queued action is applied by the daemon in arrival order once the vault is free, even after a
// restart. It ends 'applied', 'superseded' (the item was resolved elsewhere or changed meanwhile;
// nothing was changed), 'failed' or 'cancelled'. The view polls GET /review-actions until every
// action it tracks is final.

export type ReviewActionStatus = 'queued' | 'applied' | 'superseded' | 'failed' | 'cancelled'

export interface QueuedReviewAction {
  id: string
  candidate_id: string
  action: string
  status: ReviewActionStatus
  reason: string | null
  applying: boolean
  created_at: string
  finished_at: string | null
  batch_id: string | null
}

export interface TrackedReviewAction extends QueuedReviewAction {
  /** Who held the vault when the action was queued, as the daemon described it. */
  holderText: string | null
}

/** Poll interval while at least one tracked action is still queued. */
export const POLL_MS = 2000

type QueuedBody = {
  status?: unknown
  detail?: unknown
  holder?: { kind?: unknown; id?: unknown; held_for_s?: unknown } | null
  action?: unknown
  actions?: unknown
}

function isAction(v: unknown): v is QueuedReviewAction {
  const a = v as QueuedReviewAction | null
  return !!a && typeof a === 'object' && typeof a.id === 'string' && typeof a.status === 'string'
}

/** True when a review route answered 202 "queued" instead of applying the action. */
export function isQueuedAnswer(body: unknown): boolean {
  return !!body && typeof body === 'object' && (body as QueuedBody).status === 'queued'
}

/** "mcp-remember (held 12 s)" from the 202 body's holder, or null. */
export function holderText(body: unknown): string | null {
  const holder = (body as QueuedBody | null)?.holder
  if (!holder || typeof holder !== 'object' || typeof holder.kind !== 'string') return null
  const id = typeof holder.id === 'string' && holder.id !== '-' ? ` ${holder.id}` : ''
  const held =
    typeof holder.held_for_s === 'number' && Number.isFinite(holder.held_for_s)
      ? ` (held ${Math.round(holder.held_for_s)} s)`
      : ''
  return `${holder.kind}${id}${held}`
}

/** The actions a 202 body carries (single route: `action`; batch: `actions`). */
export function queuedActions(body: unknown): TrackedReviewAction[] {
  if (!isQueuedAnswer(body)) return []
  const b = body as QueuedBody
  const raw = Array.isArray(b.actions) ? b.actions : b.action ? [b.action] : []
  const text = holderText(body)
  return raw.filter(isAction).map((a) => ({ ...a, holderText: text }))
}

/** Add newly queued actions to the tracked list (an id already tracked is kept once). */
export function track(
  tracked: TrackedReviewAction[],
  added: TrackedReviewAction[],
): TrackedReviewAction[] {
  const seen = new Set(tracked.map((a) => a.id))
  const fresh = added.filter((a) => !seen.has(a.id))
  return fresh.length ? [...tracked, ...fresh] : tracked
}

/** Apply a GET /review-actions listing to the tracked actions (keeps the holder text). */
export function mergeListing(
  tracked: TrackedReviewAction[],
  listing: QueuedReviewAction[],
): TrackedReviewAction[] {
  const byId = new Map(listing.filter(isAction).map((a) => [a.id, a]))
  let changed = false
  const next = tracked.map((a) => {
    const fresh = byId.get(a.id)
    if (!fresh) return a
    if (
      fresh.status === a.status &&
      fresh.applying === a.applying &&
      fresh.reason === a.reason
    ) {
      return a
    }
    changed = true
    return { ...a, ...fresh, holderText: a.holderText }
  })
  return changed ? next : tracked
}

export function isFinal(status: string): boolean {
  return status !== 'queued'
}

export function hasPending(tracked: TrackedReviewAction[]): boolean {
  return tracked.some((a) => !isFinal(a.status))
}

/** Ids of tracked actions that became final between two lists (they change the review queue). */
export function newlyFinal(
  before: TrackedReviewAction[],
  after: TrackedReviewAction[],
): string[] {
  const was = new Map(before.map((a) => [a.id, a.status]))
  return after.filter((a) => isFinal(a.status) && was.get(a.id) === 'queued').map((a) => a.id)
}

/** The pending action of one candidate, if any (the newest wins). */
export function pendingFor(
  tracked: TrackedReviewAction[],
  candidateId: string,
): TrackedReviewAction | null {
  for (let i = tracked.length - 1; i >= 0; i--) {
    const a = tracked[i]
    if (a.candidate_id === candidateId && a.status === 'queued') return a
  }
  return null
}

/** One line for the UI. */
export function describe(a: TrackedReviewAction): string {
  switch (a.status) {
    case 'queued':
      if (a.applying) return `${a.action}: applying now`
      return `${a.action}: queued, the vault is busy${a.holderText ? ` (${a.holderText})` : ''}; it is applied when the vault is free`
    case 'applied':
      return `${a.action}: applied`
    case 'superseded':
      return `${a.action}: not applied, ${a.reason ?? 'the item changed meanwhile'}`
    case 'failed':
      return `${a.action}: failed, ${a.reason ?? 'unknown error'}`
    case 'cancelled':
      return `${a.action}: cancelled`
    default:
      return `${a.action}: ${String(a.status)}`
  }
}

/** Whether the user may still cancel it. */
export function cancellable(a: TrackedReviewAction): boolean {
  return a.status === 'queued' && !a.applying
}
