import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createPinia, setActivePinia } from 'pinia'

const vite = await createServer({
  configFile: false,
  server: { middlewareMode: true, hmr: false },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
  plugins: [{
    name: 'stub-worker-events-registration',
    enforce: 'pre',
    transform(_code, id) {
      if (!id.replaceAll('\\', '/').endsWith('/src/utils/events.ts')) return null
      return `
        let register = async () => undefined
        export function __setRegisterWorkerEventsForTest(next) { register = next }
        export function registerWorkerEvents() { return register() }
      `
    },
  }],
})
after(() => vite.close())

const { useAppStore } = await vite.ssrLoadModule('/src/stores/app.ts')
const { __setRegisterWorkerEventsForTest } = await vite.ssrLoadModule('/src/utils/events.ts')

function installTauriInvoke(invoke) {
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { __TAURI_INTERNALS__: { invoke } },
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
  const app = appStore(async command => {
    calls.push(command)
    if (command === 'start_runtime_core_update') return true
    if (command === 'runtime_info') return { ready: true }
    if (command === 'runtime_core_versions' || command === 'get_env_info') return {}
    throw new Error(`unexpected ${command}`)
  })

  const taskId = await within(app.updateRuntimeCore('cpu'), 'core update ACK')
  const blockedQuery = app.checkRuntimeInfo()
  await Promise.resolve()
  assert.deepEqual(calls, ['start_runtime_core_update'])
  app.handleRuntimeEvent({ type: 'runtime_core_update_finished', taskId, payload: {} })
  await within(blockedQuery, 'query after core update success')
  await Promise.resolve()
  assert.equal(app.runtimeCoreUpdateTaskId, null)
  assert.ok(calls.filter(command => command === 'runtime_info').length >= 2)
  assert.ok(calls.includes('runtime_core_versions'))
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
  assert.deepEqual(calls, ['start_runtime_core_update', 'cancel_runtime_core_update', 'runtime_info'])
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
