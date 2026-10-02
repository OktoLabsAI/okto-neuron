import type { BusyWait } from '@/lib/busy-retry'
import { busyMessage } from '@/lib/busy-retry'

/** Shown while a review action waits for the writer lock; the user can stop the retries. */
export function BusyRetryNotice({ wait, onStop }: { wait: BusyWait | null; onStop: () => void }) {
  if (!wait) return null
  return (
    <div
      role="status"
      className="flex items-center justify-between gap-3 rounded-lg border border-amber-900/60 bg-amber-950/30 px-4 py-3 text-sm text-amber-300"
    >
      <span>{busyMessage(wait)}</span>
      <button
        onClick={onStop}
        className="rounded-lg border border-amber-700/60 px-3 py-1 text-xs text-amber-200 hover:bg-amber-900/40"
      >
        Stop retrying
      </button>
    </div>
  )
}
