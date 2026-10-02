import assert from 'node:assert/strict'
import test, { after, afterEach } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createPinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'

const vite = await createServer({
  configFile: false,
  server: {
    middlewareMode: true,
    hmr: false,
    watch: { ignored: ['**/*'] },
  },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
  plugins: [{
    name: 'stub-page-size-store-environment',
    enforce: 'pre',
    transform(_code, id) {
      const path = id.replaceAll('\\', '/')
      if (path.endsWith('/src/stores/settings.ts')) {
        return `export function useSettingsStore() {
          return {
            maxConcurrentSeparations: 1,
            outputDir: 'outputs', downloadSource: 'modelscope', downloadMethod: 'aria2c',
            defaultFormat: 'wav', developerMode: false,
            getRuntimeDeviceConfig: () => ({ device: 'cpu', deviceIds: [] }),
            getAudioParams: () => ({}),
          }
        }`
      }
      if (path.endsWith('/src/i18n/index.ts')) {
        return `export default { global: { t: key => key } }`
      }
      return null
    },
  }],
})

after(() => vite.close())

const { useModelStore } = await vite.ssrLoadModule('/src/stores/model.ts')
const { useTaskStore } = await vite.ssrLoadModule('/src/stores/task.ts')

const stores = []

afterEach(async () => {
  // Hydration can schedule an existing Store watcher on Vue's next flush.
  // Let any debounced write finish before removing the browser storage shim.
  await waitForDebouncedSave()
  for (const store of stores.splice(0)) store.$dispose()
  Reflect.deleteProperty(globalThis, 'window')
  Reflect.deleteProperty(globalThis, 'localStorage')
})

function browserStorage(initial = {}, options = {}) {
  const values = new Map(
    Object.entries(initial).map(([name, value]) => [
      `pymss-studio:${name}`,
      JSON.stringify(value),
    ]),
  )
  const writes = []
  globalThis.window = {}
  globalThis.localStorage = {
    getItem: key => values.get(key) ?? null,
    setItem(key, value) {
      if (options.failWrites) throw new Error('storage write failed')
      values.set(key, value)
      writes.push({ key, value: JSON.parse(value) })
    },
  }
  return { writes }
}

function newModelStore() {
  const store = useModelStore(createPinia())
  stores.push(store)
  return store
}

function newTaskStore() {
  const store = useTaskStore(createPinia())
  stores.push(store)
  return store
}

const waitForDebouncedSave = () => new Promise(resolve => setTimeout(resolve, 180))

test('model library restores and persists its page size preference', async () => {
  const storage = browserStorage({ 'model-state': { modelPageSize: 48 } })
  const store = newModelStore()

  await store.initialize()
  assert.equal(store.modelPageSize, 48)

  store.modelPageSize = 96
  await waitForDebouncedSave()

  assert.equal(storage.writes.at(-1)?.key, 'pymss-studio:model-state')
  assert.equal(storage.writes.at(-1)?.value.modelPageSize, 96)
})

test('separation model list restores and persists its page size preference', async () => {
  const storage = browserStorage({ 'separate-state': { modelListPageSize: 24 } })
  const store = newTaskStore()

  await store.initialize()
  assert.equal(store.modelListPageSize, 24)

  store.modelListPageSize = 8
  await waitForDebouncedSave()

  assert.equal(storage.writes.at(-1)?.key, 'pymss-studio:separate-state')
  assert.equal(storage.writes.at(-1)?.value.modelListPageSize, 8)
})

