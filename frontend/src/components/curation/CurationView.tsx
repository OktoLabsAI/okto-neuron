// Curation Control Plane (ADR 0009 + 0017), review-first layout:
//   Overview  — what needs you + plain-language health (old dashboard under Details)
//   Review    — one inbox: predicate folds · entity merges · node candidates
//   Semantic quality — explicit complete-store audit and per-layer scorecard
//   Maintenance — heal/rebuild, authority audit, drift, manual reconcile
import { useState } from 'react'
import { Activity, Inbox, ScanSearch, Wrench } from 'lucide-react'
import { CurationOverview } from './CurationOverview'
import { ReviewInbox } from './ReviewInbox'
import { MaintenanceView } from './MaintenanceView'
import { SemanticQualityView } from './SemanticQualityView'
import { useAttention } from './useAttention'

type Tab = 'overview' | 'review' | 'quality' | 'maintenance'

const TABS: { id: Tab; label: string; icon: typeof Activity }[] = [
  { id: 'overview', label: 'Overview', icon: Activity },
  { id: 'review', label: 'Review', icon: Inbox },
  { id: 'quality', label: 'Semantic quality', icon: ScanSearch },
  { id: 'maintenance', label: 'Maintenance', icon: Wrench },
]

export function CurationView() {
  const [tab, setTab] = useState<Tab>('overview')
  const { attention } = useAttention()
  return (
    <div className="flex h-full flex-col">
      <header className="border-b border-surface-800 px-6 pt-5">
        <h1 className="text-lg font-semibold text-surface-100">Curation</h1>
        <p className="text-xs text-surface-500">
          The graph maintains itself; you decide the borderline calls. Heal applies them.
        </p>
        <nav className="mt-4 flex gap-1 overflow-x-auto">
          {TABS.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              onClick={() => setTab(id)}
              className={`flex items-center gap-2 whitespace-nowrap rounded-t-lg px-3 py-2 text-sm transition-colors ${
                tab === id
                  ? 'bg-surface-800 text-accent-300'
                  : 'text-surface-400 hover:bg-surface-900 hover:text-surface-200'
              }`}
            >
              <Icon size={15} />
              {label}
              {id === 'review' && attention.total > 0 && (
                <span className="rounded-full bg-amber-700/80 px-1.5 py-0.5 text-xs font-medium text-white">
                  {attention.total}
                </span>
              )}
            </button>
          ))}
        </nav>
      </header>
      <div className="min-h-0 flex-1 overflow-auto p-6">
        {tab === 'overview' && (
          <CurationOverview
            attention={attention}
            onGoReview={() => setTab('review')}
            onGoMaintenance={() => setTab('maintenance')}
          />
        )}
        {tab === 'review' && <ReviewInbox attention={attention} />}
        {tab === 'quality' && <SemanticQualityView />}
        {tab === 'maintenance' && <MaintenanceView />}
      </div>
    </div>
  )
}
