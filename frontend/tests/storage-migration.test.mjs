// node --test tests/storage-migration.test.mjs  (Node >= 22.18 strips the TS types)
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { migrateLegacyStorageKeys, renamedStorageKey } from '../src/lib/storage-migration.ts'

class MemoryStorage {
  constructor(entries = {}) {
    this.map = new Map(Object.entries(entries))
  }
  get length() {
    return this.map.size
  }
  key(i) {
    return [...this.map.keys()][i] ?? null
  }
  getItem(k) {
    return this.map.has(k) ? this.map.get(k) : null
  }
  setItem(k, v) {
    this.map.set(k, String(v))
  }
}

test('renames both separators and ignores other keys', () => {
  assert.equal(renamedStorageKey('marginalia.ask.retrievalPolicy.v1'), 'okto-neuron.ask.retrievalPolicy.v1')
  assert.equal(renamedStorageKey('marginalia:mock'), 'okto-neuron:mock')
  assert.equal(renamedStorageKey('okto-neuron.ask.retrievalPolicy.v1'), null)
  assert.equal(renamedStorageKey('other'), null)
})

test('copies legacy keys, keeps the originals, never overwrites a new key', () => {
  const s = new MemoryStorage({
    'marginalia.ask.retrievalPolicy.v1': '{"seed_k":12}',
    'marginalia.query.thread.v2:%2Fv': '[]',
    'marginalia.graph.boundsDefaults.v1': '{"old":true}',
    'okto-neuron.graph.boundsDefaults.v1': '{"new":true}',
    unrelated: 'x',
  })
  const written = migrateLegacyStorageKeys(s)
  assert.deepEqual(written.sort(), [
    'okto-neuron.ask.retrievalPolicy.v1',
    'okto-neuron.query.thread.v2:%2Fv',
  ])
  assert.equal(s.getItem('okto-neuron.ask.retrievalPolicy.v1'), '{"seed_k":12}')
  assert.equal(s.getItem('marginalia.ask.retrievalPolicy.v1'), '{"seed_k":12}')
  assert.equal(s.getItem('okto-neuron.graph.boundsDefaults.v1'), '{"new":true}')
  assert.deepEqual(migrateLegacyStorageKeys(s), [])
})
