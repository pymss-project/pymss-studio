import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { extrapolateTransportPosition, formatTimecodeMs, parseTimecode } from '../src/utils/editorTime.ts'

describe('native transport position', () => {
  const anchor = { position: 10, duration: 60, at: 1000 }

  it('advances by the time elapsed since the last report', () => {
    assert.equal(extrapolateTransportPosition(anchor, 1000, false), 10)
    assert.equal(extrapolateTransportPosition(anchor, 3500, false), 12.5)
  })

  it('never moves before the reported position', () => {
    assert.equal(extrapolateTransportPosition(anchor, 900, false), 10)
  })

  it('stops at the end of the project without looping', () => {
    assert.equal(extrapolateTransportPosition(anchor, 1000 + 55_000, false), 60)
  })

  it('wraps to the start of the project while looping', () => {
    assert.equal(extrapolateTransportPosition(anchor, 1000 + 55_000, true), 5)
  })

  it('advances without a limit when the duration is unknown', () => {
    assert.equal(extrapolateTransportPosition({ ...anchor, duration: 0 }, 1000 + 55_000, true), 65)
  })
})

describe('editable timecodes', () => {
  it('formats seconds with millisecond precision', () => {
    assert.equal(formatTimecodeMs(0), '0:00.000')
    assert.equal(formatTimecodeMs(246.30886), '4:06.309')
    assert.equal(formatTimecodeMs(3725.5), '1:02:05.500')
    assert.equal(formatTimecodeMs(-1), '0:00.000')
  })

  it('parses timecodes and plain seconds', () => {
    assert.equal(parseTimecode('4:06.309'), 246.309)
    assert.equal(parseTimecode('1:02:05.5'), 3725.5)
    assert.equal(parseTimecode('12.5'), 12.5)
    assert.equal(parseTimecode('0,25'), 0.25)
    for (const value of ['', 'abc', '1:75', '1.5:20', '1:2:3:4', '-3']) assert.equal(parseTimecode(value), null)
  })
})
