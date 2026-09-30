// ONE shared query for GET /api/v1/status, used by the persistent migration banner.
// Application-scoped on purpose (no X-Okto-Neuron-Vault header): the daemon only lists vaults it
// refused (a v1 review queue) in the application-wide payload. One timer, paused while the tab
// is hidden (lib/shared-query.ts).
import { useSyncExternalStore } from 'react'
import { createSharedQuery } from '@/lib/shared-query'
import { migrationRequiredVaults, type MigrationRequired } from '@/lib/migration-required'
import { apiFetch } from './http'

export interface StatusReviewQueue {
  state?: string
  code?: string | null
  path?: string
  remedy?: string | null
}
export interface StatusVault {
  path: string
  review_queue?: StatusReviewQueue
}
export interface StatusVaultWarning {
  code?: string
  path?: string
  detail?: string
  remedy?: string
}
export interface StatusResponse {
  status: string
  vaults?: StatusVault[]
  vault_warning?: StatusVaultWarning | null
}

export function getStatus(): Promise<StatusResponse> {
  return apiFetch<StatusResponse>('/status', { vaultScoped: false, timeoutMs: 10_000 })
}

export const STATUS_POLL_MS = 10_000

// Last good payload is kept across transport errors; the offline banner covers the daemon being down.
export const statusQuery = createSharedQuery<StatusResponse, { migrationRequired: MigrationRequired[] }>({
  initial: { migrationRequired: [] },
  fetch: getStatus,
  loading: (prev) => prev,
  reduce: (prev, outcome) =>
    outcome.ok ? { migrationRequired: migrationRequiredVaults(outcome.body) } : prev,
  pollMs: () => STATUS_POLL_MS,
  key: () => '',
})

export function useMigrationRequired(): MigrationRequired[] {
  return useSyncExternalStore(statusQuery.subscribe, statusQuery.getState, statusQuery.getState)
    .migrationRequired
}
