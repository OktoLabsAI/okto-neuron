// Ingest service — paste/write/attach markdown, runs the companion (LLM
// extraction + gate) so it becomes queryable knowledge. Loopback-only write.
import { apiFetch } from './http'
import { isMock } from '@/lib/mode'
import { mock } from '@/lib/mock'
import type { IngestResponse, IngestQueueItemResponse, IngestQueueResponse, UploadFile } from '@/types'

export function ingest(content: string, filename?: string): Promise<IngestResponse> {
  if (isMock()) return mock.ingest(content, filename)
  return apiFetch<IngestResponse>('/ingest', {
    method: 'POST',
    body: JSON.stringify({ content, filename }),
  })
}

export function ingestFolder(path: string, recursive = true): Promise<IngestQueueResponse> {
  if (isMock()) return mock.ingestFolder(path)
  return apiFetch<IngestQueueResponse>('/ingest-folder', {
    method: 'POST',
    body: JSON.stringify({ path, recursive }),
  })
}

export function ingestBatch(files: UploadFile[]): Promise<IngestQueueResponse> {
  if (isMock()) return mock.ingestBatch(files)
  return apiFetch<IngestQueueResponse>('/ingest-batch', {
    method: 'POST',
    body: JSON.stringify({ files }),
  })
}

export function getIngestQueue(): Promise<IngestQueueResponse> {
  if (isMock()) return mock.ingestQueue()
  return apiFetch<IngestQueueResponse>('/ingest-queue', { timeoutMs: 8_000 })
}

export function getIngestQueueItem(itemId: string): Promise<IngestQueueItemResponse> {
  if (isMock()) return mock.ingestQueueItem(itemId)
  return apiFetch<IngestQueueItemResponse>(`/ingest-queue/${encodeURIComponent(itemId)}`)
}

export function retryIngestQueueItem(itemId: string): Promise<IngestQueueResponse> {
  if (isMock()) return mock.retryIngestQueueItem(itemId)
  return apiFetch<IngestQueueResponse>(`/ingest-queue/${encodeURIComponent(itemId)}/retry`, {
    method: 'POST',
    timeoutMs: 10_000,
  })
}

export function cancelIngest(): Promise<IngestQueueResponse> {
  if (isMock()) return mock.cancelIngest()
  return apiFetch<IngestQueueResponse>('/ingest-cancel', {
    method: 'POST',
    timeoutMs: 10_000,
  })
}
