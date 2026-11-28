// Shared level meter scale and volume formatting for editor channel controls.

/** Headroom above full scale shown on meters and reachable with faders. */
export const METER_MAX_DB = 6
const METER_MIN_DB = -60
/** Largest fader gain: +6.02 dB, the 0..2 linear range channel volumes are stored in. */
const FADER_MAX_GAIN = 2

export const METER_TICKS = [
  { db: 6, position: 1 },
  { db: 3, position: 0.92 },
  { db: 0, position: 0.84 },
  { db: -3, position: 0.74 },
  { db: -6, position: 0.64 },
  { db: -9, position: 0.54 },
  { db: -12, position: 0.44 },
  { db: -15, position: 0.34 },
  { db: -24, position: 0.2 },
  { db: -30, position: 0.1 },
  { db: -60, position: 0 },
] as const

/** Linear sample level, kept within the meter's headroom. */
export function normalizeLevel(value: number | undefined) {
  const max = 10 ** (METER_MAX_DB / 20)
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.min(max, value)) : 0
}

export function levelDb(value: number) {
  return value > 0 ? 20 * Math.log10(value) : -Infinity
}

/** Level in dBFS, floored at the bottom of the meter. */
export function meterValue(value: number) {
  return Math.max(METER_MIN_DB, levelDb(value))
}

/** Position of a dB value on the meter, 0 to 1, piecewise linear between the printed ticks. */
export function dbPosition(db: number) {
  if (db >= METER_MAX_DB) return 1
  for (let index = 1; index < METER_TICKS.length; index++) {
    const lower = METER_TICKS[index]
    const upper = METER_TICKS[index - 1]
    if (db >= lower.db) return lower.position + (db - lower.db) / (upper.db - lower.db) * (upper.position - lower.position)
  }
  return 0
}

/** dB value at a meter position; the inverse of `dbPosition`. */
export function positionDb(position: number) {
  if (position >= 1) return METER_MAX_DB
  for (let index = 1; index < METER_TICKS.length; index++) {
    const lower = METER_TICKS[index]
    const upper = METER_TICKS[index - 1]
    if (position >= lower.position) return lower.db + (position - lower.position) / (upper.position - lower.position) * (upper.db - lower.db)
  }
  return METER_MIN_DB
}

/** Meter fill from 0 to 1 for a linear sample level. */
export function meterScale(value: number) {
  return dbPosition(meterValue(value))
}

/** Fader position on the meter scale for a linear gain; the bottom is silence. */
export function faderPosition(gain: number) {
  return gain > 0 ? dbPosition(levelDb(gain)) : 0
}

/** Linear gain for a fader position on the meter scale. */
export function faderGain(position: number) {
  if (position <= 0) return 0
  return Math.min(FADER_MAX_GAIN, 10 ** (positionDb(position) / 20))
}

/** Tick label with an explicit sign, for example "+6", "0" or "−12". */
export function formatTickDb(db: number) {
  return db > 0 ? `+${db}` : db < 0 ? `−${Math.abs(db)}` : '0'
}

export function formatLevel(value: number) {
  return value > 0 ? levelDb(value).toFixed(1) : '−∞'
}

/** Linear gain as a signed dB value, for example "+1.5 dB" or "−∞ dB". */
export function formatVolumeDb(value: number) {
  const volume = Math.max(0, Number(value || 0))
  if (volume <= 0.0001) return '−∞ dB'
  const db = 20 * Math.log10(volume)
  return `${db > 0.05 ? '+' : ''}${db.toFixed(1)} dB`
}

/** Pan as a compact label: C, L25 or R40. */
export function formatPanShort(value: number) {
  const pan = Number(value || 0)
  if (Math.abs(pan) < 0.025) return 'C'
  return `${pan < 0 ? 'L' : 'R'}${Math.round(Math.abs(pan) * 100)}`
}
