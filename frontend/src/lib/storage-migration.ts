// Okto Neuron 0.3.0 renamed the browser storage keys from `marginalia.*` to
// `okto-neuron.*`. The daemon keeps its port, so an upgraded install serves the
// UI from the same origin and the old keys are still in this browser. Copy each
// one to its new name once, before anything reads storage, so a saved retrieval
// policy, per-vault thread or graph default survives the upgrade. The old keys
// are left in place (a downgrade still finds them) and an existing new key is
// never overwritten.

export const STORAGE_PREFIX = 'okto-neuron'
export const LEGACY_STORAGE_PREFIX = 'marginalia'

interface KeyValueStorage {
  readonly length: number
  key(index: number): string | null
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

/** `marginalia.x` -> `okto-neuron.x`, `marginalia:mock` -> `okto-neuron:mock`, else null. */
export function renamedStorageKey(key: string): string | null {
  for (const sep of ['.', ':']) {
    const legacy = `${LEGACY_STORAGE_PREFIX}${sep}`
    if (key.startsWith(legacy)) return `${STORAGE_PREFIX}${sep}${key.slice(legacy.length)}`
  }
  return null
}

/** Copy legacy keys to their new names. Returns the new keys that were written. */
export function migrateLegacyStorageKeys(storage: KeyValueStorage): string[] {
  const legacyKeys: string[] = []
  for (let i = 0; i < storage.length; i += 1) {
    const key = storage.key(i)
    if (key !== null && renamedStorageKey(key) !== null) legacyKeys.push(key)
  }
  const written: string[] = []
  for (const legacyKey of legacyKeys) {
    const newKey = renamedStorageKey(legacyKey)
    const value = storage.getItem(legacyKey)
    if (newKey === null || value === null || storage.getItem(newKey) !== null) continue
    try {
      storage.setItem(newKey, value)
      written.push(newKey)
    } catch {
      // Storage is best-effort (quota, private mode); the UI falls back to defaults.
    }
  }
  return written
}

/** Browser entry point: never throws, so a blocked storage cannot stop the UI. */
export function migrateBrowserStorage(): void {
  try {
    migrateLegacyStorageKeys(window.localStorage)
  } catch {
    // localStorage can throw on access when the browser blocks site data.
  }
}

// Runs when this module is evaluated. main.tsx imports it first, so it runs
// before any other module (the graph store, mock mode) reads storage at load.
if (typeof window !== 'undefined') migrateBrowserStorage()
