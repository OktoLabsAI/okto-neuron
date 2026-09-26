import { useEffect, useRef, useState } from 'react'
import { Paperclip, Sparkles, X, FileText, Layers } from 'lucide-react'
import { ingest, getIngestQueue } from '@/services/ingest-api'
import type { IngestResponse } from '@/types'
import { Spinner, ErrorBox, Badge } from '@/components/ui'
import { useApp } from '@/store/app'
import { BulkImport } from './BulkImport'
import {
  IngestOutcomeBadge,
  ingestFailedUnitCount,
  ingestFailureClasses,
} from './IngestOutcomeSummary'

type Mode = 'single' | 'bulk'

// Text-like sources only — Okto Neuron's trust root is markdown.
const ACCEPT = '.md,.markdown,.txt,text/markdown,text/plain'

export function IngestView() {
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | undefined>()
  const [result, setResult] = useState<IngestResponse | undefined>()
  const fileRef = useRef<HTMLInputElement>(null)
  const setView = useApp((s) => s.setView)
  const [mode, setMode] = useState<Mode>('single')

  // After a refresh, if the server still has a live/recent ingest queue, land
  // on the Bulk tab so in-flight progress is visible instead of hidden.
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const q = await getIngestQueue()
        if (!cancelled && q.summary.active) setMode('bulk')
      } catch {
        // No queue / server not ready — keep the default Single tab.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const onAttach = async (file: File) => {
    const text = await file.text()
    setContent(text)
    if (!title.trim()) setTitle(file.name)
  }

  const submit = async () => {
    const body = content.trim()
    if (!body || loading) return
    setLoading(true)
    setError(undefined)
    setResult(undefined)
    try {
      const res = await ingest(body, title.trim() || undefined)
      setResult(res)
      setContent('')
      setTitle('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'ingest failed')
    } finally {
      setLoading(false)
    }
  }

  const committed = result?.outcomes.filter((o) => o.action === 'committed') ?? []
  const queued = result?.outcomes.filter((o) => o.action === 'queued') ?? []
  const failedUnits = result ? ingestFailedUnitCount(result) : 0
  const failureClasses = result ? ingestFailureClasses(result) : []

  return (
    <div className="flex h-full flex-col">
      <header className="border-b border-surface-800 px-6 py-4">
        <h1 className="text-base font-semibold">Add knowledge</h1>
        <p className="text-xs text-surface-500">
          Saved as markdown (the trust root), then the companion extracts entities and claims so it
          becomes queryable in Query &amp; Browse.
        </p>
        <div className="mt-3 flex gap-1">
          <button
            onClick={() => setMode('single')}
            className={`flex items-center gap-2 rounded-lg px-3 py-1.5 text-xs font-medium ${
              mode === 'single' ? 'bg-accent-600 text-white' : 'text-surface-400 hover:bg-surface-800'
            }`}
          >
            <FileText size={14} /> Single note
          </button>
          <button
            onClick={() => setMode('bulk')}
            className={`flex items-center gap-2 rounded-lg px-3 py-1.5 text-xs font-medium ${
              mode === 'bulk' ? 'bg-accent-600 text-white' : 'text-surface-400 hover:bg-surface-800'
            }`}
          >
            <Layers size={14} /> Bulk import
          </button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto px-6 py-5">
        {mode === 'bulk' && <BulkImport />}
        {mode === 'single' && (
        <div className="mx-auto flex max-w-3xl flex-col gap-4">
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Title (optional) — becomes the source filename"
            className="rounded-lg border border-surface-700 bg-surface-900 px-3 py-2 text-sm text-surface-100 placeholder:text-surface-600 focus:border-accent-600 focus:outline-none"
          />

          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
                e.preventDefault()
                void submit()
              }
            }}
            placeholder="Write or paste markdown here…  (⌘/Ctrl+Enter to add)"
            className="min-h-[260px] flex-1 resize-y rounded-lg border border-surface-700 bg-surface-900 px-3 py-3 font-mono text-sm leading-relaxed text-surface-100 placeholder:text-surface-600 focus:border-accent-600 focus:outline-none"
          />

          <div className="flex items-center gap-3">
            <input
              ref={fileRef}
              type="file"
              accept={ACCEPT}
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0]
                if (f) void onAttach(f)
                e.target.value = ''
              }}
            />
            <button
              onClick={() => fileRef.current?.click()}
              className="flex items-center gap-2 rounded-lg border border-surface-700 px-3 py-2 text-xs text-surface-300 hover:bg-surface-800"
            >
              <Paperclip size={14} /> Attach .md / .txt
            </button>
            <div className="flex-1" />
            <span className="text-[11px] text-surface-600">{content.length} chars</span>
            <button
              onClick={() => void submit()}
              disabled={!content.trim() || loading}
              className="flex h-10 items-center gap-2 rounded-lg bg-accent-600 px-4 text-sm font-medium text-white transition-colors hover:bg-accent-500 disabled:opacity-40"
            >
              <Sparkles size={15} /> Make it knowledge
            </button>
          </div>

          {loading && (
            <div className="rounded-lg border border-surface-800 bg-surface-900/60 px-4 py-4">
              <Spinner label="extracting entities & claims (the model is reading your note)…" />
            </div>
          )}
          {error && <ErrorBox message={error} />}

          {result && (
            <div className="flex flex-col gap-3 rounded-lg border border-accent-700/40 bg-accent-950/20 px-4 py-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 text-sm text-surface-100">
                  <Sparkles size={15} className="text-accent-400" />
                  Added <code className="font-mono text-xs text-surface-400">{result.filename}</code>
                </div>
                <button onClick={() => setResult(undefined)} className="text-surface-500 hover:text-surface-300">
                  <X size={15} />
                </button>
              </div>

              <div className="flex flex-wrap gap-2 text-xs">
                <Badge tone="accent">{result.committed} committed</Badge>
                {result.queued > 0 && <Badge tone="warn">{result.queued} queued for review</Badge>}
                <IngestOutcomeBadge item={result} />
                {failedUnits > 0 && (
                  <Badge tone="danger">{failedUnits} failed unit{failedUnits === 1 ? '' : 's'}</Badge>
                )}
                {failureClasses.map((failureClass) => (
                  <Badge key={failureClass} tone="danger">{failureClass.replace(/[_-]+/g, ' ')}</Badge>
                ))}
              </div>

              {committed.length > 0 && (
                <div className="flex flex-col gap-1">
                  <div className="text-[11px] uppercase tracking-wide text-surface-500">Now in the graph</div>
                  {committed.map((o) => (
                    <div key={o.candidate_id} className="flex items-center gap-2 text-sm text-surface-200">
                      <Badge tone="violet">{o.type}</Badge>
                      <span className="truncate">{o.title}</span>
                      <span className="ml-auto font-mono text-[11px] text-surface-500">{o.confidence.toFixed(2)}</span>
                    </div>
                  ))}
                </div>
              )}

              {queued.length > 0 && (
                <div className="flex flex-col gap-1">
                  <div className="text-[11px] uppercase tracking-wide text-surface-500">
                    Parked (below the auto-commit gate)
                  </div>
                  {queued.map((o) => (
                    <div key={o.candidate_id} className="flex items-center gap-2 text-sm text-surface-400">
                      <Badge>{o.type}</Badge>
                      <span className="truncate">{o.title}</span>
                      <span className="ml-auto font-mono text-[11px] text-surface-600">{o.confidence.toFixed(2)}</span>
                    </div>
                  ))}
                </div>
              )}

              <button
                onClick={() => setView('browser')}
                className="self-start text-xs text-accent-400 hover:text-accent-300"
              >
                View in Browse →
              </button>
            </div>
          )}
        </div>
        )}
      </div>
    </div>
  )
}
