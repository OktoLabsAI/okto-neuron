import { apiFetch } from './http'
import type { NamedCredential, ProviderProfile, ProviderType } from '@/types'

export async function listCredentials(): Promise<NamedCredential[]> {
  const response = await apiFetch<{ credentials: NamedCredential[] }>('/credentials', {
    vaultScoped: false,
  })
  return response.credentials
}

export async function createCredential(name: string, apiKey: string): Promise<NamedCredential> {
  const response = await apiFetch<{ credential: NamedCredential }>('/credentials', {
    method: 'POST',
    body: JSON.stringify({ name, api_key: apiKey }),
    vaultScoped: false,
  })
  return response.credential
}

export async function updateCredential(
  id: string,
  patch: { name?: string; api_key?: string },
): Promise<NamedCredential> {
  const response = await apiFetch<{ credential: NamedCredential }>(`/credentials/${id}`, {
    method: 'PUT',
    body: JSON.stringify(patch),
    vaultScoped: false,
  })
  return response.credential
}

export function deleteCredential(id: string): Promise<{ deleted: string }> {
  return apiFetch(`/credentials/${id}`, { method: 'DELETE', vaultScoped: false })
}

export async function listProviders(): Promise<ProviderProfile[]> {
  const response = await apiFetch<{ providers: ProviderProfile[] }>('/providers', {
    vaultScoped: false,
  })
  return response.providers
}

export async function listProviderTypes(): Promise<ProviderType[]> {
  const response = await apiFetch<{ provider_types: ProviderType[] }>('/provider-types', {
    vaultScoped: false,
  })
  return response.provider_types
}

export interface ProviderDraft {
  name: string
  driver: string
  api_base?: string | null
  allow_remote: boolean
  credential_id?: string | null
  parameter_mode: 'safe' | 'local_extended'
  request_timeout_s: number | null
}

export async function createProvider(body: ProviderDraft): Promise<ProviderProfile> {
  const response = await apiFetch<{ provider: ProviderProfile }>('/providers', {
    method: 'POST',
    body: JSON.stringify(body),
    vaultScoped: false,
  })
  return response.provider
}

export async function updateProvider(
  id: string,
  patch: Partial<ProviderDraft>,
): Promise<ProviderProfile> {
  const response = await apiFetch<{ provider: ProviderProfile }>(`/providers/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
    vaultScoped: false,
  })
  return response.provider
}

export function deleteProvider(id: string): Promise<{ deleted: string }> {
  return apiFetch(`/providers/${id}`, { method: 'DELETE', vaultScoped: false })
}
