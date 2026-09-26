import { apiFetch } from './http'
import { isMock } from '@/lib/mode'
import { mock } from '@/lib/mock'
import type { BackendInfo, ReembedStatus, VaultCreateRequest, VaultListResponse } from '@/types'

export function getVaults(): Promise<VaultListResponse> {
  if (isMock()) return mock.listVaults()
  return apiFetch<VaultListResponse>('/vaults', { vaultScoped: false })
}

// The graph backends this daemon can create a vault against, name plus
// capabilities (e.g. whether it's flagged `experimental`, D-12) -- e.g.
// [{ name: "ladybug", capabilities: {...} }]. Best-effort: callers should
// fall back to a single "ladybug" option if this rejects (older daemons
// don't expose the route yet).
export function getBackends(): Promise<BackendInfo[]> {
  if (isMock()) return Promise.resolve([{ name: 'ladybug', capabilities: null }])
  return apiFetch<BackendInfo[]>('/backends', { vaultScoped: false })
}

export function createVault(request: VaultCreateRequest): Promise<VaultListResponse> {
  if (isMock()) return mock.createVault(request)
  return apiFetch<VaultListResponse>('/vaults', {
    method: 'POST',
    body: JSON.stringify(request),
    vaultScoped: false,
  })
}

export function deleteVault(
  vaultId: string,
  confirmName: string,
): Promise<VaultListResponse> {
  if (isMock()) return mock.deleteVault(vaultId, confirmName)
  return apiFetch<VaultListResponse>(`/vaults/${encodeURIComponent(vaultId)}`, {
    method: 'DELETE',
    body: JSON.stringify({ confirm_name: confirmName }),
    vaultScoped: false,
  })
}

export function startVaultReembed(
  vault: string,
): Promise<{ status: string; vault: string; reembed: ReembedStatus }> {
  if (isMock()) return mock.startVaultReembed(vault)
  return apiFetch<{ status: string; vault: string; reembed: ReembedStatus }>('/vaults/reembed', {
    method: 'POST',
    body: JSON.stringify({ vault }),
    vaultScoped: false,
  })
}

export function getVaultReembedStatus(vault: string): Promise<ReembedStatus> {
  if (isMock()) return mock.getVaultReembedStatus(vault)
  return apiFetch<ReembedStatus>(`/vaults/reembed/status?vault=${encodeURIComponent(vault)}`, {
    vaultScoped: false,
  })
}
