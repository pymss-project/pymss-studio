import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { selectPluginUpdates } from '../src/features/plugin-marketplace/updates.ts'

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
        .replace("import { useSettingsStore } from '@/stores/settings'", 'const useSettingsStore = () => globalThis.__pluginSettings')
    },
  }],
})
after(() => vite.close())
const { usePluginMarketplace, pluginRevision } = await vite.ssrLoadModule('/src/features/plugin-marketplace/composable.ts')
const market = usePluginMarketplace()
let callback
let requests = []
globalThis.__pluginSettings = { pluginDefaultsPolicy: 'review', initialize() {} }
const empty = (identity = 'cpu-runtime') => ({
  catalog: [{ name: 'hello', url: 'https://example.com/hello', description: 'Test', tags: ['audio'] }],
  catalogError: null, installed: [], pluginsDir: '/plugins',
  runtime: { identity, pythonPath: `/runtime/${identity}/python` },
})
const event = (type, payload = {}, id = requests.at(-1)?.payload.taskId) => callback({ payload: { type, taskId: id, payload } })
globalThis.__pluginListen = async (_name, listener) => { callback = listener; return () => {} }

async function reset() {
  requests = []
  market.activities.value = []
  market.logRecords.value = []
  market.updateBatch.value = null
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    return command === 'plugin_marketplace' ? empty() : { started: true }
  }
  await market.refresh()
}
const nextTurn = () => new Promise(resolve => setImmediate(resolve))

const installedResult = name => ({ ...empty(), updates: {}, installed: [{ name, managed: true, loaded: true, operations: [] }] })

test('creating the marketplace does not request inventory before it is used', () => {
  assert.equal(market.inventoryRequested.value, false)
  assert.equal(requests.length, 0)
})

test('inventory usage remains recorded after a failed query and a later runtime can recover', async () => {
  market.snapshot.value = null
  globalThis.__pluginInvoke = async () => { throw new Error('Runtime unavailable') }
  await market.refreshInventory()
  assert.equal(market.inventoryRequested.value, true)
  assert.equal(market.ready.value, false)
  assert.equal(market.busy.value, false)
  assert.equal(market.loading.value, false)
  globalThis.__pluginInvoke = async () => empty('available-runtime')
  await market.reloadRuntimeInventory()
  assert.equal(market.inventoryRequested.value, true)
  assert.equal(market.snapshot.value.runtime.identity, 'available-runtime')
  assert.equal(market.error.value, '')
})

test('an installation result retains each read-only copy with the same plugin ID', async () => {
  await reset()
  const copies = ['/manual/a/analysis', '/manual/b/analysis'].map(path => ({ name: 'analysis', managed: false, loaded: true, path }))
  market.snapshot.value.installed = copies
  const completion = market.manage('install', { name: 'new-plugin' })
  await nextTurn()
  event('plugin_manage_result', { ...installedResult('new-plugin'), installed: [copies[1], ...installedResult('new-plugin').installed] })
  await completion
  assert.deepEqual(market.snapshot.value.installed.filter(plugin => !plugin.managed).map(plugin => plugin.path), copies.map(plugin => plugin.path))
  assert.equal(market.snapshot.value.installed.filter(plugin => plugin.name === 'new-plugin').length, 1)
})

test('diagnostic catalog requests can suppress default preparation', async () => {
  await reset()
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    return { ...empty(), preinstalledPending: ['alpha'] }
  }
  await market.refresh(false, { prepare: false })
  assert.equal(requests.some(request => request.command === 'manage_plugin'), false)
})

test('catalog failures retain the last confirmed source for an explicitly stale display', async () => {
  await reset()
  const source = { supported: true, provider: 'gitcode', preference: 'auto', gitAvailable: true }
  globalThis.__pluginInvoke = async (command, args) => ({
    ...empty(), ...(args?.payload?.catalogOnly ? { catalogSource: source } : {}),
  })
  await market.openMarketplace(true)
  assert.deepEqual(market.snapshot.value.catalogSource, source)
  await market.refreshInventory()
  assert.deepEqual(market.snapshot.value.catalogSource, source)
  globalThis.__pluginInvoke = async (command, args) => {
    if (args?.payload?.catalogOnly) throw new Error('Source unavailable')
    return empty()
  }
  await market.openMarketplace(true)
  assert.deepEqual(market.snapshot.value.catalogSource, source)
  assert.deepEqual(market.snapshot.value.catalog, [])
  assert.match(market.snapshot.value.catalogError, /Source unavailable/)
})

test('a later task keeps failed task details even when its own logs reach the limit', async () => {
  await reset()
  const first = market.manage('repair', { name: 'broken' })
  const rejected = assert.rejects(first, /Missing dependency/)
  await nextTurn()
  const firstId = requests.at(-1).payload.taskId
  event('error', { message: 'Missing dependency' }, firstId)
  await rejected
  const next = market.manage('install', { name: 'new' })
  await nextTurn()
  const nextId = requests.at(-1).payload.taskId
  for (let i = 0; i < 180; i++) event('plugin_log', { message: `next task ${i}` }, nextId)
  try {
    assert.equal(market.activities.value.find(task => task.id === firstId).error, 'Missing dependency')
    assert.ok(market.logRecords.value.some(record => record.context.pluginTaskId === firstId && record.message === 'Missing dependency'))
    assert.equal(market.logRecords.value.filter(record => record.context.pluginTaskId === nextId).length, 160)
  } finally {
    event('plugin_manage_result', installedResult('new'), nextId)
    await next
  }
})

