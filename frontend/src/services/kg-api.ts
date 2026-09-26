// KG / database browser service — read-only (contract §2a).
import { apiFetch, qs } from './http'
import { isMock } from '@/lib/mode'
import { mock } from '@/lib/mock'
import type { NodeListResponse, NodeDetail, NodeTypeInfo } from '@/types'

export async function listNodeTypes(includeStructural = false): Promise<NodeTypeInfo[]> {
  if (isMock()) return mock.listNodeTypes()
  const r = await apiFetch<{ status: string; types: NodeTypeInfo[] }>(
    `/node-types${qs({ include_structural: includeStructural ? 1 : undefined })}`,
  )
  return r.types
}

export function listNodes(params: {
  type?: string
  q?: string
  limit?: number
  offset?: number
  // Low-salience structural anchors (deterministic has_heading Claims and
  // friends) are hidden by default — they are a third of every Claim list.
  includeStructural?: boolean
}): Promise<NodeListResponse> {
  if (isMock()) return mock.listNodes(params)
  return apiFetch<NodeListResponse>(
    `/nodes${qs({
      type: params.type,
      q: params.q,
      limit: params.limit ?? 50,
      offset: params.offset ?? 0,
      include_structural: params.includeStructural ? 1 : undefined,
    })}`,
  )
}

export function getNode(id: string): Promise<NodeDetail> {
  if (isMock()) return mock.getNode(id)
  return apiFetch<NodeDetail>(`/nodes/${encodeURIComponent(id)}`)
}
