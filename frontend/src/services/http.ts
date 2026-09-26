// Shared fetch wrapper. Relative base so the app works on whatever host serves it
// (localhost default; remote opt-in owned by the backend). Never hard-code a host.

import { useApp } from '@/store/app'

export const API_BASE = '/api/v1'

export class HttpError extends Error {
  status: number
  code: string
  constructor(message: string, status: number, code: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

type FetchOptions = RequestInit & { timeoutMs?: number; vaultScoped?: boolean }

// Model-backed calls may legitimately be slow, but no browser request should be
// immortal. Polling and control services pass much shorter explicit deadlines.
const DEFAULT_TIMEOUT_MS = 300_000

async function boundedFetch(
  url: string,
  options: FetchOptions = {},
  provesConnection = true,
): Promise<Response> {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, signal, vaultScoped: _vaultScoped, ...init } = options
  const controller = new AbortController()
  const abortFromCaller = () => controller.abort(signal?.reason)
  if (signal?.aborted) abortFromCaller()
  else signal?.addEventListener('abort', abortFromCaller, { once: true })

  const timer = globalThis.setTimeout(() => controller.abort('request_timeout'), timeoutMs)
  try {
    const response = await fetch(url, { ...init, signal: controller.signal })
    if (provesConnection) {
      useApp.getState().setConnection('online')
    }
    return response
  } catch (error) {
    if (!signal?.aborted) {
      const timedOut = controller.signal.reason === 'request_timeout'
      useApp.getState().setConnection(
        'offline',
        timedOut ? 'The Okto Neuron daemon did not respond in time.' : 'The Okto Neuron daemon is unavailable.',
      )
      if (timedOut) throw new HttpError('request timed out', 0, 'request_timeout')
    }
    throw error
  } finally {
    globalThis.clearTimeout(timer)
    signal?.removeEventListener('abort', abortFromCaller)
  }
}

async function decodeResponse<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    const detail = body?.detail || body?.error || resp.statusText
    throw new HttpError(detail, resp.status, body?.error || 'http_error')
  }
  if (resp.status === 204) return undefined as T
  return resp.json()
}

export async function apiFetch<T>(path: string, init?: FetchOptions): Promise<T> {
  const vaultScoped = init?.vaultScoped ?? true
  const selectedVaultPath = useApp.getState().selectedVaultPath
  const resp = await boundedFetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(vaultScoped && selectedVaultPath
        ? { 'X-Okto-Neuron-Vault': selectedVaultPath }
        : {}),
      ...init?.headers,
    },
  })
  if (vaultScoped && useApp.getState().selectedVaultPath !== selectedVaultPath) {
    throw new HttpError('vault selection changed while the request was running', 0, 'stale_vault_response')
  }
  return decodeResponse<T>(resp)
}

// Some long-lived daemon endpoints predate /api/v1 and live at the ROOT
// (/health, /detect-drift, /review-queue, /resolve-review — the CLI speaks these
// over loopback). apiFetch hard-codes the /api/v1 prefix, so the P1 curation UI
// reaches them through rootFetch instead. Same error contract.
export async function rootFetch<T>(path: string, init?: FetchOptions): Promise<T> {
  const vaultScoped = init?.vaultScoped ?? !['/health', '/version'].includes(path)
  const selectedVaultPath = useApp.getState().selectedVaultPath
  const resp = await boundedFetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(vaultScoped && selectedVaultPath
        ? { 'X-Okto-Neuron-Vault': selectedVaultPath }
        : {}),
      ...init?.headers,
    },
  }, path !== '/health' && path !== '/version')
  if (vaultScoped && useApp.getState().selectedVaultPath !== selectedVaultPath) {
    throw new HttpError('vault selection changed while the request was running', 0, 'stale_vault_response')
  }
  return decodeResponse<T>(resp)
}

export function qs(params: Record<string, string | number | undefined>): string {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== '') sp.set(k, String(v))
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}
