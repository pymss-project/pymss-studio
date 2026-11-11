export type SelectionPoint = { x: number; y: number }
export type SelectionRect = { left: number; top: number; right: number; bottom: number }

/** Pixels per second while a drag is near the vertical edges of its scroll container. */
export function edgeScrollVelocity(point: SelectionPoint, rect: SelectionRect): number {
  if (point.x < rect.left || point.x > rect.right || point.y < rect.top - 32 || point.y > rect.bottom + 32) return 0
  const edge = Math.min(36, (rect.bottom - rect.top) / 3)
  if (edge <= 0) return 0
  if (point.y < rect.top + edge) return -600 * Math.min(1, (rect.top + edge - point.y) / edge)
  if (point.y > rect.bottom - edge) return 600 * Math.min(1, (point.y - rect.bottom + edge) / edge)
  return 0
}

export function segmentIntersectsRect(start: SelectionPoint, end: SelectionPoint, rect: SelectionRect): boolean {
  let enter = 0
  let exit = 1
  for (const [origin, delta, min, max] of [
    [start.x, end.x - start.x, rect.left, rect.right],
    [start.y, end.y - start.y, rect.top, rect.bottom],
  ] as const) {
    if (delta === 0) {
      if (origin < min || origin > max) return false
      continue
    }
    const first = (min - origin) / delta
    const last = (max - origin) / delta
    enter = Math.max(enter, Math.min(first, last))
    exit = Math.min(exit, Math.max(first, last))
    if (enter > exit) return false
  }
  return true
}
