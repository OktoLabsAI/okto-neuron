import { useEffect, useState } from 'react'
import { getFolderWatchStatus } from '@/services/folder-watch-api'
import type { FolderWatchStatusResponse } from '@/types'

// Polls the live folder-watch status snapshot. Modeled on useIngestPoller:
// fast while there's something to watch (pending files), slow otherwise.
// Fetch errors are reflected by the shared connection banner; the last snapshot
// remains available but is no longer presented as proof that the daemon is live.
const ACTIVE_MS = 1200
const IDLE_MS = 5000

export function useFolderWatchStatus(): FolderWatchStatusResponse | null {
  const [status, setStatus] = useState<FolderWatchStatusResponse | null>(null)

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined

    const hasPending = (s: FolderWatchStatusResponse | null) =>
      Boolean(s && Object.values(s.vaults).some((v) => v.pending.length > 0))

    const tick = async () => {
      try {
        const s = await getFolderWatchStatus()
        if (!cancelled) setStatus(s)
        if (!cancelled) timer = setTimeout(() => void tick(), hasPending(s) ? ACTIVE_MS : IDLE_MS)
      } catch {
        if (!cancelled) timer = setTimeout(() => void tick(), IDLE_MS)
      }
    }

    void tick()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [])

  return status
}
