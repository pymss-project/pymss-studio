import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import test from 'node:test'
import { parse } from 'vue/compiler-sfc'
import { appSelectEntries, appSelectKey, filterAppSelectEntries } from '../src/utils/appSelect.ts'

test('option values keep numeric, string and empty-string identities', () => {
  const values = [0, '0', '', 's:', 'n:0', 48000]
  const entries = appSelectEntries(values.map(value => ({ value, label: String(value) })))
  assert.equal(new Set(entries.map(entry => entry.key)).size, values.length)
  assert.deepEqual(entries.map(entry => entry.value), values)
  assert.notEqual(appSelectKey(0), appSelectKey('0'))
})

test('group flattening retains names, inherited disability and original options', () => {
  const option = { label: 'CPU', value: 'cpu' }
  const entries = appSelectEntries([{ label: 'Backends', disabled: true, children: [option] }, { value: 0 }, { value: 0 }, { value: Infinity }])
  assert.equal(entries.length, 2)
  assert.equal(entries[0]!.raw, option)
  assert.equal(entries[0]!.group, 'Backends')
  assert.equal(entries[0]!.disabled, true)
  assert.equal(entries[1]!.value, 0)
})

test('search trims input, ignores case and does not change the source options', () => {
  const entries = appSelectEntries([{ value: 'a', label: 'Alpha' }, { value: 1, label: '中文 Beta' }])
  assert.deepEqual(filterAppSelectEntries(entries, ' BETA ').map(entry => entry.value), [1])
  assert.equal(filterAppSelectEntries(entries, '中文')[0]!.label, '中文 Beta')
  assert.equal(filterAppSelectEntries(entries, 'missing').length, 0)
  assert.equal(entries.length, 2)
})

test('all application select call sites use the common component and an accessible name', () => {
  let count = 0
  function walkFiles(directory: string) {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = `${directory}/${entry.name}`
      if (entry.isDirectory()) { walkFiles(path); continue }
      if (!path.endsWith('.vue') || path.endsWith('/AppSelect.vue')) continue
      const root = parse(readFileSync(path, 'utf8')).descriptor.template?.ast
      function walk(node: any) {
        if (node.type === 1) {
          assert.ok(!['n-select', 'NSelect', 'NativeSelect', 'select'].includes(node.tag), `${path}: use AppSelect`)
          if (node.tag === 'AppSelect') {
            count++
            assert.ok(node.props.some((prop: any) => ['label', 'aria-label', 'aria-labelledby'].includes(prop.type === 6 ? prop.name : prop.arg?.content)), `${path}:${node.loc.start.line}: missing accessible name`)
          }
        }
        for (const child of node.children || []) walk(child)
      }
      if (root) walk(root)
    }
  }
  walkFiles('src')
  assert.ok(count > 0)
})
