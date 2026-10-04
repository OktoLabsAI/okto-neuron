// node --test tests/migration-required.test.mjs  (Node >= 22.18 strips the TS types)
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { migrationRequiredVaults } from '../src/lib/migration-required.ts'

const REMEDY = 'okto-neuron kg review-queue migrate --vault old'
const rq = (extra = {}) => ({
  state: 'migration_required',
  code: 'review_queue_migration_required',
  remedy: REMEDY,
  ...extra,
})

test('vaults[].review_queue migration_required is listed with the API remedy', () => {
  const status = {
    vaults: [
      { path: '/r/served', review_queue: { state: 'ok', code: null, remedy: null } },
      { path: '/r/old', refused: true, review_queue: rq() },
    ],
  }
  assert.deepEqual(migrationRequiredVaults(status), [{ vault: 'old', remedy: REMEDY }])
})

test('vault_warning alone is listed; a missing remedy is built', () => {
  const warning = { code: 'review_queue_migration_required', path: '/r/old', remedy: REMEDY }
  assert.deepEqual(migrationRequiredVaults({ vaults: [], vault_warning: warning }), [
    { vault: 'old', remedy: REMEDY },
  ])
  assert.deepEqual(
    migrationRequiredVaults({ vault_warning: { code: warning.code, path: '/r/old/' } }),
    [{ vault: 'old', remedy: REMEDY }],
  )
})

test('the same vault in both places is listed once; distinct vaults are kept', () => {
  const status = {
    vaults: [{ path: '/r/old', review_queue: rq() }, { path: '/r/older', review_queue: rq({ remedy: undefined }) }],
    vault_warning: { code: 'review_queue_migration_required', path: '/r/old', remedy: REMEDY },
  }
  assert.deepEqual(migrationRequiredVaults(status), [
    { vault: 'old', remedy: REMEDY },
    { vault: 'older', remedy: 'okto-neuron kg review-queue migrate --vault older' },
  ])
})

test('healthy status and unrelated warnings yield nothing', () => {
  assert.deepEqual(
    migrationRequiredVaults({
      vaults: [{ path: '/r/a', review_queue: { state: 'ok', code: null, remedy: null } }],
      vault_warning: { code: 'embedding_dim_mismatch', path: '/r/a', remedy: 'x' },
    }),
    [],
  )
  assert.deepEqual(migrationRequiredVaults({ vaults: [], vault_warning: null }), [])
})

test('malformed or missing fields yield nothing and never throw', () => {
  for (const bad of [undefined, null, 'x', 7, [], {}, { vaults: 'x' }, { vaults: [null, 3, {}] },
    { vaults: [{ review_queue: rq() }] }, { vaults: [{ path: '', review_queue: rq() }] },
    { vaults: [{ path: '/r/old', review_queue: 'migration_required' }] },
    { vault_warning: 'review_queue_migration_required' },
    { vault_warning: { code: 'review_queue_migration_required' } }]) {
    assert.deepEqual(migrationRequiredVaults(bad), [], JSON.stringify(bad))
  }
})
