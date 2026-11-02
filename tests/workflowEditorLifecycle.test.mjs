import assert from 'node:assert/strict'
import test, { after, afterEach } from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'
import { createServer, transformWithEsbuild } from 'vite'
import { parse, compileScript } from 'vue/compiler-sfc'
import { createRenderer, nextTick, watch } from 'vue'
import { createPinia } from 'pinia'

const fixturesId = '\0workflow-lifecycle-fixtures'
const stubs = {
  'vue-router': `export { useRoute, useRouter } from '${fixturesId}'`,
  'vue-i18n': 'export const useI18n = () => ({ t: key => key })',
  'naive-ui': `export const darkTheme = {}; export { useMessage } from '${fixturesId}'; export const useDialog = () => ({})`,
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
        export const messages = []
        export const useMessage = () => Object.fromEntries(['success', 'error', 'warning'].map(level => [level, text => messages.push({ level, text })]))
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
const { route, navigation, messages } = await vite.ssrLoadModule(fixturesId)
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
  let mutateHook
  let listenHook
  const reads = []
  const writes = []
  const exports = []
  const editorOpens = []
  let editorOpenError
  let nativeEditorSnapshot = null
  const callbacks = new Map()
  const listeners = new Map()
  let callbackId = 0
  globalThis.window = {
    setTimeout: () => 0,
    __TAURI_INTERNALS__: {
      metadata: { currentWindow: { label: 'main' } },
      transformCallback(handler) { callbacks.set(++callbackId, handler); return callbackId },
      async invoke(command, args) {
        if (command === 'get_workflow_editors') return structuredClone(nativeEditorSnapshot)
        if (command === 'load_app_store') {
          assert.equal(args.name, 'workflow-state')
          reads.push(args.name)
          return readHook ? readHook() : structuredClone(stored)
        }
        if (command === 'mutate_workflow_store') {
          const mutation = args.payload
          const target = mutation.entry?.id || mutation.workflowId
          if (['upsert', 'delete'].includes(mutation.action) && nativeEditorSnapshot?.editors.some(editor => editor.workflowId === target && editor.label !== 'main')) {
            throw new Error('WORKFLOW_EDITOR_IN_USE')
          }
          if (mutateHook) await mutateHook(mutation)
          const state = structuredClone(stored)
          state.workflows ||= []
          const isLegacyMatch = item => item
            && !String(item.id || '').trim()
            && (!mutation.legacyEntry || JSON.stringify(item) === JSON.stringify(mutation.legacyEntry))
          const findMutationIndex = workflowId => {
            const exactIndex = state.workflows.findIndex(item => item.id === workflowId)
            if (exactIndex >= 0) return exactIndex
            if (Number.isInteger(mutation.legacyIndex) && isLegacyMatch(state.workflows[mutation.legacyIndex])) {
              return mutation.legacyIndex
            }
            return mutation.legacyEntry ? state.workflows.findIndex(isLegacyMatch) : -1
          }
          if (mutation.action === 'upsert') {
            const index = findMutationIndex(mutation.entry.id)
            const legacyTargetMismatch = index < 0
              && (Number.isInteger(mutation.legacyIndex) || Boolean(mutation.legacyEntry))
            const existing = index >= 0 ? state.workflows[index] : null
            const actualUpdatedAt = existing?.updatedAt || 0
            if (!mutation.force && (
              legacyTargetMismatch
              || (mutation.expectedUpdatedAt !== undefined && mutation.expectedUpdatedAt !== actualUpdatedAt)
            )) {
              return {
                state,
                conflict: {
                  workflowId: mutation.entry.id,
                  expectedUpdatedAt: mutation.expectedUpdatedAt,
                  actualUpdatedAt,
                },
              }
            }
            const next = {
              ...structuredClone(mutation.entry),
              createdAt: existing?.createdAt || mutation.entry.createdAt,
              updatedAt: Math.max(mutation.entry.updatedAt, actualUpdatedAt + 1),
            }
            if (index >= 0) state.workflows[index] = next
            else state.workflows.push(next)
            state.workflows.sort((a, b) => b.updatedAt - a.updatedAt)
            state.selectedWorkflowId = next.id
          } else if (mutation.action === 'delete') {
            const index = findMutationIndex(mutation.workflowId)
            const actualUpdatedAt = index >= 0 ? state.workflows[index].updatedAt || 0 : 0
            if (
              (index < 0 && (Number.isInteger(mutation.legacyIndex) || mutation.legacyEntry))
              || (mutation.expectedUpdatedAt !== undefined
                && mutation.expectedUpdatedAt !== actualUpdatedAt)
            ) {
              return {
                state,
                conflict: {
                  workflowId: mutation.workflowId,
                  expectedUpdatedAt: mutation.expectedUpdatedAt ?? 0,
                  actualUpdatedAt,
                },
              }
            }
            if (index >= 0) state.workflows.splice(index, 1)
            if (state.selectedWorkflowId === mutation.workflowId) {
              state.selectedWorkflowId = state.workflows[0]?.id || ''
            }
          } else if (mutation.action === 'select') {
            const index = findMutationIndex(mutation.workflowId)
            if (!mutation.workflowId) {
              state.selectedWorkflowId = ''
            } else if (index >= 0) {
              state.workflows[index].id = mutation.workflowId
              state.selectedWorkflowId = mutation.workflowId
            } else if (Number.isInteger(mutation.legacyIndex) || mutation.legacyEntry) {
              return {
                state,
                conflict: {
                  workflowId: mutation.workflowId,
                  expectedUpdatedAt: 0,
                  actualUpdatedAt: 0,
                },
              }
            }
          } else if (mutation.action === 'dismiss_recovery') {
            if (state.recoveryBackupPath === mutation.recoveryBackupPath) delete state.recoveryBackupPath
          } else {
            throw new Error(`Unexpected workflow mutation: ${mutation.action}`)
          }
          stored = state
          writes.push(structuredClone(stored))
          return { state: structuredClone(stored) }
        }
        if (command === 'save_app_store') {
          assert.equal(args.name, 'workflow-state')
          stored = structuredClone(args.data)
          writes.push(stored)
          return null
        }
        if (command === 'open_workflow_simple_editor_window' || command === 'open_workflow_node_editor_window') {
          if (editorOpenError) throw editorOpenError
          editorOpens.push({ command, payload: args.payload })
          return { opened: true }
        }
        if (command === 'save_text_file_dialog') {
          exports.push(args)
          return args.defaultName
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
  messages.length = 0
  const pinia = createPinia()
  const store = useWorkflowStore(pinia)
  stores.push(store)
  return {
    store, reads, writes, listeners, exports, editorOpens,
    set editorOpenFailure(value) { editorOpenError = value },
    set nativeEditorSnapshot(value) { nativeEditorSnapshot = structuredClone(value) },
    get stored() { return stored },
    set stored(value) { stored = structuredClone(value) },
    set read(value) { readHook = value },
    set mutate(value) { mutateHook = value },
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
  Reflect.deleteProperty(globalThis, 'localStorage')
})

test('workflow recovery notice survives saves and reloads until acknowledged', async () => {
  const env = environment()
  const backupPath = 'C:/studio/settings/workflows.corrupt-original.json'
  env.stored = { workflows: [], selectedWorkflowId: '', recoveryBackupPath: backupPath }
  await env.store.initialize()
  assert.equal(env.store.recoveryBackupPath, backupPath)
  await env.store.saveWorkflow(entry('after-recovery'))
  await env.store.reload()
  assert.equal(env.store.recoveryBackupPath, backupPath)
  assert.equal(env.store.workflows[0].id, 'after-recovery')
  await env.store.dismissRecoveryNotice()
  assert.equal(env.store.recoveryBackupPath, '')
  assert.equal(env.stored.recoveryBackupPath, undefined)
  assert.equal(env.stored.workflows[0].id, 'after-recovery')
  await env.store.reload()
  assert.equal(env.store.recoveryBackupPath, '')
})

test('acknowledging an older recovery does not hide a newer backup', async () => {
  const env = environment()
  env.stored = { workflows: [], selectedWorkflowId: '', recoveryBackupPath: 'older backup' }
  await env.store.initialize()
  env.stored = { ...env.stored, recoveryBackupPath: 'newer backup' }
  await env.store.dismissRecoveryNotice()
  assert.equal(env.store.recoveryBackupPath, 'newer backup')
  assert.equal(env.stored.recoveryBackupPath, 'newer backup')
})

test('a failed recovery acknowledgement retains the notice and workflow data', async () => {
  const env = environment()
  env.stored = { ...env.stored, recoveryBackupPath: 'retained backup' }
  await env.store.initialize()
  const before = structuredClone(env.stored)
  env.mutate = async () => { throw new Error('Storage unavailable') }
  await assert.rejects(env.store.dismissRecoveryNotice(), /Storage unavailable/)
  assert.equal(env.store.recoveryBackupPath, 'retained backup')
  assert.deepEqual(env.stored, before)
})

test('overview rejects malformed simple imports without saving or changing selection', async () => {
  const env = environment()
  await env.store.initialize()
  const page = env.mount(WorkflowsView)
  const before = JSON.stringify(env.store.workflows)
  const selected = env.store.selectedWorkflowId
  const baselineWrites = env.writes.length
  for (const patch of [{ model: { name: 'x' } }, { stems: { name: 'Vocals' } }, { stems: ['Vocals', 1] }]) {
    const definition = { version: 1, steps: [{ id: 's', model: 'model.ckpt', input: 'input', stems: ['Vocals'], save: { Vocals: 'Default' }, ...patch }] }
    const input = { value: 'selected', files: [{ name: 'invalid.json', text: async () => JSON.stringify(definition) }] }
    await page.state.handleImportWorkflow({ target: input })
    assert.equal(input.value, '')
    assert.equal(env.writes.length, baselineWrites)
    assert.equal(JSON.stringify(env.store.workflows), before)
    assert.equal(env.store.selectedWorkflowId, selected)
    assert.equal(messages.at(-1).level, 'error')
    assert.match(messages.at(-1).text, /workflowImportInvalid/)
  }
})

test('overview imports an unfinished Studio draft without requiring runnable outputs', async () => {
  const env = environment()
  await env.store.initialize()
  const page = env.mount(WorkflowsView)
  const definition = { version: 1, steps: [{ id: 's', model: '', input: 'input', stems: [], save: {} }] }
  const input = { value: 'selected', files: [{ name: 'draft.json', text: async () => JSON.stringify(definition) }] }
  await page.state.handleImportWorkflow({ target: input })
  assert.equal(input.value, '')
  assert.deepEqual(JSON.parse(JSON.stringify(env.store.selectedWorkflow.definition)), definition)
  assert.equal(messages.at(-1).level, 'success')
})

test('overview import and export repair old graph versions without rewriting the saved definition', async () => {
  const source = JSON.parse(readFileSync(new URL('./fixtures/comfy-mss/example_ensemble.json', import.meta.url), 'utf8'))
  source.version = 1
  const env = environment()
  await env.store.initialize()
  const page = env.mount(WorkflowsView)
  const input = { value: 'selected', files: [{ name: 'ensemble.comfy-mss.json', text: async () => JSON.stringify(source) }] }
  await page.state.handleImportWorkflow({ target: input })
  await flush()
  assert.equal(input.value, '')
  assert.equal(env.store.selectedWorkflow.name, 'ensemble')
  assert.deepEqual(JSON.parse(JSON.stringify(env.store.selectedWorkflow.definition)), source)
  await page.state.exportWorkflowEntry(env.store.selectedWorkflow)
  assert.equal(env.exports.length, 1)
  assert.equal(env.exports[0].defaultName, 'ensemble.comfy-mss.json')
  const exported = JSON.parse(env.exports[0].content)
  assert.equal(exported.version, 0.4)
  assert.deepEqual(exported.links, source.links)
  assert.deepEqual(exported.nodes.map(n => n.widgets_values), source.nodes.map(n => n.widgets_values))
  assert.equal(env.store.selectedWorkflow.definition.version, 1)
  assert.deepEqual(messages.map(m => m.level), ['success', 'success'])
  input.files[0].text = async () => JSON.stringify(exported)
  await page.state.handleImportWorkflow({ target: input })
  assert.equal(env.store.workflows.length, 3)
  assert.deepEqual(JSON.parse(JSON.stringify(env.store.selectedWorkflow.definition)), exported)
})

test('workflow type filters do not replace the global separation target', async () => {
  const env = environment()
  env.stored = {
    workflows: [entry('simple-target'), entry('advanced-browser', 17, 'advanced')],
    selectedWorkflowId: 'simple-target',
  }
  await env.store.initialize()
  const page = env.mount(WorkflowsView)

  page.state.setWorkflowTypeFilter('advanced')
  await flush()

  assert.equal(env.store.selectedWorkflowId, 'simple-target')
  assert.deepEqual(page.state.filteredWorkflows.map(item => item.id), ['advanced-browser'])
})

test('overview export leaves simple workflow definitions unchanged', async () => {
  const env = environment()
  await env.store.initialize()
  const page = env.mount(WorkflowsView)
  const definition = JSON.parse(JSON.stringify(env.store.selectedWorkflow.definition))
  await page.state.exportWorkflowEntry(env.store.selectedWorkflow)
  assert.equal(env.exports[0].defaultName, 'original.pymss-workflow.json')
  assert.deepEqual(JSON.parse(env.exports[0].content), definition)
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
    window.__TAURI_INTERNALS__.metadata = { currentWindow: { label: 'workflow-editor' } }
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
  assert.equal(warnings.mock.calls.some(call => call.arguments.some(value => String(value).includes(failure.message))), true)
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

test('workflow editor routes do not turn the main window into an embedded workbench', async () => {
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

test('overview metadata edits do not overwrite a newer workflow saved elsewhere', async () => {
  const env = environment()
  await env.store.initialize()
  const page = env.mount(WorkflowsView)
  await flush()
  page.state.name = 'Local stale name'
  const remote = entry('original', 40)
  remote.name = 'Remote current name'
  env.stored = { workflows: [remote], selectedWorkflowId: 'original' }

  await page.state.saveMeta()
  await flush()

  assert.equal(env.stored.workflows[0].name, 'Remote current name')
  assert.equal(env.store.selectedWorkflow.name, 'Remote current name')
  assert.equal(page.state.name, 'Remote current name')
  assert.deepEqual(messages, [{ level: 'error', text: 'workflows.revisionConflictTitle' }])
})

test('overview metadata edits remain available to retry after a regular save failure', async () => {
  const env = environment()
  await env.store.initialize()
  const page = env.mount(WorkflowsView)
  await flush()
  page.state.name = 'Retry this name'
  page.state.description = 'Retry this description'
  env.mutate = () => Promise.reject(new Error('disk unavailable'))

  await page.state.saveMeta()
  await flush()

  assert.equal(page.state.name, 'Retry this name')
  assert.equal(page.state.description, 'Retry this description')
  assert.equal(env.stored.workflows[0].name, 'original')
  assert.deepEqual(messages, [{ level: 'error', text: 'disk unavailable' }])
})

test('rapid workflow default changes are serialized and keep the latest value', async () => {
  const env = environment()
  await env.store.initialize()
  const page = env.mount(WorkflowsView)
  const gate = deferred()
  let defaultsWrites = 0
  env.mutate = mutation => {
    if (mutation.action !== 'upsert') return undefined
    defaultsWrites += 1
    return defaultsWrites === 1 ? gate.promise : undefined
  }

  page.state.updateSelectedDefaultNormalize(true)
  page.state.updateSelectedDefaultNormalize(false)
  gate.resolve()
  await flush()
  await flush()

  assert.equal(defaultsWrites, 2)
  assert.equal(env.store.selectedWorkflow.definition.defaults.inference_params.normalize, false)
  assert.equal(messages.some(item => item.level === 'error'), false)
})

test('saving a legacy workflow without persisted metadata migrates it instead of duplicating it', async () => {
  const env = environment()
  const legacy = entry('temporary')
  delete legacy.id
  delete legacy.createdAt
  delete legacy.updatedAt
  env.stored = { workflows: [legacy], selectedWorkflowId: '' }
  await env.store.initialize()
  const loaded = env.store.workflows[0]
  assert.equal(loaded.updatedAt, 0)

  const saved = await env.store.saveWorkflow({
    id: loaded.id,
    name: 'Migrated workflow',
    description: loaded.description,
    definition: loaded.definition,
    expectedUpdatedAt: loaded.updatedAt,
  })

  assert.equal(env.stored.workflows.length, 1)
  assert.equal(env.stored.workflows[0].id, loaded.id)
  assert.equal(saved.name, 'Migrated workflow')
  assert.ok(saved.updatedAt > 0)
})

test('browser storage also migrates legacy workflows without duplicating them', async () => {
  const values = new Map()
  globalThis.window = {}
  globalThis.localStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  }
  const legacy = entry('temporary')
  delete legacy.id
  delete legacy.createdAt
  delete legacy.updatedAt
  values.set('pymss-studio:workflow-state', JSON.stringify({ workflows: [legacy], selectedWorkflowId: '' }))
  const store = useWorkflowStore(createPinia())
  stores.push(store)
  await store.initialize()
  const loaded = store.workflows[0]

  await store.saveWorkflow({
    id: loaded.id,
    name: 'Browser migrated workflow',
    description: loaded.description,
    definition: loaded.definition,
    expectedUpdatedAt: loaded.updatedAt,
  })

  const saved = JSON.parse(values.get('pymss-studio:workflow-state'))
  assert.equal(saved.workflows.length, 1)
  assert.equal(saved.workflows[0].id, loaded.id)
  assert.equal(saved.workflows[0].name, 'Browser migrated workflow')
})

test('legacy workflows can be selected and deleted before their first save', async () => {
  const env = environment()
  const first = entry('first legacy')
  const second = entry('second legacy')
  for (const item of [first, second]) {
    delete item.id
    delete item.updatedAt
  }
  env.stored = { workflows: [first, second], selectedWorkflowId: '' }
  await env.store.initialize()
  const selected = env.store.workflows.find(item => item.name === 'second legacy')
  assert.ok(selected)

  env.store.selectWorkflow(selected.id)
  await flush()

  assert.equal(env.stored.selectedWorkflowId, selected.id)
  assert.equal(env.stored.workflows[1].id, selected.id)
  const remainingLegacy = env.store.workflows.find(item => item.name === 'first legacy')
  assert.ok(remainingLegacy)
  await env.store.deleteWorkflow(remainingLegacy.id)
  assert.deepEqual(env.stored.workflows.map(item => item.name), ['second legacy'])
})

test('deleting a legacy workflow reports a conflict after another window migrated it', async () => {
  const env = environment()
  const legacy = entry('legacy')
  delete legacy.id
  delete legacy.updatedAt
  env.stored = { workflows: [legacy], selectedWorkflowId: '' }
  await env.store.initialize()
  const local = env.store.workflows[0]
  env.stored = {
    workflows: [{ ...legacy, id: 'remote-migrated', updatedAt: 20 }],
    selectedWorkflowId: 'remote-migrated',
  }

  await assert.rejects(env.store.deleteWorkflow(local.id), WorkflowRevisionConflictError)

  assert.equal(env.stored.workflows.length, 1)
  assert.equal(env.store.workflows[0].id, 'remote-migrated')
})

test('deleting a workflow does not remove a newer revision saved by another window', async () => {
  const env = environment()
  await env.store.initialize()
  const remote = entry('original', 40)
  remote.name = 'Updated elsewhere'
  env.stored = { workflows: [remote], selectedWorkflowId: 'original' }

  await assert.rejects(env.store.deleteWorkflow('original'), WorkflowRevisionConflictError)

  assert.equal(env.stored.workflows.length, 1)
  assert.equal(env.stored.workflows[0].name, 'Updated elsewhere')
  assert.equal(env.store.workflows[0].updatedAt, 40)
})

test('saving one workflow preserves a newer workflow written by another window', async () => {
  const env = environment()
  env.stored = {
    workflows: [entry('first', 16), entry('second', 16)],
    selectedWorkflowId: 'first',
  }
  await env.store.initialize()

  const remoteFirst = entry('first', 40)
  remoteFirst.name = 'First saved elsewhere'
  env.stored = {
    workflows: [remoteFirst, entry('second', 16)],
    selectedWorkflowId: 'first',
  }

  const savedSecond = await env.store.saveWorkflow({
    id: 'second',
    name: 'Second local edit',
    definition: entry('second').definition,
    expectedUpdatedAt: 16,
  })

  assert.equal(savedSecond.name, 'Second local edit')
  assert.equal(env.stored.workflows.find(item => item.id === 'first').name, 'First saved elsewhere')
  assert.equal(env.stored.workflows.find(item => item.id === 'first').updatedAt, 40)
  assert.deepEqual(env.store.workflows.map(item => item.id).sort(), ['first', 'second'])
})

test('clearing the workflow selection is persisted without changing the workflow list', async () => {
  const env = environment()
  await env.store.initialize()
  const before = structuredClone(env.stored.workflows)

  env.store.selectWorkflow('')
  await flush()

  assert.equal(env.store.selectedWorkflowId, '')
  assert.equal(env.stored.selectedWorkflowId, '')
  assert.deepEqual(env.stored.workflows, before)
  assert.equal(env.writes.length, 1)
})

test('rapid workflow selections do not publish an older queued selection', async () => {
  const env = environment()
  env.stored = {
    workflows: [entry('first', 17), entry('second', 16)],
    selectedWorkflowId: 'first',
  }
  await env.store.initialize()
  const gate = deferred()
  env.mutate = mutation => mutation.workflowId === 'first' ? gate.promise : undefined
  const selections = []
  const stop = watch(() => env.store.selectedWorkflowId, value => selections.push(value), { flush: 'sync' })

  env.store.selectWorkflow('first')
  env.store.selectWorkflow('second')
  gate.resolve()
  await flush()

  stop()
  assert.equal(env.store.selectedWorkflowId, 'second')
  assert.equal(env.stored.selectedWorkflowId, 'second')
  assert.deepEqual(selections, ['second'])
})

test('a pending save does not restore the selection the user has already left', async () => {
  const env = environment()
  env.stored = {
    workflows: [entry('first', 17), entry('second', 16)],
    selectedWorkflowId: 'first',
  }
  await env.store.initialize()
  const gate = deferred()
  env.mutate = mutation => mutation.action === 'upsert' ? gate.promise : undefined
  const selections = []
  const stop = watch(() => env.store.selectedWorkflowId, value => selections.push(value), { flush: 'sync' })

  const saving = env.store.saveWorkflow({
    id: 'first',
    name: 'First changed',
    definition: entry('first').definition,
    expectedUpdatedAt: 17,
  })
  env.store.selectWorkflow('second')
  gate.resolve()
  await saving
  await flush()

  stop()
  assert.equal(env.store.selectedWorkflowId, 'second')
  assert.equal(env.stored.selectedWorkflowId, 'second')
  assert.deepEqual(selections, ['second'])
})

test('legacy editor actions refresh the main window without navigating into the editor', async () => {
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
  assert.deepEqual(navigation, [])
  assert.equal(env.writes.length, 1)
  assert.equal(env.stored.selectedWorkflowId, 'saved')
  assert.deepEqual(env.stored.workflows.map(item => item.id).sort(), ['original', 'saved'])
})


test('simple and advanced workflows open native editor windows without replacing the overview', async () => {
  const env = environment()
  await env.store.initialize()
  const page = env.mount(WorkflowsView)
  await page.state.openSimpleEditor({ workflowId: 'original' })
  await page.state.openNodeEditor({ workflowId: 'advanced', skipWarning: true })
  assert.deepEqual(env.editorOpens, [
    { command: 'open_workflow_simple_editor_window', payload: { workflowId: 'original', newWorkflow: false } },
    { command: 'open_workflow_node_editor_window', payload: { workflowId: 'advanced', newWorkflow: false } },
  ])
  assert.equal(env.store.simpleEditorOpenWorkflowId, 'original')
  assert.equal(env.store.nodeEditorOpenWorkflowId, 'advanced')
  assert.deepEqual(navigation, [])
})

test('new workflows use blank native windows and a failed open leaves the overview in place', async () => {
  const env = environment()
  await env.store.initialize()
  const page = env.mount(WorkflowsView)
  await page.state.openSimpleEditor({ forceNew: true })
  assert.deepEqual(env.editorOpens, [{ command: 'open_workflow_simple_editor_window', payload: { workflowId: '', newWorkflow: true } }])
  assert.equal(env.store.simpleEditorOpenWorkflowId, '__new__')
  env.editorOpenFailure = new Error('Window creation failed')
  await page.state.openNodeEditor({ workflowId: 'original', skipWarning: true })
  assert.equal(env.store.nodeEditorOpenWorkflowId, '')
  assert.deepEqual(navigation, [])
  assert.equal(messages.at(-1).text, 'Window creation failed')
})

test('native ownership adopts formal ids and ignores older snapshots or stale open responses', () => {
  const env = environment()
  env.store.applyEditorSnapshot({ revision: 1, editors: [{ label: 'workflow-simple-editor-new', workflowId: '', kind: 'simple' }] })
  env.store.applyEditorSnapshot({ revision: 2, editors: [{ label: 'workflow-simple-editor-new', workflowId: 'original', kind: 'simple' }] })
  env.store.recordEditorOpened({ editorSnapshot: { revision: 1, editors: [] } }, 'simple', '__new__')
  assert.equal(env.store.editorFor('original').label, 'workflow-simple-editor-new')
  assert.equal(env.store.simpleEditorOpenWorkflowId, 'original')
  assert.equal(env.store.hasAnyEditorOpen, true)
})

test('closing one native editor leaves another document locked, including delayed close notifications', async () => {
  const env = environment()
  const remaining = { label: 'workflow-simple-editor-b', workflowId: 'other', kind: 'simple' }
  env.store.applyEditorSnapshot({ revision: 2, editors: [{ label: 'workflow-simple-editor-a', workflowId: 'original', kind: 'simple' }, remaining] })
  env.nativeEditorSnapshot = { revision: 3, editors: [remaining] }
  await env.store.handleEditorClosed('simple', 'workflow-simple-editor-a')
  assert.equal(env.store.editorFor('original'), undefined)
  assert.equal(env.store.editorFor('other').label, remaining.label)
  await env.store.handleEditorClosed('simple', 'workflow-simple-editor-a')
  assert.equal(env.store.editorFor('other').label, remaining.label)
  assert.equal(env.store.hasAnyEditorOpen, true)
})

test('management saves and force overwrites cannot bypass an active native workflow owner', async () => {
  const env = environment()
  await env.store.initialize()
  env.nativeEditorSnapshot = { revision: 1, editors: [{ label: 'workflow-simple-editor-new', workflowId: 'original', kind: 'simple' }] }
  const page = env.mount(WorkflowsView)
  await env.store.refreshEditorWindows()
  page.state.name = 'Management edit'
  await page.state.saveMeta()
  assert.deepEqual(env.writes, [])
  const input = { id: 'original', name: 'Forced edit', definition: env.store.selectedWorkflow.definition, force: true }
  await assert.rejects(env.store.saveWorkflow(input), error => error.code === 'WORKFLOW_EDITOR_IN_USE')
  assert.deepEqual(env.writes, [])
})

test('main app consumes ownership snapshots before reacting to legacy editor-close events', async () => {
  const env = environment()
  await env.store.initialize()
  env.mount(App)
  await flush()
  const owner = { label: 'workflow-simple-editor-new', workflowId: 'original', kind: 'simple' }
  env.dispatch('pymss://workflow-editors-changed', { revision: 8, editors: [owner] })
  env.nativeEditorSnapshot = { revision: 8, editors: [owner] }
  env.dispatch(closeEvent('simple'), { label: 'closed-older-window' })
  await flush()
  assert.equal(env.store.editorFor('original').label, owner.label)
})
