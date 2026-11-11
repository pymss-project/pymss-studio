import assert from 'node:assert/strict'
import test from 'node:test'
import { createDescriptionDraft } from '../src/workflows/descriptionDraft.ts'

function draft(initial = 'Original', write?: (text: string) => Promise<string>) {
  let saved = initial
  const writes: string[] = []
  const model = createDescriptionDraft({ read: () => saved, limit: 4000, write: async text => {
    writes.push(text)
    saved = await (write ? write(text) : Promise.resolve(text.trim()))
    return saved
  } })
  return { model, writes, external(text: string) { saved = text; model.synchronize() } }
}

test('description typing is debounced and closing flushes the latest text', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const { model, writes } = draft()
  model.update('One'); t.mock.timers.tick(500); model.update('Two')
  t.mock.timers.tick(799); assert.deepEqual(writes, [])
  t.mock.timers.tick(1); await model.flush()
  assert.deepEqual(writes, ['Two'])
  model.update('Latest'); await model.flush(); t.mock.timers.tick(1000)
  assert.deepEqual(writes, ['Two', 'Latest'])
  assert.equal(model.dirty(), false)
})

test('in-flight writes drain newer input in order and never replace the current draft with an older response', async () => {
  const responses: Array<(text: string) => void> = []
  const { model, writes } = draft('Original', () => new Promise(resolve => { responses.push(resolve) }))
  model.update('First'); const first = model.flush()
  model.update('Second'); const second = model.flush()
  assert.equal(first, second)
  assert.deepEqual(writes, ['First'])
  responses.shift()!('First')
  for (let i = 0; i < 5; i++) await Promise.resolve()
  assert.equal(model.state.text, 'Second')
  assert.deepEqual(writes, ['First', 'Second'])
  responses.shift()!('Second'); assert.equal(await first, true)
  assert.equal(model.state.savedText, 'Second')
  assert.equal(model.dirty(), false)
})

test('a failed save retains the draft and can be retried without losing input', async () => {
  let fail = true
  const { model } = draft('Original', async text => { if (fail) throw new Error('Write failed'); return text.trim() })
  model.update('Unsaved draft'); assert.equal(await model.flush(), false)
  assert.equal(model.state.text, 'Unsaved draft')
  assert.equal(model.state.savedText, 'Original')
  assert.equal(model.state.error, 'Write failed')
  assert.equal(model.state.saving, false)
  fail = false; assert.equal(await model.flush(), true)
  assert.equal(model.state.error, '')
  assert.equal(model.dirty(), false)
})

test('reverting text during a failed in-flight save still confirms the final choice on retry', async () => {
  let reject!: (error: Error) => void
  let fail = true
  const { model, writes } = draft('Original', text => fail ? new Promise((_resolve, failWrite) => { reject = failWrite }) : Promise.resolve(text))
  model.update('In flight'); const pending = model.flush()
  model.update('Original'); reject(new Error('Write failed'))
  assert.equal(await pending, false)
  assert.equal(model.state.text, 'Original')
  assert.equal(model.dirty(), true)
  fail = false; assert.equal(await model.flush(), true)
  assert.deepEqual(writes, ['In flight', 'Original'])
  assert.equal(model.dirty(), false)
})

test('clearing a description saves an explicit empty string and whitespace-only changes do not write', async () => {
  const { model, writes } = draft()
  model.update('Original  '); assert.equal(await model.flush(), true)
  assert.deepEqual(writes, [])
  model.update(''); assert.equal(await model.flush(), true)
  assert.deepEqual(writes, [''])
  assert.equal(model.state.savedText, '')
})

test('oversized edits are retained without a write, while untouched oversized legacy text remains readable', async () => {
  const { model, writes } = draft()
  model.update('x'.repeat(4001)); assert.equal(await model.flush(), false)
  assert.equal(model.state.text.length, 4001)
  assert.deepEqual(writes, [])
  model.update('x'.repeat(4000)); assert.equal(await model.flush(), true)
  assert.equal(writes[0]!.length, 4000)
  const legacy = draft('x'.repeat(5000))
  assert.equal(await legacy.model.flush(), true)
  assert.deepEqual(legacy.writes, [])
  legacy.model.update('y'.repeat(5001)); assert.equal(await legacy.model.flush(), false)
  assert.equal(legacy.model.state.text.length, 5001)
})

test('external saves refresh a pristine editor and never overwrite an unsaved draft', async () => {
  const { model, external, writes } = draft()
  external('Published'); assert.equal(model.state.text, 'Published')
  model.update('My draft'); external('Published later')
  assert.equal(model.state.text, 'My draft')
  await model.flush(); assert.deepEqual(writes, ['My draft'])
  model.update('Discarded'); model.discard()
  assert.equal(model.state.text, 'My draft')
  assert.equal(model.dirty(), false)
})

test('separate workflow drafts retain their own save destinations during asynchronous switches', async () => {
  const first = draft('First'), second = draft('Second')
  first.model.update('First edited'); second.model.update('Second edited')
  await Promise.all([first.model.flush(), second.model.flush()])
  assert.deepEqual(first.writes, ['First edited'])
  assert.deepEqual(second.writes, ['Second edited'])
})
