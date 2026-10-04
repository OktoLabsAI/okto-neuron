// node --test tests/review-queue.test.mjs  (Node >= 22.18 strips the TS types)
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { appendPage, reviewQueueQuery, REVIEW_PAGE_SIZE } from '../src/lib/review-queue.ts'

test('badge query is limit=0 and never omits limit', () => {
  assert.equal(reviewQueueQuery({ limit: 0 }), '?limit=0')
})

test('page query carries limit and an encoded opaque cursor', () => {
  assert.equal(reviewQueueQuery({ limit: REVIEW_PAGE_SIZE }), `?limit=${REVIEW_PAGE_SIZE}`)
  assert.equal(reviewQueueQuery({ limit: 50, cursor: 'djE6MDow_-=' }), '?limit=50&cursor=djE6MDow_-%3D')
  assert.equal(reviewQueueQuery({ limit: 50, cursor: null }), '?limit=50')
})

test('appendPage appends in order and drops ids already loaded', () => {
  const a = [{ candidate_id: 'a' }, { id: 'b' }]
  const out = appendPage(a, [{ candidate_id: 'b' }, { candidate_id: 'c' }])
  assert.deepEqual(out.map((x) => x.candidate_id ?? x.id), ['a', 'b', 'c'])
  assert.equal(appendPage(a, [{ id: 'a' }]), a)
})
