// Query service — recall + ask with rich byte-range provenance (contract §2c).
import { apiFetch } from './http'
import { isMock } from '@/lib/mode'
import { mock } from '@/lib/mock'
import type { RecallResponse, AskResponse, AskRetrievalPolicy } from '@/types'

export function recall(query: string, k = 10): Promise<RecallResponse> {
  if (isMock()) return mock.recall(query, k)
  return apiFetch<RecallResponse>('/recall', {
    method: 'POST',
    body: JSON.stringify({ query, k }),
  })
}

export function ask(question: string, policy?: AskRetrievalPolicy): Promise<AskResponse> {
  const k = policy?.seed_k ?? 8
  if (isMock()) return mock.ask(question, k, policy)
  return apiFetch<AskResponse>('/ask', {
    method: 'POST',
    body: JSON.stringify({ question, k, retrieval_policy: policy }),
  })
}
