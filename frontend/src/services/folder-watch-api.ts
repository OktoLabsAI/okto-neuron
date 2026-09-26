// Folder-watch service (ADR 0025 continuous monitoring). Status is a stat-only
// snapshot the daemon's watch loop writes each tick; roots add/remove edit the
// request-bound vault runtime's okto-neuron.yaml.
import { apiFetch } from './http'
import type { FolderWatchConfig, FolderWatchStatusResponse } from '@/types'

export function getFolderWatchStatus(): Promise<FolderWatchStatusResponse> {
  return apiFetch<FolderWatchStatusResponse>('/folder-watch/status', { timeoutMs: 8_000 })
}

export function addFolderWatchRoot(
  path: string,
): Promise<{ status: string; folder_watch: FolderWatchConfig }> {
  return apiFetch('/folder-watch/roots', {
    method: 'POST',
    body: JSON.stringify({ path }),
  })
}

export function removeFolderWatchRoot(
  path: string,
): Promise<{ status: string; folder_watch: FolderWatchConfig }> {
  return apiFetch('/folder-watch/roots', {
    method: 'DELETE',
    body: JSON.stringify({ path }),
  })
}
