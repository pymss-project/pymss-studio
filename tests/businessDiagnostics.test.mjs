import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { createServer } from 'vite'

const vite = await createServer({ configFile: false,
  server: { watch: null, middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true },
  plugins: [{ name: 'business-diagnostics-transport', enforce: 'pre', transform(code, id) {
    if (!id.replaceAll('\\', '/').endsWith('/src/features/diagnostics/businessDiagnostics.ts')) return null
    return code.replace("import { invoke } from '@tauri-apps/api/core'", 'const invoke = (...args) => globalThis.__businessInvoke(...args)')
  } }],
})
after(() => vite.close())
const { useBusinessDiagnostics, useRendererDiagnostics } = await vite.ssrLoadModule('/src/features/diagnostics/businessDiagnostics.ts')
function deferred() { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }

test('window collection requests observations and never invokes host control operations', async () => {
  const calls = []
  globalThis.__businessInvoke = async (command, args) => { calls.push([command, args]); return { requestId: args.requestId, windows: { missing: ['editor-slow'] } } }
  const diagnostics = useBusinessDiagnostics()
  await diagnostics.refresh()
  assert.deepEqual(calls.map(call => call[0]), ['begin_business_diagnostics', 'get_business_diagnostics'])
  assert.equal(calls[0][1].requestId, calls[1][1].requestId)
  assert.equal(calls[0][1].scope, 'business')
  assert.deepEqual(diagnostics.snapshot.value.windows.missing, ['editor-slow'])
})

test('rendering requests use a separate provider topic and never query the native audio host', async () => {
  const calls = []
  globalThis.__businessInvoke = async (command, args) => { calls.push([command, args]); return { webviewVersion: '123.4', windows: { missing: [] } } }
  const diagnostics = useRendererDiagnostics()
  await diagnostics.refresh()
  assert.deepEqual(calls.map(call => call[0]), ['begin_business_diagnostics', 'get_renderer_diagnostics'])
  assert.equal(calls[0][1].scope, 'rendering')
  assert.equal(diagnostics.snapshot.value.webviewVersion, '123.4')
})

test('invalidation prevents an old collection from performing another query', async () => {
  const first = deferred()
  const calls = []
  globalThis.__businessInvoke = async command => { calls.push(command); return first.promise }
  const diagnostics = useBusinessDiagnostics()
  const pending = diagnostics.refresh()
  diagnostics.invalidate()
  first.resolve({})
  await pending
  assert.deepEqual(calls, ['begin_business_diagnostics'])
  assert.equal(diagnostics.snapshot.value, null)
})

test('files are checked separately and outdated results are discarded', async () => {
  const result = deferred()
  const calls = []
  globalThis.__businessInvoke = (command, args) => { calls.push([command, args]); return result.promise }
  const diagnostics = useBusinessDiagnostics()
  assert.equal(diagnostics.outputs.value, null)
  const pending = diagnostics.inspectOutputs(['/recorded/result'])
  diagnostics.invalidateOutputs()
  result.resolve({ files: [{ path: '/recorded/result', state: 'present' }] })
  await pending
  assert.equal(calls[0][0], 'inspect_diagnostic_outputs')
  assert.equal(diagnostics.outputs.value, null)
})

test('failed checks clear healthy results and preserve their cause', async () => {
  globalThis.__businessInvoke = async () => ({ files: [{ path: '/output', state: 'present' }] })
  const diagnostics = useBusinessDiagnostics()
  await diagnostics.inspectOutputs(['/output'])
  globalThis.__businessInvoke = async () => { throw new Error('Permission denied') }
  await diagnostics.inspectOutputs(['/output'])
  assert.equal(diagnostics.outputs.value, null)
  assert.match(diagnostics.outputsError.value, /Permission denied/)
})
