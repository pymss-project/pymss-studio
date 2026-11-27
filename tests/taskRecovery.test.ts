import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { failedTasksToRetry, taskRecoveryAction, taskRecoveryActions } from '../src/features/tasks/recovery.ts'

type Task = { id: string; status: string; retryOf?: string }

function task(id: string, status: string, retryOf?: string): Task {
  return { id, status, retryOf }
}

function failure(errorCode: string, runConfig: Record<string, unknown> = { runMode: 'model' }) {
  return { status: 'failed', errorCode, runConfig } as Parameters<typeof taskRecoveryAction>[0]
}

describe('task recovery actions', () => {
  it('maps failure codes to the setting that resolves them', () => {
    assert.equal(taskRecoveryAction(failure('INPUT_NOT_FOUND')), 'input')
    assert.equal(taskRecoveryAction(failure('MODEL_NOT_FOUND')), 'models')
    assert.equal(taskRecoveryAction(failure('INFERENCE_PARAMS_INVALID')), 'parameters')
    assert.equal(taskRecoveryAction(failure('ROCM_OUT_OF_MEMORY')), 'parameters')
    assert.equal(taskRecoveryAction(failure('WORKER_NATIVE_CRASH')), 'runtime')
    assert.equal(taskRecoveryAction(failure('UNKNOWN')), null)
    assert.equal(taskRecoveryAction({ ...failure('INPUT_NOT_FOUND'), status: 'done' }), null)
  })

  it('offers model parameters only for single-model runs', () => {
    const workflow = { runMode: 'workflow' }
    assert.equal(taskRecoveryAction(failure('ROCM_OUT_OF_MEMORY', workflow)), 'runtime')
    assert.equal(taskRecoveryAction(failure('INFERENCE_PARAMS_INVALID', workflow)), null)
  })

  it('lists each action once for a batch, keeping the first task that needs it', () => {
    const first = failure('INPUT_NOT_FOUND')
    const second = failure('INPUT_MEDIA_UNSUPPORTED')
    const crash = failure('WORKER_NATIVE_CRASH')
    assert.deepEqual(taskRecoveryActions([first, crash, second]), [
      { action: 'input', task: first },
      { action: 'runtime', task: crash },
    ])
  })
})

describe('failed task retry selection', () => {
  it('skips failures already covered by a queued or successful retry chain', () => {
    const all = [
      task('a', 'failed'), task('a1', 'failed', 'a'), task('a2', 'done', 'a1'),
      task('b', 'failed'), task('b1', 'queued', 'b'),
      task('c', 'failed'), task('c1', 'cancelled', 'c'),
      task('d', 'done'),
    ]
    assert.deepEqual(failedTasksToRetry(all, all).map(item => item.id), ['c'])
  })

  it('does not retry a failure whose input is already being retried from another batch', () => {
    // a1 failed in its own batch; the user then retried the original a again from the older batch.
    const all = [task('a', 'failed'), task('a1', 'failed', 'a'), task('a1b', 'queued', 'a')]
    assert.deepEqual(failedTasksToRetry([all[1]!], all), [])
    assert.deepEqual(failedTasksToRetry([all[0]!], all), [])
  })

  it('ignores duplicate candidates and stops on cyclic retry links', () => {
    const all = [task('x', 'failed', 'y'), task('y', 'queued', 'x')]
    assert.deepEqual(failedTasksToRetry([all[0], all[0]], all), [])
    const failed = task('z', 'failed')
    assert.deepEqual(failedTasksToRetry([failed, failed], [failed]), [failed])
  })
})
