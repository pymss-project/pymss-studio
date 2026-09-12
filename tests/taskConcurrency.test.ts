import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import {
  DEFAULT_CONCURRENT_SEPARATIONS,
  MAX_CONCURRENT_SEPARATIONS,
  normalizeConcurrentSeparations,
} from '../src/features/tasks/concurrency.ts'
import { selectQueuedJobGroups, type TaskLifecycleItem } from '../src/features/tasks/lifecycle.ts'

describe('separation concurrency settings', () => {
  it('preserves the existing default, limit and all valid persisted values', () => {
    assert.equal(DEFAULT_CONCURRENT_SEPARATIONS, 1)
    assert.equal(MAX_CONCURRENT_SEPARATIONS, 16)
    for (let count = 1; count <= 16; count += 1) {
      assert.equal(normalizeConcurrentSeparations(count), count)
    }
  })

  for (const [value, expected] of [
    [undefined, 1], [null, 1], [NaN, 1], [Infinity, 1], [-Infinity, 1],
    ['', 1], ['invalid', 1], ['4', 4], [' 3.9 ', 3],
    [-8, 1], [0, 1], [0.5, 1], [2.9, 2], [16.9, 16], [99, 16],
  ] as const) {
    it(`normalizes ${String(value)} to ${expected}`, () => {
      assert.equal(normalizeConcurrentSeparations(value), expected)
    })
  }

  it('limits queued worker groups even when the scheduler receives an unnormalized setting', () => {
    const tasks: TaskLifecycleItem[] = Array.from({ length: 20 }, (_, index) => ({
      id: `task-${index}`, jobId: `job-${index}`, status: 'queued', createdAt: index,
    }))
    for (const [value, expected] of [[99, 16], [2.9, 2], [-1, 1], [NaN, 1]]) {
      const groups = selectQueuedJobGroups(tasks, normalizeConcurrentSeparations(value))
      assert.equal(groups.length, expected)
      assert.deepEqual(groups.map(group => group[0]?.id), tasks.slice(0, expected).map(task => task.id))
    }
  })
})