test('retained task history and its logs are pruned together after twenty completed tasks', async () => {
  await reset()
  for (let i = 0; i < 23; i++) {
    const completion = market.manage('install', { name: `plugin-${i}` })
    await nextTurn()
    event('plugin_manage_result', installedResult(`plugin-${i}`))
    await completion
  }
  assert.equal(market.activities.value.length, 20)
  const ids = new Set(market.activities.value.map(task => task.id))
  assert.ok(market.logRecords.value.every(record => ids.has(record.context.pluginTaskId)))
})

test('three installs overlap, queued work starts as a slot frees, and delayed inventories merge', async () => {
  await reset()
  const completions = ['one', 'two', 'three', 'four'].map(name => market.manage('install', { name }))
  await nextTurn()
  const dispatched = () => requests.filter(request => request.command === 'manage_plugin')
  assert.equal(dispatched().length, 3)
  assert.equal(market.activities.value.find(task => task.name === 'four').phase, 'queued')
  event('plugin_manage_result', installedResult('three'), dispatched()[2].payload.taskId)
  await nextTurn()
  assert.equal(dispatched().length, 4)
  event('plugin_manage_result', installedResult('one'), dispatched()[0].payload.taskId)
  event('plugin_manage_result', installedResult('four'), dispatched()[3].payload.taskId)
  event('plugin_manage_result', installedResult('two'), dispatched()[1].payload.taskId)
  await Promise.all(completions)
  assert.deepEqual(market.snapshot.value.installed.map(plugin => plugin.name).sort(), ['four', 'one', 'three', 'two'])
  assert.equal(market.busy.value, false)
})

test('cancelling one installation leaves another task running and independently supervised', async () => {
  await reset()
  const first = market.manage('install', { name: 'one' })
  const rejected = assert.rejects(first, /cancelled/)
  const second = market.manage('install', { name: 'two' })
  await nextTurn()
  const [one, two] = requests.filter(request => request.command === 'manage_plugin').map(request => request.payload.taskId)
  await market.cancel(one)
  event('task_cancelled', {}, one)
  await rejected
  assert.equal(market.busy.value, true)
  assert.equal(market.activities.value.find(task => task.id === two).status, 'running')
  event('plugin_stage', { phase: 'installing', message: 'Installing two' }, two)
  event('plugin_manage_result', installedResult('two'), two)
  await second
  assert.equal(market.activities.value.find(task => task.id === one).status, 'cancelled')
  assert.equal(market.busy.value, false)
})

test('duplicate installs and exclusive mutations cannot overlap active installations', async () => {
  await reset()
  const first = market.manage('install', { name: 'one' })
  await assert.rejects(market.manage('install', { name: 'one' }), /active task/)
  await assert.rejects(market.manage('update', { name: 'two' }), /already running/)
  await nextTurn()
  event('plugin_manage_result', installedResult('one'), requests.find(request => request.command === 'manage_plugin').payload.taskId)
  await first
})

test('concurrent server acknowledgements consume only their own buffered terminal events', async () => {
  await reset()
  const acknowledgements = new Map()
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    if (command === 'plugin_marketplace') return empty()
    return new Promise(resolve => acknowledgements.set(args.payload.name, resolve))
  }
  const first = market.manage('install', { name: 'one' })
  const second = market.manage('install', { name: 'two' })
  await nextTurn()
  event('plugin_manage_result', installedResult('one'), 'server-one')
  event('plugin_manage_result', installedResult('two'), 'server-two')
  acknowledgements.get('two')({ taskId: 'server-two' })
  await second
  assert.equal(market.busy.value, true)
  acknowledgements.get('one')({ taskId: 'server-one' })
  await first
  assert.deepEqual(market.snapshot.value.installed.map(plugin => plugin.name).sort(), ['one', 'two'])
})

test('closing cancels every queued or cancellable install and waits for a committing sibling', async () => {
  await reset()
  const { runWindowCloseGuards } = await vite.ssrLoadModule('/src/utils/windowCloseGuards.ts')
  const completions = ['one', 'two', 'three', 'four'].map(name => market.manage('install', { name }))
  const settled = Promise.allSettled(completions)
  await nextTurn()
  const ids = requests.filter(request => request.command === 'manage_plugin').map(request => request.payload.taskId)
  event('plugin_stage', { phase: 'activating', message: 'Activating one' }, ids[0])
  let closed = false
  const closing = runWindowCloseGuards().then(() => { closed = true })
  await nextTurn()
  assert.deepEqual(requests.filter(request => request.command === 'cancel_task').map(request => request.taskId).sort(), ids.slice(1).sort())
  event('task_cancelled', {}, ids[1])
  event('task_cancelled', {}, ids[2])
  await nextTurn()
  assert.equal(closed, false)
  assert.equal(requests.filter(request => request.command === 'manage_plugin').length, 3)
  event('plugin_manage_result', installedResult('one'), ids[0])
  await closing
  assert.equal((await settled).filter(result => result.status === 'rejected').length, 3)
  assert.equal(market.busy.value, false)
})

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
  const before = pluginRevision.value
  const responses = []
  globalThis.__pluginInvoke = () => new Promise(resolve => responses.push(resolve))
  const first = market.refresh()
  const second = market.refresh()
  await nextTurn()
  responses[1](empty('new-runtime'))
  await second
  responses[0](empty('old-runtime'))
  await first
  assert.equal(market.snapshot.value.runtime.identity, 'new-runtime')
  assert.equal(pluginRevision.value, before + 1)
})

test('a runtime switch refreshes plugin consumers exactly once', async () => {
  await reset()
  const before = pluginRevision.value
  globalThis.__pluginInvoke = async () => empty('cuda-runtime')
  await market.refresh()
  assert.equal(market.snapshot.value.runtime.identity, 'cuda-runtime')
  assert.equal(pluginRevision.value, before + 1)
  await market.refresh()
  assert.equal(pluginRevision.value, before + 1)
})

