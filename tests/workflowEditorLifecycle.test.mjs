import assert from 'node:assert/strict'
import test, { after, afterEach } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, transformWithEsbuild } from 'vite'
import { parse, compileScript } from 'vue/compiler-sfc'
import { createRenderer, nextTick, watch } from 'vue'
import { createPinia } from 'pinia'

const fixturesId = '\0workflow-lifecycle-fixtures'
const stubs = {
  'vue-router': `export { useRoute, useRouter } from '${fixturesId}'`,
  'vue-i18n': 'export const useI18n = () => ({ t: key => key })',
  'naive-ui': 'export const darkTheme = {}; export const useMessage = () => ({}); export const useDialog = () => ({})',
  '@vicons/ionicons5': 'export const AlertCircleOutline={}, CheckmarkCircle={}, CubeOutline={}, EllipsisHorizontalOutline={}, GitNetworkOutline={}, MusicalNotesOutline={}, OpenOutline={}, PlayOutline={}, SearchOutline={}',
}
const vite = await createServer({
  configFile: false,
  server: { watch: null, middlewareMode: true, hmr: false, preTransformRequests: false },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  resolve: { alias: {
    ...Object.fromEntries(Object.keys(stubs).map(id => [id, `\0workflow-stub:${id}`])),
    '@': fileURLToPath(new URL('../src', import.meta.url)),
  } },
  plugins: [{
    name: 'workflow-lifecycle-boundaries',
    enforce: 'pre',
    resolveId(id) {
      if (id === fixturesId || id.startsWith('\0workflow-stub:')) return id
      return null
    },
    load(id) {
      if (id.startsWith('\0workflow-stub:')) return stubs[id.slice('\0workflow-stub:'.length)]
      if (id === fixturesId) return `
        import { reactive } from 'vue'
        export const route = reactive({ path: '/workflows', query: {} })
        export const navigation = []
        export const useRoute = () => route
        export const useRouter = () => ({ push: async target => { navigation.push(target) } })
      `
      return null
    },
    async transform(code, id) {
      const path = id.replaceAll('\\', '/')
      if (path.endsWith('/src/App.vue') || path.endsWith('/src/views/WorkflowsView.vue')) {
        const { descriptor } = parse(code, { filename: path })
        const script = compileScript(descriptor, { id: path })
        return transformWithEsbuild(script.content, `${path}.ts`, { loader: 'ts', tsconfigRaw: {} })
      }
      if (path.endsWith('.vue')) return 'export default { render: () => null }'
      if (path.endsWith('/src/stores/model.ts')) return `
        import { defineStore } from 'pinia'
        export const useModelStore = defineStore('model', { state: () => ({ downloadedModels: [] }) })
      `
      if (path.endsWith('/src/stores/settings.ts')) return 'export const useSettingsStore = () => ({ shouldShowStartupOnboarding: false })'
      if (path.endsWith('/src/stores/app.ts')) return 'export const useAppStore = () => ({ runtimeInfo: null, runtimeCoreVersions: null })'
      if (path.endsWith('/src/stores/update.ts')) return 'export const useUpdateStore = () => ({ shouldShowDeferred: false })'
      if (path.endsWith('/src/utils/theme.ts')) return `
        import { ref } from 'vue'
        export const themeIsDark = ref(false)
        export const getResolvedThemeTokens = () => ({})
        export const getThemeOverrides = () => ({})
      `
      if (path.endsWith('/src/utils/events.ts')) return 'export const connectWorkerEvents = async () => {}'
      return null
    },
  }],
})
after(() => vite.close())

const { useWorkflowStore, WorkflowRevisionConflictError } = await vite.ssrLoadModule('/src/stores/workflow.ts')
const App = (await vite.ssrLoadModule('/src/App.vue')).default
const WorkflowsView = (await vite.ssrLoadModule('/src/views/WorkflowsView.vue')).default
const { route, navigation } = await vite.ssrLoadModule(fixturesId)
App.render = WorkflowsView.render = () => null

