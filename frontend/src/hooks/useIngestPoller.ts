import { useEffect } from 'react'
import { getIngestQueue } from '@/services/ingest-api'
import { useApp, isIngestActive } from '@/store/app'

// One app-level poller, mounted once in the App shell. Gives every view/session
// global awareness of an ingest job — even one started after mount, from another
// tab, or via MCP. Polls fast while active, slow while idle. The shared HTTP
// wrapper surfaces fetch failures globally; retaining the last queue here is
// useful only because the shell labels it unavailable rather than live.
const ACTIVE_MS = 1200
const IDLE_MS = 5000

export function useIngestPoller(): void {
  const setIngestQueue = useApp((s) => s.setIngestQueue)
  const active = useApp(isIngestActive)

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined

    const tick = async () => {
      try {
        const q = await getIngestQueue()
        if (!cancelled) setIngestQueue(q)
      } catch {
        // Connection state is recorded by apiFetch; retry on the normal cadence.
      } finally {
        if (!cancelled) timer = setTimeout(() => void tick(), active ? ACTIVE_MS : IDLE_MS)
      }
    }

    void tick()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [active, setIngestQueue])
}