test('inventory waits for the persisted default tool policy before querying', async () => {
  await reset()
  requests = []
  let release
  globalThis.__pluginSettings = {
    pluginDefaultsPolicy: 'review',
    initialize: () => new Promise(resolve => { release = () => {
      globalThis.__pluginSettings.pluginDefaultsPolicy = 'auto'
      resolve()
    } }),
  }
  try {
    const loading = market.refreshInventory()
    await nextTurn()
    assert.equal(requests.length, 0)
    release()
    await loading
    assert.equal(requests[0].payload.defaultPolicy, 'auto')
  } finally { globalThis.__pluginSettings = { pluginDefaultsPolicy: 'review', initialize() {} } }
})

test('inventory-only refresh preserves the catalog without requesting it again', async () => {
  await reset()
  requests = []
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    return { ...empty(), catalog: [] }
  }
  await market.refreshInventory()
  assert.equal(market.snapshot.value.catalog.length, 1)
  assert.deepEqual(requests, [{ command: 'plugin_marketplace', payload: { includeCatalog: false, defaultPolicy: 'review' } }])
})

test('first inventory preparation runs once and finishes without a refresh loop', async () => {
  await reset()
  requests = []
  let prepared = false
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    if (command === 'plugin_marketplace') return { ...empty(), preinstalledPending: prepared ? [] : ['convert'] }
    prepared = true
    event('plugin_manage_result', { ...empty(), preinstalledPending: [], updates: {} }, args.payload.taskId)
    return { started: true }
  }
  await market.refreshInventory()
  assert.equal(prepared, true)
  assert.equal(market.busy.value, false)
  assert.equal(requests.filter(request => request.command === 'manage_plugin').length, 1)
  assert.deepEqual(market.snapshot.value.preinstalledPending, [])
})

test('failed preinstalled preparation preserves the error and does not retry recursively', async () => {
  await reset()
  let attempts = 0
  globalThis.__pluginInvoke = async command => {
    if (command === 'plugin_marketplace') return { ...empty(), preinstalledPending: ['convert'] }
    attempts++
    throw new Error('Preinstalled plugin checksum mismatch')
  }
  await market.refreshInventory()
  assert.equal(attempts, 1)
  assert.equal(market.busy.value, false)
  assert.equal(market.loading.value, false)
  assert.match(market.error.value, /checksum mismatch/)
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

test('update checks survive unchanged snapshots and expire on contents, source or interpreter changes', async () => {
  for (const change of [
    value => { value.installed[0].contentDigest = 'changed' },
    value => { value.installed[0].url = '/other-source' },
    value => { value.runtime.pythonPath = '/other/python' },
    value => { value.runtime.updateSourceIdentity = 'other-source' },
  ]) {
    await reset()
    const inventory = { ...empty(), installed: [{ name: 'hello', managed: true, url: '/source', contentDigest: 'first' },
      { name: 'hello', managed: false, path: '/manual/hello', url: '/external-source' }] }
    globalThis.__pluginInvoke = async (command, args) => {
      requests.push({ command, ...args })
      return command === 'plugin_marketplace' ? structuredClone(inventory) : { started: true }
    }
    await market.refreshInventory()
    const completion = market.manage('check_updates', { name: 'hello' })
    await nextTurn()
    event('plugin_manage_result', { ...inventory, updates: { hello: { status: 'latest', up_to_date: true } } })
    await completion
    assert.ok(market.updates.value.hello.checked_at)
    await market.refreshInventory()
    assert.equal(market.updates.value.hello.status, 'latest')
    change(inventory)
    await market.refreshInventory()
    assert.equal(market.updates.value.hello, undefined)
  }
})

test('a failed selective check replaces its old latest state and preserves other checks', async () => {
  await reset()
  const inventory = { ...empty(), installed: ['one', 'two'].map(name => ({ name, managed: true, url: `/source/${name}` })) }
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    return command === 'plugin_marketplace' ? structuredClone(inventory) : { started: true }
  }
  await market.refreshInventory()
  const first = market.manage('check_updates')
  await nextTurn()
  event('plugin_manage_result', { ...inventory, updates: { one: { status: 'latest' }, two: { status: 'latest' } } })
  await first
  const retry = market.manage('check_updates', { name: 'one' })
  const rejected = assert.rejects(retry, /Offline/)
  await nextTurn()
  assert.equal(requests.at(-1).payload.name, 'one')
  event('error', { message: 'Offline' })
  await rejected
  assert.equal(market.updates.value.one.status, 'error')
  assert.equal(market.updates.value.two.status, 'latest')
  market.clearUpdateChecks()
  assert.deepEqual(market.updates.value, {})
})

test('simultaneous page entries share one inventory request', async () => {
  await reset()
  let resolve
  requests = []
  globalThis.__pluginInvoke = (command, args) => {
    requests.push({ command, ...args })
    return new Promise(done => { resolve = done })
  }
  const first = market.ensureInventory()
  const second = market.ensureInventory()
  await nextTurn()
  assert.equal(requests.length, 1)
  resolve(empty())
  await Promise.all([first, second])
  assert.equal(market.loading.value, false)
})

test('simultaneous marketplace entries also share their catalog request', async () => {
  await reset()
  let resolveCatalog
  requests = []
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    if (args.payload.catalogOnly) return new Promise(resolve => { resolveCatalog = resolve })
    return empty()
  }
  const first = market.openMarketplace()
  const second = market.openMarketplace()
  await nextTurn()
  assert.equal(requests.filter(request => request.payload.catalogOnly).length, 1)
  resolveCatalog(empty())
  await Promise.all([first, second])
})