test('session task queue resets on restart without deleting persisted task history', async () => {
  browserStorage()
  const pinia = createPinia()
  const taskStore = useTaskStore(pinia)
  const modelStore = useModelStore(pinia)
  stores.push(taskStore, modelStore)
  await Promise.all([taskStore.initialize(), modelStore.initialize()])
  modelStore.selectedModel = 'session-model.ckpt'
  taskStore.addInputFiles(['D:/Audio/one.wav', 'D:/Audio/two.wav'])

  const submitted = await taskStore.startSeparation()
  assert.equal(submitted.tasks.length, 2)
  assert.equal(taskStore.sessionJobs.length, 1)
  assert.equal(taskStore.sessionJobs[0].inputCount, 2)

  taskStore.tasks.forEach((item) => {
    item.status = 'done'
    item.outputs = [{ stem: 'Vocals', path: `D:/Outputs/${item.id}.wav` }]
  })
  assert.equal(taskStore.sessionJobs[0].status, 'done')
  assert.equal(taskStore.resultJobs.length, 1)
  assert.equal(taskStore.clearFinishedSessionJobs(), 1)
  assert.equal(taskStore.sessionJobs.length, 0)
  assert.equal(taskStore.resultJobs.length, 1)
  assert.equal(taskStore.tasks.length, 2)

  await waitForDebouncedSave()
  const restartedStore = useTaskStore(createPinia())
  stores.push(restartedStore)
  await restartedStore.initialize()
  assert.equal(restartedStore.sessionJobs.length, 0)
  assert.equal(restartedStore.tasks.length, 2)
})

test('invalid stored page sizes fall back to each view default', async () => {
  browserStorage({
    'model-state': { modelPageSize: 13 },
    'separate-state': { modelListPageSize: '24' },
  })
  const modelStore = newModelStore()
  const taskStore = newTaskStore()

  await Promise.all([modelStore.initialize(), taskStore.initialize()])

  assert.equal(modelStore.modelPageSize, 24)
  assert.equal(taskStore.modelListPageSize, 12)
})

test('model inference overrides merge hidden fields and persist before resolving', async () => {
  const storage = browserStorage({
    'model-state': {
      modelInferenceOverrides: {
        'test-model': {
          window_size: 1024,
          normalize: true,
        },
      },
    },
  })
  const store = newModelStore()

  await store.initialize()
  await store.setModelInferenceOverrides('test-model', {
    batch_size: 2,
    overlap_size: 2048,
    chunk_size: 16384,
  })

  assert.deepEqual(
    { ...store.getModelInferenceOverrides('test-model') },
    {
      window_size: 1024,
      normalize: true,
      batch_size: 2,
      overlap_size: 2048,
      chunk_size: 16384,
    },
  )
  assert.deepEqual(
    storage.writes.at(-1)?.value.modelInferenceOverrides['test-model'],
    store.getModelInferenceOverrides('test-model'),
  )

  await store.resetModelInferenceOverrides('test-model')
  assert.equal(store.getModelInferenceOverrides('test-model'), undefined)
  assert.equal(storage.writes.at(-1)?.value.modelInferenceOverrides['test-model'], undefined)
})

