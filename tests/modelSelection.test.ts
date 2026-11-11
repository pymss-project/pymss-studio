import assert from 'node:assert/strict'
import test from 'node:test'
import { edgeScrollVelocity, segmentIntersectsRect } from '../src/utils/selection.ts'

test('a fast vertical sweep intersects every row in either direction', () => {
  const rows = Array.from({ length: 10 }, (_, index) => ({ left: 20, right: 500, top: index * 60, bottom: index * 60 + 50 }))
  for (const [start, end] of [[{ x: 35, y: 20 }, { x: 35, y: 590 }], [{ x: 35, y: 590 }, { x: 35, y: 20 }]]) {
    assert.deepEqual(rows.map(rect => segmentIntersectsRect(start!, end!, rect)), Array(10).fill(true))
  }
})

test('a diagonal sweep follows its path rather than selecting its whole bounding box', () => {
  const start = { x: 10, y: 10 }, end = { x: 290, y: 290 }
  assert.equal(segmentIntersectsRect(start, end, { left: 100, top: 100, right: 200, bottom: 200 }), true)
  assert.equal(segmentIntersectsRect(start, end, { left: 210, top: 10, right: 290, bottom: 90 }), false)
  assert.equal(segmentIntersectsRect(start, end, { left: 10, top: 210, right: 90, bottom: 290 }), false)
})

test('stationary, horizontal and outside movements respect the rectangle boundaries', () => {
  const rect = { left: 100, top: 100, right: 200, bottom: 200 }
  assert.equal(segmentIntersectsRect({ x: 150, y: 150 }, { x: 150, y: 150 }, rect), true)
  assert.equal(segmentIntersectsRect({ x: 50, y: 50 }, { x: 50, y: 50 }, rect), false)
  assert.equal(segmentIntersectsRect({ x: -100, y: 150 }, { x: 300, y: 150 }, rect), true)
  assert.equal(segmentIntersectsRect({ x: -100, y: 50 }, { x: 300, y: 50 }, rect), false)
  assert.equal(segmentIntersectsRect({ x: 50, y: 150 }, { x: 90, y: 150 }, rect), false)
  assert.equal(segmentIntersectsRect({ x: 200, y: 50 }, { x: 200, y: 100 }, rect), true)
})

test('edge scrolling is bounded, directional and stops outside the scroll container', () => {
  const rect = { left: 100, top: 100, right: 500, bottom: 400 }
  assert.equal(edgeScrollVelocity({ x: 200, y: 250 }, rect), 0)
  assert.equal(edgeScrollVelocity({ x: 200, y: 118 }, rect), -300)
  assert.equal(edgeScrollVelocity({ x: 200, y: 382 }, rect), 300)
  assert.equal(edgeScrollVelocity({ x: 200, y: 90 }, rect), -600)
  assert.equal(edgeScrollVelocity({ x: 200, y: 420 }, rect), 600)
  assert.equal(edgeScrollVelocity({ x: 90, y: 382 }, rect), 0)
  assert.equal(edgeScrollVelocity({ x: 200, y: 450 }, rect), 0)
  assert.equal(edgeScrollVelocity({ x: 200, y: 0 }, { ...rect, top: 0, bottom: 0 }), 0)
})
