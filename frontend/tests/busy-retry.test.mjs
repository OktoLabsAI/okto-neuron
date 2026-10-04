// node --test tests/busy-retry.test.mjs  (Node >= 22.18 strips the TS types)
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  BACKOFF_S,
  BusyRetryStopped,
  busyInfo,
  busyMessage,
  runWithBusyRetry,
  waitSeconds,
} from '../src/lib/busy-retry.ts'

function busyErr(over = {}) {
  return {
    status: 503,
    code: 'busy',
    retryAfterS: 15,
    body: {
      error: 'busy',
      holder: { kind: 'ingest-item', id: 'item-7', since: 1, held_for_s: 42.4 },
      retry_after_s: 15,
    },
    ...over,
  }
}
const fakeSleep = (log) => async (ms) => { log.push(ms) }

test('busyInfo reads the holder and retry-after from a 503 busy and a 409 audit_busy', () => {
  assert.deepEqual(busyInfo(busyErr()), { kind: 'ingest-item', id: 'item-7', heldForS: 42.4, retryAfterS: 15 })
  assert.equal(busyInfo(busyErr({ status: 409, code: 'audit_busy' })).kind, 'ingest-item')
  assert.equal(busyInfo({ status: 503, code: 'busy', body: { error: 'busy' } }).kind, null)
})

test('other errors are not busy: 409 busy (maintenance), 404, timeouts, plain Error', () => {
  assert.equal(busyInfo({ status: 409, code: 'busy' }), null)
  assert.equal(busyInfo({ status: 404, code: 'review_item_not_found' }), null)
  assert.equal(busyInfo({ status: 0, code: 'request_timeout' }), null)
  assert.equal(busyInfo(new Error('x')), null)
  assert.equal(busyInfo(null), null)
})

test('backoff is 2,4,8,16,30 and honours a longer Retry-After, capped at 60', () => {
  assert.deepEqual(BACKOFF_S.map((_, i) => waitSeconds(i, null)), [2, 4, 8, 16, 30])
  assert.equal(waitSeconds(0, 15), 15)
  assert.equal(waitSeconds(4, 10), 30)
  assert.equal(waitSeconds(0, 300), 60)
  assert.equal(waitSeconds(9, null), 30)
})

test('message names the holder, the held time and the countdown', () => {
  const w = { kind: 'ingest-item', id: 'item-7', heldForS: 42.4, retryAfterS: 15, retryInS: 8, attempt: 2, maxRetries: 5 }
  assert.equal(busyMessage(w), 'Busy: ingest-item item-7 has held the lock for 42 s; retrying in 8 s (attempt 2 of 5)')
  assert.match(busyMessage({ ...w, kind: null, id: null, heldForS: null }), /^Busy: another operation holds the lock; retrying in 8 s/)
})

test('retries after busy, ticks once per second, and applies the action exactly once', async () => {
  let calls = 0
  let applied = 0
  const ticks = []
  const sleeps = []
  const out = await runWithBusyRetry(
    async () => {
      calls++
      if (calls <= 2) throw busyErr({ retryAfterS: null, body: { holder: { kind: 'mcp-remember', id: '-', held_for_s: 10 } } })
      applied++
      return 'ok'
    },
    { sleep: fakeSleep(sleeps), onWait: (w) => ticks.push([w.attempt, w.retryInS, w.heldForS, w.id]) },
  )
  assert.equal(out, 'ok')
  assert.equal(calls, 3)
  assert.equal(applied, 1)
  assert.equal(sleeps.length, 2 + 4)
  assert.deepEqual(ticks.slice(0, 3), [[1, 2, 10, null], [1, 1, 11, null], [2, 4, 10, null]])
})

test('gives up after maxRetries and rethrows the busy error', async () => {
  let calls = 0
  const e = busyErr()
  await assert.rejects(
    runWithBusyRetry(async () => { calls++; throw e }, { sleep: fakeSleep([]), maxRetries: 3 }),
    (got) => got === e,
  )
  assert.equal(calls, 4)
})

test('a non-busy error is rethrown at once without retrying (timeout is never re-sent)', async () => {
  let calls = 0
  await assert.rejects(
    runWithBusyRetry(async () => { calls++; throw { status: 0, code: 'request_timeout' } }, { sleep: fakeSleep([]) }),
  )
  assert.equal(calls, 1)
})

test('stopping while waiting ends the retries and never re-submits', async () => {
  const ctl = new AbortController()
  let calls = 0
  const p = runWithBusyRetry(async () => { calls++; throw busyErr() }, {
    signal: ctl.signal,
    sleep: async () => { ctl.abort() },
  })
  await assert.rejects(p, (e) => e instanceof BusyRetryStopped)
  assert.equal(calls, 1)
})

test('an already-aborted signal never calls the action', async () => {
  const ctl = new AbortController()
  ctl.abort()
  let calls = 0
  await assert.rejects(runWithBusyRetry(async () => { calls++ }, { signal: ctl.signal }), BusyRetryStopped)
  assert.equal(calls, 0)
})

test('attempts are strictly sequential: the next call starts only after the previous settled', async () => {
  let active = 0
  let maxActive = 0
  let calls = 0
  await runWithBusyRetry(
    async () => {
      active++; maxActive = Math.max(maxActive, active)
      await new Promise((r) => setTimeout(r, 1))
      active--
      if (++calls < 3) throw busyErr({ retryAfterS: null })
    },
    { sleep: fakeSleep([]) },
  )
  assert.equal(maxActive, 1)
})
