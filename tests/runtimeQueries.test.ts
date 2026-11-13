import assert from 'node:assert/strict'
import test from 'node:test'

import { createRuntimeQueryQueue, createRuntimeReadCache, isRuntimeAdmissionBusyError, isRuntimeBusyError, retryRuntimeQuery } from '../src/utils/runtimeQueries.ts'

const busy = new Error('worker error: Another runtime operation is in progress. Wait for it to finish and retry.')
const accessBusy = 'worker error: RUNTIME_BUSY: The runtime is in use. Wait for running operations to finish before updating core packages or starting another operation.'

test('runtime admission errors are recognized without accepting unrelated failures', () => {
  assert.equal(isRuntimeBusyError(accessBusy), true)
  assert.equal(isRuntimeBusyError(new Error(accessBusy)), true)
  assert.equal(isRuntimeBusyError({ code: 'RUNTIME_BUSY', message: 'Runtime occupied' }), true)
  assert.equal(isRuntimeBusyError('RUNTIME_RECOVERY_REQUIRED: RUNTIME_BUSY'), false)
  assert.equal(isRuntimeAdmissionBusyError(accessBusy), true)
  assert.equal(isRuntimeAdmissionBusyError(busy), false)
  assert.equal(isRuntimeAdmissionBusyError({ code: 'RUNTIME_BUSY', message: 'worker already started' }), false)
})

test('recovery admission retries never replay a worker-side busy error', async () => {
  let attempts = 0
  await assert.rejects(retryRuntimeQuery(async () => {
    attempts += 1
    throw busy
  }, { delaysMs: [1], wait: async () => {}, shouldRetry: isRuntimeAdmissionBusyError }), /Another runtime operation/)
  assert.equal(attempts, 1)
})

test('independent reads retry admission conflicts without serializing each other', async () => {
  let release!: () => void
  const waiting = new Promise<void>(resolve => { release = resolve })
  let attempts = 0
  const first = retryRuntimeQuery(async () => {
    if (++attempts === 1) throw new Error(accessBusy)
    return 'inventory'
  }, { delaysMs: [1], wait: () => waiting })
  assert.equal(await retryRuntimeQuery(async () => 'catalog'), 'catalog')
  release()
  assert.equal(await first, 'inventory')
  assert.equal(attempts, 2)
})

test('a cancelled or superseded read cannot dispatch another retry', async () => {
  let current = true
  let attempts = 0
  await assert.rejects(retryRuntimeQuery(async () => {
    attempts += 1
    throw new Error(accessBusy)
  }, { delaysMs: [1], shouldRetry: () => current, wait: async () => { current = false } }), /RUNTIME_BUSY/)
  assert.equal(attempts, 1)
})

test('page reads reuse an in-flight query and a recent result, then expire', async () => {
  let clock = 0
  let calls = 0
  const cache = createRuntimeReadCache<number>(100, () => clock)
  const load = async () => ++calls
  assert.deepEqual(await Promise.all([cache.read(load), cache.read(load)]), [1, 1])
  clock = 99
  assert.equal(await cache.read(load), 1)
  clock = 100
  assert.equal(await cache.read(load), 2)
  assert.equal(await cache.read(load, true), 3)
})

test('a result started before a runtime mutation cannot refill the cache', async () => {
  const cache = createRuntimeReadCache<string>(1000)
  let finish!: (value: string) => void
  const old = cache.read(() => new Promise(resolve => { finish = resolve }))
  cache.invalidate()
  assert.equal(await cache.read(async () => 'new'), 'new')
  finish('old')
  await old
  assert.equal(await cache.read(async () => 'unexpected'), 'new')
})

test('an explicit refresh failure leaves the read cache retryable', async () => {
  const cache = createRuntimeReadCache<string>(1000)
  await cache.read(async () => 'old')
  await assert.rejects(cache.read(async () => { throw new Error('unavailable') }, true), /unavailable/)
  assert.equal(await cache.read(async () => 'recovered'), 'recovered')
})

test('info and sizes queries never overlap and each queued callback runs', async () => {
  const queue = createRuntimeQueryQueue()
  let active = 0
  let maxActive = 0
  const calls: string[] = []
  const query = async (name: string) => {
    active += 1
    maxActive = Math.max(maxActive, active)
    calls.push(name)
    await new Promise(resolve => setTimeout(resolve, 1))
    active -= 1
    return name
  }

  assert.deepEqual(await Promise.all([queue(() => query('info')), queue(() => query('sizes'))]), ['info', 'sizes'])
  assert.equal(maxActive, 1)
  assert.deepEqual(calls, ['info', 'sizes'])
})

test('a later failed query does not discard an earlier successful snapshot', async () => {
  const queue = createRuntimeQueryQueue()
  let snapshot: string | null = null
  const first = queue(async () => {
    snapshot = 'fresh-info'
    return snapshot
  })
  const later = queue(async () => { throw new Error('worker failed') })

  assert.equal(await first, 'fresh-info')
  await assert.rejects(later, /worker failed/)
  assert.equal(snapshot, 'fresh-info')
})

test('busy errors retry a bounded number of times', async () => {
  const waits: number[] = []
  const queue = createRuntimeQueryQueue({ delaysMs: [1, 2], wait: async delay => { waits.push(delay) } })
  let attempts = 0
  await assert.rejects(queue(async () => {
    attempts += 1
    throw busy
  }), /Another runtime operation/)
  assert.equal(attempts, 3)
  assert.deepEqual(waits, [1, 2])
})

test('non-busy errors fail immediately and do not block the queue', async () => {
  const waits: number[] = []
  const queue = createRuntimeQueryQueue({ delaysMs: [1, 2], wait: async delay => { waits.push(delay) } })
  await assert.rejects(queue(async () => { throw new Error('runtime corrupt') }), /runtime corrupt/)
  assert.equal(await queue(async () => 'next snapshot'), 'next snapshot')
  assert.deepEqual(waits, [])
  assert.equal(isRuntimeBusyError(new Error('RUNTIME_BUSY')), false)
})


test('busy retry success releases the queue for the next query', async () => {
  const waits: number[] = []
  const queue = createRuntimeQueryQueue({ delaysMs: [1], wait: async delay => { waits.push(delay) } })
  let attempts = 0
  const recovered = queue(async () => {
    attempts += 1
    if (attempts === 1) throw busy
    return 'recovered'
  })
  const next = queue(async () => 'fresh-after-retry')

  assert.equal(await recovered, 'recovered')
  assert.equal(await next, 'fresh-after-retry')
  assert.equal(attempts, 2)
  assert.deepEqual(waits, [1])
})

test('recognizes only the Rust worker busy-error string', () => {
  assert.equal(isRuntimeBusyError(busy), true)
  assert.equal(isRuntimeBusyError('Another runtime operation is in progress. Wait for it to finish and retry.'), true)
  assert.equal(isRuntimeBusyError(new Error('worker error: RUNTIME_BUSY: Another runtime operation is in progress. Wait for it to finish and retry.')), false)
})

test('retryBusy false does not retry a busy write and releases the queue', async () => {
  const waits: number[] = []
  const queue = createRuntimeQueryQueue({ delaysMs: [1, 2], wait: async delay => { waits.push(delay) } })
  let attempts = 0
  await assert.rejects(queue(async () => {
    attempts += 1
    throw busy
  }, { retryBusy: false }), /Another runtime operation/)
  assert.equal(attempts, 1)
  assert.deepEqual(waits, [])
  assert.equal(await queue(async () => 'next operation'), 'next operation')
})