test('runtime changes supersede an inventory request from the previous runtime', async () => {
  await reset()
  const responses = []
  globalThis.__pluginInvoke = (_command, { payload }) => payload.catalogOnly
    ? Promise.resolve(empty('new-runtime'))
    : new Promise(resolve => responses.push(resolve))
  const previous = market.openMarketplace()
  await nextTurn()
  const changed = market.openMarketplace(true)
  await nextTurn()
  assert.equal(responses.length, 2)
  responses[1](empty('new-runtime'))
  await changed
  responses[0](empty('old-runtime'))
  await previous
  assert.equal(market.snapshot.value.runtime.identity, 'new-runtime')
})

test('slow catalog fetching leaves verified installed tools available', async () => {
  await reset()
  let resolveCatalog
  globalThis.__pluginInvoke = async (_command, { payload }) => {
    if (payload.catalogOnly) return new Promise(resolve => { resolveCatalog = resolve })
    return { ...empty(), installed: [{ name: 'convert', managed: true, loaded: true }] }
  }
  const opening = market.openMarketplace()
  await nextTurn()
  assert.equal(market.snapshot.value.installed[0].name, 'convert')
  assert.equal(market.loading.value, false)
  assert.equal(market.catalogLoading.value, true)
  await assert.rejects(market.manage('update', { name: 'convert' }), /already running/)
  resolveCatalog({ ...empty(), catalogError: 'offline' })
  await opening
  assert.equal(market.snapshot.value.catalogError, 'offline')
  assert.equal(market.catalogLoading.value, false)
})

test('successful management consumes its inventory without a second verification query', async () => {
  await reset()
  requests = []
  const completion = market.manage('disable', { name: 'hello' })
  await nextTurn()
  event('plugin_manage_result', { ...empty(), installed: [{ name: 'hello', enabled: false }], updates: {} })
  await completion
  assert.equal(requests.filter(request => request.command === 'plugin_marketplace').length, 0)
  assert.equal(market.snapshot.value.installed[0].enabled, false)
})

test('late catalog cannot overwrite a newer runtime or management result', async () => {
  await reset()
  let resolveCatalog
  globalThis.__pluginInvoke = async (_command, { payload }) => {
    if (payload.catalogOnly) return new Promise(resolve => { resolveCatalog = resolve })
    return empty()
  }
  const opening = market.openMarketplace()
  await nextTurn()
  globalThis.__pluginInvoke = async () => ({ ...empty('new-runtime'), catalog: [] })
  await market.refreshInventory()
  resolveCatalog({ ...empty(), catalog: [{ name: 'stale' }] })
  await opening
  assert.equal(market.snapshot.value.runtime.identity, 'new-runtime')
  assert.equal(market.snapshot.value.catalog.length, 0)
  assert.equal(market.catalogLoading.value, false)
})

test('task activity retains its target, phases and outcome across page consumers', async () => {
  await reset()
  const completion = market.manage('update', { name: 'hello', installDependencies: false })
  await nextTurn()
  const start = market.activity.value.startedAt
  assert.equal(market.activity.value.status, 'running')
  event('plugin_stage', { phase: 'preserving', message: 'Keeping installed dependencies' })
  assert.equal(usePluginMarketplace().activity.value.phase, 'preserving')
  assert.equal(market.logRecords.value.at(-1).context.pluginPhase, 'preserving')
  assert.equal(market.logs.value.at(-1), 'Keeping installed dependencies')
  event('plugin_log', { message: 'Plugin files ready' })
  assert.ok(market.activity.value.updatedAt >= start)
  event('plugin_manage_result', { ...empty(), updates: {} })
  await completion
  assert.equal(market.activity.value.name, 'hello')
  assert.equal(market.activity.value.action, 'update')
  assert.equal(market.activity.value.status, 'succeeded')
  assert.ok(market.activity.value.finishedAt >= start)
  assert.equal(market.busy.value, false)
})

test('cancellation remains pending until the supervisor emits its terminal event', async () => {
  await reset()
  const completion = market.manage('update', { name: 'hello' })
  const rejected = assert.rejects(completion, /cancelled/)
  await nextTurn()
  globalThis.__pluginInvoke = async command => command === 'cancel_task' ? true : empty()
  await market.cancel()
  assert.equal(market.activity.value.status, 'cancelling')
  assert.equal(market.busy.value, true)
  event('task_cancelled')
  await rejected
  assert.equal(market.activity.value.status, 'cancelled')
  assert.equal(market.busy.value, false)
})

test('failed cancellation remains retryable and a later success is not labelled cancelled', async () => {
  await reset()
  const completion = market.manage('repair', { name: 'hello' })
  await nextTurn()
  globalThis.__pluginInvoke = async command => {
    if (command === 'cancel_task') throw new Error('cancel failed')
    return empty()
  }
  await assert.rejects(market.cancel(), /cancel failed/)
  assert.equal(market.activity.value.status, 'running')
  assert.equal(market.busy.value, true)
  event('plugin_manage_result', { ...empty(), updates: {} })
  await completion
  assert.equal(market.activity.value.status, 'succeeded')
})

test('failed tasks retain their error without a refresh changing the outcome', async () => {
  await reset()
  const completion = market.manage('update', { name: 'hello' })
  const rejected = assert.rejects(completion, /verification failed/)
  await nextTurn()
  event('error', { message: 'verification failed' })
  await rejected
  assert.equal(market.activity.value.status, 'failed')
  assert.match(market.activity.value.error, /verification failed/)
})

