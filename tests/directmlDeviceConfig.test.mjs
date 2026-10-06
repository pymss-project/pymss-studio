import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createPinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'
import { Window } from 'happy-dom'

const browser = new Window()
globalThis.window = browser
globalThis.document = browser.document

const vite = await createServer({
  configFile: false,
  server: { watch: null, middlewareMode: true, hmr: false },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
})
after(() => vite.close())
const { useSettingsStore } = await vite.ssrLoadModule('/src/stores/settings.ts')
const { useAppStore } = await vite.ssrLoadModule('/src/stores/app.ts')

test('DirectML lists all adapter vendors and preserves the selected adapter ID', () => {
  setActivePinia(createPinia())
  const settings = useSettingsStore()
  const env = {
    torchBackend: 'dml', dmlAvailable: true,
    dmlDevices: [{ id: 0, name: 'Intel GPU' }, { id: 1, name: 'AMD GPU' }, { id: 2, name: 'NVIDIA GPU' }],
  }
  assert.deepEqual(settings.deviceOptions(env).filter(item => item.type === 'dml').map(item => item.value), ['dml:0', 'dml:1', 'dml:2'])
  settings.defaultDevice = 'dml:2'
  assert.deepEqual(settings.getRuntimeDeviceConfig(env), { device: 'dml', deviceIds: [2] })
  settings.defaultDevice = 'cuda:1'
  assert.deepEqual(settings.getRuntimeDeviceConfig(), { device: 'cuda', deviceIds: [1] })
  settings.defaultDevice = 'rocm:1'
  assert.deepEqual(settings.getRuntimeDeviceConfig(), { device: 'cuda', deviceIds: [1] })
  assert.equal(settings.deviceOptions({ dmlAvailable: false, dmlDevices: env.dmlDevices }).some(item => item.type === 'dml'), false)
  settings.$dispose()
})

test('DirectML readiness requires its backend and a live accelerator', () => {
  setActivePinia(createPinia())
  const app = useAppStore()
  app.runtimeInfo = { ready: true, installedBackend: 'dml', torchBackend: 'dml', acceleratorAvailable: true }
  assert.equal(app.runtimeInstalledBackend, 'dml')
  assert.equal(app.runtimeReadyForBackend('dml'), true)
  assert.equal(app.runtimeReadyForBackend('cpu'), false)
  app.runtimeInfo.acceleratorAvailable = false
  assert.equal(app.runtimeReadyForBackend('dml'), false)
  app.runtimeInfo.torchBackend = 'cpu'
  app.runtimeInfo.acceleratorAvailable = true
  assert.equal(app.runtimeReadyForBackend('dml'), false)
  app.$dispose()
})

test('runtime changes reconcile incompatible device preferences and retain compatible choices', () => {
  setActivePinia(createPinia())
  const settings = useSettingsStore()
  const dml = { torchAvailable: true, torchBackend: 'dml', dmlAvailable: true }
  settings.defaultDevice = 'cuda:1'
  assert.deepEqual(settings.getRuntimeDeviceConfig(dml), { device: 'auto', deviceIds: [0] })
  settings.reconcileRuntimeDevice(dml)
  assert.equal(settings.defaultDevice, 'auto')
  settings.defaultDevice = 'dml:2'
  settings.reconcileRuntimeDevice(dml)
  assert.deepEqual(settings.getRuntimeDeviceConfig(dml), { device: 'dml', deviceIds: [2] })
  settings.defaultDevice = 'cpu'
  settings.reconcileRuntimeDevice(dml)
  assert.equal(settings.defaultDevice, 'cpu')
  settings.defaultDevice = 'dml:2'
  settings.reconcileRuntimeDevice({ torchAvailable: true, torchBackend: 'cuda' })
  assert.equal(settings.defaultDevice, 'auto')
  settings.defaultDevice = 'cuda:1'
  settings.reconcileRuntimeDevice({ torchAvailable: true, torchBackend: 'rocm' })
  assert.equal(settings.defaultDevice, 'cuda:1')
  settings.reconcileRuntimeDevice({ torchAvailable: false, torchBackend: 'missing' })
  assert.equal(settings.defaultDevice, 'cuda:1')
  settings.defaultDevice = 'dml:2'
  settings.reconcileRuntimeDevice({ torchAvailable: true, torchBackend: 'dml', dmlAvailable: false, dmlError: 'driver failed' })
  assert.equal(settings.defaultDevice, 'dml:2')
  settings.$dispose()
})
