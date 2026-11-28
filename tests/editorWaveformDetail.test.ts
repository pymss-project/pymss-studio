import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { DETAIL_BLOCK_SECONDS, DETAIL_POINTS_PER_SECOND, detailBlockRange, detailPeak, type DetailBlock } from '../src/utils/editorWaveformDetail.ts'

const points = DETAIL_BLOCK_SECONDS * DETAIL_POINTS_PER_SECOND

function block(left: Record<number, number>, right: Record<number, number> = {}): DetailBlock {
  const channel = (values: Record<number, number>) => Array.from({ length: points }, (_, index) => values[index] ?? 0)
  return [channel(left), channel(right)]
}

describe('editor waveform detail peaks', () => {
  it('lists the blocks a stretch of audio overlaps', () => {
    assert.deepEqual(detailBlockRange(0, DETAIL_BLOCK_SECONDS), [0])
    assert.deepEqual(detailBlockRange(3.9, 8.1), [0, 1, 2])
    assert.deepEqual(detailBlockRange(-1, 0.5), [0])
  })

  it('finds a peak inside one column and across a block boundary', () => {
    const blocks = [block({ 10: 0.25 }), block({ 0: 0.75 }, { 1: 0.5 })]
    const lookup = (index: number) => blocks[index]
    const at = (point: number) => point / DETAIL_POINTS_PER_SECOND
    assert.equal(detailPeak(lookup, 0, at(10), at(11)), 0.25)
    assert.equal(detailPeak(lookup, 0, at(11), at(20)), 0)
    assert.equal(detailPeak(lookup, 0, DETAIL_BLOCK_SECONDS - at(1), DETAIL_BLOCK_SECONDS + at(1)), 0.75)
    assert.equal(detailPeak(lookup, 1, DETAIL_BLOCK_SECONDS, DETAIL_BLOCK_SECONDS + at(2)), 0.5)
    assert.equal(detailPeak(lookup, 'all', DETAIL_BLOCK_SECONDS, DETAIL_BLOCK_SECONDS + at(2)), 0.75)
  })

  it('falls back while a needed block is missing or could not be measured', () => {
    assert.equal(detailPeak(() => undefined, 0, 0, 1), null)
    assert.equal(detailPeak(index => (index === 0 ? block({}) : null), 0, 3, 5), null)
    // A block past the end of the file is known to hold no audio.
    assert.equal(detailPeak(() => [], 0, 0, 1), 0)
  })
})