test('cancelling before startup dispatch does not start a plugin worker', async () => {
  await reset()
  requests = []
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    return command === 'cancel_task' ? false : empty()
  }
  const completion = market.manage('update', { name: 'hello' })
  const rejected = assert.rejects(completion, /cancelled/)
  await market.cancel()
  await nextTurn()
  assert.equal(requests.some(request => request.command === 'manage_plugin'), false)
  await rejected
  assert.equal(market.activity.value.status, 'cancelled')
})

test('a cancellation before acknowledgement is retried against the acknowledged worker', async () => {
  await reset()
  const cancelledIds = []
  let acknowledge
  globalThis.__pluginInvoke = async (command, args) => {
    if (command === 'manage_plugin') return new Promise(resolve => { acknowledge = resolve })
    if (command === 'cancel_task') { cancelledIds.push(args.taskId); return false }
    return empty()
  }
  const completion = market.manage('update', { name: 'hello' })
  const rejected = assert.rejects(completion, /cancelled/)
  await nextTurn()
  await market.cancel()
  acknowledge({ taskId: 'plugin_manage_acknowledged' })
  await nextTurn()
  assert.equal(cancelledIds.at(-1), 'plugin_manage_acknowledged')
  event('task_cancelled', {}, 'plugin_manage_acknowledged')
  await rejected
})

test('a failed post-acknowledgement cancel keeps the running task supervised', async () => {
  await reset()
  let acknowledge
  let cancellations = 0
  globalThis.__pluginInvoke = async (command) => {
    if (command === 'manage_plugin') return new Promise(resolve => { acknowledge = resolve })
    if (command === 'cancel_task' && ++cancellations === 2) throw new Error('cancel retry failed')
    return command === 'cancel_task' ? false : empty()
  }
  const completion = market.manage('update', { name: 'hello' })
  await nextTurn()
  await market.cancel()
  acknowledge({ taskId: 'plugin_manage_acknowledged' })
  await nextTurn()
  assert.equal(market.busy.value, true)
  assert.equal(market.activity.value.status, 'running')
  assert.match(market.error.value, /cancel retry failed/)
  event('plugin_manage_result', { ...empty(), updates: {} }, 'plugin_manage_acknowledged')
  await completion
  assert.equal(market.activity.value.status, 'succeeded')
  assert.equal(market.error.value, '')
})

test('window closure waits for the plugin supervisor after requesting cancellation', async () => {
  await reset()
  const { runWindowCloseGuards } = await vite.ssrLoadModule('/src/utils/windowCloseGuards.ts')
  const completion = market.manage('update', { name: 'hello' })
  const rejected = assert.rejects(completion, /cancelled/)
  await nextTurn()
  globalThis.__pluginInvoke = async command => command === 'cancel_task' ? true : empty()
  let closed = false
  const closing = runWindowCloseGuards().then(() => { closed = true })
  await nextTurn()
  assert.equal(closed, false)
  event('task_cancelled')
  await Promise.all([closing, rejected])
  assert.equal(closed, true)
})

test('committing plugin changes finish before window closure without being killed', async () => {
  await reset()
  const { runWindowCloseGuards } = await vite.ssrLoadModule('/src/utils/windowCloseGuards.ts')
  let cancelled = false
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    if (command === 'cancel_task') cancelled = true
    return empty()
  }
  const completion = market.manage('update', { name: 'hello' })
  await nextTurn()
  event('plugin_stage', { phase: 'activating', message: 'Activating plugin' })
  await market.cancel()
  let closed = false
  const closing = runWindowCloseGuards().then(() => { closed = true })
  await nextTurn()
  assert.equal(cancelled, false)
  assert.equal(closed, false)
  assert.equal(market.activity.value.status, 'running')
  event('plugin_manage_result', { ...empty(), updates: {} })
  await Promise.all([completion, closing])
  assert.equal(closed, true)
})

async function batchFixture(names = ['one', 'two']) {
  await reset()
  const inventory = { ...empty(), installed: names.map(name => ({
    name, managed: true, prepared: true, enabled: true, loaded: true, version: '1.0', url: `/source/${name}`, contentDigest: `${name}-before`,
  })) }
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    return command === 'plugin_marketplace' ? JSON.parse(JSON.stringify(inventory)) : { started: true }
  }
  await market.refreshInventory()
  market.updates.value = Object.fromEntries(names.map(name => [name, {
    status: 'update', remote_version: '2.0', target: { schemaVersion: 1, candidate: { version: '2.0', commit: 'a'.repeat(40) } },
  }]))
  const selection = selectPluginUpdates(market.snapshot.value, market.updates.value)
  const managed = () => requests.filter(request => request.command === 'manage_plugin')
  const succeed = name => {
    Object.assign(inventory.installed.find(plugin => plugin.name === name), { version: '2.0', contentDigest: `${name}-after` })
    event('plugin_manage_result', { ...JSON.parse(JSON.stringify(inventory)), updates: {} }, managed().at(-1).payload.taskId)
  }
  return { inventory, selection, managed, succeed }
}

test('bulk updates run sequentially with reviewed targets and retain state across consumers', async () => {
  const fixture = await batchFixture()
  const completion = market.updatePlugins(fixture.selection)
  await nextTurn()
  assert.equal(fixture.managed().length, 1)
  assert.equal(market.busy.value, true)
  assert.equal(market.exclusiveBusy.value, true)
  assert.strictEqual(usePluginMarketplace().updateBatch, market.updateBatch)
  await assert.rejects(market.manage('install', { name: 'unrelated' }), /already running/)
  await assert.rejects(market.updatePlugins(fixture.selection), /already running/)
  market.updates.value.one.target.candidate.commit = 'b'.repeat(40)
  fixture.succeed('one')
  await nextTurn()
  assert.equal(fixture.managed().length, 2)
  assert.deepEqual(market.updateBatch.value.items.map(item => item.status), ['succeeded', 'running'])
  for (const { payload } of fixture.managed()) {
    assert.equal(payload.expectedRuntime, 'cpu-runtime')
    assert.equal(payload.expectedUpdate.target.candidate.commit, 'a'.repeat(40))
    assert.equal(payload.installDependencies, false)
  }
  fixture.succeed('two')
  await completion
  assert.equal(market.updateBatch.value.phase, 'finished')
  assert.deepEqual(market.updateBatch.value.items.map(item => item.status), ['succeeded', 'succeeded'])
  assert.equal(market.busy.value, false)
})

