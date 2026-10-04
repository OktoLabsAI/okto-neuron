// Derives the vaults a v1 review queue has locked out from the GET /api/v1/status payload.
// Kept free of '@/…' imports so `node --test tests/migration-required.test.mjs` can load it.
//
// The daemon reports a refused vault two ways (server/http.py, status route):
//   vaults[].review_queue = {state: 'migration_required', code, path, remedy}
//   vault_warning         = {code: 'review_queue_migration_required', path, remedy}
// Both are read; a vault present in both is listed once.

export interface MigrationRequired {
  vault: string
  remedy: string
}

const CODE = 'review_queue_migration_required'

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v)

const nonEmpty = (v: unknown): v is string => typeof v === 'string' && v.trim() !== ''

function vaultName(path: string): string {
  const parts = path.split(/[\\/]+/).filter(Boolean)
  return parts[parts.length - 1] ?? ''
}

export function migrationRequiredVaults(status: unknown): MigrationRequired[] {
  if (!isRecord(status)) return []
  const found = new Map<string, MigrationRequired>()
  const add = (path: unknown, remedy: unknown) => {
    if (!nonEmpty(path)) return
    const vault = vaultName(path)
    if (!vault || found.has(vault)) return
    found.set(vault, {
      vault,
      remedy: nonEmpty(remedy) ? remedy : `okto-neuron kg review-queue migrate --vault ${vault}`,
    })
  }

  if (Array.isArray(status.vaults)) {
    for (const entry of status.vaults) {
      if (!isRecord(entry) || !isRecord(entry.review_queue)) continue
      const rq = entry.review_queue
      if (rq.state !== 'migration_required') continue
      add(nonEmpty(entry.path) ? entry.path : rq.path, rq.remedy)
    }
  }
  const warning = status.vault_warning
  if (isRecord(warning) && warning.code === CODE) add(warning.path, warning.remedy)

  return [...found.values()]
}
