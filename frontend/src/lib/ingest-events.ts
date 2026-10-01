// An event body above the persist budget is stored as a preview (server side, #37):
// { truncated: true, original_bytes, sha256, preview }. An in-flight item still carries
// the full body. These helpers let the log views show either one.

export interface TruncatedPayload {
  truncated: true
  original_bytes: number
  sha256: string
  preview: string
}

export function isTruncatedPayload(payload: unknown): payload is TruncatedPayload {
  if (typeof payload !== 'object' || payload === null) return false
  const p = payload as Record<string, unknown>
  return p.truncated === true && typeof p.preview === 'string' && typeof p.original_bytes === 'number'
}

export interface EventPayloadView {
  text: string
  // Set when the text is only a preview of a larger body.
  note: string | null
}

export function formatByteCount(n: number): string {
  if (n < 1024) return `${n} bytes`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

export function eventPayloadView(payload: unknown): EventPayloadView {
  if (isTruncatedPayload(payload)) {
    return {
      text: payload.preview,
      note: `truncated (${payload.original_bytes} bytes, ${formatByteCount(payload.original_bytes)}; sha256 ${payload.sha256.slice(0, 12)}…)`,
    }
  }
  return { text: JSON.stringify(payload, null, 2), note: null }
}
