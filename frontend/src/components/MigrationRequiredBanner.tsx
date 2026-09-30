import { AlertTriangle } from 'lucide-react'
import { useMigrationRequired } from '@/services/status-api'

/** Persistent banner for vaults the daemon refuses until their v1 review queue is migrated. */
export function MigrationRequiredBanner() {
  const pending = useMigrationRequired()
  if (pending.length === 0) return null
  return (
    <div
      role="alert"
      data-testid="migration-required-banner"
      className="shrink-0 border-b border-amber-700/60 bg-amber-950/80 px-4 py-2.5 text-sm text-amber-100"
    >
      <div className="flex items-center gap-2 font-medium">
        <AlertTriangle size={16} className="shrink-0" />
        {pending.length === 1
          ? 'A vault is not served until its review queue is migrated'
          : `${pending.length} vaults are not served until their review queues are migrated`}
      </div>
      <ul className="mt-1 space-y-1">
        {pending.map(({ vault, remedy }) => (
          <li key={vault} data-vault={vault} className="min-w-0">
            <span className="font-medium">{vault}</span>: run{' '}
            <code className="select-all break-all rounded bg-amber-900/60 px-1.5 py-0.5 text-xs">
              {remedy}
            </code>
          </li>
        ))}
      </ul>
    </div>
  )
}
