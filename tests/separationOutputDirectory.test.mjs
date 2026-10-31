import assert from 'node:assert/strict'
import test, { after, afterEach } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createPinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'

const vite = await createServer({
  configFile: false,
  server: { middlewareMode: true, hmr: false, watch: null },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
  plugins: [{
    name: 'stub-output-directory-store-environment',
    enforce: 'pre',
    transform(_code, id) {
      const path = id.replaceAll('\\', '/')
      if (path.endsWith('/src/stores/settings.ts')) {
        return `
          import { reactive } from 'vue'
          export const settings = reactive({
            maxConcurrentSeparations: 1,
            outputDir: 'D:/DefaultOutput', downloadSource: 'modelscope', downloadMethod: 'aria2c',
            defaultFormat: 'wav', developerMode: false,
            getRuntimeDeviceConfig: () => ({ device: 'cpu', deviceIds: [] }),
            getAudioParams: () => ({}),
          })
          export function useSettingsStore() { return settings }
        `
      }
      if (path.endsWith('/src/i18n/index.ts')) return 'export default { global: { t: key => key } }'
      return null
    },
  }],
})

after(() => vite.close())
const { settings } = await vite.ssrLoadModule('/src/stores/settings.ts')
const { useTaskStore } = await vite.ssrLoadModule('/src/stores/task.ts')
const { useModelStore } = await vite.ssrLoadModule('/src/stores/model.ts')
const { useAppStore } = await vite.ssrLoadModule('/src/stores/app.ts')
const { selectQueuedJobGroups } = await vite.ssrLoadModule('/src/features/tasks/lifecycle.ts')
const stores = []
const waitForSave = () => new Promise(resolve => setTimeout(resolve, 180))

afterEach(async () => {
  await waitForSave()
  for (const store of stores.splice(0)) store.$dispose()
  Reflect.deleteProperty(globalThis, 'window')
  Reflect.deleteProperty(globalThis, 'localStorage')
  settings.outputDir = 'D:/DefaultOutput'
  settings.maxConcurrentSeparations = 1
})

function storage(initial = {}) {
  const values = new Map(Object.entries(initial).map(([key, value]) => [
    `pymss-studio:${key}`, JSON.stringify(value),
  ]))
  globalThis.window = {}
  globalThis.localStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  }
  return values
}

async function newStore(platform = 'win32') {
  const pinia = createPinia()
  setActivePinia(pinia)
  const task = useTaskStore(pinia)
  const model = useModelStore(pinia)
  const app = useAppStore(pinia)
  app.envInfo = { platform }
  stores.push(task, model, app)
  await Promise.all([task.initialize(), model.initialize()])
  model.selectedModel = 'model-a'
  task.addInputFiles(['D:/Audio/song.wav'])
  return task
}

const workflow = {
  id: 'workflow-a', name: 'Workflow',
  definition: { version: 1, steps: [{ id: 'split', model: 'model-a', input: 'input', stems: ['vocals'], save: { vocals: 'vocals' } }] },
}

test('the initial separation directory follows Settings until a custom directory is entered', async () => {
  storage()
  const task = await newStore()
  assert.equal(task.separateOutputDir, 'D:/DefaultOutput')
  assert.equal(task.separateTemporaryOutputDir, '')
  settings.outputDir = 'F:/NewDefault'
  assert.equal(task.separateOutputDir, 'F:/NewDefault')
  task.separateTemporaryOutputDir = ' E:/CustomOutput '
  assert.equal(task.separateOutputDir, 'E:/CustomOutput')
  assert.equal(settings.outputDir, 'F:/NewDefault')
})

test('a custom directory survives page reuse but resets to Settings on application restart', async () => {
  const values = storage()
  const task = await newStore()
  task.separateTemporaryOutputDir = 'E:/CustomOutput'
  assert.equal(useTaskStore(), task)
  assert.equal(useTaskStore().separateOutputDir, 'E:/CustomOutput')
  task.separateOutputLayout = 'folders'
  await waitForSave()
  assert.equal(Object.hasOwn(JSON.parse(values.get('pymss-studio:separate-state')), 'temporaryOutputDir'), false)
  settings.outputDir = 'F:/NewDefault'
  assert.equal((await newStore()).separateOutputDir, 'F:/NewDefault')
})

test('clearing a directory restores inheritance for the current session', async () => {
  storage()
  const task = await newStore()
  task.separateTemporaryOutputDir = 'E:/CustomOutput'
  task.separateTemporaryOutputDir = ''
  assert.equal(task.separateOutputDir, 'D:/DefaultOutput')
  settings.outputDir = 'F:/NewDefault'
  assert.equal(task.separateOutputDir, 'F:/NewDefault')
})

