import { create } from 'zustand'
import { isMock, setMock } from '@/lib/mode'
import type { IngestQueueResponse } from '@/types'

export type View = 'browser' | 'config' | 'query' | 'ingest' | 'logs' | 'graph' | 'curation'
export type LogsMode = 'queue' | 'ledger'
export type ConnectionStatus = 'checking' | 'online' | 'offline'

interface AppState {
  view: View
  setView: (v: View) => void
  logsMode: LogsMode
  setLogsMode: (v: LogsMode) => void
  selectedNodeId: string | null
  selectNode: (id: string | null) => void
  mock: boolean
  toggleMock: (v: boolean) => void
  // Global ingest-queue awareness — written by the app-level poller
  // (useIngestPoller) and by ingest-trigger UI, read by any view.
  ingestQueue?: IngestQueueResponse
  setIngestQueue: (q: IngestQueueResponse) => void
  // Clear queue state — the always-mounted poller keeps the last response,
  // so a vault reset must zero it explicitly (KGBrowser unmounts on its own).
  clearIngestQueue: () => void
  // Shared daemon reachability. The HTTP wrapper owns updates so pollers cannot
  // silently leave stale data looking live when the server disappears.
  connectionStatus: ConnectionStatus
  connectionMessage?: string
  setConnection: (status: ConnectionStatus, message?: string) => void
  // The selected vault belongs to this browser tab. Every vault-scoped request
  // carries it explicitly; changing it never mutates daemon-global state.
  selectedVaultPath: string | null
  vaultEpoch: number
  setSelectedVault: (path: string | null) => void
  // Bumped on a vault reset — cheap invalidation hook for any already-mounted view.
  dataVersion: number
  bumpData: () => void
}

export const useApp = create<AppState>((set) => ({
  view: 'query',
  setView: (view) => set({ view }),
  logsMode: 'queue',
  setLogsMode: (logsMode) => set({ logsMode }),
  selectedNodeId: null,
  selectNode: (selectedNodeId) => set({ selectedNodeId }),
  mock: isMock(),
  toggleMock: (v) => {
    setMock(v)
    set({ mock: v })
  },
  ingestQueue: undefined,
  setIngestQueue: (ingestQueue) => set({ ingestQueue }),
  clearIngestQueue: () => set({ ingestQueue: undefined }),
  connectionStatus: 'checking',
  connectionMessage: undefined,
  setConnection: (connectionStatus, connectionMessage) =>
    set((state) =>
      state.connectionStatus === connectionStatus &&
      state.connectionMessage === connectionMessage
        ? state
        : { connectionStatus, connectionMessage },
    ),
  selectedVaultPath: null,
  vaultEpoch: 0,
  setSelectedVault: (selectedVaultPath) =>
    set((state) =>
      state.selectedVaultPath === selectedVaultPath
        ? state
        : {
            selectedVaultPath,
            vaultEpoch: state.vaultEpoch + 1,
          },
    ),
  dataVersion: 0,
  bumpData: () => set((s) => ({ dataVersion: s.dataVersion + 1 })),
}))

// Derived selector — true while anything is queued/processing.
export const isIngestActive = (s: AppState): boolean => Boolean(s.ingestQueue?.summary.active)
