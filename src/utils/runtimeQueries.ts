/**
 * Runtime management shares a cross-process lock. Queue local queries and writes
 * together so they do not compete for that lock; background writes retain their
 * queue slot until the worker reports a terminal event.
 */
export const RUNTIME_BUSY_RETRY_DELAYS_MS = [100, 250, 500] as const

const RUNTIME_BUSY_MESSAGE = 'Another runtime operation is in progress. Wait for it to finish and retry.'
const RUNTIME_ACCESS_BUSY_MESSAGE = 'RUNTIME_BUSY: The runtime is in use. Wait for running operations to finish before updating core packages or starting another operation.'

/** Rust rejects admission before starting the worker, so no operation has been replayed. */
export function isRuntimeAdmissionBusyError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)
  return message === RUNTIME_ACCESS_BUSY_MESSAGE || message === `worker error: ${RUNTIME_ACCESS_BUSY_MESSAGE}`
}

/**
 * Worker errors are surfaced by Rust as `worker error: <worker message>`; the
 * JSON error code itself is not retained by that boundary. Match the complete
 * known message rather than treating arbitrary timeouts or worker failures as
 * retryable.
 */
export function isRuntimeBusyError(error: unknown): boolean {
  if (error !== null && typeof error === 'object' && 'code' in error && error.code === 'RUNTIME_BUSY') return true
  const message = error instanceof Error ? error.message : String(error)
  return [RUNTIME_BUSY_MESSAGE, RUNTIME_ACCESS_BUSY_MESSAGE].some(value => message === value || message === `worker error: ${value}`)
}

export type RuntimeQueryQueueOptions = {
  delaysMs?: readonly number[]
  wait?: (delayMs: number) => Promise<void>
}

const defaultWait = (delayMs: number) => new Promise<void>(resolve => setTimeout(resolve, delayMs))

/** Retry read admission conflicts without making independent reads wait for each other. */
export async function retryRuntimeQuery<T>(
  operation: () => Promise<T>,
  options: RuntimeQueryQueueOptions & { shouldRetry?: (error: unknown) => boolean } = {},
): Promise<T> {
  const delaysMs = options.delaysMs || RUNTIME_BUSY_RETRY_DELAYS_MS
  const wait = options.wait || defaultWait
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await operation()
    } catch (error) {
      if (!isRuntimeBusyError(error) || attempt === delaysMs.length || options.shouldRetry?.(error) === false) throw error
      await wait(Math.max(0, delaysMs[attempt]))
      if (options.shouldRetry?.(error) === false) throw error
    }
  }
}

/** Queues distinct operations; only read queries retry the worker's lock-busy error. */
export function createRuntimeQueryQueue(options: RuntimeQueryQueueOptions = {}) {
  let tail: Promise<void> = Promise.resolve()

  return function runRuntimeQuery<T>(operation: () => Promise<T>, { retryBusy = true } = {}): Promise<T> {
    const current = tail.then(() => retryBusy ? retryRuntimeQuery(operation, options) : operation())
    // A failed query must not prevent the next fresh query from starting.
    tail = current.then(() => undefined, () => undefined)
    return current
  }
}

/** Reuse read snapshots on page visits; explicit refreshes and mutations stay fresh. */
export function createRuntimeReadCache<T>(maxAgeMs: number, now = () => Date.now()) {
  let generation = 0
  let sequence = 0
  let snapshot: { value: T; checkedAt: number } | undefined
  let pending: Promise<T> | undefined

  function invalidate() {
    generation += 1
    snapshot = undefined
    pending = undefined
  }

  function read(load: () => Promise<T>, fresh = false): Promise<T> {
    if (!fresh) {
      if (pending) return pending
      if (snapshot && now() - snapshot.checkedAt < maxAgeMs) return Promise.resolve(snapshot.value)
    }
    const revision = generation
    const request = ++sequence
    const result = load().then(value => {
      if (revision === generation && request === sequence) snapshot = { value, checkedAt: now() }
      return value
    }, error => {
      if (revision === generation && request === sequence) snapshot = undefined
      throw error
    }).finally(() => {
      if (pending === result) pending = undefined
    })
    pending = result
    return result
  }

  return { read, invalidate }
}
