import assert from 'node:assert/strict'
import test from 'node:test'

import { createRuntimeQueryQueue, isRuntimeBusyError } from '../src/utils/runtimeQueries.ts'

const busy = new Error('worker error: Another runtime operation is in progress. Wait for it to finish and retry.')

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
