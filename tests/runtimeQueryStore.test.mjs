import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, watch } from 'vue'
import { Window } from 'happy-dom'

const browser = new Window()
globalThis.window = browser
globalThis.document = browser.document
const navigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'navigator')
// Runtime mutation cases use Windows regardless of the machine running the tests.
Object.defineProperty(browser.navigator, 'platform', { configurable: true, value: 'Win32' })
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: browser.navigator })

const vite = await createServer({
  configFile: false,
  server: { watch: null, middlewareMode: true, hmr: false },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
  plugins: [{
    name: 'stub-worker-events-and-model-refresh',
    enforce: 'pre',
    transform(_code, id) {
      const normalized = id.replaceAll('\\', '/')
      if (normalized.endsWith('/src/stores/model.ts')) {
        return 'export function useModelStore() { return { loadModels: async () => undefined } }'
      }
      if (!normalized.endsWith('/src/utils/events.ts')) return null
      return `
        let register = async () => undefined
        export function __setRegisterWorkerEventsForTest(next) { register = next }
        export function registerWorkerEvents() { return register() }
      `
    },
  }],
})
after(async () => {
  try {
    await vite.close()
  } finally {
    if (navigatorDescriptor) Object.defineProperty(globalThis, 'navigator', navigatorDescriptor)
    else delete globalThis.navigator
  }
})

const { useAppStore } = await vite.ssrLoadModule('/src/stores/app.ts')
const { buildDiagnosticChecks } = await vite.ssrLoadModule('/src/utils/diagnostics.ts')
const { useSettingsStore } = await vite.ssrLoadModule('/src/stores/settings.ts')
const { __setRegisterWorkerEventsForTest } = await vite.ssrLoadModule('/src/utils/events.ts')

function installTauriInvoke(invoke) {
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { __TAURI_INTERNALS__: { invoke, metadata: { currentWindow: { label: 'main' } } } },
  })
}

function appStore(invoke) {
  __setRegisterWorkerEventsForTest(async () => undefined)
  installTauriInvoke(invoke)
  setActivePinia(createPinia())
  return useAppStore()
}

const pause = () => new Promise(resolve => setTimeout(resolve, 1))

test('Settings Promise.all serializes runtime info and size probes in the actual store', async () => {
  let active = 0
  let maxActive = 0
  const calls = []
  const app = appStore(async command => {
    active += 1
    maxActive = Math.max(maxActive, active)
    calls.push(command)
    await pause()
    active -= 1
    return command === 'runtime_info'
      ? { manifestVersion: 'info', ready: true }
      : { sizes: { cpu: 42 }, incompleteBackends: [] }
  })

  await Promise.all([app.checkRuntimeInfo(), app.loadRuntimeEnvSizes()])

  assert.equal(maxActive, 1)
  assert.deepEqual(calls, ['runtime_info', 'runtime_env_sizes'])
  assert.equal(app.runtimeInfo?.manifestVersion, 'info')
  assert.deepEqual(app.runtimeEnvSizes, { cpu: 42 })
})

test('a later runtime-info failure does not suppress the earlier successful store snapshot', async () => {
  let calls = 0
  const app = appStore(async command => {
    assert.equal(command, 'runtime_info')
    calls += 1
    if (calls === 1) return { manifestVersion: 'successful-snapshot', ready: true }
    throw new Error('worker error: malformed runtime state')
  })

  const first = app.checkRuntimeInfo()
  const second = app.checkRuntimeInfo()
  assert.equal((await first).manifestVersion, 'successful-snapshot')
  await assert.rejects(second, /malformed runtime state/)
  assert.equal(app.runtimeInfo?.manifestVersion, 'successful-snapshot')
})

test('each checkRuntimeInfo call starts a fresh worker probe', async () => {
  let calls = 0
  const app = appStore(async command => {
    assert.equal(command, 'runtime_info')
    calls += 1
    return { manifestVersion: `snapshot-${calls}`, ready: true }
  })

  const [first, second] = await Promise.all([app.checkRuntimeInfo(), app.checkRuntimeInfo()])

  assert.equal(calls, 2)
  assert.equal(first.manifestVersion, 'snapshot-1')
  assert.equal(second.manifestVersion, 'snapshot-2')
  assert.equal(app.runtimeInfo?.manifestVersion, 'snapshot-2')
})

test('runtime page visits reuse startup queries and recent snapshots without another env probe', async () => {
  const info = deferred()
  const calls = []
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'runtime_info') return info.promise
    if (command === 'runtime_env_sizes') return { sizes: { cuda: 123 }, incompleteBackends: [] }
    if (command === 'runtime_core_versions') return { packages: {} }
    throw new Error(`unexpected ${command}`)
  })
  const startup = app.checkRuntimeInfo()
  const visit = app.ensureRuntimeInfo()
  assert.equal(app.runtimeInfoLoading, true)
  info.resolve({ ready: true, installedBackend: 'cuda' })
  await Promise.all([startup, visit])
  await Promise.all([app.ensureRuntimeEnvSizes(), app.ensureRuntimeCoreVersions()])
  await Promise.all([app.ensureRuntimeInfo(), app.ensureRuntimeEnvSizes(), app.ensureRuntimeCoreVersions()])
  assert.deepEqual(calls.toSorted(), ['runtime_core_versions', 'runtime_env_sizes', 'runtime_info'])
  assert.equal(app.runtimeInfoLoading, false)
})

test('environment information becomes ready before a slow disk scan', async () => {
  const scan = deferred()
  const app = appStore(async command => {
    if (command === 'runtime_info') return { ready: true }
    if (command === 'runtime_env_sizes') return scan.promise
    throw new Error(`unexpected ${command}`)
  })
  const info = app.ensureRuntimeInfo()
  const sizes = app.ensureRuntimeEnvSizes()
  await info
  assert.equal(app.runtimeInfo.ready, true)
  assert.equal(app.runtimeInfoLoading, false)
  assert.equal(app.runtimeEnvSizesLoading, true)
  scan.resolve({ sizes: { cpu: 42 } })
  await sizes
  assert.equal(app.runtimeEnvSizesLoading, false)
})

test('a failed size refresh preserves the display but retries on the next visit', async () => {
  let scans = 0
  const app = appStore(async command => {
    assert.equal(command, 'runtime_env_sizes')
    if (++scans === 2) throw new Error('scan failed')
    return { sizes: { cpu: scans } }
  })
  await app.ensureRuntimeEnvSizes()
  assert.deepEqual(await app.loadRuntimeEnvSizes(), { cpu: 1 })
  assert.equal(app.runtimeEnvSizesError, 'scan failed')
  assert.deepEqual(await app.ensureRuntimeEnvSizes(), { cpu: 3 })
  assert.equal(app.runtimeEnvSizesError, '')
})

test('deleting an environment invalidates cached disk sizes and runtime snapshots', async () => {
  const calls = []
  let deleted = false
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'runtime_info') return { installedEnvironments: deleted ? [] : [{ backend: 'cpu' }] }
    if (command === 'runtime_env_sizes') return { sizes: deleted ? {} : { cpu: 42 } }
    if (command === 'delete_runtime') { deleted = true; return true }
    throw new Error(`unexpected ${command}`)
  })
  await app.ensureRuntimeInfo()
  await app.ensureRuntimeEnvSizes()
  await app.deleteRuntime('cpu')
  await app.ensureRuntimeInfo()
  await app.ensureRuntimeEnvSizes()
  assert.deepEqual(calls, ['runtime_info', 'runtime_env_sizes', 'delete_runtime', 'runtime_info', 'runtime_env_sizes'])
  assert.deepEqual(app.runtimeEnvSizes, {})
})

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

