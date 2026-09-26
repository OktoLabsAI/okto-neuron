// The rarely-used surfaces, demoted but fully functional: apply decisions
// (heal/rebuild), audit past merges (authority), and manual sweep triggers.
import { useState } from 'react'
import { AlertTriangle, GitMerge, Network, Wrench } from 'lucide-react'
import { AuthorityView } from './AuthorityView'
import { RebuildHealView } from './RebuildHealView'
import { DriftView } from './DriftView'
import { ReconcileRunView } from './ReconcileRunView'

type Section = 'rebuild-heal' | 'authority' | 'drift' | 'reconcile-run'

const SECTIONS: { id: Section; label: string; icon: typeof Wrench; hint: string }[] = [
  {
    id: 'rebuild-heal',
    label: 'Apply / Heal',
    icon: Wrench,
    hint: 'materialize confirmed decisions into the graph',
  },
  {
    id: 'authority',
    label: 'Authority',
    icon: Network,
    hint: 'audit confirmed merges; undo here',
  },
  {
    id: 'drift',
    label: 'Detect Drift',
    icon: AlertTriangle,
    hint: 'check citations against edited sources',
  },
  {
    id: 'reconcile-run',
    label: 'Manual Reconcile',
    icon: GitMerge,
    hint: 'run an entity-merge sweep on demand',
  },
]

export function MaintenanceView({ initial }: { initial?: Section }) {
  const [section, setSection] = useState<Section>(initial ?? 'rebuild-heal')
  return (
    <div className="space-y-4">
      <nav className="flex flex-wrap gap-2">
        {SECTIONS.map(({ id, label, icon: Icon, hint }) => (
          <button
            key={id}
            onClick={() => setSection(id)}
            title={hint}
            className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-sm transition-colors ${
              section === id
                ? 'border-accent-700 bg-surface-800 text-accent-300'
                : 'border-surface-700 text-surface-400 hover:bg-surface-900 hover:text-surface-200'
            }`}
          >
            <Icon size={14} />
            {label}
          </button>
        ))}
      </nav>
      {section === 'rebuild-heal' && <RebuildHealView />}
      {section === 'authority' && <AuthorityView />}
      {section === 'drift' && <DriftView />}
      {section === 'reconcile-run' && <ReconcileRunView />}
    </div>
  )
}
