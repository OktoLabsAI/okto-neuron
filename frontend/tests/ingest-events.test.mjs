// node --test tests/ingest-events.test.mjs  (Node >= 22.18 strips the TS types)
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { eventPayloadView, formatByteCount, isTruncatedPayload } from '../src/lib/ingest-events.ts'

const truncated = { truncated: true, original_bytes: 40123, sha256: 'a'.repeat(64), preview: '{"messages":[' }

test('a truncated payload shows the preview and the original length', () => {
  const view = eventPayloadView(truncated)
  assert.equal(view.text, '{"messages":[')
  assert.match(view.note, /^truncated \(40123 bytes, 39\.2 KB; sha256 aaaaaaaaaaaa…\)$/)
})

test('a full payload is pretty-printed with no truncation note', () => {
  const view = eventPayloadView({ block: 3, nodes: [] })
  assert.equal(view.text, JSON.stringify({ block: 3, nodes: [] }, null, 2))
  assert.equal(view.note, null)
})

test('only the exact truncated shape is treated as a preview', () => {
  assert.equal(isTruncatedPayload(truncated), true)
  assert.equal(isTruncatedPayload({ truncated: true }), false)
  assert.equal(isTruncatedPayload({ truncated: 'yes', preview: 'x', original_bytes: 1 }), false)
  assert.equal(isTruncatedPayload(null), false)
  assert.equal(isTruncatedPayload('text'), false)
})

test('byte counts are human readable', () => {
  assert.equal(formatByteCount(12), '12 bytes')
  assert.equal(formatByteCount(2048), '2.0 KB')
  assert.equal(formatByteCount(5 * 1024 * 1024), '5.0 MB')
})