test('legacy persisted custom directories are ignored on startup', async () => {
  storage({ 'separate-state': { temporaryOutputDir: 'E:/LegacyOutput' } })
  const task = await newStore()
  assert.equal(task.separateTemporaryOutputDir, '')
  assert.equal(task.separateOutputDir, 'D:/DefaultOutput')
})

for (const selectedDirectory of [undefined, 'E:/WorkflowOutput']) {
  test(`native graph submission and retry preserve the ${selectedDirectory ? 'selected' : 'default'} output root and graph parameters`, async () => {
    storage()
    const task = await newStore()
    const definition = {
      nodes: [
        { id: 1, type: 'pymss_load_audio_batch', widgets_values: ['D:/Music', true, false, ''] },
        { id: 2, type: 'pymss_save_audio', widgets_values: ['', 'stems', '44100'] },
      ], links: [[1, 1, 0, 2, 0, 'AUDIO']],
    }
    const saved = structuredClone(definition)
    const bindings = {
      inputs: { '1': { files: ['D:/Other/first.wav', 'D:/Other/second.wav'] } },
      outputs: { '2': 'E:/Ignored' }, primaryInput: '1', outputLayout: 'folders',
      outputDirectory: selectedDirectory,
    }
    const submitted = await task.startWorkbenchInference({ ...workflow, definition }, bindings)
    assert.equal(submitted.total, 1)
    const original = submitted.tasks[0]
    assert.equal(original.input, '')
    assert.equal(original.output, selectedDirectory || 'D:/DefaultOutput')
    assert.equal(original.jobOutput, original.output)
    assert.equal(settings.outputDir, 'D:/DefaultOutput')
    assert.equal(original.runConfig.outputLayout, 'flat')
    assert.equal(original.runConfig.workbench.nativeGraph, true)
    assert.deepEqual(original.runConfig.workflowDefinition, saved)
    assert.deepEqual(original.runConfig.workbench.outputs, {})
    definition.nodes[0].widgets_values[0] = 'D:/Changed'
    assert.deepEqual(original.runConfig.workbench.sourceDefinition, saved)
    original.status = 'failed'
    settings.outputDir = 'F:/NextRun'
    bindings.outputDirectory = 'G:/NextSelection'
    const retried = await task.retryTask(original.id)
    assert.equal(retried.output, original.output)
    assert.deepEqual(retried.runConfig, JSON.parse(JSON.stringify(original.runConfig)))
  })
}

test('clearing the native graph directory restores the current default for new runs', async () => {
  storage()
  const task = await newStore()
  const graph = { ...workflow, definition: { nodes: [{ id: 1, type: 'pymss_save_audio' }], links: [] } }
  const bindings = { inputs: {}, outputs: {}, primaryInput: '', outputLayout: 'flat', outputDirectory: 'E:/Selected' }
  const first = await task.startWorkbenchInference(graph, bindings)
  delete bindings.outputDirectory
  settings.outputDir = 'F:/NewDefault'
  const second = await task.startWorkbenchInference(graph, bindings)
  assert.equal(first.tasks[0].output, 'E:/Selected')
  assert.equal(second.tasks[0].output, 'F:/NewDefault')
})


for (const layout of ['flat', 'folders']) {
  test(`simple workbench ${layout} output roots use the chosen directory and remain frozen on retry`, async () => {
    storage()
    const task = await newStore()
    settings.outputDir = 'D:/UnavailableDefault'
    const bindings = { inputs: { input: { files: ['D:/Audio/song.wav'] } }, outputs: {},
      outputDirectory: 'E:/WorkflowOutput', primaryInput: 'input', outputLayout: layout }
    const submitted = await task.startWorkbenchInference({ ...workflow, updatedAt: 1 }, bindings)
    const first = submitted.tasks[0]
    assert.equal(first.jobOutput, 'E:/WorkflowOutput')
    assert.equal(first.output, layout === 'flat' ? 'E:/WorkflowOutput' : 'E:/WorkflowOutput/song')
    assert.equal(first.runConfig.workbench.outputs['save:split:vocals'].replaceAll('\\', '/'), first.output)
    first.status = 'failed'
    settings.outputDir = 'F:/LaterDefault'
    bindings.outputDirectory = 'G:/NextRun'
    const retry = await task.retryTask(first.id)
    assert.equal(retry.jobOutput, 'E:/WorkflowOutput')
    assert.equal(retry.output, first.output)
    assert.deepEqual(retry.runConfig, JSON.parse(JSON.stringify(first.runConfig)))
  })
}

