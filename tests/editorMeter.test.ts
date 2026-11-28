import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { dbPosition, faderGain, faderPosition, formatTickDb, meterScale, METER_TICKS, normalizeLevel, positionDb } from '../src/utils/editorMeter.ts'

describe('editor meter scale', () => {
  it('reserves 6 dB of headroom above full scale', () => {
    assert.equal(METER_TICKS[0].db, 6)
    assert.equal(meterScale(10 ** (6 / 20)), 1)
    assert.ok(meterScale(1) < 1)
    assert.equal(normalizeLevel(4), 10 ** (6 / 20))
    assert.equal(normalizeLevel(Number.NaN), 0)
  })

  it('places every printed tick at its own position and maps positions back', () => {
    for (const tick of METER_TICKS) {
      assert.equal(dbPosition(tick.db), tick.position)
      assert.ok(Math.abs(positionDb(tick.position) - tick.db) < 1e-9)
    }
    for (const db of [-45, -20, -7.5, -1, 1.5, 4.2]) {
      assert.ok(Math.abs(positionDb(dbPosition(db)) - db) < 1e-9)
    }
  })

  it('aligns fader gain with the meter ticks', () => {
    assert.equal(faderPosition(1), 0.84)
    assert.equal(faderGain(0.84), 1)
    assert.equal(faderPosition(0), 0)
    assert.equal(faderGain(0), 0)
    assert.equal(faderGain(1), 10 ** (6 / 20))
    assert.ok(Math.abs(faderGain(faderPosition(0.5)) - 0.5) < 1e-9)
  })

  it('labels ticks with explicit signs', () => {
    assert.deepEqual([6, 0, -12].map(formatTickDb), ['+6', '0', '−12'])
  })
})