async function within(promise, label) {
  let timer
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${label} timed out`)), 2000) }),
    ])
  } finally {
    clearTimeout(timer)
  }
}

test('runtime changes reconcile incompatible device preferences and retain compatible choices', () => {
  setActivePinia(createPinia())
  const settings = useSettingsStore()
  const cpu = { torchAvailable: true, torchBackend: 'cpu' }
  settings.defaultDevice = 'cuda:1'
  assert.deepEqual(settings.getRuntimeDeviceConfig(cpu), { device: 'auto', deviceIds: [0] })
  settings.reconcileRuntimeDevice(cpu)
  assert.equal(settings.defaultDevice, 'auto')
  settings.defaultDevice = 'cpu'
  settings.reconcileRuntimeDevice(cpu)
  assert.equal(settings.defaultDevice, 'cpu')
  settings.defaultDevice = 'cuda:1'
  settings.reconcileRuntimeDevice({ torchAvailable: true, torchBackend: 'rocm' })
  assert.deepEqual(settings.getRuntimeDeviceConfig(), { device: 'cuda', deviceIds: [1] })
  settings.reconcileRuntimeDevice({ torchAvailable: false, torchBackend: 'missing' })
  assert.equal(settings.defaultDevice, 'cuda:1')
  settings.defaultDevice = 'mps'
  settings.reconcileRuntimeDevice({ ...cpu, mpsAvailable: true })
  assert.equal(settings.defaultDevice, 'mps')
  settings.reconcileRuntimeDevice(cpu)
  assert.equal(settings.defaultDevice, 'auto')
  settings.defaultDevice = 'mlx'
  settings.reconcileRuntimeDevice({ ...cpu, mlxAvailable: true })
  assert.equal(settings.defaultDevice, 'mlx')
  settings.reconcileRuntimeDevice(cpu)
  assert.equal(settings.defaultDevice, 'auto')
  settings.$dispose()
})

test('a macOS MPS runtime offers MLX only when the MLX package is available', () => {
  setActivePinia(createPinia())
  const settings = useSettingsStore()
  const env = { torchAvailable: true, torchBackend: 'cpu', mpsAvailable: true, mlxAvailable: false }
  assert.equal(settings.deviceOptions(env).some(option => option.value === 'mlx'), false)
  assert.equal(settings.deviceOptions(env).some(option => option.value === 'mps'), true)
  settings.defaultDevice = 'mlx'
  assert.deepEqual(settings.getRuntimeDeviceConfig(env), { device: 'auto', deviceIds: [0] })
  settings.reconcileRuntimeDevice(env)
  assert.equal(settings.defaultDevice, 'auto')
  const mlx = { ...env, mlxAvailable: true }
  assert.equal(settings.deviceOptions(mlx).some(option => option.value === 'mlx'), true)
  settings.defaultDevice = 'mlx'
  assert.deepEqual(settings.getRuntimeDeviceConfig(mlx), { device: 'mlx', deviceIds: [0] })
  settings.$dispose()
})

test('the macOS MLX runtime still supports selecting CPU as an inference device', () => {
  setActivePinia(createPinia())
  const settings = useSettingsStore()
  const env = { torchAvailable: true, torchBackend: 'cpu', mpsAvailable: true, mlxAvailable: true }
  const devices = settings.deviceOptions(env).map(option => option.value)
  assert.ok(devices.includes('cpu'))
  assert.ok(devices.includes('mlx'))
  settings.defaultDevice = 'cpu'
  settings.reconcileRuntimeDevice(env)
  assert.equal(settings.defaultDevice, 'cpu')
  assert.deepEqual(settings.getRuntimeDeviceConfig(env), { device: 'cpu', deviceIds: [0] })
  settings.defaultDevice = 'mlx'
  assert.deepEqual(settings.getRuntimeDeviceConfig(env), { device: 'mlx', deviceIds: [0] })
  settings.$dispose()
})

for (const platform of ['darwin']) {
  test(`${platform} rejects environment management before IPC and preserves read-only probes`, async () => {
    const commands = []
    const app = appStore(async command => {
      commands.push(command)
      if (command === 'runtime_info') return { platform, ready: true }
      if (command === 'get_env_info') return { platform, pymssAvailable: true, torchAvailable: true }
      if (command === 'runtime_env_sizes') return { sizes: { mlx: 42 }, incompleteBackends: [] }
      throw new Error(`unexpected ${command}`)
    })
    await app.checkRuntimeInfo()
    assert.equal(app.runtimeManagementSupported, false)
    assert.equal(app.runtimeCoreUpdateSupported, true)
    for (const mutate of [
      () => app.installRuntime('cpu'),
      () => app.activateRuntime('cpu'),
      () => app.deleteRuntime('cpu'),
      () => app.updateDebugRuntime('debug_runtime_write_file', { path: '/runtime/active-runtime.json', content: '{}' }),
      () => app.updateDebugRuntime('debug_runtime_restore_file', { path: '/runtime/active-runtime.json' }),
      () => app.updateDebugRuntime('debug_runtime_override_active', { backend: 'cpu', pythonPath: '/runtime/cpu/bin/python' }),
    ]) {
      await assert.rejects(mutate(), /only available on Windows/)
    }
    assert.equal(app.runtimeInstallStatus, 'idle')
    assert.equal(app.runtimeCoreUpdateStatus, 'idle')
    await app.checkEnv()
    await app.loadRuntimeEnvSizes()
    assert.deepEqual(commands, ['runtime_info', 'get_env_info', 'runtime_env_sizes'])
    app.$dispose()
  })

}

test('Linux dispatches runtime installation through the existing task queue', async () => {
  const calls = []
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'runtime_info') return { platform: 'linux', ready: false }
    if (command === 'start_runtime_install') return true
    throw new Error(`unexpected ${command}`)
  })
  await app.checkRuntimeInfo()
  assert.equal(app.runtimeManagementSupported, true)
  assert.equal(app.runtimeDebugSupported, false)
  await assert.rejects(() => app.updateDebugRuntime('debug_runtime_write_file', { path: '/runtime/active-runtime.json', content: '{}' }), /only available on Windows/)
  const taskId = await within(app.installRuntime('rocm'), 'Linux runtime installation ACK')
  assert.ok(calls.includes('start_runtime_install'))
  assert.equal(app.runtimeInstallBackend, 'rocm')
  assert.equal(app.runtimeInstallStatus, 'installing')
  app.handleRuntimeEvent({ type: 'task_cancelled', taskId })
  app.$dispose()
})

for (const platform of ['darwin', 'linux']) {
  test(`${platform} updates the active core and refreshes its versions after success`, async () => {
    const calls = []
    const refreshed = deferred()
    let infoCalls = 0
    const app = appStore(async command => {
      calls.push(command)
      if (command === 'runtime_info') {
        if (++infoCalls === 2) refreshed.resolve()
        return { platform, ready: true }
      }
      if (command === 'start_runtime_core_update') return true
      if (command === 'runtime_core_versions') return { packages: { pymss: { latestVersion: '2.2.0' } } }
      if (command === 'get_env_info') return { platform }
      throw new Error(`unexpected ${command}`)
    })
    await app.checkRuntimeInfo()
    const taskId = await within(app.updateRuntimeCore('cpu'), 'cross-platform core update ACK')
    assert.ok(calls.includes('start_runtime_core_update'))
    app.handleRuntimeEvent({ type: 'runtime_core_update_finished', taskId, payload: {} })
    await within(refreshed.promise, 'runtime refresh after core update')
    await app.ensureRuntimeCoreVersions()
    assert.equal(app.runtimeCoreVersions.packages.pymss.latestVersion, '2.2.0')
    assert.equal(app.runtimeManagementSupported, platform === 'linux')
    app.$dispose()
  })
}

for (const action of [
  {
    label: 'runtime installation', command: 'start_runtime_install', finished: 'runtime_install_finished',
    failed: 'runtime_install_failed', start: app => app.installRuntime('cuda'),
  },
  {
    label: 'runtime core repair', command: 'start_runtime_core_update', finished: 'runtime_core_update_finished',
    failed: 'error', start: app => app.updateRuntimeCore('cuda', 'auto', '', { repairDependencies: true }),
  },
]) {
  test(`${action.label} ignores old environment replies and preserves the selected GPU`, async () => {
    let requestId
    const persisted = deferred()
    const cuda = { torchAvailable: true, torchBackend: 'cuda', cudaAvailable: true, cudaDeviceCount: 2 }
    const app = appStore(async (command, args) => {
      if (command === 'start_env_check') { requestId = args.requestId; return { started: true } }
      if (command === action.command) return true
      if (command === 'get_env_info') return cuda
      if (command === 'runtime_info') return { ready: true, installedBackend: 'cuda' }
      if (command === 'runtime_core_versions') return {}
      if (command === 'save_app_store') { persisted.resolve(args.data); return }
      throw new Error(`unexpected ${command}`)
    })
    const settings = useSettingsStore()
    settings.initialized = true
    settings.defaultDevice = 'cuda:1'
    const stop = watch(() => app.envInfo, env => {
      if (env?.torchAvailable) settings.reconcileRuntimeDevice(env)
    })
    let taskId
    try {
      await app.checkEnvInBackground()
      taskId = await within(action.start(app), `${action.label} ACK`)
      app.handleWorkerEvent({ type: 'env_info', requestId, payload: { torchAvailable: true, torchBackend: 'cpu' } })
      app.handleWorkerEvent({ type: 'error', requestId, payload: { code: 'ENV_CHECK_FAILED', message: 'obsolete probe' } })
      await nextTick()
      assert.equal(app.envInfo, null)
      assert.equal(app.envLoading, true)
      assert.equal(app.lastError, null)
      assert.equal(settings.defaultDevice, 'cuda:1')

      app.handleRuntimeEvent({ type: action.finished, taskId, payload: { state: { backend: 'cuda' } } })
      await within(app.checkEnv(), 'environment after background mutation')
      await nextTick()
      assert.equal(settings.defaultDevice, 'cuda:1')
      assert.deepEqual(settings.getRuntimeDeviceConfig(app.envInfo), { device: 'cuda', deviceIds: [1] })
      assert.equal(app.envInfo.torchBackend, 'cuda')
      assert.equal(app.envLoading, false)
      assert.equal((await within(persisted.promise, 'saved GPU preference')).defaultDevice, 'cuda:1')
    } finally {
      if (taskId) app.handleRuntimeEvent({ type: 'task_cancelled', taskId })
      stop()
      await within(persisted.promise, 'pending settings persistence')
      settings.$dispose()
      app.$dispose()
    }
  })

  test(`${action.label} start failures release the queue and invalidated probe state`, async () => {
    let requestId
    const failure = new Error('Runtime mutation could not start')
    const app = appStore(async (command, args) => {
      if (command === 'start_env_check') { requestId = args.requestId; return { started: true } }
      if (command === action.command) throw failure
      if (command === 'get_env_info') return { torchBackend: 'cuda' }
      throw new Error(`unexpected ${command}`)
    })
    await app.checkEnvInBackground()
    await assert.rejects(action.start(app), error => error === failure)
    assert.equal(app.envLoading, false)
    app.handleWorkerEvent({ type: 'env_info', requestId, payload: { torchBackend: 'cpu' } })
    assert.equal(app.envInfo, null)
    await within(app.checkEnv(), 'environment after failed background dispatch')
    assert.equal(app.envInfo.torchBackend, 'cuda')
  })

  for (const terminal of [action.failed, 'task_cancelled']) {
    test(`${action.label} ${terminal} restores loading state and permits a fresh probe`, async () => {
      let requestId
      const refreshed = deferred()
      const app = appStore(async (command, args) => {
        if (command === 'start_env_check') { requestId = args.requestId; return { started: true } }
        if (command === action.command) return true
        if (command === 'runtime_info') return { ready: true }
        if (command === 'get_env_info') return refreshed.promise
        throw new Error(`unexpected ${command}`)
      })
      await app.checkEnvInBackground()
      const taskId = await within(action.start(app), `${action.label} ACK`)
      app.handleRuntimeEvent({ type: terminal, taskId, payload: { message: 'Runtime mutation stopped' } })
      await within(app.checkRuntimeInfo(), 'queue after terminal event')
      assert.equal(app.envLoading, false)

      const probe = app.checkEnv()
      app.handleWorkerEvent({ type: 'env_info', requestId, payload: { torchBackend: 'cpu' } })
      assert.equal(app.envInfo, null)
      assert.equal(app.envLoading, true)
      refreshed.resolve({ torchBackend: 'cuda' })
      await within(probe, 'new probe after terminal event')
      assert.equal(app.envInfo.torchBackend, 'cuda')
      assert.equal(app.envLoading, false)
    })
  }

  test(`${action.label} cleanup preserves the loading state of a newer queued probe`, async () => {
    let requestId
    const probing = deferred()
    const refreshed = deferred()
    const app = appStore(async (command, args) => {
      if (command === 'start_env_check') { requestId = args.requestId; return { started: true } }
      if (command === action.command) return true
      if (command === 'get_env_info') { probing.resolve(); return refreshed.promise }
      throw new Error(`unexpected ${command}`)
    })
    await app.checkEnvInBackground()
    const taskId = await within(action.start(app), `${action.label} ACK`)
    const probe = app.checkEnv()
    app.handleRuntimeEvent({ type: 'task_cancelled', taskId })
    await within(probing.promise, 'probe after mutation cleanup')
    assert.equal(app.envLoading, true)
    app.handleWorkerEvent({ type: 'env_info', requestId, payload: { torchBackend: 'cpu' } })
    assert.equal(app.envInfo, null)
    refreshed.resolve({ torchBackend: 'cuda' })
    await within(probe, 'queued environment probe')
    assert.equal(app.envInfo.torchBackend, 'cuda')
    assert.equal(app.envLoading, false)
  })
}

test('cancellation before dispatch preserves an existing background environment probe', async () => {
  let requestId
  const registered = deferred()
  const calls = []
  const app = appStore(async (command, args) => {
    calls.push(command)
    if (command === 'start_env_check') { requestId = args.requestId; return { started: true } }
    throw new Error(`unexpected ${command}`)
  })
  await app.checkEnvInBackground()
  __setRegisterWorkerEventsForTest(() => registered.promise)
  const start = app.installRuntime('cuda')
  await Promise.resolve()
  assert.equal(await app.cancelRuntimeInstall(), true)
  registered.resolve()
  await within(start, 'cancelled dispatch')
  assert.equal(app.envLoading, true)
  app.handleWorkerEvent({ type: 'env_info', requestId, payload: { torchBackend: 'cpu' } })
  assert.equal(app.envInfo.torchBackend, 'cpu')
  assert.equal(app.envLoading, false)
  assert.deepEqual(calls, ['start_env_check'])
})

test('a late background probe cannot change device preferences after runtime activation', async () => {
  let backgroundId
  const persisted = deferred()
  const cpu = { torchAvailable: true, torchBackend: 'cpu' }
  const cuda = { torchAvailable: true, torchBackend: 'cuda', cudaAvailable: true }
  const app = appStore(async (command, args) => {
    if (command === 'start_env_check') {
      backgroundId = args?.requestId
      return { started: true }
    }
    if (command === 'activate_runtime') return true
    if (command === 'get_env_info') return cuda
    if (command === 'runtime_info') return { ready: true, installedBackend: 'cuda' }
    if (command === 'save_app_store') {
      persisted.resolve(args.data)
      return
    }
    throw new Error(`unexpected ${command}`)
  })
  const settings = useSettingsStore()
  settings.initialized = true
  settings.defaultDevice = 'cpu'
  app.envInfo = cpu
  const stop = watch([() => settings.initialized, () => app.envInfo], ([initialized, env]) => {
    if (initialized && env?.torchAvailable) settings.reconcileRuntimeDevice(env)
  })
  try {
    await app.checkEnvInBackground()
    await app.activateRuntime('cuda', {}, { refreshCoreVersions: false })
    await nextTick()
    settings.defaultDevice = 'cuda:1'
    app.handleWorkerEvent({ type: 'env_info', requestId: backgroundId, payload: cpu })
    await nextTick()
    const saved = await within(persisted.promise, 'saved device preference')

    assert.equal(settings.defaultDevice, 'cuda:1')
    assert.deepEqual(settings.getRuntimeDeviceConfig(app.envInfo), { device: 'cuda', deviceIds: [1] })
    assert.equal(app.envInfo.torchBackend, 'cuda')
    assert.equal(saved.defaultDevice, 'cuda:1')
    assert.equal(typeof backgroundId, 'string')
  } finally {
    stop()
    settings.$dispose()
    app.$dispose()
  }
})

test('background probes accept only their own pending request and ignore duplicate replies', async () => {
  let requestId
  const app = appStore(async (command, args) => {
    assert.equal(command, 'start_env_check')
    requestId = args?.requestId
    return { started: true }
  })
  await app.checkEnvInBackground()
  app.handleWorkerEvent({ type: 'env_info', requestId: 'another-window', payload: { torchBackend: 'cpu' } })
  assert.equal(app.envInfo, null)
  assert.equal(app.envLoading, true)
  app.handleWorkerEvent({ type: 'env_info', requestId, payload: { torchBackend: 'cuda' } })
  assert.equal(app.envInfo.torchBackend, 'cuda')
  assert.equal(app.envLoading, false)
  assert.equal(app.envCheckedOnce, true)
  app.handleWorkerEvent({ type: 'env_info', requestId, payload: { torchBackend: 'cpu' } })
  assert.equal(app.envInfo.torchBackend, 'cuda')
})

test('a stale background failure cannot clear a newer probe or overwrite its error state', async () => {
  const ids = []
  const app = appStore(async (command, args) => {
    if (command === 'start_env_check') { ids.push(args?.requestId); return { started: true } }
    if (command === 'get_env_info') return { torchBackend: 'cuda' }
    throw new Error(`unexpected ${command}`)
  })
  await app.checkEnvInBackground()
  await app.checkEnv()
  await app.checkEnvInBackground()
  app.handleWorkerEvent({ type: 'error', requestId: ids[0], payload: { code: 'ENV_CHECK_FAILED', message: 'old failure' } })
  assert.equal(app.envLoading, true)
  assert.equal(app.lastError, null)
  app.handleWorkerEvent({ type: 'error', requestId: ids[1], payload: { code: 'ENV_CHECK_FAILED', message: 'current failure' } })
  assert.equal(app.envLoading, false)
  assert.equal(app.lastError, 'current failure')
  assert.equal(app.envInfo.torchBackend, 'cuda')
})

test('a superseded direct probe cannot finish the loading state of a newer queued probe', async () => {
  const first = deferred()
  const second = deferred()
  let calls = 0
  const app = appStore(command => {
    assert.equal(command, 'get_env_info')
    return ++calls === 1 ? first.promise : second.promise
  })
  const oldProbe = app.checkEnv()
  await Promise.resolve()
  const newProbe = app.checkEnv()
  first.resolve({ torchBackend: 'cpu' })
  await oldProbe
  assert.equal(app.envInfo, null)
  assert.equal(app.envLoading, true)
  second.resolve({ torchBackend: 'cuda' })
  await newProbe
  assert.equal(app.envInfo.torchBackend, 'cuda')
  assert.equal(app.envLoading, false)
})

test('a superseded direct probe failure leaves the current probe error state unchanged', async () => {
  const first = deferred()
  const second = deferred()
  let calls = 0
  const app = appStore(command => {
    assert.equal(command, 'get_env_info')
    return ++calls === 1 ? first.promise : second.promise
  })
  const oldProbe = app.checkEnv()
  await Promise.resolve()
  const newProbe = app.checkEnv()
  first.reject(new Error('old failure'))
  await assert.rejects(oldProbe, /old failure/)
  assert.equal(app.envLoading, true)
  assert.equal(app.lastError, null)
  second.reject(new Error('current failure'))
  await assert.rejects(newProbe, /current failure/)
  assert.equal(app.envLoading, false)
  assert.equal(app.lastError, 'current failure')
})

test('direct environment probes wait for runtime activation to finish', async () => {
  const switching = deferred()
  const calls = []
  const app = appStore(command => {
    calls.push(command)
    if (command === 'activate_runtime') return switching.promise
    if (command === 'runtime_info') return { ready: true, installedBackend: 'cuda' }
    if (command === 'get_env_info') return { torchBackend: 'cuda' }
    throw new Error(`unexpected ${command}`)
  })
  const activation = app.activateRuntime('cuda', {}, { refreshCoreVersions: false })
  await Promise.resolve()
  const probe = app.checkEnv()
  await Promise.resolve()
  assert.deepEqual(calls, ['activate_runtime'])
  switching.resolve(true)
  await within(Promise.all([activation, probe]), 'environment probes after activation')
  assert.equal(app.envInfo.torchBackend, 'cuda')
  assert.equal(app.envLoading, false)
})

for (const command of ['debug_runtime_override_active', 'debug_runtime_write_file', 'debug_runtime_restore_file']) {
  test(`${command} invalidates old probes before mutating and refreshes the active environment`, async () => {
    const mutation = deferred()
    const persisted = deferred()
    const calls = []
    let oldRequestId
    const cuda = { torchAvailable: true, torchBackend: 'cuda', cudaAvailable: true }
    const app = appStore((operation, args) => {
      calls.push(operation)
      if (operation === 'start_env_check') {
        oldRequestId = args.requestId
        return Promise.resolve({ started: true })
      }
      if (operation === command) return mutation.promise
      if (operation === 'get_env_info') return Promise.resolve(cuda)
      if (operation === 'runtime_info') return Promise.resolve({ ready: true, installedBackend: 'cuda' })
      if (operation === 'save_app_store') { persisted.resolve(args.data); return Promise.resolve() }
      throw new Error(`unexpected ${operation}`)
    })
    const settings = useSettingsStore()
    settings.initialized = true
    settings.defaultDevice = 'cuda:1'
    const stop = watch(() => app.envInfo, env => {
      if (env?.torchAvailable) settings.reconcileRuntimeDevice(env)
    })
    try {
      await app.checkEnvInBackground()
      const change = app.updateDebugRuntime(command, { path: 'active-runtime.json' })
      await Promise.resolve()
      app.handleWorkerEvent({ type: 'env_info', requestId: oldRequestId, payload: { torchAvailable: true, torchBackend: 'cpu' } })
      await nextTick()
      assert.equal(app.envInfo, null)
      assert.equal(app.envLoading, true)
      mutation.resolve({ files: [] })
      assert.deepEqual(await within(change, 'runtime debug mutation'), { files: [] })
      app.handleWorkerEvent({ type: 'env_info', requestId: oldRequestId, payload: { torchAvailable: true, torchBackend: 'cpu' } })
      await nextTick()

      assert.equal(settings.defaultDevice, 'cuda:1')
      assert.equal(app.envInfo.torchBackend, 'cuda')
      assert.equal(app.envLoading, false)
      assert.equal((await within(persisted.promise, 'debug device preference')).defaultDevice, 'cuda:1')
      assert.deepEqual(calls.filter(operation => operation !== 'save_app_store'), ['start_env_check', command, 'runtime_info', 'get_env_info'])
    } finally {
      stop()
      settings.$dispose()
      app.$dispose()
    }
  })
}

test('debug runtime mutation failures release the queue and pending-probe loading state', async () => {
  const calls = []
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'debug_runtime_restore_file') throw new Error('restore failed')
    if (command === 'get_env_info') return { torchBackend: 'cpu' }
    throw new Error(`unexpected ${command}`)
  })
  await assert.rejects(app.updateDebugRuntime('debug_runtime_restore_file', { path: 'active-runtime.json' }), /restore failed/)
  assert.equal(app.envLoading, false)
  await within(app.checkEnv(), 'probe after failed debug mutation')
  assert.equal(app.envInfo.torchBackend, 'cpu')
  assert.deepEqual(calls, ['debug_runtime_restore_file', 'get_env_info'])
})

test('debug runtime saves report successful writes and retain a failed environment probe for diagnostics', async () => {
  const app = appStore(async command => {
    if (command === 'debug_runtime_write_file') return { files: [{ path: 'pyvenv.cfg' }] }
    if (command === 'runtime_info') return { ready: false }
    if (command === 'get_env_info') throw new Error('invalid venv configuration')
    throw new Error(`unexpected ${command}`)
  })
  assert.deepEqual(await app.updateDebugRuntime('debug_runtime_write_file', {}), { files: [{ path: 'pyvenv.cfg' }] })
  assert.equal(app.runtimeInfo.ready, false)
  assert.equal(app.lastError, 'invalid venv configuration')
  assert.equal(app.envLoading, false)
})

test('all runtime writes wait behind an active probe and retain FIFO order', async () => {
  const probe = deferred()
  const calls = []
  let runtimeInfoCalls = 0
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'runtime_info') {
      runtimeInfoCalls += 1
      if (runtimeInfoCalls === 1) return probe.promise
      return { ready: true }
    }
    if (command === 'get_env_info') return {}
    if (command === 'runtime_core_versions') return {}
    return true
  })

  const activeProbe = app.checkRuntimeInfo()
  await Promise.resolve()
  const install = app.installRuntime('cpu')
  const update = app.updateRuntimeCore('cpu')
  const activate = app.activateRuntime('cpu', {}, { refreshCoreVersions: false })
  const remove = app.deleteRuntime('cpu')
  await Promise.resolve()
  assert.deepEqual(calls, ['runtime_info'])

  probe.resolve({ ready: true })
  await activeProbe
  const installId = await within(install, 'queued install start')
  app.handleRuntimeEvent({ type: 'error', taskId: installId, payload: { message: 'install stopped' } })
  const updateId = await within(update, 'queued update start')
  app.handleRuntimeEvent({ type: 'error', taskId: updateId, payload: { message: 'update stopped' } })
  await within(activate, 'queued activation')
  await within(remove, 'queued deletion')

  assert.deepEqual(calls.filter(command => [
    'runtime_info', 'start_runtime_install', 'start_runtime_core_update', 'activate_runtime', 'delete_runtime',
  ].includes(command)).slice(0, 5), [
    'runtime_info',
    'start_runtime_install',
    'start_runtime_core_update',
    'activate_runtime',
    'delete_runtime',
  ])
})

test('background start returns after ACK but holds following queries until its terminal event', async () => {
  const calls = []
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'start_runtime_install') return true
    if (command === 'runtime_info') return { ready: true }
    if (command === 'get_env_info' || command === 'runtime_core_versions') return {}
    throw new Error(`unexpected ${command}`)
  })

  const taskId = await within(app.installRuntime('cpu'), 'install ACK')
  const query = app.checkRuntimeInfo()
  await Promise.resolve()
  assert.deepEqual(calls, ['start_runtime_install'])
  app.handleRuntimeEvent({ type: 'error', taskId, payload: { message: 'install failed' } })
  await within(query, 'query after install error')
  assert.ok(calls.includes('runtime_info'))
})

test('a failed start releases the write queue for a following query', async () => {
  const calls = []
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'start_runtime_install') throw new Error('start failed')
    if (command === 'runtime_info') return { ready: true }
    throw new Error(`unexpected ${command}`)
  })

  const install = app.installRuntime('cpu')
  const query = app.checkRuntimeInfo()
  await assert.rejects(install, /start failed/)
  await within(query, 'query after failed start')
  assert.deepEqual(calls, ['start_runtime_install', 'runtime_info'])
})

test('cancelling a queued install does not dispatch its start IPC and does not block later probes', async () => {
  const probe = deferred()
  const calls = []
  let infoCalls = 0
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'runtime_info') {
      infoCalls += 1
      return infoCalls === 1 ? probe.promise : { ready: true }
    }
    if (command === 'cancel_runtime_install') return true
    if (command === 'get_env_info' || command === 'runtime_core_versions') return {}
    return true
  })

  const running = app.checkRuntimeInfo()
  await Promise.resolve()
  const pendingInstall = app.installRuntime('cpu')
  assert.equal(await app.cancelRuntimeInstall(), true)
  probe.resolve({ ready: true })
  await running
  await pendingInstall.catch(() => undefined)
  await within(app.checkRuntimeInfo(), 'probe after queued cancellation')
  assert.equal(calls.includes('start_runtime_install'), false)
  assert.equal(calls.includes('cancel_runtime_install'), false)
})

test('successful cancellation after start ACK releases a background operation without an event', async () => {
  const calls = []
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'start_runtime_install' || command === 'cancel_runtime_install') return true
    if (command === 'runtime_info') return { ready: true }
    throw new Error(`unexpected ${command}`)
  })

  await within(app.installRuntime('cpu'), 'install ACK')
  const query = app.checkRuntimeInfo()
  assert.equal(await app.cancelRuntimeInstall(), true)
  await within(query, 'query after cancellation ACK')
  assert.deepEqual(calls, ['start_runtime_install', 'cancel_runtime_install', 'runtime_info'])
})

test('cancellation requested before a start ACK waits for the ACK before sending cancel', async () => {
  const startAck = deferred()
  const calls = []
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'start_runtime_install') return startAck.promise
    if (command === 'cancel_runtime_install') return true
    if (command === 'runtime_info') return { ready: true }
    throw new Error(`unexpected ${command}`)
  })

  const start = app.installRuntime('cpu')
  await pause()
  const cancel = app.cancelRuntimeInstall()
  await Promise.resolve()
  assert.deepEqual(calls, ['start_runtime_install'])
  startAck.resolve(true)
  await within(start, 'install ACK after deferred start')
  assert.equal(await within(cancel, 'cancel after start ACK'), true)
  assert.deepEqual(calls, ['start_runtime_install', 'cancel_runtime_install'])
})

test('a terminal event for another task does not release a background write', async () => {
  let queried = false
  const app = appStore(async command => {
    if (command === 'start_runtime_install') return true
    if (command === 'runtime_info') {
      queried = true
      return { ready: true }
    }
    if (command === 'get_env_info' || command === 'runtime_core_versions') return {}
    throw new Error(`unexpected ${command}`)
  })

  const taskId = await app.installRuntime('cpu')
  const query = app.checkRuntimeInfo()
  app.handleRuntimeEvent({ type: 'error', taskId: 'another-task', payload: { message: 'other failed' } })
  await Promise.resolve()
  assert.equal(queried, false)
  app.handleRuntimeEvent({ type: 'task_cancelled', taskId, payload: {} })
  await within(query, 'query after matching terminal event')
  assert.equal(queried, true)
})

test('a failed synchronous write releases the queue for a following runtime probe', async () => {
  const calls = []
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'activate_runtime') throw new Error('activation failed')
    if (command === 'runtime_info') return { ready: true }
    throw new Error(`unexpected ${command}`)
  })

  const activation = app.activateRuntime('cpu', {}, { refreshCoreVersions: false })
  const query = app.checkRuntimeInfo()
  await assert.rejects(activation, /activation failed/)
  await within(query, 'probe after failed activation')
  assert.deepEqual(calls, ['activate_runtime', 'runtime_info'])
})

test('synchronous writes refresh outside their queue item and do not self-deadlock', async () => {
  const calls = []
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'activate_runtime' || command === 'delete_runtime') return true
    if (command === 'runtime_info') return { ready: true }
    if (command === 'get_env_info' || command === 'runtime_core_versions') return {}
    throw new Error(`unexpected ${command}`)
  })

  await within(app.activateRuntime('cpu', {}, { refreshCoreVersions: false }), 'activation refresh')
  await within(app.deleteRuntime('cpu'), 'deletion refresh')
  assert.equal(calls[0], 'activate_runtime')
  assert.equal(calls.filter(command => command === 'runtime_info').length, 2)
  assert.ok(calls.includes('get_env_info'))
  assert.ok(calls.indexOf('delete_runtime') > calls.indexOf('activate_runtime'))
})

test('runtime env-size scan prevents a queued write from starting', async () => {
  const sizes = deferred()
  const calls = []
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'runtime_env_sizes') return sizes.promise
    if (command === 'activate_runtime') return true
    if (command === 'runtime_info') return { ready: true }
    if (command === 'get_env_info') return {}
    throw new Error(`unexpected ${command}`)
  })

  const scan = app.loadRuntimeEnvSizes()
  await Promise.resolve()
  const activation = app.activateRuntime('cpu', {}, { refreshCoreVersions: false })
  await Promise.resolve()
  assert.deepEqual(calls, ['runtime_env_sizes'])
  sizes.resolve({ sizes: { cpu: 1 } })
  await within(scan, 'env-size scan')
  await within(activation, 'activation after size scan')
})

test('core-update ACK returns before its terminal success, then refreshes without self-locking', async () => {
  const calls = []
  const refreshed = deferred()
  const versionsRefreshed = deferred()
  let infoCalls = 0
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'start_runtime_core_update') return true
    if (command === 'runtime_info') {
      if (++infoCalls === 2) refreshed.resolve()
      return { ready: true }
    }
    if (command === 'runtime_core_versions') { versionsRefreshed.resolve(); return {} }
    if (command === 'get_env_info') return {}
    throw new Error(`unexpected ${command}`)
  })

  const taskId = await within(app.updateRuntimeCore('cpu'), 'core update ACK')
  const blockedQuery = app.checkRuntimeInfo()
  await Promise.resolve()
  assert.deepEqual(calls, ['start_runtime_core_update'])
  app.handleRuntimeEvent({ type: 'runtime_core_update_finished', taskId, payload: {} })
  await within(blockedQuery, 'query after core update success')
  await within(refreshed.promise, 'automatic post-update runtime refresh')
  await within(versionsRefreshed.promise, 'version discovery after runtime refresh')
  assert.equal(app.runtimeCoreUpdateTaskId, null)
  assert.ok(calls.filter(command => command === 'runtime_info').length >= 2)
  assert.ok(calls.includes('runtime_core_versions'))
})

const preparedCorePlan = {
  id: '0123456789abcdef0123456789abcdef', backend: 'cpu', pythonPath: '/runtime/bin/python',
  mode: 'update', channel: 'stable', mirror: 'pypi',
  currentVersions: { pymss: '2.1.8', 'pymss-core': '0.1.11' },
  targetVersions: { pymss: '2.1.9', 'pymss-core': '0.1.11' }, dependencyCount: 0,
}

test('discarding a prepared plan releases prepared state without starting an installation', async () => {
  const calls = []
  const app = appStore(async command => { calls.push(command); return true })
  const pending = app.prepareRuntimeCoreUpdate('cpu', 'pypi', '', { mode: 'repair' })
  app.handleRuntimeEvent({ type: 'runtime_core_update_plan', taskId: app.runtimeCoreUpdateTaskId, payload: preparedCorePlan })
  const plan = await pending
  assert.equal(app.runtimeCoreUpdateStatus, 'prepared')
  assert.equal(app.discardRuntimeCorePlan(plan.id), true)
  assert.equal(app.runtimeCoreUpdateStatus, 'idle')
  assert.equal(app.discardRuntimeCorePlan(plan.id), false)
  await app.checkRuntimeInfo()
  assert.equal(calls.filter(command => command === 'start_runtime_core_update').length, 1)
  app.$dispose()
})

test('discarding an old plan cannot clear a newer prepared plan or an active application', async () => {
  const app = appStore(async command => {
    if (command === 'runtime_info') return { platform: 'win32', ready: true }
    return true
  })
  const first = app.prepareRuntimeCoreUpdate('cpu', 'pypi', '', { mode: 'repair' })
  app.handleRuntimeEvent({ type: 'runtime_core_update_plan', taskId: app.runtimeCoreUpdateTaskId, payload: preparedCorePlan })
  await first
  const nextPlan = { ...preparedCorePlan, id: 'abcdef0123456789abcdef0123456789' }
  const second = app.prepareRuntimeCoreUpdate('cpu', 'pypi', '', { mode: 'repair' })
  app.handleRuntimeEvent({ type: 'runtime_core_update_plan', taskId: app.runtimeCoreUpdateTaskId, payload: nextPlan })
  await second
  assert.equal(app.discardRuntimeCorePlan(preparedCorePlan.id), false)
  assert.equal(app.runtimeCoreUpdateStatus, 'prepared')
  const applyId = await app.updateRuntimeCore('cpu', 'pypi', '', { planId: nextPlan.id })
  assert.equal(app.discardRuntimeCorePlan(nextPlan.id), false)
  assert.equal(app.runtimeCoreUpdateStatus, 'updating')
  app.handleRuntimeEvent({ type: 'task_cancelled', taskId: applyId })
  await app.checkRuntimeInfo()
  app.$dispose()
})

test('a core operation invalidates version comparisons that were already in flight', async () => {
  const oldComparison = deferred()
  const app = appStore(async command => {
    if (command === 'runtime_core_versions') return oldComparison.promise
    if (command === 'start_runtime_core_update') return true
    if (command === 'runtime_info') return { platform: 'win32', ready: true }
    throw new Error(`unexpected ${command}`)
  })
  app.runtimeInfo = { platform: 'win32', pymssVersion: '2.1.8' }
  const old = app.loadRuntimeCoreVersions()
  const taskId = await app.updateRuntimeCore('cuda')
  oldComparison.resolve({ packages: { pymss: { currentVersion: '2.1.8', latestVersion: '2.1.9', updateAvailable: true } } })
  await old
  assert.equal(app.runtimeCoreVersions, null)
  app.handleRuntimeEvent({ type: 'task_cancelled', taskId })
  await app.checkRuntimeInfo()
  app.$dispose()
})

test('post-update version discovery waits for the refreshed runtime snapshot', async () => {
  const env = deferred()
  const comparison = deferred()
  const app = appStore(async (command, args) => {
    if (command === 'start_runtime_core_update') return true
    if (command === 'get_env_info') return env.promise
    if (command === 'runtime_info') return { platform: 'win32', ready: true, pymssVersion: '2.1.9', pymssCoreVersion: '0.1.11' }
    if (command === 'runtime_core_versions') { comparison.resolve(args.payload); return {} }
    throw new Error(`unexpected ${command}`)
  })
  app.runtimeInfo = { platform: 'win32', pymssVersion: '2.1.8', pymssCoreVersion: '0.1.11' }
  const taskId = await app.updateRuntimeCore('cuda')
  app.handleRuntimeEvent({ type: 'runtime_core_update_finished', taskId, payload: {} })
  env.resolve({ pymssVersion: '2.1.9' })
  const payload = await within(comparison.promise, 'version comparison after refreshed runtime')
  assert.equal(payload.versions.pymss, '2.1.9')
  app.$dispose()
})

test('core planning releases the write queue for confirmation and apply forwards the confirmed plan', async () => {
  const calls = []
  const dispatched = deferred()
  const app = appStore(async (command, args) => {
    calls.push({ command, args })
    if (command === 'start_runtime_core_update') { dispatched.resolve(); return true }
    if (command === 'runtime_info') return { ready: true }
    if (command === 'get_env_info' || command === 'runtime_core_versions') return {}
    throw new Error(`unexpected ${command}`)
  })
  const preparation = app.prepareRuntimeCoreUpdate('cpu', 'pypi', 'zh-CN', { mode: 'update' })
  await within(dispatched.promise, 'plan dispatch')
  const taskId = app.runtimeCoreUpdateTaskId
  assert.equal(app.runtimeCoreUpdateStatus, 'preparing')
  assert.equal(calls[0].args.payload.prepareOnly, true)
  app.handleRuntimeEvent({ type: 'runtime_core_update_plan', taskId, payload: preparedCorePlan })
  const plan = await within(preparation, 'prepared plan result')
  assert.deepEqual(plan, preparedCorePlan)
  assert.equal(app.runtimeCoreUpdateStatus, 'prepared')
  await within(app.checkRuntimeInfo(), 'read while waiting for user confirmation')
  const applyId = await app.updateRuntimeCore('cpu', 'pypi', 'zh-CN', { planId: plan.id, mode: plan.mode })
  const apply = calls.filter(call => call.command === 'start_runtime_core_update')[1]
  assert.equal(apply.args.payload.planId, plan.id)
  assert.equal(apply.args.payload.prepareOnly, undefined)
  app.handleRuntimeEvent({ type: 'runtime_core_update_finished', taskId: applyId, payload: {} })
  await within(app.checkRuntimeInfo(), 'post-activation queries')
  app.$dispose()
})

test('a plan completion before its start ACK settles the waiter without dispatching queries early', async () => {
  const started = deferred()
  const ack = deferred()
  const calls = []
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'start_runtime_core_update') { started.resolve(); return ack.promise }
    if (command === 'runtime_info') return { ready: true }
    throw new Error(`unexpected ${command}`)
  })
  const preparation = app.prepareRuntimeCoreUpdate('cpu', 'auto', '', { mode: 'sync' })
  await within(started.promise, 'early plan dispatch')
  app.handleRuntimeEvent({ type: 'runtime_core_update_plan', taskId: app.runtimeCoreUpdateTaskId, payload: preparedCorePlan })
  await within(preparation, 'plan completion before ACK')
  const query = app.checkRuntimeInfo()
  await Promise.resolve()
  assert.deepEqual(calls, ['start_runtime_core_update'])
  ack.resolve(true)
  await within(query, 'query after plan ACK')
  app.$dispose()
})

for (const terminal of ['error', 'task_cancelled']) {
  test(`planning ${terminal} rejects its waiter and releases following queries`, async () => {
    const started = deferred()
    const app = appStore(async command => {
      if (command === 'start_runtime_core_update') { started.resolve(); return true }
      if (command === 'runtime_info') return { ready: true }
      throw new Error(`unexpected ${command}`)
    })
    const preparation = app.prepareRuntimeCoreUpdate('cpu', 'auto', '', { mode: 'update' })
    const rejected = assert.rejects(preparation, terminal === 'error' ? /plan failed/ : /cancelled/)
    await within(started.promise, 'failed plan dispatch')
    app.handleRuntimeEvent({ type: terminal, taskId: app.runtimeCoreUpdateTaskId, payload: { message: 'plan failed' } })
    await within(rejected, 'plan rejection')
    await within(app.checkRuntimeInfo(), 'query after plan rejection')
    assert.equal(app.runtimeCoreUpdateTaskId, null)
    if (terminal === 'task_cancelled') {
      assert.equal(app.runtimeCoreUpdateStatus, 'cancelled')
      assert.equal(app.runtimeCoreUpdateError, null)
      assert.equal(app.runtimeCoreUpdateMessage, '')
    }
    app.$dispose()
  })
}

for (const diagnostics of [{}, { detail: 'Original traceback', manifestStatus: 'newer', runtimeManifestVersion: 'future' }, { details: { manifestStatus: 'unknown' }, extra: { appManifestVersion: '2026-10' } }]) {
  test(`planning preserves worker error codes and diagnostics: ${JSON.stringify(diagnostics)}`, async () => {
    const dispatched = deferred()
    const app = appStore(async command => {
      if (command === 'start_runtime_core_update') { dispatched.resolve(); return true }
      if (command === 'runtime_info') return { ready: true }
      throw new Error(`unexpected ${command}`)
    })
    const preparation = app.prepareRuntimeCoreUpdate('cpu', 'auto', 'zh-CN', { mode: 'update' })
    const rejected = assert.rejects(preparation, error => {
      assert.equal(error.code, 'RUNTIME_MANIFEST_INCOMPATIBLE')
      assert.equal(error.message, 'Manifest is newer or unknown')
      assert.deepEqual(error.diagnostics, diagnostics)
      assert.equal(error, app.runtimeCoreUpdateError)
      return true
    })
    await within(dispatched.promise, 'manifest error dispatch')
    app.handleRuntimeEvent({ type: 'error', taskId: app.runtimeCoreUpdateTaskId, payload: {
      code: 'RUNTIME_MANIFEST_INCOMPATIBLE', message: 'Manifest is newer or unknown', ...diagnostics,
    } })
    await within(rejected, 'manifest error rejection')
    assert.equal(app.runtimeCoreUpdateStatus, 'error')
    await app.checkRuntimeInfo()
    app.$dispose()
  })
}

test('structured IPC failure is retained when the start command cannot dispatch', async () => {
  const app = appStore(async () => { throw { code: 'RUNTIME_PERMISSION_DENIED', message: 'Access denied', detail: 'Cannot write state file' } })
  await assert.rejects(app.updateRuntimeCore('cpu'), error => {
    assert.equal(error.code, 'RUNTIME_PERMISSION_DENIED')
    assert.equal(error.diagnostics.detail, 'Cannot write state file')
    return true
  })
  assert.equal(app.runtimeCoreUpdateError.code, 'RUNTIME_PERMISSION_DENIED')
  assert.equal(app.runtimeCoreUpdateTaskId, null)
  app.$dispose()
})

test('late start failure cannot replace an earlier worker error or the next core operation', async () => {
  const firstDispatched = deferred()
  const secondDispatched = deferred()
  const firstAck = deferred()
  let calls = 0
  const app = appStore(async command => {
    if (command === 'start_runtime_core_update') {
      if (++calls === 1) { firstDispatched.resolve(); return firstAck.promise }
      secondDispatched.resolve()
      return true
    }
    if (command === 'runtime_info') return { ready: true }
    throw new Error(`unexpected ${command}`)
  })
  const first = app.prepareRuntimeCoreUpdate('cpu', 'auto', '', { mode: 'update' })
  const rejected = assert.rejects(first, error => error.code === 'RUNTIME_MANIFEST_INCOMPATIBLE')
  await firstDispatched.promise
  const firstTaskId = app.runtimeCoreUpdateTaskId
  app.handleRuntimeEvent({ type: 'error', taskId: firstTaskId, payload: { code: 'RUNTIME_MANIFEST_INCOMPATIBLE', message: 'Manifest incompatible' } })
  await rejected
  const second = app.prepareRuntimeCoreUpdate('cpu', 'auto', '', { mode: 'sync' })
  const secondTaskId = app.runtimeCoreUpdateTaskId
  firstAck.reject(new Error('late IPC failure'))
  await within(secondDispatched.promise, 'next operation after late ACK failure')
  app.handleRuntimeEvent({ type: 'error', taskId: firstTaskId, payload: { code: 'RUNTIME_CORE_UPDATE_FAILED', message: 'late worker failure' } })
  assert.equal(app.runtimeCoreUpdateTaskId, secondTaskId)
  assert.equal(app.runtimeCoreUpdateStatus, 'preparing')
  assert.equal(app.runtimeCoreUpdateError, null)
  assert.equal(app.runtimeCoreUpdateMessage, '')
  app.handleRuntimeEvent({ type: 'runtime_core_update_plan', taskId: secondTaskId, payload: preparedCorePlan })
  await within(second, 'second core plan')
  await app.checkRuntimeInfo()
  app.$dispose()
})

test('channel changes invalidate discovery and late replies cannot overwrite the new channel', async () => {
  const stable = deferred()
  const preview = deferred()
  const requests = []
  const app = appStore(async (command, args) => {
    assert.equal(command, 'runtime_core_versions')
    requests.push(args.payload)
    return args.payload.channel === 'stable' ? stable.promise : preview.promise
  })
  const settings = useSettingsStore()
  app.runtimeInfo = { platform: 'win32', pymssVersion: '2.2.0rc1', pymssCoreVersion: '0.1.11' }
  const old = app.loadRuntimeCoreVersions()
  settings.updateChannel = 'prerelease'
  await nextTick()
  preview.resolve({ channel: 'prerelease', packages: { pymss: { latestVersion: '2.3.0rc1', currentVersion: '2.2.0rc1', updateAvailable: true } } })
  await within(app.ensureRuntimeCoreVersions(), 'new channel discovery')
  stable.resolve({ channel: 'stable', packages: { pymss: { latestVersion: '2.2.0' } } })
  await old
  assert.equal(app.runtimeCoreVersions.channel, 'prerelease')
  assert.equal(app.runtimeCoreVersions.packages.pymss.latestVersion, '2.3.0rc1')
  assert.equal(requests.length, 2)
  assert.equal(requests[0].versions.pymss, '2.2.0rc1')
  app.$dispose()
  settings.$dispose()
})

test('successful core activation immediately updates the active Windows path alias before probes complete', async () => {
  const pending = deferred()
  const app = appStore(async command => command === 'start_runtime_core_update' ? true : pending.promise)
  app.runtimeInfo = {
    manifestVersion: '2026.10.2', installedBackend: 'cpu',
    installState: { pythonPath: 'C:/runtime/cpu/Scripts/python.exe' },
    installedEnvironments: [{ backend: 'cpu', pythonPath: '\\\\?\\C:\\runtime\\cpu\\Scripts\\python.exe', pymssVersion: '2.1.8' }],
  }
  const taskId = await app.updateRuntimeCore('cpu', 'pypi', '', { planId: preparedCorePlan.id })
  app.handleRuntimeEvent({ type: 'runtime_core_update_finished', taskId, payload: {
    state: { pythonPath: 'C:/runtime/cpu/Scripts/python.exe', pymssVersion: '2.1.9', pymssCoreVersion: '0.1.11' }, rollbackAvailable: true,
  } })
  assert.equal(app.runtimeInfo.installedEnvironments[0].pymssVersion, '2.1.9')
  assert.equal(app.runtimeInfo.rollbackAvailable, true)
  pending.resolve({})
  await within(app.checkRuntimeInfo(), 'post-activation probe drain')
  app.$dispose()
})

test('dependency repair reuses the core-update queue and forwards repair mode', async () => {
  const calls = []
  const app = appStore(async (command, args) => {
    calls.push({ command, args })
    if (command === 'start_runtime_core_update') return true
    if (command === 'runtime_info') return { ready: true }
    if (command === 'runtime_core_versions' || command === 'get_env_info') return {}
    throw new Error(`unexpected ${command}`)
  })

  const taskId = await app.updateRuntimeCore('cuda', 'pypi', 'zh-CN', {
    pythonPath: 'D:/runtime/cuda/Scripts/python.exe',
    repairDependencies: true,
  })

  assert.equal(app.runtimeCoreUpdateMode, 'repair')
  assert.deepEqual(calls[0], {
    command: 'start_runtime_core_update',
    args: {
      payload: {
        taskId,
        backend: 'cuda',
        mirror: 'pypi',
        locale: 'zh-CN',
        channel: 'stable',
        pythonPath: 'D:/runtime/cuda/Scripts/python.exe',
        repairDependencies: true,
      },
    },
  })
  app.handleRuntimeEvent({ type: 'runtime_core_update_finished', taskId, payload: {} })
})

test('queued core-update cancellation skips start IPC and clears its task state', async () => {
  const probe = deferred()
  const calls = []
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'runtime_info') return probe.promise
    if (command === 'runtime_core_versions' || command === 'get_env_info') return {}
    return true
  })

  const running = app.checkRuntimeInfo()
  await Promise.resolve()
  const pending = app.updateRuntimeCore('cpu')
  assert.equal(await app.cancelRuntimeCoreUpdate(), true)
  probe.resolve({ ready: true })
  await running
  await within(pending, 'cancelled core update acknowledgement')
  assert.equal(calls.includes('start_runtime_core_update'), false)
  assert.equal(calls.includes('cancel_runtime_core_update'), false)
  assert.equal(app.runtimeCoreUpdateTaskId, null)
})

test('active core-update cancellation ACK releases the queue when its event is absent', async () => {
  const calls = []
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'start_runtime_core_update' || command === 'cancel_runtime_core_update') return true
    if (command === 'runtime_info') return { ready: true }
    throw new Error(`unexpected ${command}`)
  })

  await within(app.updateRuntimeCore('cpu'), 'core update ACK')
  const query = app.checkRuntimeInfo()
  assert.equal(await app.cancelRuntimeCoreUpdate(), true)
  await within(query, 'query after core cancellation ACK')
  assert.equal(app.runtimeCoreUpdateTaskId, null)
  assert.deepEqual(calls, ['start_runtime_core_update', 'cancel_runtime_core_update', 'runtime_info', 'runtime_info'])
})

test('background start waits for worker-event registration before dispatching', async () => {
  const registered = deferred()
  const calls = []
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'start_runtime_install') return true
    throw new Error(`unexpected ${command}`)
  })
  // appStore restores the default stub, so set the controlled registration after it.
  __setRegisterWorkerEventsForTest(() => registered.promise)

  const start = app.installRuntime('cpu')
  await Promise.resolve()
  assert.deepEqual(calls, [])
  registered.resolve()
  const taskId = await within(start, 'install after event registration')
  assert.deepEqual(calls, ['start_runtime_install'])
  app.handleRuntimeEvent({ type: 'task_cancelled', taskId, payload: {} })
})

test('worker-event registration failure prevents dispatch and releases the queue', async () => {
  const calls = []
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'runtime_info') return { ready: true }
    throw new Error(`unexpected ${command}`)
  })
  __setRegisterWorkerEventsForTest(async () => { throw new Error('listener registration failed') })

  const start = app.installRuntime('cpu')
  const query = app.checkRuntimeInfo()
  await assert.rejects(start, /listener registration failed/)
  await within(query, 'query after registration failure')
  assert.deepEqual(calls, ['runtime_info'])
})

test('local cancellation while worker-event registration is pending dispatches neither start nor cancel', async () => {
  const registered = deferred()
  const calls = []
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'runtime_info') return { ready: true }
    throw new Error(`unexpected ${command}`)
  })
  __setRegisterWorkerEventsForTest(() => registered.promise)

  const start = app.installRuntime('cpu')
  await Promise.resolve()
  assert.equal(await app.cancelRuntimeInstall(), true)
  assert.equal(calls.length, 0)
  registered.resolve()
  await within(start, 'locally cancelled install acknowledgement')
  await within(app.checkRuntimeInfo(), 'query after registration-time cancellation')
  assert.deepEqual(calls, ['runtime_info'])
})

test('a core-update completion received before start ACK releases the queue', async () => {
  const startAck = deferred()
  const calls = []
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'start_runtime_core_update') return startAck.promise
    if (command === 'runtime_info') return { ready: true }
    if (command === 'runtime_core_versions' || command === 'get_env_info') return {}
    throw new Error(`unexpected ${command}`)
  })

  const update = app.updateRuntimeCore('cpu')
  await pause()
  assert.deepEqual(calls, ['start_runtime_core_update'])
  const taskId = app.runtimeCoreUpdateTaskId
  app.handleRuntimeEvent({ type: 'runtime_core_update_finished', taskId, payload: {} })
  startAck.resolve(true)
  await within(update, 'core update ACK after early completion')
  await within(app.checkRuntimeInfo(), 'query after early completion')
  assert.equal(app.runtimeCoreUpdateTaskId, null)
})


test('business task failures leave environment diagnostics healthy', () => {
  const app = appStore(async () => undefined)
  app.envInfo = { pythonVersion: '3.12.10', pymssAvailable: true, torchAvailable: true }
  app.runtimeInfo = { ready: true }
  app.handleWorkerEvent({ type: 'error', taskId: 'bad-input', payload: { code: 'INVALID_INPUT', message: 'Input file does not exist' } })

  assert.equal(app.lastError, 'Input file does not exist')
  assert.equal(app.envError, null)
  const checks = buildDiagnosticChecks({ env: app.envInfo, runtime: app.runtimeInfo, envError: app.envError, connection: 'connected', isMac: false })
  assert.equal(checks.find(check => check.key === 'runtime').state, 'ok')
})

test('a direct environment failure remains separate from later task failures and clears on recovery', async () => {
  let failing = true
  const env = { pythonVersion: '3.12.10', pymssAvailable: true, torchAvailable: true }
  const app = appStore(async command => {
    assert.equal(command, 'get_env_info')
    if (failing) throw new Error('Interpreter failed to start')
    return env
  })
  await assert.rejects(app.checkEnv(), /Interpreter failed to start/)
  assert.equal(app.envError, 'Interpreter failed to start')

  app.handleWorkerEvent({ type: 'error', taskId: 'bad-input', payload: { code: 'INVALID_INPUT', message: 'Input file does not exist' } })
  assert.equal(app.lastError, 'Input file does not exist')
  assert.equal(app.envError, 'Interpreter failed to start')
  failing = false
  await app.checkEnv()
  assert.equal(app.envError, null)
  assert.deepEqual(app.envInfo, env)
})

test('background environment failures clear only when the matching request recovers', async () => {
  const ids = []
  const app = appStore(async (command, args) => {
    assert.equal(command, 'start_env_check')
    ids.push(args.requestId)
  })
  await app.checkEnvInBackground()
  app.handleWorkerEvent({ type: 'error', requestId: ids[0], payload: { code: 'ENV_CHECK_FAILED', message: 'Environment import failed' } })
  assert.equal(app.envError, 'Environment import failed')

  await app.checkEnvInBackground()
  assert.equal(app.envError, null)
  app.handleWorkerEvent({ type: 'error', requestId: ids[0], payload: { code: 'ENV_CHECK_FAILED', message: 'Obsolete failure' } })
  assert.equal(app.envError, null)
  assert.equal(app.envLoading, true)
  app.handleWorkerEvent({ type: 'env_info', requestId: ids[1], payload: { pythonVersion: '3.12.10' } })
  assert.equal(app.envError, null)
  assert.equal(app.envLoading, false)
})

test('background environment listener failures remain visible in environment diagnostics', async () => {
  const app = appStore(async () => assert.fail('no worker request should be sent'))
  __setRegisterWorkerEventsForTest(async () => { throw new Error('Listener registration failed') })
  await assert.rejects(app.checkEnvInBackground(), /Listener registration failed/)
  assert.equal(app.envError, 'Listener registration failed')
  assert.equal(app.envLoading, false)
})