test('clearing the simple workbench directory restores the default for new runs', async () => {
  storage()
  const task = await newStore()
  const submitted = await task.startWorkbenchInference(workflow, {
    inputs: { input: { files: ['D:/Audio/song.wav'] } }, outputs: {}, primaryInput: 'input', outputLayout: 'flat',
  })
  assert.equal(submitted.tasks[0].jobOutput, settings.outputDir)
})

test('simple workbench batches retain the selected file order when tasks share a timestamp', async () => {
  storage()
  const task = await newStore()
  const submitted = await task.startWorkbenchInference(workflow, {
    inputs: { input: { files: ['D:/Audio/first.wav', 'D:/Audio/second.wav', 'D:/Audio/third.wav'] } },
    outputs: {}, primaryInput: 'input', outputLayout: 'flat',
  })
  assert.deepEqual(submitted.tasks.map(item => item.batchIndex), [0, 1, 2])
  // Tasks are inserted at the front of the store. Equal timestamps must still
  // produce the source order consumed by the worker's `inputIndex` field.
  for (const item of task.tasks) item.createdAt = 1
  const expected = ['D:/Audio/first.wav', 'D:/Audio/second.wav', 'D:/Audio/third.wav']
  assert.deepEqual(selectQueuedJobGroups(task.tasks, 1)[0].map(item => item.input), expected)
  for (const jobs of [task.allJobs, task.sessionJobs]) {
    assert.deepEqual(jobs[0].tasks.map(item => item.input), expected)
    assert.equal(jobs[0].primary.input, expected[0])
  }
  for (const item of task.tasks) {
    item.status = 'done'
    item.files = [item.input]
  }
  assert.deepEqual(task.resultJobs[0].tasks.map(item => item.input), expected)
  assert.equal(task.resultJobs[0].primary.input, expected[0])
})

test('scoped session cleanup retains workflow records and all underlying tasks', async () => {
  storage()
  const task = await newStore()
  const modelRun = await task.startSeparation()
  const quickRun = await task.startWorkflowInference(workflow)
  const workflowRun = await task.startWorkbenchInference(workflow, {
    inputs: { input: { files: ['D:/Workflow.wav'] } }, outputs: {}, primaryInput: 'input', outputLayout: 'flat',
  })
  task.tasks.forEach(item => { item.status = 'done' })
  assert.equal(task.clearFinishedSessionJobs([modelRun.jobId, quickRun.jobId]), 2)
  assert.deepEqual(task.sessionJobs.map(job => job.id), [workflowRun.jobId])
  assert.equal(task.tasks.length, 3)
})

