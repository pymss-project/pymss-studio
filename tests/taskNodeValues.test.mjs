import assert from 'node:assert/strict'
import test, { after, afterEach } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createPinia, disposePinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'
import { watch } from 'vue'

const vite = await createServer({
  configFile: false,
  server: { middlewareMode: true, hmr: false, watch: null },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
  plugins: [{
    name: 'task-node-values-environment',
    enforce: 'pre',
    transform(_code, id) {
      const path = id.replaceAll('\\', '/')
      if (path.endsWith('/src/stores/settings.ts')) {
        return `export const useSettingsStore = () => ({
          maxConcurrentSeparations: 1, outputDir: 'D:/output', developerMode: false,
        })`
      }
      if (path.endsWith('/src/i18n/index.ts')) return 'export default { global: { t: key => key } }'
      return null
    },
  }],
})
after(() => vite.close())
const { useTaskStore } = await vite.ssrLoadModule('/src/stores/task.ts')
const { useAppStore } = await vite.ssrLoadModule('/src/stores/app.ts')
const { useWorkbenchStore } = await vite.ssrLoadModule('/src/stores/workbench.ts')
const { createWorkbenchBridge } = await vite.ssrLoadModule('/src/workflows/workbenchBridge.ts')
const instances = []
const clone = value => JSON.parse(JSON.stringify(value))
const waitForSave = () => new Promise(resolve => setTimeout(resolve, 180))

afterEach(async () => {
  await waitForSave()
  instances.splice(0).forEach(disposePinia)
  Reflect.deleteProperty(globalThis, 'window')
  Reflect.deleteProperty(globalThis, 'localStorage')
})

const text = { nodeId: 2, nodeType: 'ASRTranscribe', slot: 0, value: '转写结果\nSecond line' }
const measurement = { nodeId: 'save:measure:lufs', nodeType: 'MeasureLufs', slot: 0, value: '-14.0' }
const audio = { stem: 'vocals', path: 'D:/output/vocals.wav' }

function record(id, changes = {}) {
  return {
    id, jobId: `job-${id}`, model: 'Workflow', input: 'D:/source.wav', output: 'D:/output',
    status: 'done', files: [], outputs: [], logs: [], createdAt: 1, updatedAt: 2,
    runConfig: { runMode: 'workflow', workflowId: 'workflow', workbench: { sourceDefinition: { nodes: [], links: [] } } },
    ...changes,
  }
}

function storage(records) {
  const values = new Map([['pymss-studio:task-history', JSON.stringify({ tasks: records })]])
  globalThis.window = {}
  globalThis.localStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  }
  return () => JSON.parse(values.get('pymss-studio:task-history')).tasks
}

async function newStore() {
  const pinia = createPinia()
  instances.push(pinia)
  setActivePinia(pinia)
  useAppStore(pinia).envInfo = { platform: 'win32' }
  const task = useTaskStore(pinia)
  await task.initialize()
  return task
}

test('worker text and measurement results preserve numeric and string node IDs across persistence and restart', async () => {
  const persisted = storage([record('text'), record('mixed')])
  const store = await newStore()
  for (const task of store.tasks) task.status = 'separating'
  store.handleWorkerEvent({ type: 'task_done', taskId: 'text', payload: { nodeValues: [text] } })
  store.handleWorkerEvent({ type: 'task_done', taskId: 'mixed', payload: { outputs: [audio], nodeValues: [measurement] } })
  assert.deepEqual(store.resultTasks.map(task => task.id), ['text', 'mixed'])
  assert.deepEqual(clone(store.resultJobs[0].primary.nodeValues), [text])
  await waitForSave()
  assert.deepEqual(persisted().map(task => task.nodeValues), [[text], [measurement]])

  const restored = await newStore()
  assert.deepEqual(restored.resultTasks.map(task => task.id), ['text', 'mixed'])
  assert.deepEqual(clone(restored.tasks.map(task => task.nodeValues)), [[text], [measurement]])
  assert.equal(restored.tasks[1].outputs[0].path, audio.path)
})

