import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

const source = ts.createSourceFile('windowState.ts', readFileSync(new URL('../src/features/diagnostics/windowState.ts', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true)
const declaration = source.statements.find(node => ts.isFunctionDeclaration(node) && node.name.text === 'useDiagnosticWindowState')
const code = ts.transpileModule(declaration.getText(source).replace(/^export /, ''), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
function environment(subscribe) {
  const lifecycle = {}, calls = [], warnings = [], topics = []
  let callback
  const create = vm.runInNewContext(`${code}\nuseDiagnosticWindowState`, {
    Error,
    onMounted: fn => { lifecycle.mount = fn }, onBeforeUnmount: fn => { lifecycle.unmount = fn },
    isTauriRuntime: () => true,
    listen: async (event, handler) => { topics.push(event); callback = handler; return subscribe ? subscribe() : () => calls.push('unlisten') },
    invoke: async (command, args) => { calls.push({ command, args }) },
    log: { warn: (...args) => warnings.push(args) },
  })
  return { create, lifecycle, calls, warnings, topics, request: id => callback({ payload: { requestId: id } }) }
}

test('renderer providers use their own topic without replacing editor observations', async () => {
  const env = environment()
  env.create('renderer', () => ({ rendering: { themeClass: 'dark' } }))
  await env.lifecycle.mount()
  assert.deepEqual(env.topics, ['pymss://renderer-state-request'])
  env.request('render-a')
  assert.equal(env.calls[0].args.snapshot.kind, 'renderer')
})

test('providers read current view state only when requested and stop after unmount', async () => {
  const env = environment()
  let value = 'first', reads = 0
  env.create('editor', () => { reads++; return { projectId: value } })
  await env.lifecycle.mount()
  assert.equal(reads, 0)
  value = 'second'
  env.request('request-a')
  assert.equal(env.calls[0].args.snapshot.projectId, 'second')
  assert.equal(env.calls[0].args.snapshot.kind, 'editor')
  env.lifecycle.unmount()
  env.request('request-b')
  assert.equal(reads, 1)
  assert.equal(env.calls.at(-1), 'unlisten')
})

test('a late listener registration is released after a view has closed', async () => {
  let resolve, removed = 0
  const subscription = new Promise(done => { resolve = done })
  const env = environment(() => subscription)
  env.create('workflow', () => ({}))
  const mounting = env.lifecycle.mount()
  env.lifecycle.unmount()
  resolve(() => { removed++ })
  await mounting
  assert.equal(removed, 1)
})

test('snapshot collection failure is reported without throwing into the editor', async () => {
  const env = environment()
  env.create('workflow', () => { throw new Error('Draft unavailable') })
  await env.lifecycle.mount()
  assert.doesNotThrow(() => env.request('a'))
  assert.equal(env.calls[0].args.snapshot.error, 'Draft unavailable')
})

test('listener failure does not interrupt the business view', async () => {
  const env = environment(() => Promise.reject(new Error('Event bridge unavailable')))
  env.create('editor', () => ({}))
  await assert.doesNotReject(env.lifecycle.mount())
  assert.equal(env.warnings.length, 1)
})
