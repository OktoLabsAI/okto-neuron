// Small shared presentational primitives — keeps the dark theme consistent.
import type { ReactNode } from 'react'

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-surface-400 text-sm" role="status" aria-live="polite">
      <span
        aria-hidden="true"
        className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-surface-600 border-t-accent-500"
      />
      {label && <span>{label}</span>}
    </div>
  )
}

export function ErrorBox({ message }: { message: string }) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-red-900/60 bg-red-950/40 px-4 py-3 text-sm text-red-300"
    >
      {message}
    </div>
  )
}

export function Badge({ children, tone = 'default' }: { children: ReactNode; tone?: 'default' | 'accent' | 'violet' | 'warn' | 'danger' }) {
  const tones: Record<string, string> = {
    default: 'bg-surface-800 text-surface-300 border-surface-700',
    accent: 'bg-accent-700/30 text-accent-400 border-accent-600/40',
    violet: 'bg-violet-600/20 text-violet-400 border-violet-500/40',
    warn: 'bg-amber-600/20 text-amber-300 border-amber-500/40',
    danger: 'bg-rose-600/20 text-rose-300 border-rose-500/40',
  }
  return (
    <span className={`inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium ${tones[tone]}`}>
      {children}
    </span>
  )
}

export function ProvenanceChip({
  path,
  byteStart,
  byteEnd,
}: {
  path: string
  byteStart: number
  byteEnd: number
}) {
  return (
    <code className="inline-flex items-center gap-1 rounded bg-surface-900 px-2 py-1 font-mono text-xs text-accent-400 ring-1 ring-surface-700">
      {path}
      <span className="text-surface-500">
        [{byteStart}:{byteEnd}]
      </span>
    </code>
  )
}

// ── form primitives ────────────────────────────────────────────────────────────

const _inputBase =
  'rounded-lg border border-surface-700 bg-surface-900 px-3 py-2 text-sm text-surface-100 placeholder:text-surface-600 focus:border-accent-600 focus:outline-none'

export function Select({
  value,
  onChange,
  options,
  disabled,
}: {
  value: string
  onChange: (v: string) => void
  options: { value: string; label: string }[]
  disabled?: boolean
}) {
  const knownValues = options.map((o) => o.value)
  const allOptions =
    value && !knownValues.includes(value) ? [{ value, label: value }, ...options] : options
  return (
    <select
      className={`${_inputBase} w-full`}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
    >
      {allOptions.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  )
}

export function NumberField({
  value,
  onChange,
  min,
  max,
  step,
  placeholder,
  disabled,
}: {
  value: number | null
  onChange: (v: number | null) => void
  min?: number
  max?: number
  step?: number
  placeholder?: string
  disabled?: boolean
}) {
  return (
    <input
      type="number"
      className={`${_inputBase} w-full`}
      value={value ?? ''}
      min={min}
      max={max}
      step={step}
      placeholder={placeholder}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))}
    />
  )
}

export function Slider({
  value,
  onChange,
  min,
  max,
  step,
  disabled,
}: {
  value: number | null
  onChange: (v: number | null) => void
  min: number
  max: number
  step?: number
  disabled?: boolean
}) {
  return (
    <input
      type="range"
      className="w-full accent-accent-500"
      value={value ?? min}
      min={min}
      max={max}
      step={step ?? 0.01}
      disabled={disabled}
      onChange={(e) => onChange(Number(e.target.value))}
    />
  )
}

export function TextArea({
  value,
  onChange,
  placeholder,
  rows,
  disabled,
}: {
  value: string | null
  onChange: (v: string | null) => void
  placeholder?: string
  rows?: number
  disabled?: boolean
}) {
  return (
    <textarea
      className={`${_inputBase} w-full resize-y font-mono`}
      value={value ?? ''}
      rows={rows ?? 4}
      placeholder={placeholder}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value || null)}
    />
  )
}