test('a failed bulk item refreshes its inventory, retains the error and continues', async () => {
  const fixture = await batchFixture()
  const completion = market.updatePlugins(fixture.selection, { installDependencies: true })
  await nextTurn()
  const firstId = fixture.managed()[0].payload.taskId
  event('error', { message: 'Missing plugin dependency' }, firstId)
  await nextTurn()
  assert.equal(fixture.managed().length, 2)
  assert.equal(fixture.managed()[1].payload.installDependencies, true)
  fixture.succeed('two')
  await completion
  assert.deepEqual(market.updateBatch.value.items.map(item => item.status), ['failed', 'succeeded'])
  assert.match(market.updateBatch.value.items[0].error, /Missing plugin dependency/)
  assert.ok(market.logRecords.value.some(record => record.context.pluginTaskId === firstId && record.message === 'Missing plugin dependency'))
  assert.equal(market.snapshot.value.installed.find(plugin => plugin.name === 'one').version, '1.0')
})

test('bulk updates skip stale remaining targets after environment, source or installation changes', async () => {
  for (const change of [
    value => { value.runtime.identity = 'cuda' },
    value => { value.runtime.pythonPath = '/other/python' },
    value => { value.runtime.updateSourceIdentity = 'other-source' },
    value => { value.installed.find(plugin => plugin.name === 'two').contentDigest = 'changed' },
    value => { value.installed = value.installed.filter(plugin => plugin.name !== 'two') },
  ]) {
    const fixture = await batchFixture()
    const completion = market.updatePlugins(fixture.selection)
    await nextTurn()
    fixture.succeed('one')
    change(market.snapshot.value)
    await completion
    assert.equal(fixture.managed().length, 1)
    assert.deepEqual(market.updateBatch.value.items.map(item => item.status), ['succeeded', 'skipped'])
  }
})

test('stopping bulk work lets the current item finish and prevents remaining workers', async () => {
  const fixture = await batchFixture(['one', 'two', 'three'])
  const completion = market.updatePlugins(fixture.selection)
  await nextTurn()
  market.stopBatchUpdates()
  assert.equal(market.updateBatch.value.phase, 'stopping')
  assert.equal(market.busy.value, true)
  fixture.succeed('one')
  await completion
  assert.equal(fixture.managed().length, 1)
  assert.deepEqual(market.updateBatch.value.items.map(item => item.status), ['succeeded', 'cancelled', 'cancelled'])
  assert.equal(market.busy.value, false)
})

test('cancelling the active bulk task stops the remaining queue', async () => {
  const fixture = await batchFixture()
  const completion = market.updatePlugins(fixture.selection)
  await nextTurn()
  const id = fixture.managed()[0].payload.taskId
  await market.cancel(id)
  event('task_cancelled', {}, id)
  await completion
  assert.equal(fixture.managed().length, 1)
  assert.deepEqual(market.updateBatch.value.items.map(item => item.status), ['cancelled', 'cancelled'])
  assert.equal(market.error.value, '')
})

test('window closure stops queued updates and waits for the active publication', async () => {
  const fixture = await batchFixture()
  const completion = market.updatePlugins(fixture.selection)
  await nextTurn()
  const { runWindowCloseGuards } = await vite.ssrLoadModule('/src/utils/windowCloseGuards.ts')
  event('plugin_stage', { phase: 'activating' }, fixture.managed()[0].payload.taskId)
  let closed = false
  const closing = runWindowCloseGuards().then(() => { closed = true })
  await nextTurn()
  assert.equal(closed, false)
  fixture.succeed('one')
  await Promise.all([completion, closing])
  assert.equal(fixture.managed().length, 1)
  assert.deepEqual(market.updateBatch.value.items.map(item => item.status), ['succeeded', 'cancelled'])
  assert.equal(closed, true)
})

test('window closure does not launch inventory workers after batch cancellation or failure', async () => {
  const { runWindowCloseGuards } = await vite.ssrLoadModule('/src/utils/windowCloseGuards.ts')
  for (const terminal of ['task_cancelled', 'error']) {
    const fixture = await batchFixture()
    const completion = market.updatePlugins(fixture.selection)
    await nextTurn()
    const before = requests.filter(request => request.command === 'plugin_marketplace').length
    const closing = runWindowCloseGuards()
    await nextTurn()
    event(terminal, terminal === 'error' ? { message: 'Update interrupted during shutdown' } : {}, fixture.managed()[0].payload.taskId)
    await Promise.all([completion, closing])
    assert.equal(requests.filter(request => request.command === 'plugin_marketplace').length, before, terminal)
    assert.equal(fixture.managed().length, 1)
    assert.equal(market.updateBatch.value.items[1].status, 'cancelled')
  }
})

