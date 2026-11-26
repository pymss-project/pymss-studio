import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { extrapolateTransportPosition } from '../src/utils/editorTime.ts'

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
