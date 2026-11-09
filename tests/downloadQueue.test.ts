import assert from 'node:assert/strict'
import test from 'node:test'
import { enqueueDownloads, isActiveDownloadStatus, takeQueuedDownloads } from '../src/utils/downloadQueue.ts'

test('enqueue keeps order and drops duplicates and skipped names', () => {
  const next = enqueueDownloads(['a'], ['b', 'a', 'c', 'b', '', 'skip'], (name) => name === 'skip')
  assert.deepEqual(next, ['a', 'b', 'c'])
})

test('take starts only as many downloads as there are free slots', () => {
  assert.deepEqual(takeQueuedDownloads(['a', 'b', 'c'], 0, 2), { start: ['a', 'b'], rest: ['c'] })
  assert.deepEqual(takeQueuedDownloads(['a', 'b', 'c'], 1, 2), { start: ['a'], rest: ['b', 'c'] })
  // A manual download beyond the limit must not produce a negative slice.
  assert.deepEqual(takeQueuedDownloads(['a'], 5, 2), { start: [], rest: ['a'] })
})

test('only preparing and downloading occupy a slot', () => {
  assert.equal(isActiveDownloadStatus('preparing'), true)
  assert.equal(isActiveDownloadStatus('downloading'), true)
  for (const status of ['paused', 'error', 'done', 'cancelled', 'interrupted', undefined]) {
    assert.equal(isActiveDownloadStatus(status), false)
  }
})
