// A tiny shared polling query: ONE timer and ONE in-flight request for any number of
// subscribers, paused while the tab is hidden, reset when the vault changes.
//
// Kept free of React and of '@/…' imports so `node --test tests/shared-query.test.mjs` can load
// it directly; services/predicate-snapshot.ts wires it to the app.

export interface SharedQueryEnv {
  isHidden: () => boolean
  /** Register for tab visibility changes; returns the unregister function. */
  onVisibilityChange: (cb: () => void) => () => void
  setTimer: (cb: () => void, ms: number) => unknown
  clearTimer: (handle: unknown) => void
}

export type Outcome<Body> = { ok: true; body: Body } | { ok: false; error: string }

export interface SharedQueryConfig<Body, State> {
  initial: State
  fetch: () => Promise<Body>
  /** Fold a response (or a failure) into the state. Must keep the last good data on failure. */
  reduce: (prev: State, outcome: Outcome<Body>) => State
  /** Mark the state as "request in flight" (only while there is nothing to show yet). */
  loading: (prev: State) => State
  /** Milliseconds until the next poll, given the current state. */
  pollMs: (state: State) => number
  /** Identity of what is being queried (the selected vault); a change drops the state. */
  key: () => string
  env?: SharedQueryEnv
}

export interface SharedQuery<State> {
  getState: () => State
  subscribe: (listener: () => void) => () => void
  refresh: () => Promise<void>
  reset: () => void
}

const browserEnv = (): SharedQueryEnv => ({
  isHidden: () => typeof document !== 'undefined' && document.visibilityState === 'hidden',
  onVisibilityChange: (cb) => {
    if (typeof document === 'undefined') return () => {}
    document.addEventListener('visibilitychange', cb)
    return () => document.removeEventListener('visibilitychange', cb)
  },
  setTimer: (cb, ms) => setTimeout(cb, ms),
  clearTimer: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
})

export function createSharedQuery<Body, State>(
  cfg: SharedQueryConfig<Body, State>,
): SharedQuery<State> {
  const env = cfg.env ?? browserEnv()
  let state = cfg.initial
  let key = ''
  let inFlight: Promise<void> | null = null
  let timer: unknown = null
  let unbindVisibility: (() => void) | null = null
  const listeners = new Set<() => void>()

  const set = (next: State) => {
    state = next
    listeners.forEach((l) => l())
  }

  const clearTimer = () => {
    if (timer !== null) {
      env.clearTimer(timer)
      timer = null
    }
  }

  const schedule = () => {
    clearTimer()
    if (listeners.size === 0 || env.isHidden()) return
    timer = env.setTimer(() => {
      timer = null
      void refresh().finally(schedule)
    }, cfg.pollMs(state))
  }

  function refresh(): Promise<void> {
    if (inFlight) return inFlight // every caller shares the one request
    if (key !== cfg.key()) {
      key = cfg.key()
      set(cfg.initial) // another vault: never show the previous one's data
    }
    const requestKey = key
    set(cfg.loading(state))
    inFlight = (async () => {
      try {
        const body = await cfg.fetch()
        if (requestKey === cfg.key()) set(cfg.reduce(state, { ok: true, body }))
      } catch (e) {
        if (requestKey === cfg.key()) {
          set(cfg.reduce(state, { ok: false, error: e instanceof Error ? e.message : String(e) }))
        }
      } finally {
        inFlight = null
      }
    })()
    return inFlight
  }

  const onVisibility = () => {
    if (listeners.size === 0) return
    if (env.isHidden()) clearTimer()
    else void refresh().finally(schedule)
  }

  return {
    getState: () => state,
    subscribe(listener) {
      listeners.add(listener)
      if (!unbindVisibility) unbindVisibility = env.onVisibilityChange(onVisibility)
      if (listeners.size === 1 && !env.isHidden()) void refresh().finally(schedule)
      return () => {
        listeners.delete(listener)
        if (listeners.size === 0) clearTimer()
      }
    },
    refresh,
    reset() {
      clearTimer()
      listeners.clear()
      unbindVisibility?.()
      unbindVisibility = null
      inFlight = null
      key = ''
      state = cfg.initial
    },
  }
}
