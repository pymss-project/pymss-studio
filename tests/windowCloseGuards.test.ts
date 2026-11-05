import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { registerWindowCloseGuard, runWindowCloseGuards, WindowClosePersistenceError } from '../src/utils/windowCloseGuards.ts'

describe('window close guards', () => {
  it('requires an explicit discard decision and continues all remaining guards', async () => {
    const calls: string[] = []
    const error = new WindowClosePersistenceError(new Error('Save failed'), async () => { calls.push('discard') })
    const removeFirst = registerWindowCloseGuard(() => { calls.push('save'); throw error }, 1)
    const removeLast = registerWindowCloseGuard(() => { calls.push('finalize') })
    try {
      await assert.rejects(runWindowCloseGuards(), /Save failed/)
      await assert.rejects(runWindowCloseGuards(async () => 'cancel'), /Save failed/)
      assert.equal(calls.includes('discard'), false)
      calls.length = 0
      await runWindowCloseGuards(async () => 'discard')
      assert.deepEqual(calls, ['save', 'discard', 'finalize'])
    } finally { removeFirst(); removeLast() }
  })

  it('retries persistence and never discards when retry succeeds', async () => {
    let attempts = 0
    let discarded = false
    const remove = registerWindowCloseGuard(() => {
      if (++attempts === 1) throw new WindowClosePersistenceError('Save failed', async () => { discarded = true })
    })
    try {
      await runWindowCloseGuards(async () => 'retry')
      assert.equal(attempts, 2)
      assert.equal(discarded, false)
    } finally { remove() }
  })

  it('cannot bypass a recording/task guard or a failed release through persistence recovery', async () => {
    let decisions = 0
    const remove = registerWindowCloseGuard(() => { throw new Error('Recording is not finalized') })
    try {
      await assert.rejects(runWindowCloseGuards(async () => { decisions++; return 'discard' }), /Recording/)
      assert.equal(decisions, 0)
    } finally { remove() }
    const unregister = registerWindowCloseGuard(() => {
      throw new WindowClosePersistenceError('Save failed', async () => { throw new Error('Release failed') })
    })
    try { await assert.rejects(runWindowCloseGuards(async () => 'discard'), /Release failed/) }
    finally { unregister() }
  })

  it('waits for registered asynchronous cleanup before closing', async () => {
    const calls: string[] = []
    const unregisterFirst = registerWindowCloseGuard(async () => {
      await Promise.resolve()
      calls.push('first')
    })
    const unregisterSecond = registerWindowCloseGuard(() => {
      calls.push('second')
    })

    try {
      await runWindowCloseGuards()
      assert.deepEqual(calls, ['first', 'second'])
    } finally {
      unregisterFirst()
      unregisterSecond()
    }
  })

  it('does not run a guard after it is unregistered', async () => {
    let called = false
    const unregister = registerWindowCloseGuard(() => {
      called = true
    })
    unregister()

    await runWindowCloseGuards()
    assert.equal(called, false)
  })

  it('runs resource finalizers before lower-priority persistence guards', async () => {
    const calls: string[] = []
    const unregisterSave = registerWindowCloseGuard(() => {
      calls.push('save')
    }, -100)
    const unregisterRecording = registerWindowCloseGuard(async () => {
      await Promise.resolve()
      calls.push('recording')
    }, 100)

    try {
      await runWindowCloseGuards()
      assert.deepEqual(calls, ['recording', 'save'])
    } finally {
      unregisterSave()
      unregisterRecording()
    }
  })

  it('recovers suspended work only after closure is cancelled', async () => {
    const calls: string[] = []
    const unregisterTask = registerWindowCloseGuard(() => { calls.push('stop') }, 70,
      () => { calls.push('resume') })
    const unregisterSave = registerWindowCloseGuard(() => {
      calls.push('save')
      throw new WindowClosePersistenceError('Save failed', async () => { calls.push('discard') })
    })
    try {
      await assert.rejects(runWindowCloseGuards(async () => 'cancel'), /Save failed/)
      assert.deepEqual(calls, ['stop', 'save', 'resume'])
      calls.length = 0
      await runWindowCloseGuards(async () => 'discard')
      assert.deepEqual(calls, ['stop', 'save', 'discard'])
    } finally { unregisterTask(); unregisterSave() }
  })

  it('does not retain cancellation recovery after a guard is unregistered', async () => {
    let resumed = false
    const unregister = registerWindowCloseGuard(() => {}, 70, () => { resumed = true })
    unregister()
    const unregisterFailure = registerWindowCloseGuard(() => { throw new Error('Close blocked') })
    try {
      await assert.rejects(runWindowCloseGuards(), /Close blocked/)
      assert.equal(resumed, false)
    } finally { unregisterFailure() }
  })
})
