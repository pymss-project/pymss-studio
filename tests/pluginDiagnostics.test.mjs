import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { createServer } from 'vite'

const vite = await createServer({
  configFile: false, server: { watch: null, middlewareMode: true, hmr: false }, appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  plugins: [{ name: 'diagnostics-transport', enforce: 'pre', transform(code, id) {
    if (!id.replaceAll('\\', '/').endsWith('/src/features/diagnostics/pluginDiagnostics.ts')) return null
    return code.replace("import { invoke } from '@tauri-apps/api/core'", 'const invoke = (...args) => globalThis.__diagnosticInvoke(...args)')
  } }],
})
after(() => vite.close())
const { usePluginDiagnostics } = await vite.ssrLoadModule('/src/features/diagnostics/pluginDiagnostics.ts')
const observed = id => ({ runtime: { identity: id }, plugins: [], verification: { performed: false, error: null } })
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }

test('default refresh is read-only and loading verification requires an explicit request', async () => {
  const calls = []
  globalThis.__diagnosticInvoke = async (command, args) => { calls.push([command, args.payload]); return observed('cpu') }
  const diagnostics = usePluginDiagnostics()
  await diagnostics.refresh()
  await diagnostics.refresh(true)
  assert.deepEqual(calls, [['plugin_diagnostics', { verify: false }], ['plugin_diagnostics', { verify: true }]])
})

test('concurrent refreshes share the pending query', async () => {
  const result = deferred()
  let calls = 0
  globalThis.__diagnosticInvoke = () => { calls++; return result.promise }
  const diagnostics = usePluginDiagnostics()
  const first = diagnostics.refresh()
  const second = diagnostics.refresh()
  assert.equal(first, second)
  result.resolve(observed('cpu'))
  await Promise.all([first, second])
  assert.equal(calls, 1)
  assert.equal(diagnostics.loading.value, false)
})

test('an old environment response cannot replace a newly selected runtime', async () => {
  const old = deferred(), current = deferred()
  let calls = 0
  globalThis.__diagnosticInvoke = () => (++calls === 1 ? old.promise : current.promise)
  const diagnostics = usePluginDiagnostics()
  const first = diagnostics.refresh()
  diagnostics.invalidate()
  const second = diagnostics.refresh()
  current.resolve(observed('cuda'))
  await second
  old.resolve(observed('cpu'))
  await first
  assert.equal(diagnostics.snapshot.value.runtime.identity, 'cuda')
})

test('query failure clears previously healthy observations', async () => {
  const diagnostics = usePluginDiagnostics()
  globalThis.__diagnosticInvoke = async () => observed('cpu')
  await diagnostics.refresh()
  globalThis.__diagnosticInvoke = async () => { throw new Error('Interpreter unavailable') }
  await diagnostics.refresh()
  assert.equal(diagnostics.snapshot.value, null)
  assert.match(diagnostics.error.value, /Interpreter unavailable/)
  assert.equal(diagnostics.verifying.value, false)
})
