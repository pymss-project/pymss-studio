import assert from 'node:assert/strict'
import test from 'node:test'

import { reconcileMountedTools, type MountedTool } from '../src/features/plugin-marketplace/toolInstances.ts'

type Entry = { name: string }
const tool = (id: string, usedAt: number, busy = false): MountedTool<Entry> => ({ id, entry: { name: id }, pluginsRoot: '/plugins', busy, usedAt })
const shown = (id: string) => ({ id, entry: { name: id }, pluginsRoot: '/plugins' })
const ids = (instances: MountedTool<Entry>[]) => instances.map(instance => instance.id)

test('switching mounts the shown tool and keeps recent ones', () => {
  const next = reconcileMountedTools([tool('a', 1), tool('b', 2)], new Set(['a', 'b', 'c']), shown('c'), 4, 3)
  assert.deepEqual(ids(next), ['a', 'b', 'c'])
  assert.equal(next.at(-1)?.usedAt, 3)
})

test('returning to a mounted tool reuses it and refreshes its use time', () => {
  const next = reconcileMountedTools([tool('a', 1), tool('b', 2)], new Set(['a', 'b']), shown('a'), 4, 5)
  assert.deepEqual(ids(next), ['a', 'b'])
  assert.equal(next[0]?.usedAt, 5)
})

test('beyond the limit the least recently used idle tool is evicted, never a busy one', () => {
  const instances = [tool('a', 1, true), tool('b', 2), tool('c', 3)]
  const next = reconcileMountedTools(instances, new Set(['a', 'b', 'c', 'd']), shown('d'), 3, 4)
  assert.deepEqual(ids(next), ['a', 'c', 'd'])
})

test('busy tools may exceed the limit rather than be interrupted', () => {
  const instances = [tool('a', 1, true), tool('b', 2, true)]
  const next = reconcileMountedTools(instances, new Set(['a', 'b', 'c']), shown('c'), 2, 3)
  assert.deepEqual(ids(next), ['a', 'b', 'c'])
})

test('an updated or removed tool is replaced when idle and kept while its task runs', () => {
  const instances = [tool('a@1', 1, true), tool('b@1', 2)]
  const next = reconcileMountedTools(instances, new Set(['a@2', 'b@2']), null, 4, 3)
  assert.deepEqual(ids(next), ['a@1'])
})
