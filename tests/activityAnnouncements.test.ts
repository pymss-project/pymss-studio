import assert from 'node:assert/strict'
import test from 'node:test'
import { activityChanges, downloadAnnouncementState, taskAnnouncementState } from '../src/utils/activityAnnouncements.ts'

test('preparation and inference stages announce a single start, then a terminal result', () => {
  let previous = [{ id: 'one', name: 'audio.wav', state: taskAnnouncementState('queued') }]
  const announced = []
  for (const status of ['preparing', 'validating_input', 'downloading_model', 'loading_model', 'separating', 'writing_output', 'done', 'done']) {
    const current = [{ ...previous[0]!, state: taskAnnouncementState(status) }]
    announced.push(...activityChanges(previous, current, 'task'))
    previous = current
  }
  assert.deepEqual(announced.map(item => item.state), ['running', 'done'])
})

test('downloads announce pause, resume and errors without treating percentage updates as events', () => {
  let previous = [{ id: 'download', name: 'model', state: downloadAnnouncementState('idle') }]
  const announced = []
  for (const status of ['preparing', 'downloading', 'downloading', 'paused', 'downloading', 'error', 'error']) {
    const current = [{ ...previous[0]!, state: downloadAnnouncementState(status) }]
    announced.push(...activityChanges(previous, current, 'download'))
    previous = current
  }
  assert.deepEqual(announced.map(item => item.state), ['running', 'paused', 'running', 'failed'])
})

test('new fast failures and cancellations are reported, removed records are silent', () => {
  assert.equal(activityChanges([], [{ id: 'fast', name: 'a.wav', state: 'failed' }], 'task')[0]?.state, 'failed')
  assert.equal(activityChanges([{ id: 'cancel', name: 'b.wav', state: 'running' }], [{ id: 'cancel', name: 'b.wav', state: 'cancelled' }], 'task')[0]?.state, 'cancelled')
  assert.deepEqual(activityChanges([{ id: 'removed', name: 'c.wav', state: 'done' }], [], 'task'), [])
})

test('unknown states stay silent rather than being announced as running', () => {
  assert.equal(taskAnnouncementState('unrecognized'), null)
  assert.equal(downloadAnnouncementState('unrecognized'), null)
})
