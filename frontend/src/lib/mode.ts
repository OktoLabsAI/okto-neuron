// Mock responses are a developer aid, never a normal production mode. A production
// build exposes them only for an explicit `?mock=1` session, so a stale localStorage
// value cannot make a user believe fake data came from their vault.
const KEY = 'okto-neuron:mock'

export function mockModeAvailable(): boolean {
  if (import.meta.env.DEV) return true
  try {
    return new URLSearchParams(window.location.search).get('mock') === '1'
  } catch {
    return false
  }
}

let _mock = (() => {
  if (!mockModeAvailable()) return false
  try {
    return localStorage.getItem(KEY) === '1'
  } catch {
    return false
  }
})()

const listeners = new Set<(v: boolean) => void>()

export function isMock(): boolean {
  return _mock
}

export function setMock(v: boolean): void {
  _mock = v && mockModeAvailable()
  try {
    localStorage.setItem(KEY, _mock ? '1' : '0')
  } catch {
    /* ignore */
  }
  listeners.forEach((l) => l(_mock))
}

export function onModeChange(fn: (v: boolean) => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}
