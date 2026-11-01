import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createPinia, disposePinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { setupLitegraphEnvDom } from './_litegraphEnv.mjs'

await setupLitegraphEnvDom()

const vite = await createServer({
  configFile: false, server: { middlewareMode: true, hmr: false, ws: false, watch: null },
  appType: 'custom', optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
})
const { useWorkflowNodeStore } = await vite.ssrLoadModule('/src/stores/workflowNodes.ts')
const { pluginRevision, usePluginMarketplace } = await vite.ssrLoadModule('/src/features/plugin-marketplace/composable.ts')
const originalWindow = globalThis.window
after(async () => { globalThis.window = originalWindow; await vite.close() })

test('inventory handoff gives default-tool preparation priority over new node queries', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const market = usePluginMarketplace()
  const commands = []
  globalThis.window = { __TAURI_INTERNALS__: { invoke: async command => { commands.push(command); return { nodes: [] } } } }
  const nodes = useWorkflowNodeStore()
  try {
    market.loading.value = true
    pluginRevision.value++
    await nextTick()
    assert.deepEqual(commands, [])
    const ready = nodes.load()
    assert.deepEqual(commands, [])
    market.loading.value = false
    market.activities.value = [{ action: 'prepare_preinstalled', status: 'running' }]
    await nextTick()
    assert.deepEqual(commands, [], 'metadata readers must not take a runtime lease before the preparation writer')
    market.activities.value = []
    await ready
    assert.deepEqual(commands, ['list_nodes'])
  } finally {
    market.loading.value = false
    market.activities.value = []
    disposePinia(pinia)
  }
})

test('first inventory prepares defaults before a live metadata reader reserves the runtime', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const market = usePluginMarketplace()
  const commands = []
  const callbacks = new Map()
  const listeners = new Map()
  let sequence = 0
  let readers = 0
  const inventory = { installed: [], pluginsDir: '/plugins', runtime: { identity: 'fresh', pythonPath: '/python' },
    preinstalledPending: ['pymss.convert'], catalog: [], catalogError: null }
  globalThis.window = originalWindow
  window.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener: (_event, id) => listeners.delete(id) }
  window.__TAURI_INTERNALS__ = {
    transformCallback(callback) { callbacks.set(++sequence, callback); return sequence },
    async invoke(command, args) {
      commands.push(command)
      if (command === 'load_app_store') return { pluginDefaultsPolicy: 'auto' }
      if (command === 'get_app_paths') return { dataRoot: '/test', settingsDir: '/test/settings', outputsDir: '/test/outputs', modelsDir: '/test/models', logsDir: '/test/logs', tempDir: '/test/temp', editorProjectsDir: '/test/projects' }
      if (command === 'plugin_marketplace') return structuredClone(inventory)
      if (command === 'plugin:event|listen') { listeners.set(++sequence, args.handler); return sequence }
      if (command === 'manage_plugin') {
        if (readers) throw new Error('worker error: RUNTIME_BUSY: The runtime is in use')
        inventory.preinstalledPending = []
        queueMicrotask(() => {
          for (const [id, handler] of listeners) callbacks.get(handler)({ event: 'pymss://worker-event', id,
            payload: { type: 'plugin_manage_result', taskId: args.payload.taskId, payload: structuredClone(inventory) } })
        })
        return { taskId: args.payload.taskId }
      }
      if (command === 'list_nodes') {
        readers++
        return new Promise(resolve => setTimeout(() => { readers--; resolve({ nodes: [] }) }, 20))
      }
      return null
    },
  }
  const nodes = useWorkflowNodeStore()
  try {
    await market.ensureInventory()
    await nodes.load()
    assert.equal(market.error.value, '')
    assert.deepEqual(market.snapshot.value.preinstalledPending, [])
    assert.ok(commands.indexOf('manage_plugin') >= 0)
    assert.ok(commands.indexOf('manage_plugin') < commands.indexOf('list_nodes'))
    assert.equal(commands.filter(command => command === 'manage_plugin').length, 1)
  } finally {
    disposePinia(pinia)
    delete window.__TAURI_INTERNALS__
    delete window.__TAURI_EVENT_PLUGIN_INTERNALS__
  }
})

test('disposing a metadata store cancels its inventory wait without leaving a reader', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const market = usePluginMarketplace()
  const commands = []
  globalThis.window = { __TAURI_INTERNALS__: { invoke: async command => { commands.push(command); return { nodes: [] } } } }
  const nodes = useWorkflowNodeStore()
  market.loading.value = true
  const pending = nodes.load()
  disposePinia(pinia)
  await assert.rejects(pending, /store is closed/)
  market.loading.value = false
  await nextTick()
  assert.deepEqual(commands, [])
})

test('live node metadata is shared, invalidated on plugin changes and never inferred from STRING', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const responses = []
  globalThis.window = { __TAURI_INTERNALS__: { invoke: () => new Promise((resolve, reject) => responses.push({ resolve, reject })) } }
  const nodes = useWorkflowNodeStore()
  try {
    const first = nodes.load()
    const concurrent = nodes.load()
    assert.equal(responses.length, 1)
    responses.shift().resolve({ nodes: [
      { type: 'file_writer', registered: false, savesOutput: true },
      { type: 'file_writer', registered: true, savesOutput: true },
      { type: 'value', registered: true, outputs: [{ type: 'STRING' }] },
      { type: 'failed_writer', registered: false, savesOutput: true },
    ] })
    await Promise.all([first, concurrent])
    assert.deepEqual([...nodes.saveNodeTypes], ['file_writer'])
    await nodes.load()
    assert.equal(responses.length, 0)
    pluginRevision.value++
    assert.equal(nodes.saveNodeTypes.size, 0)
    const disabled = nodes.load()
    responses.shift().resolve({ nodes: [{ type: 'file_writer', registered: false, savesOutput: false }] })
    await disabled
    assert.equal(nodes.saveNodeTypes.size, 0)

    pluginRevision.value++
    const stale = nodes.load()
    const old = responses.shift()
    pluginRevision.value++
    const current = nodes.load()
    old.resolve({ nodes: [{ type: 'stale_writer', registered: true, savesOutput: true }] })
    responses.shift().resolve({ nodes: [{ type: 'new_writer', registered: true, savesOutput: true }] })
    await Promise.all([stale, current])
    assert.deepEqual([...nodes.saveNodeTypes], ['new_writer'])

    pluginRevision.value++
    const staleFailure = nodes.load()
    const oldFailure = responses.shift()
    pluginRevision.value++
    const failed = nodes.load()
    oldFailure.resolve({ nodes: [] })
    responses.shift().reject(new Error('runtime unavailable'))
    const failures = await Promise.allSettled([staleFailure, failed])
    assert.ok(failures.every(result => result.status === 'rejected' && /runtime unavailable/.test(result.reason.message)))
    assert.equal(responses.length, 0)
    assert.equal(nodes.saveNodeTypes.size, 0)
    const retry = nodes.load()
    responses.shift().resolve({ nodes: [] })
    await retry
    assert.equal(nodes.error, '')
    assert.equal(nodes.loading, false)
  } finally { disposePinia(pinia) }
})