for (const mode of ['model', 'workflow']) {
  const submit = (task, options) => mode === 'model'
    ? task.startSeparation(options)
    : task.startWorkflowInference(workflow, options)

  test(`${mode} submission trims paths and treats whitespace as default inheritance`, async () => {
    storage()
    const task = await newStore()
    task.separateTemporaryOutputDir = '   '
    const inherited = await submit(task, { outputDir: '   ', outputLayout: 'flat' })
    assert.equal(inherited.tasks[0].output, 'D:/DefaultOutput')
    const custom = await submit(task, { outputDir: ' E:/CustomOutput ', outputLayout: 'folders' })
    assert.equal(custom.tasks[0].jobOutput, 'E:/CustomOutput')
    assert.equal(custom.tasks[0].output, 'E:/CustomOutput/song')
    settings.outputDir = 'F:/NewDefault'
    assert.equal(custom.tasks[0].jobOutput, 'E:/CustomOutput')
    assert.equal(inherited.tasks[0].jobOutput, 'D:/DefaultOutput')
  })

  for (const layout of ['flat', 'folders']) {
    test(`${mode} retry preserves the original ${layout} directory after Settings changes`, async () => {
      storage()
      const task = await newStore()
      const submitted = await submit(task, { outputDir: 'E:/CustomOutput', outputLayout: layout })
      const original = submitted.tasks[0]
      original.status = 'failed'
      settings.outputDir = 'F:/NewDefault'
      task.separateTemporaryOutputDir = 'G:/OtherOutput'
      const retried = await task.retryTask(original.id)
      assert.equal(retried.jobOutput, 'E:/CustomOutput')
      assert.equal(retried.output, original.output)
      assert.equal(retried.runConfig.outputLayout, layout)
    })
  }

  test(`${mode} retry recovers the root of legacy folder tasks without adding another input folder`, async () => {
    storage()
    const task = await newStore()
    const submitted = await submit(task, { outputDir: 'E:/', outputLayout: 'folders' })
    const original = submitted.tasks[0]
    delete original.jobOutput
    original.status = 'failed'
    const retried = await task.retryTask(original.id)
    assert.equal(retried.jobOutput, 'E:/')
    assert.equal(retried.output, 'E:/song')
  })

  test(`${mode} retry keeps a historical absolute folder path after restart`, async () => {
    storage()
    const task = await newStore()
    const submitted = await submit(task, { outputDir: 'delivery', outputLayout: 'folders' })
    const original = submitted.tasks[0]
    const savedDirectory = 'E:/OldWorkingDirectory/delivery/song'
    task.handleWorkerEvent({
      type: 'task_done', taskId: original.id,
      payload: { outputDir: savedDirectory, files: [], outputs: [] },
    })
    assert.equal(original.jobOutput, 'delivery')
    await waitForSave()

    settings.outputDir = 'F:/NewDefault'
    const restarted = await newStore()
    const retried = await restarted.retryTask(original.id)
    assert.equal(retried.jobOutput, 'E:/OldWorkingDirectory/delivery')
    assert.equal(retried.output, savedDirectory)
  })

  test(`${mode} retry keeps the physical output directory reported by the worker`, async () => {
    storage()
    const task = await newStore()
    const submitted = await submit(task, { outputDir: 'D:/OutputAlias', outputLayout: 'folders' })
    const original = submitted.tasks[0]
    task.handleWorkerEvent({
      type: 'task_done', taskId: original.id,
      payload: { outputRoot: 'E:/PhysicalOutput', outputDir: 'E:/PhysicalOutput/song', files: [], outputs: [] },
    })
    const retried = await task.retryTask(original.id)
    assert.equal(retried.jobOutput, 'E:/PhysicalOutput')
    assert.equal(retried.output, 'E:/PhysicalOutput/song')
  })

  test(`${mode} retry preserves filesystem roots in resolved worker paths`, async () => {
    storage()
    const separator = String.fromCharCode(92)
    const roots = [
      { platform: 'win32', root: 'E:/' },
      { platform: 'win32', root: `E:${separator}` },
      { platform: 'linux', root: '/' },
      { platform: 'win32', root: `${separator}${separator}server${separator}share` },
    ]
    for (const { platform, root } of roots) {
      const task = await newStore(platform)
      const joined = root.endsWith('/') || root.endsWith(separator)
        ? `${root}song`
        : `${root}${separator}song`
      const submitted = await submit(task, { outputDir: 'delivery', outputLayout: 'folders' })
      const original = submitted.tasks[0]
      task.handleWorkerEvent({
        type: 'task_done', taskId: original.id,
        payload: { outputDir: joined, files: [], outputs: [] },
      })
      const retried = await task.retryTask(original.id)
      assert.equal(retried.jobOutput, root)
      assert.equal(retried.output, joined)
    }
  })

  for (const reportRoot of [false, true]) {
    test(`${mode} retry preserves a linked subdirectory whose physical name differs (root metadata: ${reportRoot})`, async () => {
      storage()
      const task = await newStore()
      const submitted = await submit(task, { outputDir: 'E:/Outputs', outputLayout: 'folders' })
      const original = submitted.tasks[0]
      const payload = { outputDir: 'E:/Archive/renamed-song', files: [], outputs: [] }
      if (reportRoot) payload.outputRoot = 'E:/Outputs'
      task.handleWorkerEvent({ type: 'task_done', taskId: original.id, payload })
      await waitForSave()
      const restarted = await newStore()
      const retried = await restarted.retryTask(original.id)
      assert.equal(retried.jobOutput, 'E:/Outputs')
      assert.equal(retried.output, 'E:/Outputs/song')
      restarted.handleWorkerEvent({ type: 'task_done', taskId: retried.id, payload })
      assert.equal(retried.output, original.output)
    })
  }

  test(`${mode} retry rejects legacy records whose original output root cannot be recovered`, async () => {
    storage()
    const task = await newStore()
    const original = (await submit(task, { outputDir: 'delivery', outputLayout: 'folders' })).tasks[0]
    task.handleWorkerEvent({
      type: 'task_done', taskId: original.id,
      payload: { outputDir: 'E:/Archive/renamed-song', files: [], outputs: [] },
    })
    const count = task.tasks.length
    await assert.rejects(task.retryTask(original.id), /separate.retryOutputDirectoryUnknown/)
    assert.equal(task.tasks.length, count)
  })

  for (const platform of ['linux', 'darwin']) {
    for (const outputLayout of ['flat', 'folders']) {
      test(`${mode} ${platform} history and retry preserve spaces in native ${outputLayout} output paths`, async () => {
        const values = storage()
        const task = await newStore(platform)
        task.clearInputFiles()
        task.addInputFiles(['/audio/ song .wav'])
        const original = (await submit(task, { outputDir: '/tmp/alias', outputLayout })).tasks[0]
        const outputRoot = '/tmp/physical-root '
        const outputDir = outputLayout === 'folders' ? `${outputRoot}/ song ` : outputRoot
        const output = { stem: 'vocals', path: `${outputDir}/vocals.wav` }
        task.handleWorkerEvent({
          type: 'task_done', taskId: original.id,
          payload: { outputRoot, outputDir, files: [output.path], outputs: [output] },
        })
        await waitForSave()
        const stored = JSON.parse(values.get('pymss-studio:task-history')).tasks.find(item => item.id === original.id)
        assert.equal(stored.output, outputDir)
        assert.equal(stored.jobOutput, outputRoot)
        const restarted = await newStore(platform)
        const restored = restarted.getTaskById(original.id)
        assert.equal(restored.output, outputDir)
        assert.equal(restored.jobOutput, outputRoot)
        const retried = await restarted.retryTask(original.id)
        assert.equal(retried.jobOutput, outputRoot)
        assert.equal(retried.output, outputDir)
      })
    }

    test(`${mode} ${platform} submission and retry preserve backslashes in POSIX names`, async () => {
      storage()
      const task = await newStore(platform)
      const separator = String.fromCharCode(92)
      const root = `/tmp/results${separator}`
      for (const stem of [`AC${separator}DC`, `trailing${separator}`, ` AC${separator}DC `]) {
        task.clearInputFiles()
        task.addInputFiles([`/audio/${stem}.wav`])
        const submitted = await submit(task, { outputDir: root, outputLayout: 'folders' })
        const original = submitted.tasks[0]
        const savedDirectory = `${root}/${stem}`
        assert.equal(original.output, savedDirectory)
        task.handleWorkerEvent({
          type: 'task_done', taskId: original.id,
          payload: { outputDir: savedDirectory, files: [], outputs: [] },
        })
        const retried = await task.retryTask(original.id)
        assert.equal(retried.jobOutput, root)
        assert.equal(retried.output, savedDirectory)
      }
    })

    test(`${mode} ${platform} historical POSIX output keeps its directory after restart`, async () => {
      storage()
      const task = await newStore(platform)
      const separator = String.fromCharCode(92)
      task.clearInputFiles()
      task.addInputFiles([`/audio/AC${separator}DC.wav`])
      const submitted = await submit(task, { outputDir: 'delivery', outputLayout: 'folders' })
      const original = submitted.tasks[0]
      const savedDirectory = `/tmp/results/AC${separator}DC`
      task.handleWorkerEvent({
        type: 'task_done', taskId: original.id,
        payload: { outputDir: savedDirectory, files: [], outputs: [] },
      })
      await waitForSave()
      const restarted = await newStore(platform)
      const retried = await restarted.retryTask(original.id)
      assert.equal(retried.jobOutput, '/tmp/results')
      assert.equal(retried.output, savedDirectory)
    })
  }
}

