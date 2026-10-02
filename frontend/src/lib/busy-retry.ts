// Retry of a curation action that the daemon answered "busy" (the writer lock is held by a
// long ingest item, MCP remember or job). Pure helpers, free of '@/…' imports so
// `node --test tests/busy-retry.test.mjs` can load them directly.
//
// Safety rule: the action is re-sent ONLY after a definitive busy answer (503 busy, 409
// audit_busy). The daemon refuses those before it touches anything, so the earlier attempt
// applied nothing. A timed-out or dropped request is NOT retried: its outcome is unknown.
// Attempts run strictly one after another, never in parallel.

/** Wait before retry n (seconds): capped exponential backoff. */
export const BACKOFF_S = [2, 4, 8, 16, 30]
/** Upper bound for one wait, even when the daemon's Retry-After is larger. */
export const MAX_WAIT_S = 60

/** What the daemon said about the lock holder (all fields optional on older daemons). */
export interface BusyInfo {
  kind: string | null
  id: string | null
  heldForS: number | null
  retryAfterS: number | null
}

/** Progress shown while waiting for the next attempt. */
export interface BusyWait extends BusyInfo {
  retryInS: number
  attempt: number
  maxRetries: number
}

/** Thrown when the user stops waiting, the view unmounts, or a second submit is refused. */
export class BusyRetryStopped extends Error {
  constructor(message = 'busy retry stopped') {
    super(message)
    this.name = 'BusyRetryStopped'
  }
}

export function isBusyRetryStopped(e: unknown): boolean {
  return e instanceof BusyRetryStopped
}

type ErrLike = { status?: unknown; code?: unknown; body?: unknown; retryAfterS?: unknown }

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null
}

/** BusyInfo when `e` is a retryable busy answer, otherwise null. */
export function busyInfo(e: unknown): BusyInfo | null {
  const err = e as ErrLike | null
  if (!err || typeof err !== 'object') return null
  const busy =
    (err.status === 503 && err.code === 'busy') || (err.status === 409 && err.code === 'audit_busy')
  if (!busy) return null
  const body = (err.body && typeof err.body === 'object' ? err.body : {}) as {
    holder?: { kind?: unknown; id?: unknown; held_for_s?: unknown }
    retry_after_s?: unknown
  }
  const holder = body.holder && typeof body.holder === 'object' ? body.holder : null
  return {
    kind: typeof holder?.kind === 'string' ? holder.kind : null,
    id: typeof holder?.id === 'string' && holder.id !== '-' ? holder.id : null,
    heldForS: num(holder?.held_for_s),
    retryAfterS: num(err.retryAfterS) ?? num(body.retry_after_s),
  }
}

/** Seconds to wait before retry number `attempt` (0-based): the backoff step, or the daemon's
 *  Retry-After when that is longer, never above MAX_WAIT_S. */
export function waitSeconds(attempt: number, retryAfterS: number | null): number {
  const step = BACKOFF_S[Math.min(attempt, BACKOFF_S.length - 1)]
  return Math.min(Math.max(step, Math.ceil(retryAfterS ?? 0)), MAX_WAIT_S)
}

/** "Busy: ingest-item item-7 has held the lock for 42 s; retrying in 8 s" */
export function busyMessage(w: BusyWait): string {
  const who = [w.kind ?? 'another operation', w.id].filter(Boolean).join(' ')
  const held = w.heldForS != null ? ` has held the lock for ${Math.round(w.heldForS)} s` : ' holds the lock'
  return `Busy: ${who}${held}; retrying in ${w.retryInS} s (attempt ${w.attempt} of ${w.maxRetries})`
}

function abortableSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) return resolve()
    const t = globalThis.setTimeout(done, ms)
    function done() {
      signal?.removeEventListener('abort', done)
      globalThis.clearTimeout(t)
      resolve()
    }
    signal?.addEventListener('abort', done, { once: true })
  })
}

export interface RetryOptions {
  signal?: AbortSignal
  onWait?: (w: BusyWait) => void
  /** Injected by tests. Receives one-second ticks. */
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>
  maxRetries?: number
}

/** Run `fn`; while the daemon answers busy, wait (ticking `onWait` every second) and run it again,
 *  up to `maxRetries` times. Any other error, or the last busy answer, is rethrown. */
export async function runWithBusyRetry<T>(fn: () => Promise<T>, opts: RetryOptions = {}): Promise<T> {
  const { signal, onWait, sleep = abortableSleep, maxRetries = BACKOFF_S.length } = opts
  for (let attempt = 0; ; attempt++) {
    if (signal?.aborted) throw new BusyRetryStopped()
    try {
      return await fn()
    } catch (e) {
      const info = busyInfo(e)
      if (!info || attempt >= maxRetries) throw e
      const total = waitSeconds(attempt, info.retryAfterS)
      for (let left = total; left > 0; left--) {
        onWait?.({
          ...info,
          heldForS: info.heldForS != null ? info.heldForS + (total - left) : null,
          retryInS: left,
          attempt: attempt + 1,
          maxRetries,
        })
        await sleep(1000, signal)
        if (signal?.aborted) throw new BusyRetryStopped()
      }
    }
  }
}
