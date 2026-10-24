import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const vite = await createServer({
  configFile: false,
  server: { watch: null, middlewareMode: true, hmr: false },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
  plugins: [{
    name: 'stub-marketplace-transport',
    enforce: 'pre',
    transform(code, id) {
      if (!id.replaceAll('\\', '/').endsWith('/src/features/plugin-marketplace/composable.ts')) return null
      return code
        .replace("import { invoke } from '@tauri-apps/api/core'", 'const invoke = (...args) => globalThis.__pluginInvoke(...args)')
        .replace("import { listen, type UnlistenFn } from '@tauri-apps/api/event'", 'type UnlistenFn = () => void; const listen = (...args) => globalThis.__pluginListen(...args)')
    },
  }],
})
after(() => vite.close())
const { usePluginMarketplace, pluginRevision } = await vite.ssrLoadModule('/src/features/plugin-marketplace/composable.ts')
const market = usePluginMarketplace()
let callback
let requests = []
const empty = (identity = 'cpu-runtime') => ({
  catalog: [{ name: 'hello', url: 'https://example.com/hello', description: 'Test', tags: ['audio'] }],
  catalogError: null, installed: [], pluginsDir: '/plugins',
  runtime: { identity, pythonPath: `/runtime/${identity}/python` },
})
const event = (type, payload = {}, id = requests.at(-1)?.payload.taskId) => callback({ payload: { type, taskId: id, payload } })
globalThis.__pluginListen = async (_name, listener) => { callback = listener; return () => {} }

async function reset() {
  requests = []
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    return command === 'plugin_marketplace' ? empty() : { started: true }
  }
  await market.refresh()
}
const nextTurn = () => new Promise(resolve => setImmediate(resolve))

// The tests drive the actual controller through the same command/event boundary as Tauri.
test('a terminal event delivered before invoke returns completes installation', async () => {
  await reset()
  const before = pluginRevision.value
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    if (command === 'plugin_marketplace') return empty()
    event('plugin_manage_result', { ...empty(), updates: {} }, args.payload.taskId)
    return { started: true }
  }
  await market.manage('install', { name: 'hello' })
  assert.equal(market.busy.value, false)
  assert.equal(pluginRevision.value, before + 1)
})

test('a server-assigned id is accepted even when its event arrives before acknowledgement', async () => {
  await reset()
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    if (command === 'plugin_marketplace') return empty()
    event('plugin_manage_result', { ...empty(), updates: {} }, 'plugin_manage_server')
    return { started: true, taskId: 'plugin_manage_server' }
  }
  await market.manage('install', { name: 'hello' })
  assert.equal(market.busy.value, false)
})

test('chatty pre-ack logs cannot evict a terminal event', { timeout: 2000 }, async () => {
  await reset()
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    if (command === 'plugin_marketplace') return empty()
    event('plugin_manage_result', { ...empty(), updates: {} }, 'plugin_manage_chatty')
    for (let i = 0; i < 300; i++) event('plugin_log', { message: `line ${i}` }, 'plugin_manage_chatty')
    return { started: true, taskId: 'plugin_manage_chatty' }
  }
  await market.manage('install', { name: 'hello' })
  assert.equal(market.busy.value, false)
})

test('unrelated events cannot complete an active plugin task', async () => {
  await reset()
  const completion = market.manage('install', { name: 'hello' })
  await nextTurn()
  event('plugin_manage_result', { ...empty(), updates: {} }, 'another-task')
  assert.equal(market.busy.value, true)
  event('plugin_stage', { message: 'Verifying' })
  assert.deepEqual(market.logs.value, ['Verifying'])
  event('plugin_manage_result', { ...empty(), updates: {} })
  await completion
  assert.equal(market.busy.value, false)
})

test('failed starts release busy state and keep the error after inventory refresh', async () => {
  await reset()
  globalThis.__pluginInvoke = async command => {
    if (command === 'manage_plugin') throw new Error('RUNTIME_BUSY')
    return empty()
  }
  await assert.rejects(market.manage('install', { name: 'hello' }), /RUNTIME_BUSY/)
  assert.equal(market.busy.value, false)
  assert.match(market.error.value, /RUNTIME_BUSY/)
  assert.ok(market.logs.value.some(line => line.includes('RUNTIME_BUSY')))
})

test('worker failure preserves old inventory and rejects the pending action', async () => {
  await reset()
  const completion = market.manage('update', { name: 'hello' })
  const rejected = assert.rejects(completion, /dependency conflict/)
  await nextTurn()
  event('error', { message: 'dependency conflict' })
  await rejected
  assert.equal(market.busy.value, false)
  assert.ok(market.snapshot.value)
})

test('cancel does not claim completion when a terminal event is already queued', async () => {
  await reset()
  const completion = market.manage('install', { name: 'hello' })
  const rejected = assert.rejects(completion, /cancelled/)
  await nextTurn()
  globalThis.__pluginInvoke = async command => command === 'cancel_task' ? false : empty()
  await market.cancel()
  assert.equal(market.busy.value, true)
  event('task_cancelled')
  await rejected
  assert.equal(market.busy.value, false)
})

test('out-of-order refresh responses cannot replace a newer runtime', async () => {
  await reset()
  const responses = []
  globalThis.__pluginInvoke = () => new Promise(resolve => responses.push(resolve))
  const first = market.refresh()
  const second = market.refresh()
  responses[1](empty('new-runtime'))
  await second
  responses[0](empty('old-runtime'))
  await first
  assert.equal(market.snapshot.value.runtime.identity, 'new-runtime')
})

test('navigation keeps the task alive and records only a bounded activity log', async () => {
  await reset()
  const completion = market.manage('check_updates')
  await nextTurn()
  assert.equal(usePluginMarketplace().busy.value, true)
  for (let i = 0; i < 180; i++) event('plugin_log', { message: `line ${i}` })
  assert.equal(market.logs.value.length, 160)
  const id = requests.at(-1).payload.taskId
  event('plugin_manage_result', { ...empty(), updates: { hello: { up_to_date: false, remote_version: '2.0.0' } } }, id)
  await completion
  assert.equal(market.updates.value.hello.remote_version, '2.0.0')
})
