import { useCallback, useEffect, useRef, useState } from 'react'
import { BusyRetryStopped, runWithBusyRetry, type BusyWait } from '@/lib/busy-retry'

/** Run one curation action at a time, retrying while the daemon reports a busy writer lock.
 *  `wait` is non-null while a retry is pending; `stop` cancels it (also done on unmount).
 *  A second `run` while one is in flight is refused with BusyRetryStopped (never submitted). */
export function useBusyRetry() {
  const [wait, setWait] = useState<BusyWait | null>(null)
  const inFlight = useRef<AbortController | null>(null)

  useEffect(() => () => inFlight.current?.abort(), [])

  const run = useCallback(async <T,>(fn: () => Promise<T>): Promise<T> => {
    if (inFlight.current) throw new BusyRetryStopped('an action is already in flight')
    const controller = new AbortController()
    inFlight.current = controller
    try {
      return await runWithBusyRetry(fn, { signal: controller.signal, onWait: setWait })
    } finally {
      inFlight.current = null
      setWait(null)
    }
  }, [])

  const stop = useCallback(() => inFlight.current?.abort(), [])
  return { run, wait, stop }
}
