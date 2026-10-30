import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'
import { reactive } from 'vue'

const sdkContract = JSON.parse(readFileSync(new URL('../python/plugin-sdk.json', import.meta.url), 'utf8'))
const sdkExports = {}
vm.runInNewContext(ts.transpileModule(
  readFileSync(new URL('../src/features/plugin-marketplace/sdkContract.ts', import.meta.url), 'utf8'),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, esModuleInterop: true } },
).outputText, {
  exports: sdkExports,
  require: name => {
    assert.equal(name, '../../../python/plugin-sdk.json')
    return sdkContract
  },
})

const descriptor = parse(readFileSync(new URL('../src/features/plugin-marketplace/PluginViewHost.vue', import.meta.url), 'utf8')).descriptor
const source = ts.createSourceFile('host.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const names = new Set([
  'allowed', 'handleCall', 'postToFrame', 'onWindowMessage', 'syncBusy',
  'pluginSettingsKey', 'plainObject', 'boundedPath', 'boundedPaths',
  'cancelOwned', 'settleRun', 'closeRunningOperations', 'startBackground', 'workerError', 'isAllowedExternalUrl',
  'pushTheme',
])
const snippets = source.statements.flatMap(node => {
  if (ts.isFunctionDeclaration(node) && names.has(node.name?.text)) return [node.getText(source)]
  if (ts.isClassDeclaration(node) && ['TaskCancelledError', 'WorkerRequestError'].includes(node.name?.text || '')) return [node.getText(source)]
  if (ts.isVariableStatement(node) && node.declarationList.declarations.some(item => ['disposed', 'unregisterCloseGuard', 'MIN_VIEW_HEIGHT', 'MAX_VIEW_HEIGHT', 'THEME_TOKENS', 'sdkInfo'].includes(item.name.getText(source)))) return [node.getText(source)]
  if (ts.isExpressionStatement(node) && ts.isCallExpression(node.expression)) {
    const call = node.expression
    if (['onMounted', 'onBeforeUnmount'].includes(call.expression.getText(source))) {
      return [`const ${call.expression.getText(source)}Callback = ${call.arguments[0].getText(source)}`]
    }
  }
  return []
})
const code = ts.transpileModule(snippets.join('\n'), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

function host(overrides = {}) {
  const messages = []
  let closeGuard
  let closeGuardUnregistered = 0
  const state = {
    ...sdkExports,
    disposed: false, runRequests: new Map(), unlistenWorker: undefined, themeObserver: undefined,
    props: { pluginName: 'gain', pluginsRoot: '/plugins', view: { id: 'editor', entry: 'ui/index.html', sdkVersion: sdkContract.version } },
    t: (key, params) => `${key}: ${JSON.stringify(params)}`,
    permissions: () => ['operations'],
    frame: { value: { contentWindow: { postMessage: value => messages.push(structuredClone(value)) } } },
    src: { value: '' }, status: { value: 'loading' }, errorText: { value: '' }, measuredHeight: { value: 640 }, overlayOpen: { value: false },
    window: { addEventListener() {}, removeEventListener() {} }, document: { documentElement: { classList: { contains: () => false } } }, getComputedStyle: () => ({ getPropertyValue: () => '' }),
    MutationObserver: class { observe() {} disconnect() {} },
    getThemeOverrides: () => ({}),
    onWindowMessage() {}, pushTheme() {},
    pluginViewUrl: () => 'http://pymss-plugin.localhost/view/gain/editor/index.html', convertFileSrc() {},
    listen: async () => () => {}, invoke: async () => {}, open: async () => null,
    settings: { outputDir: '/output', modelDir: '/models' }, app: { runtimeInfo: { platform: 'win32', backend: 'cpu', packageVersions: { pymss: '2.1.12' } } }, locale: { value: 'en' }, emit() {},
    navigator: { clipboard: { writeText: async () => {} } }, openExternal: async () => {},
    registerWindowCloseGuard: guard => { closeGuard = guard; return () => { closeGuardUnregistered++ } }, ...overrides,
  }
  const controller = vm.runInNewContext(`${code}\n({handleCall, postToFrame, onWindowMessage, close: closeRunningOperations, mount: onMountedCallback, unmount: onBeforeUnmountCallback})`, state)
  return { state, controller, messages, close: () => closeGuard(), closeGuardUnregistered: () => closeGuardUnregistered }
}

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

test('native operation start failure removes its pending SDK request', async () => {
  const failure = new Error('worker unavailable')
  const { state, controller } = host({ invoke: async () => { throw failure } })
  await assert.rejects(controller.handleCall(1, 'operations.start', {}), error => error === failure)
  assert.equal(state.runRequests.size, 0)
})

test('file, application, and fixed-namespace settings SDK calls use validated host inputs', async () => {
  const calls = []
  const { controller } = host({
    permissions: () => ['files', 'settings', 'app'],
    locale: { value: 'zh-CN' }, settings: { outputDir: '/exports' },
    open: async options => {
      calls.push(['open', options])
      return options.directory ? '/music' : options.multiple ? ['/a.wav', '/b.wav'] : '/a.wav'
    },
    invoke: async (command, args) => {
      calls.push([command, args])
      if (command === 'get_build_info') return { version: '1.2.3' }
      if (command === 'scan_audio_paths_with_options') return { files: args.paths }
      if (command === 'load_plugin_tool_settings') return { previous: true }
    },
  })
  assert.equal(JSON.stringify(await controller.handleCall(1, 'files.pickAudio', { multiple: true })), JSON.stringify({ paths: ['/a.wav', '/b.wav'] }))
  assert.equal(JSON.stringify(await controller.handleCall(2, 'files.pickDirectory', {})), JSON.stringify({ path: '/music' }))
  assert.equal(JSON.stringify(await controller.handleCall(3, 'files.scanAudio', { paths: ['/a.wav'], recursive: false })), JSON.stringify({ files: ['/a.wav'] }))
  await controller.handleCall(4, 'files.reveal', { path: '/a.wav' })
  assert.equal(JSON.stringify(await controller.handleCall(5, 'app.info', {})), JSON.stringify({
    studio: 'pymss-studio', version: '1.2.3', locale: 'zh-CN',
    sdkVersion: sdkContract.version, capabilities: sdkContract.capabilities, defaultOutputDir: '/exports',
    runtimeInfo: { platform: 'win32', packages: { pymss: '2.1.12' } },
  }))
  assert.equal(JSON.stringify(await controller.handleCall(6, 'settings.read', {})), JSON.stringify({ previous: true }))
  assert.equal(JSON.stringify(await controller.handleCall(7, 'settings.write', { data: { mode: 'fast' } })), JSON.stringify({ mode: 'fast' }))
  assert.equal(JSON.stringify(calls.find(([name]) => name === 'save_plugin_tool_settings')), JSON.stringify([
    'save_plugin_tool_settings', { pluginName: 'gain', viewId: 'editor', data: { mode: 'fast' } },
  ]))
  await assert.rejects(controller.handleCall(8, 'files.scanAudio', { paths: [''] }), /INVALID_PATHS|INVALID_PATH/)
})

test('unknown SDK capabilities stay unavailable even when the view grants their family', async () => {
  const calls = []
  const { controller } = host({
    permissions: () => ['files', 'files.delete'],
    invoke: async (...args) => { calls.push(args) },
  })
  await assert.rejects(controller.handleCall(1, 'files.delete', { path: '/a.wav' }), /UNSUPPORTED_CAPABILITY: files\.delete/)
  await assert.rejects(controller.handleCall(2, 'operations.describe', { operation: 'plugin.convert' }), /PERMISSION_DENIED: operations\.describe/)
  assert.deepEqual(calls, [])
})

test('unsupported required capabilities prevent mounting before native setup', async () => {
  const calls = []
  const { state, controller } = host({ invoke: async (...args) => { calls.push(args) } })
  state.props.view.requiredCapabilities = ['files.delete']
  await controller.mount()
  assert.equal(state.status.value, 'error')
  assert.match(state.errorText.value, /sdkCapabilitiesUnavailable.*files\.delete/)
  assert.equal(state.src.value, '')
  assert.deepEqual(calls, [])
})

test('app.info initialization replies transport reactive runtime packages across the iframe boundary', async () => {
  const runtimeInfo = reactive({
    platform: 'win32', machine: 'AMD64', torchBackend: 'cuda',
    packageVersions: { pymss: '2.1.12', torch: '2.7.1+cu128' },
  })
  const { state, controller, messages } = host({
    permissions: () => ['app.info'], app: { runtimeInfo },
    invoke: async () => ({ version: '0.0.12' }),
  })
  controller.onWindowMessage({
    source: state.frame.value.contentWindow,
    data: { jsonrpc: '2.0', id: 61, method: 'app.info', params: {} },
  })
  await new Promise(setImmediate)
  const response = messages.find(message => message.id === 61)
  assert.equal(response?.error, undefined)
  assert.deepEqual(response?.result.runtimeInfo.packages, { pymss: '2.1.12', torch: '2.7.1+cu128' })
  runtimeInfo.packageVersions.pymss = 'updated'
  assert.equal(response.result.runtimeInfo.packages.pymss, '2.1.12')
})

test('only the current iframe can apply a bounded content height notification', () => {
  const { state, controller } = host()
  controller.onWindowMessage({ source: {}, data: { jsonrpc: '2.0', method: 'pymss.resize', params: { height: 920 } } })
  assert.equal(state.measuredHeight.value, 640)
  controller.onWindowMessage({ source: state.frame.value.contentWindow, data: { jsonrpc: '2.0', method: 'pymss.resize', params: { height: 920.4 } } })
  assert.equal(state.measuredHeight.value, 920)
  for (const height of [119, 20_001, Infinity, NaN, '720']) {
    controller.onWindowMessage({ source: state.frame.value.contentWindow, data: { jsonrpc: '2.0', method: 'pymss.resize', params: { height } } })
    assert.equal(state.measuredHeight.value, 920)
  }
})

test('a closed host ignores late resize notifications', () => {
  const { state, controller } = host()
  controller.unmount()
  controller.onWindowMessage({ source: state.frame.value.contentWindow, data: { jsonrpc: '2.0', method: 'pymss.resize', params: { height: 960 } } })
  assert.equal(state.measuredHeight.value, 640)
})

test('theme forwarding includes the legacy tool panel tokens', () => {
  const { state, controller, messages } = host({
    getComputedStyle: () => ({ getPropertyValue: token => ({ '--primary-border': '#abc', '--shadow-panel': '0 1px', '--font-mono': 'monospace' })[token] || '' }),
  })
  // A ready signal reaches the same source-checked path and may forward theme.
  controller.onWindowMessage({ source: state.frame.value.contentWindow, data: { jsonrpc: '2.0', method: 'pymss.ready' } })
  assert.deepEqual(messages.at(-1)?.params.cssVariables, { '--primary-border': '#abc', '--shadow-panel': '0 1px', '--font-mono': 'monospace' })
})

test('progress and recovery replies snapshot nested reactive values without losing task metadata', () => {
  const { controller, messages } = host()
  const update = reactive({ id: 31, fraction: 0.5, event: { type: 'audio_tool_progress', payload: { inputs: ['/audio.wav'] } } })
  const failure = reactive({ code: 'ASR_MODEL_INCOMPLETE', recovery: { modelIds: ['selected-model'] } })
  controller.postToFrame({ jsonrpc: '2.0', method: 'task.progress', params: update })
  controller.postToFrame({ jsonrpc: '2.0', id: 31, error: { code: -32000, data: failure } })
  assert.equal(messages[0].params.id, 31)
  assert.deepEqual(messages[0].params.event.payload.inputs, ['/audio.wav'])
  assert.equal(messages[1].error.data.code, 'ASR_MODEL_INCOMPLETE')
  assert.deepEqual(messages[1].error.data.recovery.modelIds, ['selected-model'])
})

test('host SDK allowlists native audio and runtime tasks with owned terminal events', async () => {
  let workerListener
  const calls = []
  const { controller, state } = host({
    permissions: () => ['audio', 'runtime', 'models', 'files', 'clipboard', 'shell', 'app'],
    listen: async (_event, callback) => { workerListener = callback; return () => {} },
    invoke: async (command, args) => {
      calls.push([command, args])
      if (command.startsWith('start_') || command === 'manage_optional_runtime_package') return { taskId: args.payload.taskId }
      if (command === 'optional_runtime_package_status') return { package: 'funasr', installed: true }
    },
  })
  await controller.mount()
  await assert.rejects(controller.handleCall(1, 'audio.analyze', { payload: { operation: 'merge' } }), /AUDIO_ANALYSIS_NOT_ALLOWED/)
  const audio = controller.handleCall(2, 'audio.analyze', { payload: { operation: 'tempo_key', inputs: ['/a.wav'] } })
  const [audioId] = state.runRequests.keys()
  workerListener({ payload: { type: 'audio_tool_progress', taskId: audioId, payload: { completed: 1 } } })
  workerListener({ payload: { type: 'audio_tool_result', taskId: audioId, payload: { items: [] } } })
  assert.deepEqual(await audio, { items: [] })

  const packageTask = controller.handleCall(3, 'runtime.managePackage', { payload: { package: 'funasr', action: 'install' } })
  const [packageId] = state.runRequests.keys()
  workerListener({ payload: { type: 'optional_package_status', taskId: packageId, payload: { package: 'funasr', installed: true } } })
  assert.deepEqual(await packageTask, { package: 'funasr', installed: true })
  assert.deepEqual(await controller.handleCall(4, 'runtime.packageStatus', { package: 'funasr' }), { package: 'funasr', installed: true })
  await assert.rejects(controller.handleCall(5, 'runtime.packageStatus', { package: 'numpy' }), /PACKAGE_NOT_ALLOWED/)
  await controller.handleCall(6, 'models.deleteTool', { payload: { tool: 'asr', id: 'model' } })
  await assert.rejects(controller.handleCall(7, 'models.deleteTool', { payload: { tool: 'midi' } }), /TOOL_MODEL_NOT_ALLOWED/)
  assert.ok(calls.some(([command]) => command === 'start_tempo_key_analysis'))
})

test('only the iframe operation owner can cancel after the native start acknowledgement', async () => {
  const start = deferred()
  const calls = []
  const { controller, state } = host({
    permissions: () => ['operations'],
    invoke: (command, args) => {
      calls.push([command, args])
      return command === 'start_operation_task' ? start.promise : Promise.resolve(true)
    },
  })
  const running = controller.handleCall(41, 'operations.start', { operation: 'plugin.convert' })
  await assert.rejects(controller.handleCall(99, 'operations.cancel', { id: 99 }), /OPERATION_NOT_OWNED/)
  const cancelling = controller.handleCall(42, 'operations.cancel', { id: 41 })
  assert.equal(calls.filter(([name]) => name === 'cancel_task').length, 0)
  start.resolve({ taskId: 'native-task' })
  await new Promise(setImmediate)
  assert.equal(calls.filter(([name]) => name === 'cancel_task').length, 1)
  assert.equal(JSON.stringify(calls.find(([name]) => name === 'cancel_task')), JSON.stringify(['cancel_task', { taskId: 'native-task' }]))
  assert.equal(await cancelling, true)
  const [requestId] = state.runRequests.keys()
  state.runRequests.get(requestId).reject(new Error('Cancelled'))
  await assert.rejects(running, /Cancelled/)
})

test('concurrent cancellation requests share one native cancellation', async () => {
  const start = deferred()
  const calls = []
  const { controller, state } = host({
    permissions: () => ['operations'],
    invoke: (command, args) => {
      calls.push([command, args])
      return command === 'start_operation_task' ? start.promise : Promise.resolve(true)
    },
  })
  const running = controller.handleCall(51, 'operations.start', { operation: 'plugin.convert' })
  const first = controller.handleCall(52, 'operations.cancel', { id: 51 })
  const second = controller.handleCall(53, 'operations.cancel', { id: 51 })
  start.resolve({ taskId: 'shared-native-task' })
  assert.equal(await first, true)
  assert.equal(await second, true)
  assert.equal(calls.filter(([name]) => name === 'cancel_task').length, 1)
  const [requestId] = state.runRequests.keys()
  state.runRequests.get(requestId).resolve({ done: true })
  assert.deepEqual(await running, { done: true })
})

test('a rejected cancellation leaves the original operation pending for its real terminal event', async () => {
  const start = deferred()
  let workerListener
  const { controller, state } = host({
    permissions: () => ['operations'],
    listen: async (_event, callback) => { workerListener = callback; return () => {} },
    invoke: (command, args) => {
      if (command === 'start_operation_task') return start.promise
      if (command === 'cancel_task') return Promise.reject(new Error('cancel transport failed'))
      return Promise.resolve(undefined)
    },
  })
  await controller.mount()
  const running = controller.handleCall(61, 'operations.start', { operation: 'plugin.convert' })
  const cancelling = controller.handleCall(62, 'operations.cancel', { id: 61 })
  const [requestId] = state.runRequests.keys()
  start.resolve({ taskId: requestId })
  await assert.rejects(cancelling, /cancel transport failed/)
  assert.equal(state.runRequests.size, 1)
  workerListener({ payload: { type: 'operation_result', taskId: requestId, payload: { done: true } } })
  assert.deepEqual(await running, { done: true })
})

test('window close waits through a pending start acknowledgement, cancellation, and terminal event', async () => {
  const start = deferred()
  const calls = []
  let workerListener
  const { controller, state, close } = host({
    permissions: () => ['operations'],
    listen: async (_event, callback) => { workerListener = callback; return () => {} },
    invoke: (command, args) => {
      calls.push([command, args])
      return command === 'start_operation_task' ? start.promise : Promise.resolve(true)
    },
  })
  await controller.mount()
  const running = controller.handleCall(71, 'operations.start', { operation: 'plugin.convert' })
  const closing = close()
  await new Promise(setImmediate)
  assert.equal(calls.filter(([name]) => name === 'cancel_task').length, 0)
  const [requestId] = state.runRequests.keys()
  start.resolve({ taskId: requestId })
  await new Promise(setImmediate)
  assert.equal(calls.filter(([name]) => name === 'cancel_task').length, 1)
  let closed = false
  void closing.then(() => { closed = true })
  await new Promise(setImmediate)
  assert.equal(closed, false)
  workerListener({ payload: { type: 'task_cancelled', taskId: requestId, payload: { message: 'Stopped' } } })
  await assert.rejects(running, /Stopped/)
  await closing
})

test('window close rejects on cancellation transport failure without dropping the live request', async () => {
  const start = deferred()
  const { controller, state, close } = host({
    permissions: () => ['operations'],
    invoke: command => command === 'start_operation_task'
      ? start.promise
      : Promise.reject(new Error('cancel transport failed')),
  })
  const running = controller.handleCall(81, 'operations.start', { operation: 'plugin.convert' })
  const closing = close()
  const [requestId] = state.runRequests.keys()
  start.resolve({ taskId: requestId })
  await assert.rejects(closing, /cancel transport failed/)
  assert.equal(state.runRequests.size, 1)
  state.runRequests.get(requestId).resolve({ done: true })
  await running
})

test('task cancellation has a stable SDK error data code while normal errors do not', async () => {
  let workerListener
  const { controller, state, messages } = host({
    permissions: () => ['operations'],
    listen: async (_event, callback) => { workerListener = callback; return () => {} },
    invoke: async (command, args) => command === 'start_operation_task' ? { taskId: args.payload.requestId } : undefined,
  })
  await controller.mount()
  controller.onWindowMessage({ source: state.frame.value.contentWindow, data: {
    jsonrpc: '2.0', id: 7, method: 'operations.start', params: { operation: 'plugin.convert' },
  } })
  await new Promise(setImmediate)
  const cancelledRequest = [...state.runRequests.keys()][0]
  workerListener({ payload: { type: 'task_cancelled', taskId: cancelledRequest, payload: { message: 'Stopped' } } })
  await new Promise(setImmediate)
  const cancelled = messages.find(message => message.id === 7)
  assert.equal(cancelled.error.message, 'Stopped')
  assert.equal(cancelled.error.data.code, 'TASK_CANCELLED')

  controller.onWindowMessage({ source: state.frame.value.contentWindow, data: {
    jsonrpc: '2.0', id: 8, method: 'operations.start', params: { operation: 'plugin.convert' },
  } })
  await new Promise(setImmediate)
  const requestId = [...state.runRequests.keys()][0]
  workerListener({ payload: { type: 'error', requestId, payload: { message: 'Backend error' } } })
  await new Promise(setImmediate)
  const failure = messages.find(message => message.id === 8)
  assert.equal(failure.error.message, 'Backend error')
  assert.equal(failure.error.data.message, 'Backend error')
})

test('unmount releases a listener that resolves after the view is closed', async () => {
  const listener = deferred()
  const started = deferred()
  let unlistened = 0
  const { state, controller, closeGuardUnregistered } = host({ listen: () => { started.resolve(); return listener.promise } })
  const mounted = controller.mount()
  await started.promise
  assert.equal(state.src.value, '')
  controller.unmount()
  assert.equal(closeGuardUnregistered(), 1)
  listener.resolve(() => { unlistened++ })
  await mounted
  assert.equal(unlistened, 1)
  assert.equal(state.src.value, '')
})

test('listener setup failure becomes a view error', async () => {
  const { state, controller } = host({ listen: async () => { throw new Error('listen unavailable') } })
  await controller.mount()
  assert.equal(state.status.value, 'error')
  assert.match(state.errorText.value, /listen unavailable/)
  assert.equal(state.src.value, '')
})

test('the frame waits for the native plugin root and event listener', async () => {
  const root = deferred()
  const commands = []
  const { state, controller } = host({ invoke: (command, args) => {
    commands.push([command, args.root])
    return root.promise
  } })
  const mounted = controller.mount()
  assert.equal(state.src.value, '')
  root.resolve()
  await mounted
  assert.deepEqual(commands, [['set_plugin_view_root', '/plugins']])
  assert.match(state.src.value, /^http:/)
})

test('unmount rejects an operation while native startup is pending', async () => {
  const startup = deferred()
  const calls = []
  const { state, controller, messages } = host({ invoke: (command, args) => {
    calls.push([command, args])
    return command === 'start_operation_task' ? startup.promise : Promise.resolve(true)
  } })
  const running = controller.handleCall(1, 'operations.start', {})
  assert.equal(state.runRequests.size, 1)
  controller.unmount()
  assert.equal(state.runRequests.size, 0)
  controller.postToFrame({ result: 'late' })
  assert.deepEqual(messages, [])
  startup.resolve({ taskId: 'late-native-task' })
  await assert.rejects(running, /PLUGIN_VIEW_CLOSED/)
  await new Promise(setImmediate)
  assert.equal(JSON.stringify(calls.find(([name]) => name === 'cancel_task')), JSON.stringify([
    'cancel_task', { taskId: 'late-native-task' },
  ]))
})

test('the actual SDK correlates concurrent progress and waits for terminal responses', async () => {
  let workerListener
  const starts = []
  const { state, controller } = host({
    listen: async (_event, callback) => { workerListener = callback; return () => {} },
    invoke: async (command, args) => { if (command === 'start_operation_task') starts.push(args.payload) },
  })
  await controller.mount()
  const sdkWindow = { parent: { postMessage: message => {
    queueMicrotask(() => controller.onWindowMessage({ source: state.frame.value.contentWindow, data: message }))
  } } }
  let receive
  sdkWindow.addEventListener = (name, callback) => { if (name === 'message') receive = callback }
  state.frame.value.contentWindow.postMessage = message => queueMicrotask(() => receive({ data: message, source: sdkWindow.parent }))
  vm.runInNewContext(readFileSync(new URL('../src-tauri/resources/plugin-sdk/pymss-sdk.js', import.meta.url), 'utf8'), {
    window: sdkWindow,
    document: { documentElement: { style: { setProperty() {} }, dataset: {} }, dispatchEvent() {} },
    CustomEvent: class { constructor(_name, options) { this.detail = options.detail } },
  })
  await sdkWindow.pymss.ready()
  const progress = [[], []]
  let finished = false
  const first = sdkWindow.pymss.startOperation('test.first', {}, {}, value => { progress[0].push(value.fraction); throw new Error('plugin callback') })
    .then(value => { finished = true; return value })
  const second = sdkWindow.pymss.startOperation('test.second', {}, {}, value => progress[1].push(value.fraction))
  await new Promise(setImmediate)
  assert.equal(starts.length, 2)
  const emit = (index, type, payload) => workerListener({ payload: { type, requestId: starts[index].requestId, payload } })
  emit(1, 'operation_progress', { fraction: 0.75 })
  emit(0, 'operation_progress', { fraction: 0.25 })
  await new Promise(setImmediate)
  assert.deepEqual(progress, [[0.25], [0.75]])
  assert.equal(finished, false)
  emit(0, 'operation_result', { outputs: { value: 1 } })
  emit(1, 'operation_result', { outputs: { value: 2 } })
  assert.equal((await first).outputs.value, 1)
  assert.equal((await second).outputs.value, 2)
  emit(0, 'operation_progress', { fraction: 1 })
  await new Promise(setImmediate)
  assert.deepEqual(progress, [[0.25], [0.75]])
  assert.equal(state.runRequests.size, 0)
  controller.unmount()
})

test('only an authorized owning iframe can expand a modal and resize resumes after closing', () => {
  const denied = host()
  denied.controller.onWindowMessage({ source: denied.state.frame.value.contentWindow, data: { jsonrpc: '2.0', method: 'pymss.overlay', params: { open: true } } })
  assert.equal(denied.state.overlayOpen.value, false)
  const { state, controller } = host({ permissions: () => ['ui.overlay'] })
  const notify = (method, params, source = state.frame.value.contentWindow) => controller.onWindowMessage({ source, data: { jsonrpc: '2.0', method, params } })
  notify('pymss.overlay', { open: true }, {})
  assert.equal(state.overlayOpen.value, false)
  notify('pymss.overlay', { open: 'true' })
  assert.equal(state.overlayOpen.value, false)
  notify('pymss.overlay', { open: true })
  assert.equal(state.overlayOpen.value, true)
  notify('pymss.resize', { height: 900 })
  assert.equal(state.measuredHeight.value, 640)
  notify('pymss.overlay', { open: false })
  notify('pymss.resize', { height: 720 })
  assert.equal(state.measuredHeight.value, 720)
  controller.unmount()
  notify('pymss.overlay', { open: true })
  assert.equal(state.overlayOpen.value, false)
})
