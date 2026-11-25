export function formatDateTime(value?: string | null) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date)
}

const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

/** Short "updated" label: relative within a week, then a calendar date. */
export function formatRecentTime(timestamp: number, locale: string, now = Date.now()) {
  if (!Number.isFinite(timestamp) || timestamp <= 0) return ''
  const elapsed = Math.max(0, now - timestamp)
  const relative = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' })
  if (elapsed < MINUTE) return relative.format(0, 'second')
  if (elapsed < HOUR) return relative.format(-Math.floor(elapsed / MINUTE), 'minute')
  if (elapsed < DAY) return relative.format(-Math.floor(elapsed / HOUR), 'hour')
  if (elapsed < 7 * DAY) return relative.format(-Math.floor(elapsed / DAY), 'day')
  const date = new Date(timestamp)
  const sameYear = date.getFullYear() === new Date(now).getFullYear()
  return new Intl.DateTimeFormat(locale, sameYear
    ? { month: 'short', day: 'numeric' }
    : { year: 'numeric', month: 'short', day: 'numeric' }).format(date)
}