for (const count of [0, 2]) {
  test(`separation rejects a graph with ${count} inputs before creating tasks`, async () => {
    storage()
    const task = await newStore()
    const definition = { nodes: [
      ...Array.from({ length: count }, (_, index) => ({ id: index + 1, type: 'pymss_load_audio', widgets_values: ['input.wav', 'same'] })),
      { id: 10, type: 'pymss_save_audio' },
    ], links: [] }
    await assert.rejects(task.startWorkflowInference({ ...workflow, definition }), {
      message: count ? 'separate.workflowMultipleInputs' : 'separate.workflowNoInput',
    })
    assert.equal(task.tasks.length, 0)
  })
}

test('multi-input graphs retain native execution through the workflow workbench', async () => {
  storage()
  const task = await newStore()
  const definition = { nodes: [
    { id: 1, type: 'pymss_load_audio', widgets_values: ['D:/first.wav', ''] },
    { id: 2, type: 'pymss_load_audio', widgets_values: ['D:/second.wav', ''] },
    { id: 3, type: 'pymss_save_audio' },
  ], links: [] }
  const result = await task.startWorkbenchInference({ ...workflow, definition }, {
    inputs: {}, outputs: {}, primaryInput: '', outputLayout: 'flat', outputDirectory: 'D:/Output',
  })
  assert.equal(result.total, 1)
  assert.equal(result.tasks[0].runConfig.workbench.nativeGraph, true)
  assert.deepEqual(result.tasks[0].runConfig.workflowDefinition, definition)
})

