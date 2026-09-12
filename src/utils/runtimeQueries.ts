/**
 * Runtime management shares a cross-process lock. Queue local queries and writes
 * together so they do not compete for that lock; background writes retain their
 * queue slot until the worker reports a terminal event.
 */
export const RUNTIME_BUSY_RETRY_DELAYS_MS = [100, 250, 500] as const

const RUNTIME_BUSY_MESSAGE = 'Another runtime operation is in progress. Wait for it to finish and retry.'

/**
 * Worker errors are surfaced by Rust as `worker error: <worker message>`; the
 * JSON error code itself is not retained by that boundary. Match the complete
 * known message rather than treating arbitrary timeouts or worker failures as
 * retryable.
 */
export function isRuntimeBusyError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)
  return message === RUNTIME_BUSY_MESSAGE || message === `worker error: ${RUNTIME_BUSY_MESSAGE}`
}

export type RuntimeQueryQueueOptions = {
  delaysMs?: readonly number[]
  wait?: (delayMs: number) => Promise<void>
}

const defaultWait = (delayMs: number) => new Promise<void>(resolve => setTimeout(resolve, delayMs))

/** Queues distinct operations; only read queries retry the worker's lock-busy error. */
export function createRuntimeQueryQueue(options: RuntimeQueryQueueOptions = {}) {
  const delaysMs = options.delaysMs || RUNTIME_BUSY_RETRY_DELAYS_MS
  const wait = options.wait || defaultWait
  let tail: Promise<void> = Promise.resolve()

  return function runRuntimeQuery<T>(operation: () => Promise<T>, { retryBusy = true } = {}): Promise<T> {
    const current = tail.then(async () => {
      for (let attempt = 0; ; attempt += 1) {
        try {
          return await operation()
        } catch (error) {
          if (!retryBusy || !isRuntimeBusyError(error) || attempt === delaysMs.length) throw error
          await wait(Math.max(0, delaysMs[attempt]))
        }
      }
    })
    // A failed query must not prevent the next fresh query from starting.
    tail = current.then(() => undefined, () => undefined)
    return current
  }
}
