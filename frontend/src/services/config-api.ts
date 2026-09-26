// Config read/write service (contract §2b).
import { apiFetch } from './http'
import { isMock } from '@/lib/mode'
import { mock } from '@/lib/mock'
import type {
  AppConfig,
  ConfigScope,
  ConfigPatchResponse,
  CredentialStoreRequest,
  CredentialStoreResponse,
} from '@/types'

export function getConfig(scope: ConfigScope = 'vault'): Promise<AppConfig> {
  if (isMock()) return mock.getConfig()
  return apiFetch<AppConfig>(scope === 'application' ? '/config/defaults' : '/config', {
    vaultScoped: scope === 'vault',
  })
}

// Partial update. Returns post-write config + applied:"live"|"reembed".
export function patchConfig(
  patch: Record<string, unknown>,
  scope: ConfigScope = 'vault',
): Promise<ConfigPatchResponse> {
  if (isMock()) return mock.patchConfig(patch)
  return apiFetch<ConfigPatchResponse>(scope === 'application' ? '/config/defaults' : '/config', {
    method: 'PATCH',
    body: JSON.stringify(patch),
    vaultScoped: scope === 'vault',
  })
}

export function storeCredential(body: CredentialStoreRequest): Promise<CredentialStoreResponse> {
  if (isMock()) {
    const provider = body.provider.toUpperCase().replace(/[^A-Z0-9]+/g, '_')
    return Promise.resolve({
      status: 'ok',
      api_key_env: `OKTO_NEURON_PROVIDER_${provider}_API_KEY`,
      configured: true,
    })
  }
  return apiFetch<CredentialStoreResponse>('/credentials/provider', {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}
