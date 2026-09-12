import assert from 'node:assert/strict'
import test, { after, afterEach } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createPinia } from 'pinia'
import { nextTick } from 'vue'

const vite = await createServer({
  configFile: false,
  server: { middlewareMode: true, hmr: false },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
  plugins: [{
    name: 'stub-update-environment',
    enforce: 'pre',
    transform(_code, id) {
      const path = id.replaceAll('\\', '/')
      if (path.endsWith('/src/stores/app.ts')) {
        return `export function useAppStore() {
          return { buildInfoVersion: '0.0.16', buildInfoUpdateSupported: true }
        }`
      }
      if (path.endsWith('/src/stores/settings.ts')) {
        return `export function useSettingsStore() {
          return { developerMode: false, updateChannel: 'prerelease', updateEndpointOverride: '' }
        }`
      }
      return null
    },
  }],
})
after(() => vite.close())
afterEach(() => {
  Reflect.deleteProperty(globalThis, 'window')
  Reflect.deleteProperty(globalThis, 'localStorage')
})

const { useUpdateStore } = await vite.ssrLoadModule('/src/stores/update.ts')
const stores = []
afterEach(() => {
  for (const store of stores.splice(0)) store.$dispose()
})

const persisted016 = {
  deferredVersion: '0.0.17',
  deferredAt: '2026-09-12T00:00:00.000Z',
  lastAcceptedVersion: '0.0.16',
}
const offeredUpdate = {
  currentVersion: '0.0.16', version: '0.0.17', prerelease: true,
  distribution: 'portable', autoUpdateSupported: true, requiresManualInstall: false,
}
const newStore = () => {
  const store = useUpdateStore(createPinia())
  stores.push(store)
  return store
}

function browserStorage(raw = JSON.stringify(persisted016)) {
  const values = new Map([['pymss-studio:update-state', raw]])
  const writes = []
  globalThis.window = {}
  globalThis.localStorage = {
    getItem: key => values.get(key) ?? null,
    setItem(key, value) { writes.push({ key, value }); values.set(key, value) },
  }
  return { values, writes }
}

function desktopStorage({ update = offeredUpdate, checkError, installError, loadError, onSave } = {}) {
  let payload = structuredClone(persisted016)
  const writes = []
  const commands = []
  globalThis.window = {
    __TAURI_INTERNALS__: {
      transformCallback: () => 1,
      invoke: async (command, args) => {
        commands.push(command)
        if (command === 'load_app_store') {
          assert.equal(args.name, 'update-state')
          if (loadError) throw loadError
          return structuredClone(payload)
        }
        if (command === 'save_app_store') {
          assert.equal(args.name, 'update-state')
          if (onSave) await onSave(args.data)
          payload = structuredClone(args.data)
          writes.push(payload)
          return null
        }
        if (command === 'plugin:event|listen') {
          assert.equal(args.event, 'pymss://managed-update-event')
          return 1
        }
        if (command === 'check_managed_update') {
          assert.deepEqual(args, { channel: 'prerelease', endpointOverride: null })
          if (checkError) throw checkError
          return structuredClone(update)
        }
        if (command === 'start_managed_update' && installError) throw installError
        throw new Error(`Unexpected IPC command: ${command}`)
      },
    },
  }
  return { writes, commands, payload: () => payload }
}

test('browser update preferences written by 0.0.16 load without a rewrite', async () => {
  const storage = browserStorage()
  const store = newStore()
  await store.initialize()
  assert.equal(store.deferredVersion, persisted016.deferredVersion)
  assert.equal(store.deferredAt, persisted016.deferredAt)
  assert.equal(store.lastAcceptedVersion, persisted016.lastAcceptedVersion)
  assert.equal(store.hasPendingDeferredVersion('0.0.17'), true)
  assert.equal(store.hasPendingDeferredVersion('0.0.16'), false)
  assert.deepEqual(storage.writes, [])

  store.availableUpdate = { ...offeredUpdate, version: '0.0.18' }
  await store.deferUntilNextLaunch()
  assert.deepEqual(storage.writes.map(item => item.key), ['pymss-studio:update-state'])
  const saved = JSON.parse(storage.values.get('pymss-studio:update-state'))
  assert.deepEqual(Object.keys(saved).sort(), Object.keys(persisted016).sort())
  assert.equal(saved.deferredVersion, '0.0.18')
  assert.equal(saved.lastAcceptedVersion, '0.0.16')
  assert.equal(Number.isNaN(Date.parse(saved.deferredAt)), false)

  const restarted = newStore()
  await restarted.initialize()
  assert.equal(restarted.deferredVersion, '0.0.18')
  assert.equal(restarted.deferredAt, saved.deferredAt)
})