test('hiding task logs retains node-only results until the result is deleted', async () => {
  const persisted = storage([record('text', { nodeValues: [text] })])
  const store = await newStore()
  store.removeTask('text')
  assert.equal(store.taskBoardTasks.length, 0)
  assert.equal(store.resultTasks.length, 1)
  await waitForSave()
  assert.equal(persisted()[0].taskHidden, true)

  const restored = await newStore()
  assert.equal(restored.taskBoardTasks.length, 0)
  assert.deepEqual(clone(restored.resultTasks[0].nodeValues), [text])
  assert.equal(await restored.removeResult('text'), 1)
  assert.equal(restored.tasks.length, 0)
  assert.deepEqual(persisted(), [])
})

test('bulk log cleanup retains node-only results and prevents deleting their workflow run as log-only history', async () => {
  storage([record('text', { nodeValues: [text] }), record('empty'), record('failed', { status: 'failed' })])
  const store = await newStore()
  await assert.rejects(store.removeWorkflowRun('workflow', 'job-text'), /workbench.removeRunHasResults/)
  store.clearHistory()
  assert.deepEqual(store.tasks.map(task => task.id), ['text'])
  assert.equal(store.resultTasks.length, 1)
  assert.equal(await store.clearResults(), 1)
  assert.equal(store.tasks.length, 0)
})

test('invalid stored or worker node values are discarded while empty string outputs remain valid', async () => {
  const invalid = [null, [], 'text', {}, { ...text, nodeId: '' }, { ...text, nodeId: ' ' }, { ...text, nodeId: 1.2 },
    { ...text, slot: -1 }, { ...text, slot: '0' }, { ...text, nodeType: ' ' }, { ...text, value: {} }]
  const empty = { ...text, value: '' }
  storage([
    record('valid', { nodeValues: [...invalid, text, empty] }),
    record('invalid', { nodeValues: invalid }),
    record('object', { nodeValues: { value: 'wrong shape' } }),
  ])
  const store = await newStore()
  assert.deepEqual(store.resultTasks.map(task => task.id), ['valid'])
  assert.deepEqual(clone(store.tasks[0].nodeValues), [text, empty])
  store.tasks[1].status = 'separating'
  store.handleWorkerEvent({ type: 'task_done', taskId: 'invalid', payload: { nodeValues: [...invalid, measurement] } })
  assert.deepEqual(clone(store.tasks[1].nodeValues), [measurement])
  assert.deepEqual(store.resultTasks.map(task => task.id), ['valid', 'invalid'])
})

test('node values do not restore failed or previously deleted results', async () => {
  const persisted = storage([
    record('failed', { status: 'failed', nodeValues: [text] }),
    record('deleted', { resultHidden: true, nodeValues: [text] }),
  ])
  const store = await newStore()
  assert.equal(store.resultTasks.length, 0)
  assert.deepEqual(store.tasks.map(task => task.id), ['failed'])
  store.clearHistory()
  assert.equal(store.tasks.length, 0)
  await waitForSave()
  assert.deepEqual(persisted(), [])
})

test('workflow editor snapshots preserve restored node values', async t => {
  storage([record('text', { nodeValues: [text, measurement] })])
  const store = await newStore()
  const mirror = useWorkbenchStore()
  const handlers = new Map()
  const transport = label => ({
    async listen(event, handler) {
      const key = `${label}:${event}`
      handlers.set(key, handler)
      return () => handlers.delete(key)
    },
    async emitTo(target, event, payload) { await handlers.get(`${target}:${event}`)?.(clone(payload)) },
  })
  const host = await createWorkbenchBridge({ label: 'main', transport: transport('main'), host: {
    snapshot: key => clone({ workflowKey: key, jobs: store.allJobs.filter(job => job.primary.runConfig.workflowId === key) }),
    remember() {}, perform: async () => ({}),
    watchTasks: notify => watch(() => store.tasks, notify, { deep: true }),
  } })
  const editor = await createWorkbenchBridge({
    label: 'workflow-node-editor-a', transport: transport('workflow-node-editor-a'),
    applyState: state => mirror.applyRemoteState(state),
  })
  t.after(async () => { await editor.dispose(); await host.dispose() })
  await editor.attach('workflow')
  assert.deepEqual(clone(mirror.remoteJobs.workflow[0].tasks[0].nodeValues), [text, measurement])
})
