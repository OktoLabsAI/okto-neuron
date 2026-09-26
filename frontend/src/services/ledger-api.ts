import { apiFetch } from './http'
import { isMock } from '@/lib/mode'
import { mock } from '@/lib/mock'
import type { LedgerRunDetailResponse, LedgerRunsResponse, LedgerSummaryResponse } from '@/types'

export function getLedgerRuns(limit = 50): Promise<LedgerRunsResponse> {
  if (isMock()) return mock.ledgerRuns()
  return apiFetch<LedgerRunsResponse>(`/ledger/runs?limit=${encodeURIComponent(String(limit))}`)
}

export function getLedgerRun(runId: string): Promise<LedgerRunDetailResponse> {
  if (isMock()) return mock.ledgerRun(runId)
  return apiFetch<LedgerRunDetailResponse>(`/ledger/runs/${encodeURIComponent(runId)}`)
}

export function getLedgerSummary(runId?: string): Promise<LedgerSummaryResponse> {
  if (isMock()) return Promise.resolve({ status: 'ok', run: null })
  const qs = runId ? `?run_id=${encodeURIComponent(runId)}` : ''
  return apiFetch<LedgerSummaryResponse>(`/ledger/summary${qs}`)
}
