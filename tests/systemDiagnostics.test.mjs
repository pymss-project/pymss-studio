import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const vite = await createServer({ configFile: false, appType: 'custom',
  server: { watch: null, middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
  plugins: [{ name: 'system-diagnostic-boundaries', enforce: 'pre', transform(code, id) {
    const path = id.replaceAll('\\', '/')
    if (/\/src\/features\/diagnostics\/(networkDiagnostics|storageDiagnostics)\.ts$/.test(path)) {
      return code.replace("import { invoke } from '@tauri-apps/api/core'", 'const invoke = (...args) => globalThis.__systemInvoke(...args)')
    }
    if (path.endsWith('/src/utils/logger.ts')) return 'export const redactDiagnosticText = value => String(value).replace(/https?:\\/\\/[^/\\s]*@/g, "https://[redacted]@")'
  } }],
})
after(() => vite.close())
const { useNetworkDiagnostics } = await vite.ssrLoadModule('/src/features/diagnostics/networkDiagnostics.ts')
const { useStorageDiagnostics } = await vite.ssrLoadModule('/src/features/diagnostics/storageDiagnostics.ts')
const config = { modelSource: 'hf-mirror', pluginSource: 'auto', proxyMode: 'custom', updateSource: 'auto', channel: 'stable', locale: 'zh-CN', endpointOverride: 'https://alice:secret@private.test/latest' }
const defer = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
function transport(calls) { return async (command, args) => {
  calls.push([command, args])
  if (command === 'network_diagnostics') return { proxyMode: 'custom', checks: [{ id: 'models', status: 'ok', source: 'hf-mirror' }, { id: 'pypi', status: 'failed', httpStatus: 503 }] }
  if (command === 'plugin_marketplace') return { catalog: [{}], catalogSource: { supported: true, provider: 'gitcode', preference: 'auto', gitAvailable: true } }
  if (command === 'check_managed_update') return null
  throw new Error('Unexpected command ' + command)
} }

test('network checks are manual, flush proxy state first and reuse metadata-only commands', async () => {
  const calls = []
  globalThis.__systemInvoke = transport(calls)
  const diagnostics = useNetworkDiagnostics()
  assert.equal(diagnostics.snapshot.value, null)
  assert.equal(calls.length, 0)
  await diagnostics.check(config, async () => { calls.push(['proxy-sync']) })
  assert.deepEqual(calls.map(call => call[0]), ['proxy-sync', 'network_diagnostics', 'plugin_marketplace', 'check_managed_update'])
  assert.deepEqual(calls[2][1], { payload: { catalogOnly: true, includeCatalog: true } })
  assert.equal(calls[3][1].endpointOverride, config.endpointOverride)
  const snapshot = diagnostics.snapshot.value
  assert.equal(snapshot.configuration.customUpdateEndpoint, true)
  assert.equal(snapshot.observedProxyMode, 'custom')
  assert.equal(snapshot.checks[2].source, 'gitcode')
  assert.equal(snapshot.checks[2].sourcePreference, 'auto')
  assert.equal(snapshot.checks[3].outcome, 'no_update')
  assert.equal(snapshot.checks[1].status, 'failed')
  assert.ok(!JSON.stringify(snapshot).includes('secret'))
})

test('target failures remain independent and errors hide URL credentials', async () => {
  const calls = []
  const normal = transport(calls)
  globalThis.__systemInvoke = async (command, args) => {
    if (command === 'plugin_marketplace') throw new Error('Failed https://alice:secret@proxy.test')
    if (command === 'check_managed_update') return { source: 'modelscope' }
    return normal(command, args)
  }
  const diagnostics = useNetworkDiagnostics()
  await diagnostics.check(config, async () => {})
  assert.deepEqual(diagnostics.snapshot.value.checks.map(row => row.status), ['ok', 'failed', 'failed', 'ok'])
  assert.equal(diagnostics.snapshot.value.checks[3].source, 'modelscope')
  assert.equal(diagnostics.snapshot.value.checks[3].outcome, 'available')
  assert.ok(!JSON.stringify(diagnostics.snapshot.value).includes('secret'))
})

test('configuration invalidation drops late observations and prevents overlapping requests', async () => {
  const proxy = defer(), calls = []
  globalThis.__systemInvoke = transport(calls)
  const diagnostics = useNetworkDiagnostics()
  const pending = diagnostics.check(config, () => proxy.promise)
  diagnostics.invalidate()
  await diagnostics.check(config, async () => {})
  proxy.resolve()
  await pending
  assert.equal(diagnostics.snapshot.value, null)
  assert.equal(diagnostics.busy.value, false)
  assert.equal(calls.length, 0)
})

test('proxy synchronization failure never starts network clients', async () => {
  const calls = []
  globalThis.__systemInvoke = transport(calls)
  const diagnostics = useNetworkDiagnostics()
  await diagnostics.check(config, async () => { throw new Error('Proxy unavailable') })
  assert.equal(calls.length, 0)
  assert.ok(diagnostics.snapshot.value.checks.every(row => row.status === 'failed'))
})

test('unsupported catalog APIs are distinct from connectivity failures', async () => {
  const normal = transport([])
  globalThis.__systemInvoke = (command, args) => command === 'plugin_marketplace'
    ? Promise.resolve({ catalogSource: { supported: false }, catalogError: 'Core does not support catalog selection' }) : normal(command, args)
  const diagnostics = useNetworkDiagnostics()
  await diagnostics.check(config, async () => {})
  assert.equal(diagnostics.snapshot.value.checks[2].status, 'unsupported')
})

test('unsupported update distributions are not reported as connectivity failures', async () => {
  const normal = transport([])
  globalThis.__systemInvoke = (command, args) => command === 'check_managed_update'
    ? Promise.reject('worker error: UPDATE_DISTRIBUTION_UNSUPPORTED: No supported update distribution marker') : normal(command, args)
  const diagnostics = useNetworkDiagnostics()
  await diagnostics.check(config, async () => {})
  assert.equal(diagnostics.snapshot.value.checks[3].status, 'unsupported')
  assert.match(diagnostics.snapshot.value.checks[3].error, /UPDATE_DISTRIBUTION_UNSUPPORTED/)
})

test('storage reads do not request writes unless explicitly selected, and late results are discarded', async () => {
  const calls = []
  globalThis.__systemInvoke = async (command, args) => { calls.push([command, args]); return { writeProbe: args.writeProbe, directories: [] } }
  const diagnostics = useStorageDiagnostics()
  assert.equal(calls.length, 0)
  await diagnostics.inspect([{ id: 'data', path: 'D:/data' }])
  assert.equal(calls[0][1].writeProbe, false)
  await diagnostics.inspect([{ id: 'data', path: 'D:/data' }], true)
  assert.equal(calls[1][1].writeProbe, true)
  const result = defer()
  globalThis.__systemInvoke = () => result.promise
  const pending = diagnostics.inspect([{ id: 'data', path: 'D:/old' }])
  diagnostics.invalidate()
  result.resolve({ directories: [{ id: 'data', path: 'D:/old' }] })
  await pending
  assert.equal(diagnostics.snapshot.value, null)
})
