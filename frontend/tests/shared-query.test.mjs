// node --test tests/shared-query.test.mjs  (Node >= 22.18 strips the TS types)
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { createSharedQuery } from '../src/lib/shared-query.ts'

function makeEnv() {
  const env = {
    hidden: false,
    visibility: new Set(),
    timers: new Map(),
    nextId: 1,
    isHidden: () => env.hidden,
    onVisibilityChange: (cb) => {
      env.visibility.add(cb)
      return () => env.visibility.delete(cb)
    },
    setTimer: (cb, ms) => {
      const id = env.nextId++
      env.timers.set(id, { cb, ms })
      return id
    },
    clearTimer: (id) => env.timers.delete(id),
    fire: async () => {
      const [id, t] = [...env.timers.entries()][0]
      env.timers.delete(id)
      t.cb()
      await tick()
    },
    setHidden: (value) => {
      env.hidden = value
      env.visibility.forEach((cb) => cb())
    },
  }
  return env
}

const tick = () => new Promise((resolve) => setImmediate(resolve))

function make({ responses, env = makeEnv(), key = () => 'v1' }) {
  const calls = { n: 0 }
  const gates = []
  const query = createSharedQuery({
    initial: { data: null, building: false, error: null, loading: false },
    fetch: () => {
      calls.n += 1
      const next = responses.shift()
      return new Promise((resolve, reject) => {
        gates.push(() => (next instanceof Error ? reject(next) : resolve(next)))
      })
    },
    loading: (s) => (s.data ? s : { ...s, loading: true }),
    reduce: (s, o) =>
      !o.ok
        ? { ...s, loading: false, error: o.error }
        : o.body.status === 'building'
          ? { ...s, building: true, loading: false, error: null }
          : { data: o.body, building: false, error: null, loading: false },
    pollMs: (s) => (s.building ? 10 : 100),
    key,
    env,
  })
  const release = async () => {
    gates.splice(0).forEach((g) => g())
    await tick()
  }
  return { query, env, calls, release }
}

test('many subscribers share one request and one timer', async () => {
  const { query, env, calls, release } = make({ responses: [{ status: 'ok', n: 1 }] })
  const unsubs = [1, 2, 3, 4, 5].map(() => query.subscribe(() => {}))
  const manual = query.refresh()
  assert.equal(calls.n, 1, 'five subscribers + a manual refresh: still one request in flight')
  await release()
  await manual
  assert.deepEqual(query.getState().data, { status: 'ok', n: 1 })
  assert.equal(env.timers.size, 1, 'exactly one poll timer')
  unsubs.forEach((u) => u())
  assert.equal(env.timers.size, 0, 'the timer stops with the last subscriber')
})

test('a hidden tab neither polls nor keeps a timer; becoming visible refreshes once', async () => {
  const { query, env, calls, release } = make({
    responses: [{ status: 'ok', n: 1 }, { status: 'ok', n: 2 }],
  })
  query.subscribe(() => {})
  await release()
  assert.equal(env.timers.size, 1)
  env.setHidden(true)
  assert.equal(env.timers.size, 0, 'hiding the tab clears the timer')
  assert.equal(calls.n, 1, 'no request while hidden')
  env.setHidden(false)
  await release()
  assert.equal(calls.n, 2)
  assert.equal(query.getState().data.n, 2)
  assert.equal(env.timers.size, 1)
})

test('202 building keeps the last data, polls faster and never errors', async () => {
  const { query, env, release } = make({
    responses: [{ status: 'ok', n: 1 }, { status: 'building' }, { status: 'ok', n: 3 }],
  })
  query.subscribe(() => {})
  await release()
  await env.fire()
  await release()
  const building = query.getState()
  assert.equal(building.data.n, 1, 'last data kept')
  assert.equal(building.building, true)
  assert.equal(building.error, null)
  assert.equal([...env.timers.values()][0].ms, 10, 'fast poll while building')
  await env.fire()
  await release()
  assert.equal(query.getState().data.n, 3)
  assert.equal(query.getState().building, false)
})

test('a failed poll keeps the last data and records the error', async () => {
  const { query, env, release } = make({
    responses: [{ status: 'ok', n: 1 }, new Error('daemon unavailable')],
  })
  query.subscribe(() => {})
  await release()
  await env.fire()
  await release()
  assert.equal(query.getState().data.n, 1)
  assert.equal(query.getState().error, 'daemon unavailable')
})

test('switching vault drops the previous vault data and ignores its late response', async () => {
  let vault = 'a'
  const { query, env, calls, release } = make({
    responses: [{ status: 'ok', n: 1 }, { status: 'ok', n: 2 }],
    key: () => vault,
  })
  query.subscribe(() => {})
  await release()
  assert.equal(query.getState().data.n, 1)
  vault = 'b'
  await env.fire()
  assert.equal(query.getState().data, null, 'vault b must not show vault a data while loading')
  await release()
  assert.equal(query.getState().data.n, 2)
  assert.equal(calls.n, 2)
})