test('retry cannot bypass the separation input restriction in older task snapshots', async () => {
  storage()
  const task = await newStore()
  const queued = await task.startWorkflowInference(workflow)
  const existing = task.tasks.find(item => item.id === queued.tasks[0].id)
  existing.status = 'failed'
  existing.runConfig.workflowDefinition = { nodes: [
    { id: 1, type: 'input_audio' }, { id: 2, type: 'pymss_load_audio' },
    { id: 3, type: 'pymss_save_audio' },
  ], links: [] }
  const count = task.tasks.length
  await assert.rejects(task.retryTask(existing.id), { message: 'separate.workflowMultipleInputs' })
  assert.equal(task.tasks.length, count)
})

function queuedWorkflowSnapshots(count, inputCounts, workbench = false) {
  return Array.from({ length: count }, (_, index) => ({
    id: `restored-${index}`, jobId: 'restored-workflow', model: 'Workflow',
    input: `D:/Audio/${index}.wav`, jobOutput: 'D:/Outputs', output: `D:/Outputs/${index}`,
    status: 'queued', createdAt: index + 1, updatedAt: index + 1, files: [], outputs: [], logs: [],
    runConfig: {
      runMode: 'workflow', workflowId: workflow.id, workflowName: workflow.name,
      workflowDefinition: { nodes: [
        ...Array.from({ length: inputCounts[index] }, (_, node) => ({ id: node + 1, type: 'pymss_load_audio' })),
        { id: 10, type: 'pymss_save_audio' },
      ], links: [] },
      outputLayout: 'folders', device: 'cpu', outputFormat: 'wav', inferenceParams: {},
      ...(workbench ? { workbench: { version: 1, nativeGraph: true } } : {}),
    },
  }))
}

async function resumeRestoredQueue(snapshots, concurrency = 1) {
  storage({ 'task-history': { tasks: snapshots } })
  const task = await newStore()
  settings.maxConcurrentSeparations = concurrency
  const calls = []
  const startedIds = new Set()
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    async invoke(command, args) {
      if (command === 'start_workflow_inference' || command === 'start_separation') {
        calls.push({ command, ...args.payload })
        if (startedIds.has(args.payload.taskId)) throw new Error(`task already exists: ${args.payload.taskId}`)
        startedIds.add(args.payload.taskId)
      }
      return { started: true }
    },
  }
  task.scheduleQueue()
  await new Promise(resolve => setTimeout(resolve, 0))
  return { task, calls }
}

async function restoreWorkflowQueue(count, inputCounts, workbench = false) {
  return resumeRestoredQueue(queuedWorkflowSnapshots(count, inputCounts, workbench))
}

test('a restored file workflow can be cancelled while live node metadata is loading', async () => {
  const snapshots = queuedWorkflowSnapshots(1, [0], true)
  snapshots[0].runConfig.workflowDefinition = { nodes: [{ id: 1, type: 'plugin_file_writer' }], links: [] }
  storage({ 'task-history': { tasks: snapshots } })
  const task = await newStore()
  const calls = []
  let rejectMetadata
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    invoke(command) {
      calls.push(command)
      if (command === 'list_nodes') return new Promise((_resolve, reject) => { rejectMetadata = reject })
      return Promise.resolve(null)
    },
  }
  task.scheduleQueue()
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(task.tasks[0].status, 'preparing')
  await task.cancelWorkbenchJob('restored-workflow')
  assert.equal(task.tasks[0].status, 'cancelled')
  rejectMetadata(new Error('runtime unavailable'))
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(task.tasks[0].status, 'cancelled')
  assert.ok(!calls.includes('start_workflow_inference'))
  assert.ok(!calls.includes('cancel_task'))
})