test('a runtime change during the final bulk item discards its old inventory and reloads the new runtime', async () => {
  const fixture = await batchFixture(['one'])
  const completion = market.updatePlugins(fixture.selection)
  await nextTurn()
  const next = empty('cuda-runtime')
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    return command === 'plugin_marketplace' ? next : { started: true }
  }
  const before = requests.filter(request => request.command === 'plugin_marketplace').length
  const reload = market.reloadRuntimeInventory()
  const cleared = market.snapshot.value === null
  const blocked = market.loading.value
  fixture.succeed('one')
  await Promise.all([completion, reload])
  assert.equal(cleared, true)
  assert.equal(blocked, true)
  assert.equal(market.snapshot.value.runtime.identity, 'cuda-runtime')
  assert.equal(requests.filter(request => request.command === 'plugin_marketplace').length, before + 1)
  assert.equal(market.busy.value, false)
  assert.equal(market.loading.value, false)
})

test('a visible marketplace reloads its catalog without delaying the replacement inventory', async () => {
  await reset()
  requests = []
  let resolveCatalog
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    if (args.payload.catalogOnly) return new Promise(resolve => { resolveCatalog = resolve })
    return { ...empty('replacement'), catalog: [] }
  }
  const reload = market.reloadRuntimeInventory({ includeCatalog: true })
  const opening = market.openMarketplace()
  await reload
  assert.equal(market.snapshot.value.runtime.identity, 'replacement')
  assert.equal(market.loading.value, false)
  assert.equal(market.catalogLoading.value, true)
  await nextTurn()
  assert.equal(requests.filter(request => request.payload.catalogOnly).length, 1)
  resolveCatalog(empty('replacement'))
  await opening
  assert.equal(market.snapshot.value.catalog[0].name, 'hello')
  assert.equal(market.catalogLoading.value, false)
})

test('a background runtime reload does not fetch the marketplace catalog', async () => {
  await reset()
  requests = []
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    return { ...empty('replacement'), catalog: [] }
  }
  await market.reloadRuntimeInventory()
  assert.equal(requests.length, 1)
  assert.equal(requests[0].payload.includeCatalog, false)
  assert.equal(market.catalogLoading.value, false)
})

test('a failed replacement catalog retains installed plugins and can be retried', async () => {
  await reset()
  let failCatalog = true
  globalThis.__pluginInvoke = async (_command, { payload }) => {
    if (payload.catalogOnly && failCatalog) throw new Error('Catalog unavailable')
    return { ...empty('replacement'),
      installed: [{ name: 'installed-plugin', managed: true, loaded: true }],
      ...(!payload.catalogOnly && payload.includeCatalog === false ? { catalog: [] } : {}) }
  }
  await market.reloadRuntimeInventory({ includeCatalog: true })
  await nextTurn()
  assert.equal(market.snapshot.value.installed[0].name, 'installed-plugin')
  assert.match(market.snapshot.value.catalogError, /Catalog unavailable/)
  assert.equal(market.error.value, '')
  assert.equal(market.loading.value, false)
  assert.equal(market.catalogLoading.value, false)
  failCatalog = false
  await market.openMarketplace(true)
  assert.equal(market.snapshot.value.catalog[0].name, 'hello')
  assert.equal(market.snapshot.value.catalogError, null)
})

test('closing during a replacement inventory query does not start a catalog request', async () => {
  await reset()
  requests = []
  let resolveInventory
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    return new Promise(resolve => { resolveInventory = resolve })
  }
  const reload = market.reloadRuntimeInventory({ includeCatalog: true })
  await nextTurn()
  const { runWindowCloseGuards } = await vite.ssrLoadModule('/src/utils/windowCloseGuards.ts')
  const closing = runWindowCloseGuards()
  resolveInventory({ ...empty('replacement'), catalog: [] })
  await Promise.all([closing, reload])
  assert.equal(requests.length, 1)
  assert.equal(requests[0].payload.catalogOnly, undefined)
  assert.equal(market.catalogLoading.value, false)
})

test('a late catalog from a replaced runtime cannot erase the newest catalog', async () => {
  await reset()
  let resolveOldCatalog
  let identity = 'first-runtime'
  globalThis.__pluginInvoke = async (_command, { payload }) => {
    if (!payload.catalogOnly) return { ...empty(identity), catalog: [] }
    if (identity === 'first-runtime') return new Promise(resolve => { resolveOldCatalog = resolve })
    return { ...empty(identity), catalog: [{ ...empty().catalog[0], name: 'new-catalog' }] }
  }
  const first = market.reloadRuntimeInventory({ includeCatalog: true })
  const oldOpening = market.openMarketplace()
  await first
  await nextTurn()
  identity = 'last-runtime'
  await market.reloadRuntimeInventory({ includeCatalog: true })
  await nextTurn()
  resolveOldCatalog({ ...empty('first-runtime'), catalog: [{ ...empty().catalog[0], name: 'stale-catalog' }] })
  await oldOpening
  assert.equal(market.snapshot.value.runtime.identity, 'last-runtime')
  assert.equal(market.snapshot.value.catalog[0].name, 'new-catalog')
  assert.equal(market.catalogLoading.value, false)
})

test('runtime changes stop remaining bulk items and refresh after success, failure or cancellation', async () => {
  for (const terminal of ['success', 'error', 'task_cancelled']) {
    const fixture = await batchFixture()
    const completion = market.updatePlugins(fixture.selection)
    await nextTurn()
    globalThis.__pluginInvoke = async (command, args) => {
      requests.push({ command, ...args })
      if (command === 'manage_plugin') queueMicrotask(() => event('error', { message: 'Wrong runtime' }, args.payload.taskId))
      return command === 'plugin_marketplace' ? empty('new-runtime') : { started: true }
    }
    const reload = market.reloadRuntimeInventory()
    const stopping = market.updateBatch.value.phase
    if (terminal === 'success') fixture.succeed('one')
    else event(terminal, terminal === 'error' ? { message: 'Old runtime failure' } : {}, fixture.managed()[0].payload.taskId)
    const oldErrorVisible = market.error.value
    await Promise.all([completion, reload])
    assert.equal(stopping, 'stopping', terminal)
    assert.equal(fixture.managed().length, 1, terminal)
    assert.equal(market.updateBatch.value.items[1].status, 'cancelled', terminal)
    assert.equal(market.snapshot.value.runtime.identity, 'new-runtime', terminal)
    assert.equal(market.error.value, '', terminal)
    assert.equal(oldErrorVisible, '', terminal)
  }
})

