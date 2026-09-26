// Embedding test / reembed service.
import { isMock } from '@/lib/mode'
import { apiFetch } from './http'
import type {
  ConfigScope,
  EmbeddingModelDiscoveryRequest,
  EmbeddingModelDiscoveryResponse,
  EmbeddingTestRequest,
  EmbeddingTestResponse,
  ReembedStatus,
} from '@/types'

const LOCAL_PROVIDERS = new Set(['stub', 'fastembed', 'sentence-transformers'])

export function postEmbeddingTest(
  body: EmbeddingTestRequest,
  scope: ConfigScope = 'vault',
): Promise<EmbeddingTestResponse> {
  if (isMock()) {
    const models = LOCAL_PROVIDERS.has(body.provider)
      ? [body.provider]
      : ['nomic-embed-text', 'bge-small-en-v1.5', 'text-embedding-3-small']
    return Promise.resolve({
      ok: true,
      models,
      dimension: body.dimension,
      vectors: 2,
      error: null,
    })
  }
  const path = scope === 'application' ? '/config/defaults/embedding/test' : '/embedding/test'
  return apiFetch<EmbeddingTestResponse>(path, {
    method: 'POST',
    body: JSON.stringify(body),
    vaultScoped: scope === 'vault',
  })
}

export function postEmbeddingModels(
  body: EmbeddingModelDiscoveryRequest,
  scope: ConfigScope = 'vault',
): Promise<EmbeddingModelDiscoveryResponse> {
  if (isMock()) {
    return Promise.resolve({
      ok: true,
      known: body.provider === 'litellm_proxy',
      source: body.provider === 'litellm_proxy' ? 'litellm_gateway' : 'provider',
      models: body.provider === 'litellm_proxy' ? ['embedding-alias'] : [],
      error: null,
    })
  }
  const path = scope === 'application'
    ? '/config/defaults/embedding/models'
    : '/embedding/models'
  return apiFetch<EmbeddingModelDiscoveryResponse>(path, {
    method: 'POST',
    body: JSON.stringify(body),
    vaultScoped: scope === 'vault',
  })
}

// Starts a vectors-only re-embed. Returns immediately (202); poll status.
export function startReembed(): Promise<{ status: string }> {
  if (isMock()) return Promise.resolve({ status: 'started' })
  return apiFetch<{ status: string }>('/embedding/reembed', {
    method: 'POST',
    body: '{}',
  })
}

export function getReembedStatus(): Promise<ReembedStatus> {
  if (isMock()) {
    return Promise.resolve({ status: 'ok', running: false, phase: 'idle' })
  }
  return apiFetch<ReembedStatus>('/embedding/reembed/status')
}