test('cancelling one file task during metadata loading requeues the rest of its batch', async () => {
  const snapshots = queuedWorkflowSnapshots(2, [0, 0], true)
  for (const snapshot of snapshots) snapshot.runConfig.workflowDefinition = { nodes: [{ id: 1, type: 'plugin_file_writer' }], links: [] }
  storage({ 'task-history': { tasks: snapshots } })
  const task = await newStore()
  const starts = []
  let resolveMetadata
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    invoke(command, args) {
      if (command === 'list_nodes') return new Promise(resolve => { resolveMetadata = resolve })
      if (command === 'start_workflow_inference') starts.push(args.payload.taskId)
      return Promise.resolve({ started: true })
    },
  }
  task.scheduleQueue()
  await new Promise(resolve => setTimeout(resolve, 0))
  await task.cancelTask(snapshots[0].id)
  resolveMetadata({ nodes: [{ type: 'plugin_file_writer', registered: true, savesOutput: true }] })
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.deepEqual(starts, [snapshots[1].id])
  assert.equal(task.tasks.find(item => item.id === snapshots[0].id).status, 'cancelled')
  assert.equal(task.tasks.find(item => item.id === snapshots[1].id).status, 'preparing')
})

for (const count of [1, 2]) {
  for (const inputs of [0, 2]) {
    test(`restoring ${count} queued workflow task(s) rejects ${inputs} inputs before dispatch`, async () => {
      const { task, calls } = await restoreWorkflowQueue(count, Array(count).fill(inputs))
      assert.equal(calls.length, 0)
      assert.equal(task.tasks.length, count)
      assert.ok(task.tasks.every(item => item.status === 'failed'))
      assert.ok(task.tasks.every(item => item.error === (inputs ? 'separate.workflowMultipleInputs' : 'separate.workflowNoInput')))
    })
  }

  test(`restoring ${count} queued single-input task(s) still dispatches normally`, async () => {
    const { task, calls } = await restoreWorkflowQueue(count, Array(count).fill(1))
    assert.equal(calls.length, 1)
    assert.equal(Boolean(calls[0].tasks), count > 1)
    assert.ok(task.tasks.every(item => item.status === 'preparing'))
  })

  test(`restoring ${count} queued multi-input workbench task(s) preserves editor execution`, async () => {
    const { task, calls } = await restoreWorkflowQueue(count, Array(count).fill(2), true)
    assert.equal(calls.length, 1)
    assert.ok(task.tasks.every(item => item.status === 'preparing'))
  })
}

test('a restored workflow batch validates every task before starting the worker', async () => {
  const { task, calls } = await restoreWorkflowQueue(2, [1, 2])
  assert.equal(calls.length, 0)
  assert.ok(task.tasks.every(item => item.status === 'failed' && item.error === 'separate.workflowMultipleInputs'))
})

function queuedBatch(id, inputCount, mode = 'workflow', createdAt = 1) {
  return queuedWorkflowSnapshots(2, [inputCount, inputCount]).map((task, index) => ({
    ...task, id: `${id}-${index}`, jobId: id, createdAt: createdAt + index,
    runConfig: { ...task.runConfig, runMode: mode },
  }))
}

for (const mode of ['workflow', 'model']) {
  test(`a rejected batch cannot resubmit the following ${mode} batch during concurrent scheduling`, async () => {
    const { task, calls } = await resumeRestoredQueue([
      ...queuedBatch('invalid', 2), ...queuedBatch('valid', 1, mode, 10),
    ], 2)
    assert.equal(calls.length, 1)
    assert.equal(calls[0].command, mode === 'workflow' ? 'start_workflow_inference' : 'start_separation')
    assert.equal(calls[0].taskId, 'valid-0')
    assert.ok(task.tasks.filter(item => item.jobId === 'invalid').every(item => item.status === 'failed'))
    assert.ok(task.tasks.filter(item => item.jobId === 'valid').every(item => item.status === 'preparing' && !item.error))
  })
}

test('rejected batches free Worker slots without duplicate starts or starving later jobs', async () => {
  const invalid = Array.from({ length: 25 }, (_, index) => queuedBatch(`invalid-${index}`, 2, 'workflow', index * 2 + 1)).flat()
  const { task, calls } = await resumeRestoredQueue([
    ...invalid, ...queuedBatch('first', 1, 'workflow', 100),
    ...queuedBatch('second', 1, 'model', 110), ...queuedBatch('third', 1, 'workflow', 120),
  ], 2)
  assert.deepEqual(calls.map(call => call.taskId), ['first-0', 'second-0'])
  assert.ok(task.tasks.filter(item => item.jobId.startsWith('invalid-')).every(item => item.status === 'failed'))
  assert.ok(task.tasks.filter(item => item.jobId === 'third').every(item => item.status === 'queued'))
  task.scheduleQueue()
  assert.equal(calls.length, 2)
  for (const id of ['first-0', 'first-1']) {
    task.handleWorkerEvent({ type: 'task_done', taskId: id, payload: { outputDir: 'D:/Outputs', files: [], outputs: [] } })
  }
  assert.deepEqual(calls.map(call => call.taskId), ['first-0', 'second-0', 'third-0'])
  assert.ok(task.tasks.filter(item => item.jobId === 'third').every(item => item.status === 'preparing'))
})

