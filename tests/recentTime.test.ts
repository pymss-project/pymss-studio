import assert from 'node:assert/strict'
import test from 'node:test'

import { formatRecentTime } from '../src/utils/time.ts'

const now = Date.UTC(2026, 10, 25, 12, 0, 0)

test('recent updates read as relative time', () => {
  assert.equal(formatRecentTime(now - 20_000, 'en', now), 'now')
  assert.equal(formatRecentTime(now - 5 * 60_000, 'en', now), '5 minutes ago')
  assert.equal(formatRecentTime(now - 3 * 3_600_000, 'en', now), '3 hours ago')
  assert.equal(formatRecentTime(now - 26 * 3_600_000, 'en', now), 'yesterday')
  assert.equal(formatRecentTime(now - 3 * 3_600_000, 'zh-CN', now), '3小时前')
})

test('older updates show a date, with the year only outside the current year', () => {
  assert.equal(formatRecentTime(Date.UTC(2026, 9, 3, 12), 'en', now), 'Oct 3')
  assert.equal(formatRecentTime(Date.UTC(2025, 9, 3, 12), 'en', now), 'Oct 3, 2025')
})

test('missing timestamps produce no label', () => {
  assert.equal(formatRecentTime(0, 'en', now), '')
  assert.equal(formatRecentTime(Number.NaN, 'en', now), '')
})