const renderer = createRenderer({
  createComment: text => ({ text }),
  createText: text => ({ text }),
  createElement: tag => ({ tag, children: [] }),
  insert(node, parent) { node.parent = parent; parent.children.push(node) },
  remove(node) { node.parent.children = node.parent.children.filter(item => item !== node) },
  parentNode: node => node.parent,
  nextSibling: () => null,
  setText(node, text) { node.text = text },
  setElementText(node, text) { node.text = text },
  patchProp() {},
})
const mountedApps = new Set()
const stores = []
const flush = async () => { await nextTick(); await new Promise(resolve => setImmediate(resolve)); await nextTick() }
const closeEvent = kind => `pymss://workflow-${kind === 'advanced' ? 'node' : 'simple'}-editor-closed`
function deferred() {
  let resolve
  let reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function entry(id, updatedAt = 16, format = 'simple') {
  return {
    id, name: id, description: `${id} description`, format, formatVersion: 1,
    createdAt: 10, updatedAt,
    definition: format === 'simple'
      ? { version: 1, steps: [{ id: 'vocals', model: 'model.ckpt', input: 'input', stems: ['vocals'], save: { vocals: 'vocals' } }] }
      : { version: 1, nodes: [], links: [] },
  }
}

function environment() {
  let stored = { workflows: [entry('original')], selectedWorkflowId: 'original' }
  let readHook
  let listenHook
  const reads = []
  const writes = []
  const callbacks = new Map()
  const listeners = new Map()
  let callbackId = 0
  globalThis.window = {
    setTimeout: () => 0,
    __TAURI_INTERNALS__: {
      transformCallback(handler) { callbacks.set(++callbackId, handler); return callbackId },
      async invoke(command, args) {
        if (command === 'load_app_store') {
          assert.equal(args.name, 'workflow-state')
          reads.push(args.name)
          return readHook ? readHook() : structuredClone(stored)
        }
        if (command === 'save_app_store') {
          assert.equal(args.name, 'workflow-state')
          stored = structuredClone(args.data)
          writes.push(stored)
          return null
        }
        if (command === 'plugin:event|listen') {
          if (listenHook) await listenHook()
          listeners.set(args.handler, { event: args.event, handler: callbacks.get(args.handler) })
          return args.handler
        }
        if (command === 'plugin:event|unlisten') {
          listeners.delete(args.eventId)
          return null
        }
        throw new Error(`Unexpected IPC command: ${command}`)
      },
    },
    __TAURI_EVENT_PLUGIN_INTERNALS__: { unregisterListener() {} },
  }
  route.path = '/workflows'
  navigation.length = 0
  const pinia = createPinia()
  const store = useWorkflowStore(pinia)
  stores.push(store)
  return {
    store, reads, writes, listeners,
    get stored() { return stored },
    set stored(value) { stored = structuredClone(value) },
    set read(value) { readHook = value },
    set listen(value) { listenHook = value },
    dispatch(event, payload = {}) {
      for (const listener of [...listeners.values()]) {
        if (listener.event === event) listener.handler({ event, payload })
      }
    },
    mount(component) {
      const app = renderer.createApp(component)
      app.use(pinia)
      const vm = app.mount({ children: [] })
      mountedApps.add(app)
      return {
        state: vm.$.setupState,
        unmount() { app.unmount(); mountedApps.delete(app) },
      }
    },
  }
}

afterEach(async () => {
  for (const app of mountedApps) app.unmount()
  mountedApps.clear()
  await flush()
  for (const store of stores.splice(0)) store.$dispose()
  Reflect.deleteProperty(globalThis, 'window')
})

test('opening the overview only hydrates details, without rewriting 0.0.16 workflow data', async () => {
  const env = environment()
  const original = structuredClone(env.stored)
  await env.store.initialize()
  const page = env.mount(WorkflowsView)
  await flush()
  assert.equal(page.state.editingId, 'original')
  assert.equal(page.state.name, 'original')
  assert.deepEqual(env.writes, [])
  assert.deepEqual(env.stored, original)
})

test('each main-window close notification reads once and selects the newly saved workflow', async () => {
  for (const kind of ['advanced', 'simple']) {
    const env = environment()
    await env.store.initialize()
    const shell = env.mount(App)
    const page = env.mount(WorkflowsView)
    await flush()
    env.reads.length = env.writes.length = 0
    env.store[kind === 'advanced' ? 'markNodeEditorOpen' : 'markSimpleEditorOpen']('__new__')
    env.stored = { workflows: [entry('original'), entry('saved', 17, kind === 'simple' ? 'simple' : 'graph')], selectedWorkflowId: 'saved' }
    const saved = structuredClone(env.stored)
    env.dispatch(closeEvent(kind))
    await flush()
    assert.equal(env.reads.length, 1, kind)
    assert.equal(env.store.selectedWorkflowId, 'saved')
    assert.equal(page.state.editingId, 'saved')
    assert.equal(page.state.name, 'saved')
    assert.equal(page.state.description, 'saved description')
    assert.equal(env.store[kind === 'advanced' ? 'nodeEditorOpenWorkflowId' : 'simpleEditorOpenWorkflowId'], '')
    assert.deepEqual(env.writes, [])
    assert.deepEqual(env.stored, saved)
    page.unmount()
    shell.unmount()
    await flush()
    assert.equal(env.listeners.size, 0)
  }
})

test('standalone routes do not subscribe to main-window workflow events', async () => {
  for (const path of ['/editor', '/workflow-node-editor', '/workflow-simple-editor']) {
    const env = environment()
    route.path = path
    const shell = env.mount(App)
    await flush()
    assert.equal(env.listeners.size, 0, path)
    assert.equal(env.reads.length, 0)
    shell.unmount()
  }
})

test('a failed close refresh preserves the list and details and does not report success', async (t) => {
  const env = environment()
  await env.store.initialize()
  env.mount(App)
  const page = env.mount(WorkflowsView)
  await flush()
  env.reads.length = env.writes.length = 0
  env.store.markSimpleEditorOpen('original')
  const before = JSON.stringify(env.store.workflows)
  const failure = new Error('Workflow read failed')
  env.read = () => { throw failure }
  const warnings = t.mock.method(console, 'warn', () => {})
  env.dispatch(closeEvent('simple'))
  await flush()
  assert.equal(env.reads.length, 1)
  assert.equal(JSON.stringify(env.store.workflows), before)
  assert.equal(env.store.selectedWorkflowId, 'original')
  assert.equal(env.store.simpleEditorOpenWorkflowId, '')
  assert.equal(page.state.editingId, 'original')
  assert.equal(page.state.name, 'original')
  assert.deepEqual(env.writes, [])
  assert.equal(warnings.mock.calls.some(call => call.arguments.includes(failure)), true)
})

test('closing an existing editor refreshes metadata without changing saved definitions or timestamps', async () => {
  for (const kind of ['advanced', 'simple']) {
    const env = environment()
    await env.store.initialize()
    const page = env.mount(WorkflowsView)
    const updated = { ...entry('original', 42, kind === 'simple' ? 'simple' : 'graph'), name: 'Updated', description: 'Updated description' }
    env.stored = { workflows: [updated], selectedWorkflowId: 'original' }
    const saved = structuredClone(env.stored)
    const result = await env.store.handleEditorClosed(kind)
    await flush()
    assert.equal(result, env.store.selectedWorkflow)
    assert.equal(result.updatedAt, 42)
    assert.equal(result.createdAt, 10)
    assert.equal(page.state.name, 'Updated')
    assert.equal(page.state.description, 'Updated description')
    assert.deepEqual(env.stored, saved)
    assert.deepEqual(env.writes, [])
    page.unmount()
  }
})

test('the main window refreshes after close even when the overview is absent and no open marker exists', async () => {
  const env = environment()
  await env.store.initialize()
  const shell = env.mount(App)
  await flush()
  env.reads.length = 0
  env.stored = { workflows: [entry('saved', 17)], selectedWorkflowId: 'saved' }
  env.dispatch(closeEvent('simple'))
  await flush()
  assert.equal(env.reads.length, 1)
  assert.equal(env.store.selectedWorkflowId, 'saved')
  const page = env.mount(WorkflowsView)
  await flush()
  assert.equal(page.state.editingId, 'saved')
  assert.equal(env.reads.length, 1)
  assert.deepEqual(env.writes, [])
  page.unmount()
  shell.unmount()
})

test('a close with no saved changes retains the original entry and data', async () => {
  const env = environment()
  await env.store.initialize()
  const original = structuredClone(env.stored)
  const page = env.mount(WorkflowsView)
  env.store.markNodeEditorOpen('__new__')
  await env.store.handleEditorClosed('advanced')
  await flush()
  assert.equal(env.store.nodeEditorOpenWorkflowId, '')
  assert.equal(page.state.editingId, 'original')
  assert.deepEqual(env.stored, original)
  assert.deepEqual(env.writes, [])
})

test('a genuinely empty reloaded list clears the overview without writing an empty snapshot', async () => {
  const env = environment()
  await env.store.initialize()
  const page = env.mount(WorkflowsView)
  env.stored = { workflows: [], selectedWorkflowId: '' }
  assert.equal(await env.store.handleEditorClosed('simple'), null)
  await flush()
  assert.equal(page.state.editingId, '')
  assert.equal(page.state.name, '')
  assert.equal(page.state.description, '')
  assert.deepEqual(env.writes, [])
})

test('overlapping close notifications share a refresh with a trailing read and clear the relevant editor markers', async () => {
  for (const secondKind of ['simple', 'advanced']) {
    const env = environment()
    await env.store.initialize()
    const page = env.mount(WorkflowsView)
    env.store.markSimpleEditorOpen('original')
    env.store.markNodeEditorOpen('original')
    const gate = deferred()
    env.read = () => gate.promise
    env.reads.length = 0
    const first = env.store.handleEditorClosed('simple')
    const second = env.store.handleEditorClosed(secondKind)
    try {
      assert.equal(env.reads.length, 1)
      assert.equal(env.store.simpleEditorOpenWorkflowId, '')
      assert.equal(env.store.nodeEditorOpenWorkflowId, secondKind === 'advanced' ? '' : 'original')
    } finally {
      gate.resolve({ workflows: [entry('saved', 17)], selectedWorkflowId: 'saved' })
      const [left, right] = await Promise.all([first, second])
      assert.equal(left, right)
    }
    assert.equal(env.reads.length, 2)
    assert.equal(env.store.selectedWorkflowId, 'saved')
    assert.equal(page.state.editingId, 'saved')
    assert.deepEqual(env.writes, [])
    page.unmount()
  }
})

test('a save during a pending close refresh survives the next selection and persistence', async () => {
  for (const secondKind of ['simple', 'advanced']) {
    const env = environment()
    await env.store.initialize()
    const page = env.mount(WorkflowsView)
    const gate = deferred()
    env.stored = { workflows: [entry('original'), entry('saved-a', 17)], selectedWorkflowId: 'saved-a' }
    const firstSnapshot = structuredClone(env.stored)
    env.read = () => gate.promise
    env.reads.length = 0
    const first = env.store.handleEditorClosed('simple')
    env.stored = {
      workflows: [...firstSnapshot.workflows, entry('saved-b', 18)], selectedWorkflowId: 'saved-b',
    }
    const second = env.store.handleEditorClosed(secondKind)
    env.read = undefined
    gate.resolve(firstSnapshot)
    const [left, right] = await Promise.all([first, second])
    await flush()
    assert.equal(left.id, 'saved-b')
    assert.equal(left, right)
    assert.equal(env.reads.length, 2)
    assert.equal(page.state.editingId, 'saved-b')
    assert.deepEqual(env.writes, [])

    env.store.selectWorkflow('original')
    await flush()
    assert.equal(env.writes.length, 1)
    assert.deepEqual(env.stored.workflows.map(item => item.id).sort(), ['original', 'saved-a', 'saved-b'])
    assert.equal(env.stored.workflows.find(item => item.id === 'saved-b').updatedAt, 18)
    page.unmount()
  }
})

test('close refresh discards obsolete snapshots and keeps reading when another editor closes during the trailing read', async () => {
  const env = environment()
  await env.store.initialize()
  const original = JSON.stringify(env.store.workflows)
  const firstGate = deferred()
  const secondGate = deferred()
  env.reads.length = 0
  env.read = () => env.reads.length === 1 ? firstGate.promise : secondGate.promise
  const completed = []
  const stop = env.store.$onAction(({ name, after }) => {
    if (name === 'handleEditorClosed') after(result => completed.push(result.id))
  })
  const first = env.store.handleEditorClosed('simple')
  const second = env.store.handleEditorClosed('advanced')
  firstGate.resolve({ workflows: [entry('saved-a', 17)], selectedWorkflowId: 'saved-a' })
  await flush()
  assert.equal(env.reads.length, 2)
  assert.equal(JSON.stringify(env.store.workflows), original)
  assert.equal(env.store.selectedWorkflowId, 'original')
  assert.deepEqual(completed, [])

  env.stored = { workflows: [entry('original'), entry('saved-c', 19)], selectedWorkflowId: 'saved-c' }
  const third = env.store.handleEditorClosed('simple')
  env.read = undefined
  secondGate.resolve({ workflows: [entry('saved-b', 18)], selectedWorkflowId: 'saved-b' })
  const results = await Promise.all([first, second, third])
  assert.equal(env.reads.length, 3)
  assert.deepEqual(results.map(item => item.id), ['saved-c', 'saved-c', 'saved-c'])
  assert.deepEqual(completed, ['saved-c', 'saved-c', 'saved-c'])
  assert.deepEqual(env.writes, [])
  stop()
})

test('a failed trailing close read preserves the original state and saved data and permits a fresh retry', async () => {
  const env = environment()
  await env.store.initialize()
  const original = JSON.stringify(env.store.workflows)
  const firstGate = deferred()
  const failure = new Error('Read denied')
  env.reads.length = 0
  env.read = () => env.reads.length === 1 ? firstGate.promise : Promise.reject(failure)
  const successes = []
  const stop = env.store.$onAction(({ name, after }) => {
    if (name === 'handleEditorClosed') after(result => successes.push(result.id))
  })
  const first = assert.rejects(env.store.handleEditorClosed('simple'), error => error === failure)
  env.stored = { workflows: [entry('original'), entry('saved-b', 18)], selectedWorkflowId: 'saved-b' }
  const saved = structuredClone(env.stored)
  const second = assert.rejects(env.store.handleEditorClosed('advanced'), error => error === failure)
  firstGate.resolve({ workflows: [entry('saved-a', 17)], selectedWorkflowId: 'saved-a' })
  await Promise.all([first, second])
  assert.equal(env.reads.length, 2)
  assert.equal(JSON.stringify(env.store.workflows), original)
  assert.equal(env.store.selectedWorkflowId, 'original')
  assert.deepEqual(env.stored, saved)
  assert.deepEqual(env.writes, [])
  assert.deepEqual(successes, [])
  env.read = undefined
  assert.equal((await env.store.handleEditorClosed('simple')).id, 'saved-b')
  assert.deepEqual(successes, ['saved-b'])
  assert.deepEqual(env.stored, saved)
  stop()
})

test('a close arriving as the previous snapshot is published starts a fresh refresh', async () => {
  const env = environment()
  await env.store.initialize()
  env.reads.length = 0
  env.stored = { workflows: [entry('original'), entry('saved-a', 17)], selectedWorkflowId: 'saved-a' }
  let nextRefresh
  const stop = watch(() => env.store.selectedWorkflowId, (id) => {
    if (id !== 'saved-a') return
    env.stored = { workflows: [...env.stored.workflows, entry('saved-b', 18)], selectedWorkflowId: 'saved-b' }
    nextRefresh = env.store.handleEditorClosed('advanced')
  })
  try {
    await env.store.handleEditorClosed('simple')
    await flush()
    assert.ok(nextRefresh)
    assert.equal((await nextRefresh).id, 'saved-b')
    assert.equal(env.reads.length, 2)
    assert.equal(env.store.selectedWorkflowId, 'saved-b')
    assert.deepEqual(env.writes, [])
  } finally {
    stop()
  }
})

test('failed shared reads reject all close actions, skip success subscribers and allow retry', async () => {
  const env = environment()
  await env.store.initialize()
  const gate = deferred()
  env.read = () => gate.promise
  const successes = []
  const failures = []
  const stop = env.store.$onAction(({ name, after, onError }) => {
    if (name !== 'handleEditorClosed') return
    after(result => successes.push(result))
    onError(error => failures.push(error))
  })
  const failure = new Error('Read denied')
  const first = assert.rejects(env.store.handleEditorClosed('simple'), error => error === failure)
  const second = assert.rejects(env.store.handleEditorClosed('advanced'), error => error === failure)
  gate.reject(failure)
  await Promise.all([first, second])
  assert.deepEqual(successes, [])
  assert.deepEqual(failures, [failure, failure])
  assert.equal(env.store.selectedWorkflowId, 'original')
  assert.deepEqual(env.writes, [])
  env.read = undefined
  assert.equal((await env.store.handleEditorClosed('simple')).id, 'original')
  assert.equal(successes.length, 1)
  stop()
})

test('malformed workflow records do not replace the previous list during a close refresh', async () => {
  const env = environment()
  await env.store.initialize()
  const before = JSON.stringify(env.store.workflows)
  for (const malformed of [
    { workflows: {}, selectedWorkflowId: 'other' },
    { workflows: [entry('other')], selectedWorkflowId: { toString: 42 } },
  ]) {
    env.read = async () => malformed
    await assert.rejects(env.store.handleEditorClosed('simple'), TypeError)
    assert.equal(JSON.stringify(env.store.workflows), before)
    assert.equal(env.store.selectedWorkflowId, 'original')
    assert.deepEqual(env.writes, [])
  }
})

test('an overview unmounted during refresh does not run a queued completion callback', async () => {
  const env = environment()
  await env.store.initialize()
  const page = env.mount(WorkflowsView)
  const gate = deferred()
  env.read = () => gate.promise
  const pending = env.store.handleEditorClosed('simple')
  page.unmount()
  page.state.name = 'Detached'
  gate.resolve({ workflows: [entry('saved', 17)], selectedWorkflowId: 'saved' })
  await pending
  await flush()
  assert.equal(page.state.name, 'Detached')
  env.read = undefined
  await env.store.handleEditorClosed('advanced')
  assert.equal(page.state.name, 'Detached')
  assert.deepEqual(env.writes, [])
})

test('main-window listeners that finish registering after unmount are released', async () => {
  const env = environment()
  const gate = deferred()
  env.listen = () => gate.promise
  const shell = env.mount(App)
  shell.unmount()
  gate.resolve()
  await flush()
  assert.equal(env.listeners.size, 0)
  env.dispatch(closeEvent('simple'))
  assert.equal(env.reads.length, 0)
})

test('a main window temporarily displaying a standalone route ignores close events', async () => {
  const env = environment()
  await env.store.initialize()
  env.mount(App)
  await flush()
  env.reads.length = 0
  route.path = '/workflow-simple-editor'
  env.dispatch(closeEvent('simple'))
  env.dispatch('pymss://workflow-simple-editor-action', { action: 'run', workflowId: 'original' })
  await flush()
  assert.equal(env.reads.length, 0)
  assert.deepEqual(navigation, [])
})

test('revision conflicts still reject saves without changing data or closing the editor', async () => {
  const env = environment()
  await env.store.initialize()
  env.store.markSimpleEditorOpen('original')
  const saved = structuredClone(env.stored)
  await assert.rejects(env.store.saveWorkflow({
    id: 'original', name: 'Stale edit', definition: entry('original').definition, expectedUpdatedAt: 1,
  }), WorkflowRevisionConflictError)
  assert.deepEqual(env.stored, saved)
  assert.deepEqual(env.writes, [])
  assert.equal(env.store.simpleEditorOpenWorkflowId, 'original')
})

test('the main-window run action still reloads, selects the requested workflow and navigates', async () => {
  const env = environment()
  await env.store.initialize()
  env.mount(App)
  const page = env.mount(WorkflowsView)
  await flush()
  env.reads.length = 0
  env.stored = { workflows: [entry('original'), entry('saved', 17)], selectedWorkflowId: 'original' }
  env.dispatch('pymss://workflow-simple-editor-action', { action: 'run', workflowId: 'saved' })
  await flush()
  assert.equal(env.reads.length, 1)
  assert.equal(env.store.selectedWorkflowId, 'saved')
  assert.equal(page.state.editingId, 'saved')
  assert.deepEqual(navigation, [{ path: '/', query: { mode: 'workflow' } }])
  assert.equal(env.writes.length, 1)
  assert.equal(env.stored.selectedWorkflowId, 'saved')
  assert.deepEqual(env.stored.workflows.map(item => item.id).sort(), ['original', 'saved'])
})
