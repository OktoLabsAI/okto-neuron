// Vault reset service — wipes all notes, ingested sources, and the derived graph.
import { apiFetch } from './http'

// POST /reset returns the locked shape {status:"ok", wiped:true}.
export async function resetVault(): Promise<{ status: string; wiped: boolean }> {
  return apiFetch<{ status: string; wiped: boolean }>('/reset', {
    method: 'POST',
    body: '{}',
  })
}