test('repeated runtime changes only query the most recent runtime after active work finishes', async () => {
  const fixture = await batchFixture(['one'])
  const completion = market.updatePlugins(fixture.selection)
  await nextTurn()
  const before = requests.filter(request => request.command === 'plugin_marketplace').length
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    return empty('last-runtime')
  }
  const first = market.reloadRuntimeInventory()
  const last = market.reloadRuntimeInventory()
  fixture.succeed('one')
  await Promise.all([completion, first, last])
  assert.equal(market.snapshot.value.runtime.identity, 'last-runtime')
  assert.equal(requests.filter(request => request.command === 'plugin_marketplace').length, before + 1)
})

test('queued management work is not dispatched into a replacement runtime', async () => {
  await reset()
  requests = []
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    if (command === 'manage_plugin') queueMicrotask(() => event('task_cancelled', {}, args.payload.taskId))
    return command === 'plugin_marketplace' ? empty('replacement') : { taskId: args?.payload?.taskId }
  }
  const pending = market.manage('install', { name: 'old-runtime-plugin' })
  const rejected = assert.rejects(pending, /cancelled/)
  const reload = market.reloadRuntimeInventory()
  await Promise.all([rejected, reload])
  assert.equal(requests.filter(request => request.command === 'manage_plugin').length, 0)
  assert.equal(market.snapshot.value.runtime.identity, 'replacement')
})

test('window closure suppresses a deferred runtime inventory reload', async () => {
  const fixture = await batchFixture()
  const completion = market.updatePlugins(fixture.selection)
  await nextTurn()
  const before = requests.filter(request => request.command === 'plugin_marketplace').length
  const reload = market.reloadRuntimeInventory()
  const { runWindowCloseGuards } = await vite.ssrLoadModule('/src/utils/windowCloseGuards.ts')
  const closing = runWindowCloseGuards()
  await nextTurn()
  event('task_cancelled', {}, fixture.managed()[0].payload.taskId)
  await Promise.all([completion, reload, closing])
  assert.equal(requests.filter(request => request.command === 'plugin_marketplace').length, before)
  assert.equal(market.loading.value, false)
})

test('cancelling window closure resumes the suppressed runtime inventory and catalog reload', async () => {
  const fixture = await batchFixture(['one'])
  const completion = market.updatePlugins(fixture.selection)
  await nextTurn()
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    return command === 'plugin_marketplace'
      ? { ...empty('replacement'), ...(!args.payload.catalogOnly ? { catalog: [] } : {}) }
      : { started: true }
  }
  const before = requests.filter(request => request.command === 'plugin_marketplace').length
  const reload = market.reloadRuntimeInventory({ includeCatalog: true })
  const { registerWindowCloseGuard, runWindowCloseGuards, WindowClosePersistenceError } = await vite.ssrLoadModule('/src/utils/windowCloseGuards.ts')
  const unregister = registerWindowCloseGuard(() => {
    assert.equal(requests.filter(request => request.command === 'plugin_marketplace').length, before)
    throw new WindowClosePersistenceError('Save failed', async () => {})
  })
  try {
    const closing = assert.rejects(runWindowCloseGuards(async () => 'cancel'), /Save failed/)
    await nextTurn()
    event('task_cancelled', {}, fixture.managed()[0].payload.taskId)
    await Promise.all([closing, completion, reload])
    await nextTurn()
    assert.equal(market.snapshot.value?.runtime.identity, 'replacement')
    assert.equal(market.snapshot.value.catalog[0].name, 'hello')
    assert.equal(market.loading.value, false)
    assert.equal(market.error.value, '')
  } finally { unregister() }
})

test('a failed replacement inventory query does not restore old plugin rows', async () => {
  const fixture = await batchFixture(['one'])
  const completion = market.updatePlugins(fixture.selection)
  await nextTurn()
  globalThis.__pluginInvoke = async () => { throw new Error('New runtime unavailable') }
  const reload = market.reloadRuntimeInventory()
  fixture.succeed('one')
  await Promise.all([completion, reload])
  assert.equal(market.snapshot.value, null)
  assert.match(market.error.value, /New runtime unavailable/)
  assert.equal(market.loading.value, false)
})

test('late recovery of an old failed task cannot restore its error after a runtime switch', async () => {
  await reset()
  let oldResponse, inventoryCalls = 0
  globalThis.__pluginInvoke = async (command, args) => {
    requests.push({ command, ...args })
    if (command !== 'plugin_marketplace') return { started: true }
    if (++inventoryCalls === 1) return new Promise(resolve => { oldResponse = resolve })
    return empty('replacement-runtime')
  }
  const completion = market.manage('repair', { name: 'broken-old-plugin' })
  const rejected = assert.rejects(completion, /Old dependency failure/)
  await nextTurn()
  event('error', { message: 'Old dependency failure' })
  await nextTurn()
  await market.reloadRuntimeInventory()
  oldResponse(empty('old-runtime'))
  await rejected
  assert.equal(market.snapshot.value.runtime.identity, 'replacement-runtime')
  assert.equal(market.error.value, '')
  assert.equal(market.loading.value, false)
})
