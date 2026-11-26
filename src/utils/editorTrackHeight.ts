/** Track row heights in CSS pixels. Mono rows start at 50 px and stereo rows at 70 px. */
export const TRACK_HEIGHT_MIN = 50
export const TRACK_HEIGHT_MAX = 280

/** A stored height overrides the automatic row height; anything invalid means automatic. */
export function normalizeTrackHeight(value: unknown): number | undefined {
  if (value === null || value === undefined || value === '') return undefined
  const height = Number(value)
  if (!Number.isFinite(height)) return undefined
  return Math.round(Math.min(TRACK_HEIGHT_MAX, Math.max(TRACK_HEIGHT_MIN, height)))
}