for (const scenario of [
  { name: 'uses an explicit VR type without model metadata', stepType: ' VR ', entryType: null, vr: true },
  { name: 'falls back to model metadata when the step has no type', stepType: undefined, entryType: ' VR ', vr: true },
  { name: 'prefers an explicit VR type over conflicting model metadata', stepType: 'vr', entryType: 'mel_band_roformer', vr: true },
  { name: 'prefers an explicit MSS type over conflicting model metadata', stepType: 'mel_band_roformer', entryType: 'vr', vr: false },
]) {
  test(`workflow model inference ${scenario.name}`, async () => {
    const overrides = {
      batch_size: 2,
      window_size: 1024,
      aggression: 0,
      enable_post_process: false,
      post_process_threshold: 0,
      high_end_process: false,
      normalize: false,
      overlap_size: 2048,
      chunk_size: 16384,
      standardize: true,
    }
    browserStorage({
      'model-state': {
        models: scenario.entryType ? [{ name: 'workflow-model', aliases: [], modelType: scenario.entryType }] : [],
        modelInferenceOverrides: { 'workflow-model': overrides },
      },
    })
    const pinia = createPinia()
    setActivePinia(pinia)
    const modelStore = useModelStore(pinia)
    const taskStore = useTaskStore(pinia)
    stores.push(modelStore, taskStore)
    await Promise.all([modelStore.initialize(), taskStore.initialize()])
    taskStore.addInputFiles(['D:/Audio/song.wav'])
    const definition = {
      version: 1,
      steps: [{
        id: 'split',
        model: 'workflow-model',
        ...(scenario.stepType === undefined ? {} : { model_type: scenario.stepType }),
        input: 'input',
        stems: ['Vocals'],
        save: { Vocals: 'Default' },
      }],
    }

    const submitted = await taskStore.startWorkflowInference({ id: 'workflow-model-types', name: 'Workflow', definition })
    const params = submitted.tasks[0].runConfig.workflowDefinition.steps[0].inference_params
    const expectedKeys = scenario.vr
      ? ['batch_size', 'window_size', 'aggression', 'enable_post_process', 'post_process_threshold', 'high_end_process', 'normalize']
      : ['batch_size', 'overlap_size', 'chunk_size', 'standardize', 'normalize']
    assert.deepEqual(params, Object.fromEntries(expectedKeys.map(key => [key, overrides[key]])))
    assert.equal(definition.steps[0].inference_params, undefined)
  })
}

test('legacy per-model inference drafts retain stems without overriding model defaults', async () => {
  browserStorage({
    'separate-state': {
      inferenceParamsByModel: {
        'test-model': {
          overlap_size: 4096,
          chunk_size: 32768,
          standardize: true,
          selectedStems: ['vocals'],
        },
      },
    },
  })
  const store = newTaskStore()

  await store.initialize()

  assert.deepEqual(
    { ...store.getSavedModelState('test-model') },
    { selectedStems: ['vocals'] },
  )
})

test('output stem selections are cached per model', async () => {
  const storage = browserStorage({
    'separate-state': {
      inferenceParamsByModel: {
        'model-a': {
          selectedStems: ['vocals'],
          selectedStemsCleared: false,
        },
      },
    },
  })
  const pinia = createPinia()
  setActivePinia(pinia)
  const taskStore = useTaskStore(pinia)
  const modelStore = useModelStore(pinia)
  stores.push(taskStore, modelStore)

  await Promise.all([taskStore.initialize(), modelStore.initialize()])
  modelStore.selectedModel = 'model-a'
  await waitForDebouncedSave()
  const writesBeforeDraft = storage.writes.length
  taskStore.selectedStems = ['drums']
  taskStore.selectedStemsCleared = true
  await waitForDebouncedSave()
  assert.ok(storage.writes.length > writesBeforeDraft)
  assert.deepEqual(taskStore.getSavedModelState('model-a'), {
    selectedStems: ['drums'],
    selectedStemsCleared: true,
  })
  assert.deepEqual(storage.writes.at(-1)?.value.inferenceParamsByModel['model-a'], {
    selectedStems: ['drums'],
    selectedStemsCleared: true,
  })

  const restoredPinia = createPinia()
  setActivePinia(restoredPinia)
  const restoredTaskStore = useTaskStore(restoredPinia)
  stores.push(restoredTaskStore)
  await restoredTaskStore.initialize()
  assert.deepEqual(restoredTaskStore.getSavedModelState('model-a'), {
    selectedStems: ['drums'],
    selectedStemsCleared: true,
  })
})

test('failed model inference persistence restores the previous overrides', async () => {
  browserStorage({
    'model-state': {
      modelInferenceOverrides: {
        'test-model': { overlap_size: 1024 },
      },
    },
  }, { failWrites: true })
  const store = newModelStore()

  await store.initialize()
  await assert.rejects(
    store.setModelInferenceOverrides('test-model', { overlap_size: 4096 }),
    /storage write failed/,
  )

  assert.deepEqual(
    { ...store.getModelInferenceOverrides('test-model') },
    { overlap_size: 1024 },
  )
})
