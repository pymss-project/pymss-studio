// Shared level meter scale and volume formatting for editor channel controls.

export const METER_TICKS = [
  { db: 0, position: 1 },
  { db: -3, position: 0.88 },
  { db: -6, position: 0.76 },
  { db: -9, position: 0.64 },
  { db: -12, position: 0.52 },
  { db: -15, position: 0.4 },
  { db: -24, position: 0.24 },
  { db: -30, position: 0.12 },
  { db: -60, position: 0 },
] as const

export function normalizeLevel(value: number | undefined) {
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0
}

export function levelDb(value: number) {
  return value > 0 ? 20 * Math.log10(value) : -Infinity
}

/** Level in dBFS, floored at the bottom of the meter. */
export function meterValue(value: number) {
  return Math.max(-60, levelDb(value))
}

/** Meter fill from 0 to 1, piecewise linear between the printed ticks. */
export function meterScale(value: number) {
  const db = meterValue(value)
  if (db >= 0) return 1
  for (let index = 1; index < METER_TICKS.length; index++) {
    const lower = METER_TICKS[index]
    const upper = METER_TICKS[index - 1]
    if (db >= lower.db) return lower.position + (db - lower.db) / (upper.db - lower.db) * (upper.position - lower.position)
  }
  return 0
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
