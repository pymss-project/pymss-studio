import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createPinia, disposePinia, setActivePinia } from 'pinia'
import { setupLitegraphEnvDom } from './_litegraphEnv.mjs'

await setupLitegraphEnvDom()

const vite = await createServer({
  configFile: false, server: { middlewareMode: true, hmr: false, ws: false, watch: null },
  appType: 'custom', optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
})
const { useWorkflowNodeStore } = await vite.ssrLoadModule('/src/stores/workflowNodes.ts')
const { pluginRevision } = await vite.ssrLoadModule('/src/features/plugin-marketplace/composable.ts')
const originalWindow = globalThis.window
after(async () => { globalThis.window = originalWindow; await vite.close() })

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