test('damaged optional browser update preferences are not rewritten during initialization', async () => {
  for (const raw of ['{incomplete', 'null', 'false', '42', '"invalid"', '[]']) {
    const storage = browserStorage(raw)
    const store = newStore()
    await store.initialize()
    assert.equal(store.deferredVersion, '')
    assert.equal(store.deferredAt, '')
    assert.equal(store.lastAcceptedVersion, '')
    assert.equal(storage.values.get('pymss-studio:update-state'), raw)
    assert.deepEqual(storage.writes, [])
  }
})

test('unavailable browser storage does not prevent update initialization', async (t) => {
  browserStorage()
  const read = t.mock.method(globalThis.localStorage, 'getItem', () => { throw new Error('Storage access denied') })
  t.mock.method(console, 'warn', () => {})
  const store = newStore()
  await store.initialize()
  await store.initialize()
  assert.equal(read.mock.callCount(), 1)
  assert.equal(store.status, 'idle')
  assert.equal(store.deferredVersion, '')
})

test('a backend read failure leaves the stored data untouched and still registers progress events', async (t) => {
  const local = browserStorage()
  const failure = new Error('Store read failed')
  const storage = desktopStorage({ loadError: failure })
  const warning = t.mock.method(console, 'warn', () => {})
  const store = newStore()
  await Promise.all([store.initialize(), store.initialize()])
  await store.initialize()
  assert.equal(store.status, 'idle')
  assert.equal(store.deferredVersion, '')
  assert.deepEqual(storage.commands, ['load_app_store', 'plugin:event|listen'])
  assert.deepEqual(storage.writes, [])
  assert.deepEqual(local.writes, [])
  assert.equal(local.values.get('pymss-studio:update-state'), JSON.stringify(persisted016))
  assert.deepEqual(warning.mock.calls[0].arguments, ['Failed to load update state', failure])
})

test('a backend without update-state keeps the existing local key and three-field payload', async () => {
  const local = browserStorage()
  const missingStore = 'worker error: unknown app store: update-state'
  const storage = desktopStorage({
    loadError: missingStore,
    onSave: () => { throw missingStore },
  })
  const store = newStore()
  await store.initialize()
  assert.equal(store.deferredVersion, persisted016.deferredVersion)
  assert.equal(store.deferredAt, persisted016.deferredAt)
  assert.equal(store.lastAcceptedVersion, persisted016.lastAcceptedVersion)
  assert.deepEqual(local.writes, [])
  assert.equal(storage.commands.includes('plugin:event|listen'), true)

  store.availableUpdate = { ...offeredUpdate, version: '0.0.18' }
  await store.deferUntilNextLaunch()
  assert.deepEqual(storage.writes, [])
  assert.deepEqual(local.writes.map(item => item.key), ['pymss-studio:update-state'])
  const saved = JSON.parse(local.values.get('pymss-studio:update-state'))
  assert.deepEqual(saved, {
    deferredVersion: '0.0.18', deferredAt: store.deferredAt, lastAcceptedVersion: '0.0.16',
  })
  const restarted = newStore()
  await restarted.initialize()
  assert.equal(restarted.deferredVersion, saved.deferredVersion)
  assert.equal(restarted.deferredAt, saved.deferredAt)
  assert.equal(restarted.lastAcceptedVersion, saved.lastAcceptedVersion)
})

test('consecutive browser saves keep their own snapshots and omit empty optional fields', async () => {
  const storage = browserStorage('{}')
  const store = newStore()
  await store.initialize()
  store.availableUpdate = { ...offeredUpdate, version: '0.0.18' }
  const first = store.deferUntilNextLaunch()
  const firstDate = store.deferredAt
  store.availableUpdate = { ...offeredUpdate, version: '0.0.19' }
  const second = store.deferUntilNextLaunch()
  const secondDate = store.deferredAt
  await Promise.all([first, second])
  assert.deepEqual(storage.writes.map(item => item.key), [
    'pymss-studio:update-state', 'pymss-studio:update-state',
  ])
  assert.deepEqual(storage.writes.map(item => JSON.parse(item.value)), [
    { deferredVersion: '0.0.18', deferredAt: firstDate },
    { deferredVersion: '0.0.19', deferredAt: secondDate },
  ])
  assert.equal(JSON.parse(storage.values.get('pymss-studio:update-state')).deferredVersion, '0.0.19')
})

