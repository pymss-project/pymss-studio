/**
 * Scheduling helpers for batch model downloads.
 *
 * Each model download runs in its own worker process. Starting dozens at once would compete for
 * bandwidth and disk and make every individual download slower, so a batch is queued and drained
 * a few at a time.
 */

/** How many downloads a batch keeps running at once. */
export const BATCH_DOWNLOAD_CONCURRENCY = 2

/** Download statuses that occupy a slot. */
export const ACTIVE_DOWNLOAD_STATUSES = ['preparing', 'downloading'] as const

export function isActiveDownloadStatus(status: string | undefined): boolean {
  return Boolean(status && (ACTIVE_DOWNLOAD_STATUSES as readonly string[]).includes(status))
}

/** Appends names to the queue, skipping duplicates and names that must not be queued. */
export function enqueueDownloads(queue: readonly string[], names: readonly string[], skip: (name: string) => boolean = () => false): string[] {
  const next = [...queue]
  const seen = new Set(next)
  for (const name of names) {
    if (!name || seen.has(name) || skip(name)) continue
    seen.add(name)
    next.push(name)
  }
  return next
}

/**
 * Splits the queue into the names to start now and the names that keep waiting.
 *
 * `activeCount` counts every running download, including ones started by hand, so a manual
 * download also takes a slot.
 */
export function takeQueuedDownloads(queue: readonly string[], activeCount: number, concurrency = BATCH_DOWNLOAD_CONCURRENCY) {
  const free = Math.max(0, concurrency - Math.max(0, activeCount))
  return { start: queue.slice(0, free), rest: queue.slice(free) }
}
