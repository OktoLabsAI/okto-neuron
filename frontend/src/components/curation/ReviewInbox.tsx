// One inbox for everything that needs a human decision: predicate folds,
// entity merges, and gated node candidates. Each section reuses its existing
// view (own fetch + actions); the headers carry the live counts.
import { Inbox, ListChecks, Tags } from 'lucide-react'
import { PredicateUpkeepView } from './PredicateUpkeepView'
import { ReconcileReviewView } from './ReconcileReviewView'
import { CompanionReviewView } from './CompanionReviewView'
import type { Attention } from './useAttention'

function Section({
  icon: Icon,
  title,
  hint,
  count,
  children,
}: {
  icon: typeof Tags
  title: string
  hint: string
  count: number
  children: React.ReactNode
}) {
  return (
    <section className="rounded-lg border border-surface-800">
      <header className="flex items-center gap-2 border-b border-surface-800 bg-surface-900 px-4 py-3">
        <Icon size={15} className="text-surface-400" />
        <h3 className="text-sm font-medium text-surface-200">{title}</h3>
        {count > 0 ? (
          <span className="rounded-full bg-amber-700/80 px-2 py-0.5 text-xs font-medium text-white">
            {count}
          </span>
        ) : (
          <span className="text-xs text-surface-500">nothing waiting</span>
        )}
        <span className="ml-auto hidden text-xs text-surface-500 sm:block">{hint}</span>
      </header>
      {count > 0 && <div className="p-4">{children}</div>}
    </section>
  )
}

export function ReviewInbox({ attention }: { attention: Attention }) {
  return (
    <div className="space-y-4">
      <Section
        icon={Tags}
        title="Predicate folds"
        hint="should these two relation names mean the same thing?"
        count={attention.predicates}
      >
        <PredicateUpkeepView />
      </Section>
      <Section
        icon={ListChecks}
        title="Entity merges"
        hint="are these nodes the same real-world thing?"
        count={attention.entityMerges}
      >
        <ReconcileReviewView />
      </Section>
      <Section
        icon={Inbox}
        title="Node candidates"
        hint="extracted entities the confidence gate held back"
        count={attention.nodeCandidates}
      >
        <CompanionReviewView />
      </Section>
    </div>
  )
}