test('desktop saves remain ordered while an earlier write is pending', { timeout: 3000 }, async () => {
  let releaseFirst
  let notifyStarted
  const gate = new Promise(resolve => { releaseFirst = resolve })
  const started = new Promise(resolve => { notifyStarted = resolve })
  const snapshots = []
  const storage = desktopStorage({ onSave: async (data) => {
    snapshots.push(structuredClone(data))
    if (snapshots.length === 1) {
      notifyStarted()
      await gate
    }
  } })
  const store = newStore()
  await store.initialize()
  store.availableUpdate = { ...offeredUpdate, version: '0.0.18' }
  const first = store.deferUntilNextLaunch()
  await started
  store.availableUpdate = { ...offeredUpdate, version: '0.0.19' }
  const second = store.deferUntilNextLaunch()
  try {
    await new Promise(resolve => setImmediate(resolve))
    assert.deepEqual(snapshots.map(item => item.deferredVersion), ['0.0.18'])
  } finally {
    releaseFirst()
    await Promise.all([first, second])
  }
  assert.deepEqual(storage.writes.map(item => item.deferredVersion), ['0.0.18', '0.0.19'])
  assert.deepEqual(storage.payload(), {
    deferredVersion: '0.0.19', deferredAt: store.deferredAt, lastAcceptedVersion: '0.0.16',
  })
})

test('a failed native save does not write to local storage or block the next save', async () => {
  const local = browserStorage()
  let attempts = 0
  const storage = desktopStorage({ onSave: () => {
    if (++attempts === 1) throw new Error('Store write failed')
  } })
  const store = newStore()
  await store.initialize()
  store.availableUpdate = offeredUpdate
  await assert.rejects(store.deferUntilNextLaunch(), /Store write failed/)
  assert.deepEqual(storage.payload(), persisted016)
  assert.deepEqual(storage.writes, [])
  assert.deepEqual(local.writes, [])
  store.availableUpdate = { ...offeredUpdate, version: '0.0.18' }
  await store.deferUntilNextLaunch()
  assert.equal(storage.payload().deferredVersion, '0.0.18')
  assert.equal(storage.writes.length, 1)
  assert.deepEqual(local.writes, [])
})

test('desktop update preferences retain the same store key and payload across restart', async () => {
  const storage = desktopStorage()
  const store = newStore()
  await store.initialize()
  assert.deepEqual(storage.writes, [])
  assert.equal(store.hasPendingDeferredVersion('0.0.17'), true)
  await store.checkForUpdates(true)
  assert.equal(store.status, 'ready')
  assert.equal(store.shouldShowDeferred, true)
  await store.deferUntilNextLaunch()
  assert.equal(storage.writes.length, 1)
  assert.deepEqual(Object.keys(storage.payload()).sort(), Object.keys(persisted016).sort())
  assert.equal(storage.payload().lastAcceptedVersion, '0.0.16')

  const restarted = newStore()
  await restarted.initialize()
  assert.equal(restarted.deferredAt, storage.payload().deferredAt)
  assert.equal(restarted.hasPendingDeferredVersion('0.0.17'), true)
})

test('recognizing the installed deferred version preserves the accepted-version transition', async () => {
  const storage = desktopStorage()
  const store = newStore()
  await store.initialize()
  store.currentVersion = '0.0.17'
  await nextTick()
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(storage.payload(), { lastAcceptedVersion: '0.0.17' })
  assert.equal(store.hasPendingDeferredVersion('0.0.17'), false)
  assert.equal(store.deferredAt, '')
})

test('a check without a release does not erase deferred preferences', async () => {
  const storage = desktopStorage({ update: null })
  const store = newStore()
  await store.initialize()
  assert.equal(await store.checkForUpdates(true), null)
  assert.equal(store.status, 'idle')
  assert.equal(store.hasUpdate, false)
  assert.deepEqual(storage.payload(), persisted016)
  assert.deepEqual(storage.writes, [])
})

test('check and download failures keep existing deferred preferences', async () => {
  const failedCheck = desktopStorage({ checkError: new Error('check failed') })
  const checking = newStore()
  await checking.initialize()
  await assert.rejects(checking.checkForUpdates(true), /check failed/)
  assert.equal(checking.status, 'failed')
  assert.deepEqual(failedCheck.payload(), persisted016)
  assert.deepEqual(failedCheck.writes, [])

  const failedInstall = desktopStorage({ installError: new Error('download failed') })
  const installing = newStore()
  await installing.initialize()
  await installing.checkForUpdates(true)
  await assert.rejects(installing.downloadAndInstall(), /download failed/)
  assert.equal(installing.status, 'failed')
  assert.equal(installing.installFailed, true)
  assert.deepEqual(failedInstall.payload(), persisted016)
  assert.deepEqual(failedInstall.writes, [])
})

test('manual-install releases never dispatch an in-place install', async () => {
  const storage = desktopStorage({ update: { ...offeredUpdate, requiresManualInstall: true } })
  const store = newStore()
  await store.initialize()
  await store.checkForUpdates(true)
  await assert.rejects(store.downloadAndInstall(), /manual installation/)
  assert.equal(storage.commands.includes('start_managed_update'), false)
  assert.deepEqual(storage.payload(), persisted016)
})
