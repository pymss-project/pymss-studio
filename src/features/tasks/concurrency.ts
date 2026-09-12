export const DEFAULT_CONCURRENT_SEPARATIONS = 1
export const MAX_CONCURRENT_SEPARATIONS = 16

export function normalizeConcurrentSeparations(value: unknown): number {
  const count = Number(value)
  return Number.isFinite(count)
    ? Math.min(MAX_CONCURRENT_SEPARATIONS, Math.max(1, Math.trunc(count)))
    : DEFAULT_CONCURRENT_SEPARATIONS
}
