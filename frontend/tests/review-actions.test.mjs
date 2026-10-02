// node --test tests/review-actions.test.mjs  (Node >= 22.18 strips the TS types)
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  cancellable,
  describe,
  hasPending,
  holderText,
  isQueuedAnswer,
  mergeListing,
  newlyFinal,
  pendingFor,
  queuedActions,
  track,
} from '../src/lib/review-actions.ts'

function row(over = {}) {
  return {
    id: 'ra_1',
    candidate_id: 'c1',
    action: 'commit',
    status: 'queued',
    reason: null,
    applying: false,
    created_at: '2026-10-02T20:00:00+00:00',
    finished_at: null,
    batch_id: null,
    ...over,
  }
}

const single = {
  status: 'queued',
  detail: 'vault is busy; ...',
  holder: { kind: 'mcp-remember', id: '-', since: 1, held_for_s: 12.4 },
  retry_after_s: 15,
  action: row(),
  queued_ahead: 0,
}

test('a 202 queued answer is recognised; an applied answer is not', () => {
  assert.equal(isQueuedAnswer(single), true)
  assert.equal(isQueuedAnswer({ status: 'ok', outcome: {} }), false)
  assert.equal(isQueuedAnswer(null), false)
  assert.deepEqual(queuedActions({ status: 'ok' }), [])
})

test('holder text names the holder and skips the "-" id', () => {
  assert.equal(holderText(single), 'mcp-remember (held 12 s)')
  assert.equal(holderText({ holder: { kind: 'ingest-item', id: 'item-7' } }), 'ingest-item item-7')
  assert.equal(holderText({ holder: null }), null)
})

test('single and batch bodies both yield tracked actions carrying the holder', () => {
  const one = queuedActions(single)
  assert.equal(one.length, 1)
  assert.equal(one[0].holderText, 'mcp-remember (held 12 s)')
  const batch = queuedActions({
    status: 'queued',
    holder: { kind: 'ingest-item', id: 'i' },
    actions: [row({ id: 'a' }), row({ id: 'b' }), { nope: 1 }],
  })
  assert.deepEqual(batch.map((a) => a.id), ['a', 'b'])
})

test('track keeps an id once; merge applies the listing and keeps identity when unchanged', () => {
  const t = track([], queuedActions(single))
  assert.equal(track(t, queuedActions(single)), t)
  assert.equal(mergeListing(t, [row()]), t)
  const done = mergeListing(t, [row({ status: 'superseded', reason: 'gone' })])
  assert.notEqual(done, t)
  assert.equal(done[0].status, 'superseded')
  assert.equal(done[0].holderText, 'mcp-remember (held 12 s)')
  assert.deepEqual(newlyFinal(t, done), ['ra_1'])
  assert.equal(hasPending(t), true)
  assert.equal(hasPending(done), false)
})

test('describe says queued/applying/applied/superseded/failed/cancelled plainly', () => {
  const [a] = queuedActions(single)
  assert.match(describe(a), /queued, the vault is busy \(mcp-remember \(held 12 s\)\)/)
  assert.equal(describe({ ...a, applying: true }), 'commit: applying now')
  assert.equal(describe({ ...a, status: 'applied' }), 'commit: applied')
  assert.equal(
    describe({ ...a, status: 'superseded', reason: 'the candidate left the review queue' }),
    'commit: not applied, the candidate left the review queue',
  )
  assert.equal(describe({ ...a, status: 'failed', reason: 'RuntimeError: x' }), 'commit: failed, RuntimeError: x')
  assert.equal(describe({ ...a, status: 'cancelled' }), 'commit: cancelled')
})

test('only an unclaimed queued action can be cancelled; pendingFor finds the newest', () => {
  const [a] = queuedActions(single)
  assert.equal(cancellable(a), true)
  assert.equal(cancellable({ ...a, applying: true }), false)
  assert.equal(cancellable({ ...a, status: 'applied' }), false)
  const list = [a, { ...a, id: 'ra_2', action: 'discard' }, { ...a, id: 'ra_3', candidate_id: 'c2' }]
  assert.equal(pendingFor(list, 'c1').id, 'ra_2')
  assert.equal(pendingFor(list, 'zz'), null)
})
