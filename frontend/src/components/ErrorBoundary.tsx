import { Component, type ErrorInfo, type ReactNode } from 'react'
import { AlertTriangle, RotateCcw } from 'lucide-react'

interface Props {
  children: ReactNode
  // Bumping this (e.g. on view change) clears a caught error so navigating away
  // to a healthy view recovers without a full reload.
  resetKey?: string | number
}

interface State {
  error: Error | null
}

// App-level boundary: catches any render error inside a view so a single broken
// view never unmounts the nav/shell. The user can always navigate away.
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // eslint-disable-next-line no-console
    console.error('view render error:', error, info.componentStack)
  }

  componentDidUpdate(prev: Props): void {
    // Switching views (resetKey changes) clears the error so the new view renders.
    if (this.state.error && prev.resetKey !== this.props.resetKey) {
      this.setState({ error: null })
    }
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="flex h-full items-center justify-center p-8 text-center">
          <div className="max-w-md">
            <AlertTriangle className="mx-auto mb-3 text-rose-400/80" size={28} />
            <h2 className="text-base font-semibold">This view hit an error</h2>
            <p className="mt-2 text-sm text-surface-500">
              Something went wrong rendering this panel. The rest of the app is fine — pick another
              tab in the sidebar, or retry.
            </p>
            <pre className="mt-3 max-h-40 overflow-auto rounded-lg border border-surface-800 bg-surface-950 p-3 text-left font-mono text-[11px] text-rose-300/80">
              {this.state.error.message || String(this.state.error)}
            </pre>
            <button
              onClick={() => this.setState({ error: null })}
              className="mt-3 inline-flex items-center gap-2 rounded-lg border border-surface-700 px-3 py-2 text-xs text-surface-300 hover:bg-surface-800"
            >
              <RotateCcw size={13} /> Retry
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
