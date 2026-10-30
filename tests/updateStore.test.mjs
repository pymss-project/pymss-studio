import assert from 'node:assert/strict'
import test, { after, afterEach } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createPinia } from 'pinia'
import { nextTick } from 'vue'

const vite = await createServer({
  configFile: false,
  server: { watch: null, middlewareMode: true, hmr: false, preTransformRequests: false },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
  plugins: [{
    name: 'stub-update-environment',
    enforce: 'pre',
    transform(_code, id) {
      const path = id.replaceAll('\\', '/')
      if (path.endsWith('/src/i18n/index.ts')) return `export default { global: { t: key => key } }`
      if (path.endsWith('/src/stores/app.ts')) {
        return `import { reactive } from 'vue'
          export const application = reactive({ buildInfoVersion: '0.0.16', buildInfoUpdateSupported: true, runtimeInstallStatus: 'idle', runtimeCoreUpdateStatus: 'idle' })
          export function useAppStore() { return application }`
      }
      if (path.endsWith('/src/stores/settings.ts')) {
        return `import { reactive } from 'vue'
          export const settings = reactive({ developerMode: false, updateChannel: 'prerelease', updateSource: 'auto', updateEndpointOverride: '' })
          export function useSettingsStore() { return settings }`
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
const { runWindowCloseGuards } = await vite.ssrLoadModule('/src/utils/windowCloseGuards.ts')
const { settings } = await vite.ssrLoadModule('/src/stores/settings.ts')
const { application } = await vite.ssrLoadModule('/src/stores/app.ts')
const stores = []
afterEach(() => {
  for (const store of stores.splice(0)) store.$dispose()
  settings.updateSource = 'auto'
  settings.isModelDirMigrating = false
  application.runtimeInstallStatus = 'idle'
  application.runtimeCoreUpdateStatus = 'idle'
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

function desktopStorage({ update = offeredUpdate, checkError, checkRequest, installError, installRequest, integrityReport = { supported: false, valid: null, repairSupported: false }, integrityError, integrityRequest, repairError, repairRequest, exitError, destroyError, closeError, exitRequest, restartPending = false, loadError, onSave, windowLabel = 'main' } = {}) {
  let payload = structuredClone(persisted016)
  const writes = []
  const commands = []
  const argumentsList = []
  let eventCallback
  globalThis.window = {
    __TAURI_INTERNALS__: {
      metadata: { currentWindow: { label: windowLabel } },
      transformCallback: callback => { eventCallback = callback; return 1 },
      invoke: async (command, args) => {
        commands.push(command)
        argumentsList.push({ command, args })
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
          assert.deepEqual(args, { channel: 'prerelease', endpointOverride: null, source: settings.updateSource })
          if (checkError) throw checkError
          if (checkRequest) return checkRequest()
          return structuredClone(update)
        }
        if (command === 'start_managed_update') {
          if (installError) throw installError
          if (installRequest) return installRequest()
          return null
        }
        if (command === 'get_managed_update_state') return { restartRequired: restartPending }
        if (command === 'check_application_integrity') {
          if (integrityError) throw integrityError
          return integrityRequest ? integrityRequest() : structuredClone(integrityReport)
        }
        if (command === 'start_application_repair') {
          if (repairError) throw repairError
          return repairRequest ? repairRequest() : null
        }
        if (command === 'plugin:process|exit') { if (exitError) throw exitError; return exitRequest ? exitRequest() : null }
        if (command === 'plugin:window|destroy') { if (destroyError) throw destroyError; return null }
        if (command === 'close_current_window') { if (closeError) throw closeError; return null }
        throw new Error(`Unexpected IPC command: ${command}`)
      },
    },
  }
  return { writes, commands, argumentsList, window: globalThis.window, payload: () => payload,
    setExitErrors: (exit, destroy) => { exitError = exit; destroyError = destroy },
    emit: payload => eventCallback?.({ event: 'pymss://managed-update-event', id: 1, payload }) }
}

for (const source of ['auto', 'github', 'modelscope']) {
  test(`update checks and confirmed downloads retain the ${source} preference`, async () => {
    settings.updateSource = source
    const storage = desktopStorage({ update: { ...offeredUpdate, source: 'modelscope' }, installError: new Error('Download interrupted') })
    const store = newStore()
    await store.initialize()
    await store.checkForUpdates(true)
    assert.equal(store.selectedSource, 'modelscope')
    await assert.rejects(store.downloadAndInstall(), /Download interrupted/)
    assert.deepEqual(storage.argumentsList.find(call => call.command === 'start_managed_update').args, {
      channel: 'prerelease', endpointOverride: null, expectedVersion: offeredUpdate.version, source,
    })
  })
}

test('switching download sources resets progress without losing the fallback log', async () => {
  const storage = desktopStorage()
  const store = newStore()
  await store.initialize()
  storage.emit({ event: 'SourceSelected', data: { source: 'github' } })
  storage.emit({ event: 'Started', data: { contentLength: 1000 } })
  storage.emit({ event: 'Progress', data: { downloadedBytes: 750, contentLength: 1000 } })
  assert.equal(store.downloadDownloadedBytes, 750)
  storage.emit({ event: 'SourceSelected', data: { source: 'modelscope', fallback: true } })
  assert.equal(store.selectedSource, 'modelscope')
  assert.equal(store.status, 'downloading')
  assert.equal(store.downloadDownloadedBytes, 0)
  assert.equal(store.downloadTotalBytes, 0)
  assert.ok(store.downloadLogs.some(entry => entry.message === 'Retrying download from ModelScope'))
  storage.emit({ event: 'Started', data: { contentLength: 1000 } })
  storage.emit({ event: 'Progress', data: { downloadedBytes: 100, contentLength: 1000 } })
  assert.equal(store.downloadDownloadedBytes, 100)
  store.dismiss()
  assert.equal(store.selectedSource, undefined)
})

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
  assert.deepEqual(storage.commands, ['load_app_store', 'write_frontend_log', 'plugin:event|listen', 'get_managed_update_state'])
  assert.deepEqual(storage.writes, [])
  assert.deepEqual(local.writes, [])
  assert.equal(local.values.get('pymss-studio:update-state'), JSON.stringify(persisted016))
  assert.match(warning.mock.calls[0].arguments[0], /Studio.*Failed to load update state/)
  assert.match(warning.mock.calls[0].arguments[1], /Store read failed/)
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

for (const distribution of ['macosApp', 'appImage']) {
  test(`${distribution} installation leaves restart and exit to the backend`, async () => {
    const storage = desktopStorage({ update: { ...offeredUpdate, distribution } })
    const store = newStore()
    await store.initialize()
    await store.checkForUpdates(true)
    assert.equal(await store.downloadAndInstall(), true)
    assert.equal(store.status, 'installing')
    assert.equal(storage.commands.includes('start_managed_update'), true)
    assert.equal(storage.commands.includes('plugin:process|exit'), false)
    assert.deepEqual(storage.payload(), persisted016)
  })
}

test('Windows installation saves acceptance before exiting for its update helper', async () => {
  const storage = desktopStorage()
  const store = newStore()
  await store.initialize()
  await store.checkForUpdates(true)
  assert.equal(await store.downloadAndInstall(), true)
  assert.equal(storage.commands.at(-1), 'plugin:process|exit')
  assert.deepEqual(storage.payload(), { lastAcceptedVersion: offeredUpdate.version })
})

test('an application update blocks window closing until installation finishes or fails', async () => {
  const store = newStore()
  for (const status of ['downloading', 'installing']) {
    store.status = status
    await assert.rejects(runWindowCloseGuards(), /update is in progress/)
  }
  store.status = 'failed'
  await runWindowCloseGuards()
})

for (const windowLabel of ['editor-1', 'workflow-node-editor-1', 'workflow-simple-editor-1']) {
  for (const status of ['downloading', 'installing']) {
    test(`${windowLabel} ignores ${status} progress from a failed main-window update`, async () => {
      const main = desktopStorage({ installError: new Error('Update interrupted') })
      const origin = newStore()
      await origin.initialize()
      await origin.checkForUpdates(true)

      const editor = desktopStorage({ windowLabel })
      const recipient = newStore()
      await recipient.initialize()
      assert.equal(await recipient.checkForUpdates(true), null)
      assert.equal(editor.commands.includes('plugin:event|listen'), false)
      assert.equal(editor.commands.includes('check_managed_update'), false)

      globalThis.window = main.window
      const installation = origin.downloadAndInstall()
      globalThis.window = editor.window
      editor.emit({ event: 'SourceSelected', data: { source: 'github' } })
      if (status === 'installing') editor.emit({ event: 'Finished' })

      globalThis.window = main.window
      await assert.rejects(installation, /Update interrupted/)
      assert.equal(origin.status, 'failed')
      assert.equal(recipient.status, 'idle')
      assert.equal(recipient.isInstallOverlayVisible, false)

      globalThis.window = editor.window
      await runWindowCloseGuards()
    })
  }

  test(`${windowLabel} never blocks closing for application update state`, async () => {
    desktopStorage({ windowLabel })
    const store = newStore()
    for (const status of ['downloading', 'installing']) {
      store.status = status
      await runWindowCloseGuards()
    }
  })
}

for (const busy of ['installing', 'preparing', 'updating', 'migration']) {
  test(`application updates do not start during ${busy}`, async () => {
    const storage = desktopStorage()
    const store = newStore()
    await store.initialize()
    await store.checkForUpdates(true)
    const statusBefore = store.status
    if (busy === 'installing') application.runtimeInstallStatus = busy
    else if (busy === 'migration') settings.isModelDirMigrating = true
    else application.runtimeCoreUpdateStatus = busy
    assert.ok(store.installationBlockedReason)
    await assert.rejects(store.downloadAndInstall())
    assert.equal(storage.commands.includes('start_managed_update'), false)
    assert.equal(storage.commands.includes('plugin:process|exit'), false)
    assert.equal(store.status, statusBefore)
  })
}

test('a backend busy rejection does not exit the application or record acceptance', async () => {
  const storage = desktopStorage({ installError: new Error('APPLICATION_UPDATE_BUSY: active worker') })
  const store = newStore()
  await store.initialize()
  await store.checkForUpdates(true)
  await assert.rejects(store.downloadAndInstall())
  assert.equal(store.status, 'failed')
  assert.equal(storage.commands.includes('plugin:process|exit'), false)
  assert.equal(storage.writes.length, 0)
  assert.ok(!store.error.includes('APPLICATION_UPDATE_BUSY:'))
})

test('macOS update signature failures show a localized summary and retain the diagnostic in the log', async () => {
  const detail = 'worker error: MACOS_UPDATE_SIGNATURE_INVALID: a sealed resource is missing or invalid\nfile modified: Contents/Resources/version'
  const storage = desktopStorage({ update: { ...offeredUpdate, distribution: 'macosApp' }, installError: new Error(detail) })
  const store = newStore()
  await store.initialize()
  await store.checkForUpdates(true)
  await assert.rejects(store.downloadAndInstall(), { message: detail })
  assert.equal(store.error, 'settings.updateMacosSignatureInvalid')
  assert.equal(store.status, 'failed')
  assert.equal(store.installFailed, true)
  assert.ok(store.downloadLogs.some(entry => entry.message.includes(detail)))
  assert.equal(storage.commands.includes('plugin:process|exit'), false)
  assert.equal(storage.writes.length, 0)
})

test('Windows exit rejection falls back to closing the main window', async () => {
  const storage = desktopStorage({ exitError: new Error('exit rejected') })
  const store = newStore()
  await store.initialize()
  await store.checkForUpdates(true)
  assert.equal(await store.downloadAndInstall(), true)
  assert.equal(storage.commands.at(-1), 'plugin:window|destroy')
  assert.equal(store.restartRequired, true)
})

test('a hanging exit request has a bounded fallback', async () => {
  const storage = desktopStorage({ exitRequest: () => new Promise(() => {}) })
  const store = newStore()
  await store.initialize()
  await store.checkForUpdates(true)
  assert.equal(await store.downloadAndInstall(), true)
  assert.equal(storage.commands.at(-1), 'plugin:window|destroy')
})

test('staged exit failures keep a blocking state and retry without restaging', async () => {
  const storage = desktopStorage({ exitError: new Error('exit rejected'), destroyError: new Error('close rejected') })
  const store = newStore()
  await store.initialize()
  await store.checkForUpdates(true)
  assert.equal(await store.downloadAndInstall(), false)
  assert.equal(store.status, 'restart-required')
  await runWindowCloseGuards()
  store.dismissInstallError()
  store.dismiss()
  assert.equal(await store.deferUntilNextLaunch(), false)
  await store.checkForUpdates(true)
  assert.equal(store.isInstallOverlayVisible, true)
  assert.equal(store.status, 'restart-required')
  storage.emit({ event: 'Started', data: { contentLength: 123 } })
  assert.equal(store.status, 'restart-required')
  storage.setExitErrors(null, null)
  assert.equal(await store.downloadAndInstall(), true)
  assert.equal(storage.commands.filter(command => command === 'start_managed_update').length, 1)
  assert.equal(storage.writes.length, 1)
})

test('a reloaded renderer restores the native pending-exit state', async () => {
  const storage = desktopStorage({ restartPending: true })
  const store = newStore()
  await store.initialize()
  assert.equal(store.status, 'restart-required')
  assert.equal(store.isInstallOverlayVisible, true)
  assert.equal(store.restartRequired, true)
  await store.checkForUpdates(true)
  assert.equal(storage.commands.includes('check_managed_update'), false)
})

test('pending updates offer an independent native close action after plugin exit failures', async () => {
  const storage = desktopStorage({ exitError: new Error('exit rejected'), destroyError: new Error('destroy rejected') })
  const store = newStore()
  await store.initialize()
  assert.equal(await store.closeForUpdate(), false)
  assert.equal(storage.commands.includes('close_current_window'), false)
  await store.checkForUpdates(true)
  assert.equal(await store.downloadAndInstall(), false)
  assert.equal(await store.closeForUpdate(), true)
  assert.equal(storage.commands.at(-1), 'close_current_window')
  assert.equal(store.restartRequired, true)
  assert.equal(storage.commands.filter(command => command === 'start_managed_update').length, 1)
})

test('a failed native close action preserves the pending update overlay', async () => {
  const storage = desktopStorage({ restartPending: true, closeError: new Error('native close rejected') })
  const store = newStore()
  await store.initialize()
  assert.equal(await store.closeForUpdate(), false)
  assert.equal(store.status, 'restart-required')
  assert.equal(store.isInstallOverlayVisible, true)
  assert.equal(storage.commands.at(-1), 'close_current_window')
  await runWindowCloseGuards()
})

test('a native staged event restores pending exit after a renderer reload during download', async () => {
  const storage = desktopStorage()
  const store = newStore()
  await store.initialize()
  storage.emit({ event: 'SourceSelected', data: { source: 'github' } })
  storage.emit({ event: 'Started', data: { contentLength: 1000 } })
  storage.emit({ event: 'Progress', data: { downloadedBytes: 1000, contentLength: 1000 } })
  storage.emit({ event: 'Finished' })
  assert.equal(store.status, 'installing')
  storage.emit({ event: 'RestartRequired' })
  assert.equal(store.restartRequired, true)
  assert.equal(store.status, 'restart-required')
  assert.equal(store.isInstallOverlayVisible, true)
  await runWindowCloseGuards()
  assert.equal(storage.commands.includes('plugin:process|exit'), false)
  assert.equal(await store.retryUpdateExit(), true)
  assert.equal(storage.commands.at(-1), 'plugin:process|exit')
  assert.equal(storage.commands.includes('start_managed_update'), false)
})

for (const outcome of ['release', 'empty', 'error']) {
  test(`an older update check cannot replace pending exit with ${outcome}`, async () => {
    let resolveCheck
    let rejectCheck
    const check = new Promise((resolve, reject) => { resolveCheck = resolve; rejectCheck = reject })
    const storage = desktopStorage({ checkRequest: () => check })
    const store = newStore()
    await store.initialize()
    store.availableUpdate = { ...offeredUpdate, version: '0.0.18' }
    const cachedUpdate = store.availableUpdate
    const checking = store.checkForUpdates(true)
    assert.equal(store.status, 'checking')
    storage.emit({ event: 'RestartRequired' })
    const pendingExitError = store.error
    if (outcome === 'error') rejectCheck(new Error('Check interrupted'))
    else resolveCheck(outcome === 'release' ? offeredUpdate : null)
    assert.equal(await checking, cachedUpdate)
    assert.equal(store.restartRequired, true)
    assert.equal(store.status, 'restart-required')
    assert.equal(store.error, pendingExitError)
    assert.equal(store.isInstallOverlayVisible, true)
    assert.equal(store.currentVersion, '')
    assert.equal(store.lastCheckedAt, '')
    assert.equal(store.availableUpdate, cachedUpdate)
    assert.deepEqual(storage.writes, [])
    await runWindowCloseGuards()
  })
}

test('an active installation saves acceptance before handling its staged exit', async () => {
  let finishInstall
  let finishSave
  let notifySaveStarted
  const installGate = new Promise(resolve => { finishInstall = resolve })
  const saveGate = new Promise(resolve => { finishSave = resolve })
  const saveStarted = new Promise(resolve => { notifySaveStarted = resolve })
  const storage = desktopStorage({
    installRequest: () => installGate,
    onSave: async () => { notifySaveStarted(); await saveGate },
  })
  const store = newStore()
  await store.initialize()
  await store.checkForUpdates(true)
  const installation = store.downloadAndInstall()
  try {
    storage.emit({ event: 'Finished' })
    storage.emit({ event: 'RestartRequired' })
    assert.equal(store.restartRequired, true)
    assert.equal(store.status, 'installing')
    await assert.rejects(runWindowCloseGuards(), /update is in progress/)
    finishInstall()
    await saveStarted
    assert.equal(store.status, 'installing')
    assert.equal(storage.commands.includes('plugin:process|exit'), false)
    await assert.rejects(runWindowCloseGuards(), /update is in progress/)
  } finally {
    finishInstall()
    finishSave()
    await installation
  }
  assert.equal(await installation, true)
  assert.deepEqual(storage.payload(), { lastAcceptedVersion: offeredUpdate.version })
  assert.equal(storage.commands.at(-1), 'plugin:process|exit')
})

const damagedApplication = { supported: true, valid: false, repairSupported: true, detail: 'Contents/Resources/python/worker.py: resource missing' }

test('integrity diagnostics do not change update availability or install state', async () => {
  desktopStorage({ integrityReport: damagedApplication })
  const store = newStore()
  await store.initialize()
  await store.checkIntegrity()
  assert.deepEqual(store.integrity, damagedApplication)
  assert.equal(store.status, 'idle')
  assert.equal(store.hasUpdate, false)
  assert.equal(store.isInstallOverlayVisible, false)
})

test('repair uses the official current-version command and preserves an offered upgrade', async () => {
  settings.developerMode = true
  settings.updateEndpointOverride = 'https://example.com/custom.json'
  const storage = desktopStorage({ integrityReport: damagedApplication })
  const store = newStore()
  await store.initialize()
  await store.checkIntegrity()
  store.availableUpdate = { ...offeredUpdate }
  try {
    assert.equal(await store.repairApplication(), true)
    assert.deepEqual(storage.argumentsList.find(call => call.command === 'start_application_repair').args, { source: 'auto' })
    assert.equal(storage.commands.includes('start_managed_update'), false)
    assert.equal(storage.commands.includes('plugin:process|exit'), false)
    assert.deepEqual(storage.writes, [])
    assert.equal(store.availableUpdate.version, offeredUpdate.version)
    assert.equal(store.repairing, true)
    assert.equal(store.status, 'installing')
  } finally {
    settings.developerMode = false
    settings.updateEndpointOverride = ''
  }
})

test('failed repair releases its lock, retains diagnostics, and permits a retry', async () => {
  let attempts = 0
  const storage = desktopStorage({ integrityReport: damagedApplication, repairRequest: () => {
    attempts += 1
    throw new Error('MACOS_UPDATE_ROLLED_BACK: startup timed out')
  } })
  const store = newStore()
  await store.initialize()
  await store.checkIntegrity()
  for (let attempt = 0; attempt < 2; attempt += 1) {
    await assert.rejects(store.repairApplication(), /startup timed out/)
    assert.equal(store.error, 'settings.updateRolledBack')
    assert.equal(store.status, 'failed')
    assert.equal(store.isBusy, false)
    assert.equal(store.installErrorVisible, true)
    assert.ok(store.downloadLogs.some(entry => entry.message.includes('startup timed out')))
    store.dismissInstallError()
  }
  assert.equal(attempts, 2)
  assert.equal(storage.commands.filter(command => command === 'start_application_repair').length, 2)
})

test('closing a repair failure clears its presentation mode', async () => {
  desktopStorage({ integrityReport: damagedApplication, repairError: new Error('Download interrupted') })
  const store = newStore()
  await store.checkIntegrity()
  await assert.rejects(store.repairApplication(), /Download interrupted/)
  assert.equal(store.repairing, true)
  store.dismissInstallError()
  assert.equal(store.installErrorVisible, false)
  assert.equal(store.repairing, false)
})

for (const outcome of ['manual-upgrade', 'no-update', 'check-error']) {
  test(`a normal ${outcome} check clears the previous repair context`, async () => {
    desktopStorage({
      integrityReport: damagedApplication, repairError: new Error('Download interrupted'),
      update: outcome === 'no-update' ? null : { ...offeredUpdate, requiresManualInstall: true, autoUpdateSupported: false },
      checkError: outcome === 'check-error' ? new Error('Check interrupted') : undefined,
    })
    const store = newStore()
    await store.initialize()
    await store.checkIntegrity()
    await assert.rejects(store.repairApplication(), /Download interrupted/)
    assert.equal(store.repairing, true)
    assert.equal(store.installErrorVisible, true)
    const check = store.checkForUpdates(true)
    assert.equal(store.repairing, false)
    assert.equal(store.installErrorVisible, false)
    if (outcome === 'check-error') await assert.rejects(check, /Check interrupted/)
    else await check
    assert.equal(store.repairing, false)
    assert.equal(store.hasUpdate, outcome === 'manual-upgrade')
    assert.equal(store.requiresManualInstall, outcome === 'manual-upgrade')
  })
}

test('repair blocks concurrent repair, upgrade, and update checks', async () => {
  let rejectRepair
  const pending = new Promise((_resolve, reject) => { rejectRepair = reject })
  const storage = desktopStorage({ integrityReport: damagedApplication, repairRequest: () => pending })
  const store = newStore()
  await store.initialize()
  await store.checkIntegrity()
  const repair = store.repairApplication()
  try {
    await assert.rejects(store.repairApplication(), /already in progress/)
    await assert.rejects(store.downloadAndInstall(), /already in progress/)
    await store.checkForUpdates(true)
    await store.checkIntegrity()
    assert.equal(storage.commands.includes('check_managed_update'), false)
    assert.equal(storage.commands.filter(command => command === 'check_application_integrity').length, 1)
    await assert.rejects(runWindowCloseGuards(), /update is in progress/)
  } finally {
    rejectRepair(new Error('Download interrupted'))
    await assert.rejects(repair, /Download interrupted/)
  }
})

test('integrity checks share the request and allow retry after an inspection error', async () => {
  let failCheck
  let attempts = 0
  const pending = new Promise((_resolve, reject) => { failCheck = reject })
  const storage = desktopStorage({ integrityRequest: () => ++attempts === 1 ? pending : damagedApplication })
  const store = newStore()
  const first = store.checkIntegrity()
  const duplicate = store.checkIntegrity()
  await assert.rejects(store.repairApplication(), /already in progress/)
  failCheck(new Error('Unable to launch verifier'))
  assert.equal(await first, null)
  assert.equal(await duplicate, null)
  assert.equal(store.integrity, null)
  assert.match(store.integrityError, /Unable to launch verifier/)
  assert.equal(store.status, 'idle')
  await store.checkIntegrity()
  assert.deepEqual(store.integrity, damagedApplication)
  assert.equal(store.integrityError, '')
  assert.equal(storage.commands.filter(command => command === 'check_application_integrity').length, 2)
})

test('unsupported installations cannot start application repair', async () => {
  const storage = desktopStorage()
  const store = newStore()
  await store.checkIntegrity()
  await assert.rejects(store.repairApplication(), /unavailable/)
  assert.equal(storage.commands.includes('start_application_repair'), false)
})

test('runtime maintenance blocks application repair', async () => {
  const storage = desktopStorage({ integrityReport: damagedApplication })
  const store = newStore()
  await store.checkIntegrity()
  application.runtimeCoreUpdateStatus = 'updating'
  await assert.rejects(store.repairApplication(), /settings.updateTasksBusy/)
  assert.equal(storage.commands.includes('start_application_repair'), false)
})

test('editor windows cannot inspect or repair the application', async () => {
  const storage = desktopStorage({ windowLabel: 'workflow-editor-1' })
  const store = newStore()
  assert.equal(await store.checkIntegrity(), null)
  await assert.rejects(store.repairApplication(), /main window/)
  assert.equal(storage.commands.includes('check_application_integrity'), false)
  assert.equal(storage.commands.includes('start_application_repair'), false)
})