for (const platform of ['linux', 'darwin']) {
  test(`${platform} output selection preserves significant trailing spaces in native directories`, async () => {
    storage()
    const task = await newStore(platform)
    settings.outputDir = '/tmp/default '
    assert.equal(task.separateOutputDir, '/tmp/default ')
    task.separateTemporaryOutputDir = '/tmp/selected '
    assert.equal(task.separateOutputDir, '/tmp/selected ')
    const run = await task.startWorkflowInference(workflow, { outputDir: '/tmp/chosen ', outputLayout: 'flat' })
    assert.equal(run.tasks[0].output, '/tmp/chosen ')
    assert.equal(run.tasks[0].jobOutput, '/tmp/chosen ')
    const bindings = { inputs: { input: { files: ['/audio/song.wav'] } }, outputs: {},
      outputDirectory: '/tmp/workbench ', primaryInput: 'input', outputLayout: 'folders' }
    const prepared = (await task.startWorkbenchInference({ ...workflow, updatedAt: 1 }, bindings)).tasks[0]
    assert.equal(prepared.output, '/tmp/workbench /song')
    assert.equal(prepared.runConfig.workbench.outputs['save:split:vocals'], '/tmp/workbench /song')
  })

  test(`simple workbench ${platform} folders preserve native backslashes through submission and retry`, async () => {
    storage()
    const task = await newStore(platform)
    settings.outputDir = '/tmp/default'
    const root = '/tmp/results\\'
    const bindings = { inputs: { input: { files: ['/audio/AC\\DC.wav'] } }, outputs: {},
      outputDirectory: root, primaryInput: 'input', outputLayout: 'folders' }
    const original = (await task.startWorkbenchInference({ ...workflow, updatedAt: 1 }, bindings)).tasks[0]
    const directory = `${root}/AC\\DC`
    assert.equal(original.output, directory)
    assert.equal(original.runConfig.workbench.outputs['save:split:vocals'], directory)
    original.status = 'failed'
    settings.outputDir = '/tmp/changed'
    const retried = await task.retryTask(original.id)
    assert.equal(retried.output, directory)
    assert.deepEqual(retried.runConfig.workbench.outputs, original.runConfig.workbench.outputs)
  })

  test(`legacy ${platform} files-only responses preserve native paths and filenames`, async () => {
    const values = storage()
    const task = await newStore(platform)
    task.clearInputFiles()
    task.addInputFiles(['/audio/song.mix.wav'])
    const root = '/tmp/results\\'
    const original = (await task.startWorkflowInference(workflow, { outputDir: root, outputLayout: 'folders' })).tasks[0]
    original.outputPrefix = undefined
    task.handleWorkerEvent({ type: 'task_done', taskId: original.id,
      payload: { outputDir: root, outputRoot: root, files: ['vocals.wav', 'parts/voice.flac', 'tail.wav ', 'AC\\DC.wav', 'song.mix_vocals.wav', '../input.wav'], outputs: [] } })
    const expected = [`${root}/vocals.wav`, `${root}/parts/voice.flac`, `${root}/tail.wav `, `${root}/AC\\DC.wav`, `${root}/song.mix_vocals.wav`]
    assert.deepEqual(original.outputs.map(output => output.path), expected)
    assert.equal(original.outputs.at(-1).stem, 'vocals')
    assert.deepEqual(task.resultTrashTargets(original), expected)
    await waitForSave()
    const restored = (await newStore(platform)).getTaskById(original.id)
    assert.deepEqual(restored.outputs.map(output => output.path), expected)
    const data = JSON.parse(values.get('pymss-studio:task-history'))
    data.tasks.find(item => item.id === original.id).outputs = []
    globalThis.localStorage.setItem('pymss-studio:task-history', JSON.stringify(data))
    const legacyRestored = (await newStore(platform)).getTaskById(original.id)
    assert.deepEqual(legacyRestored.outputs.map(output => output.path), expected)
  })
}
