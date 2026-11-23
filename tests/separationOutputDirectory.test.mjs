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
            modelDir: '',
            outputDir: 'D:/DefaultOutput', downloadSource: 'modelscope', downloadMethod: 'aria2c',
            defaultFormat: 'wav', developerMode: false,
            getRuntimeDeviceConfig: () => ({ device: 'cpu', deviceIds: [] }),
            getAudioParams: () => ({}),
          })
          export function useSettingsStore() { return settings }
        `
      }
      if (path.endsWith('/src/i18n/index.ts')) {
        return `
          import { createI18n } from 'vue-i18n'
          import zh from '/src/i18n/zh-CN.json'
          import en from '/src/i18n/en.json'
          const { global } = createI18n({ legacy: false, locale: 'zh-CN', messages: { 'zh-CN': zh, en } })
          export default { global: { locale: global.locale, t: (key, ...args) => key.startsWith('settings.taskNotification.') ? global.t(key, ...args) : key } }
        `
      }
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
const { describeTaskReminder } = await vite.ssrLoadModule('/src/features/tasks/reminders.ts')
const { default: i18n } = await vite.ssrLoadModule('/src/i18n/index.ts')
const stores = []
const waitForSave = () => new Promise(resolve => setTimeout(resolve, 180))

afterEach(async () => {
  await waitForSave()
  for (const store of stores.splice(0)) store.$dispose()
  Reflect.deleteProperty(globalThis, 'window')
  Reflect.deleteProperty(globalThis, 'localStorage')
  settings.modelDir = ''
  settings.outputDir = 'D:/DefaultOutput'
  settings.downloadSource = 'modelscope'
  settings.maxConcurrentSeparations = 1
  settings.taskSystemNotifications = false
  settings.appPaths = null
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
  const resolution = {
    version: 1, sampleRate: 44100, fingerprint: 'output-directory-model-v1',
    defaults: { batch_size: 1, chunk_size: 573300, overlap_size: 28665, standardize: false, normalize: false },
    capabilities: { batch_size: { kind: 'free', min: 1 }, chunk_size: { kind: 'free', min: 1025 }, overlap_size: { kind: 'free', min: 0 }, standardize: { kind: 'free' }, normalize: { kind: 'free' } },
    overlap: { kind: 'samples' },
  }
  const entry = { name: 'model-a', modelType: 'bs_roformer', inferenceParamMeta: { resolution }, defaultInferenceParams: resolution.defaults }
  model.models = [entry]
  model.selectedInfo = entry
  // Output-path tests supply metadata at the native boundary; no browser/worker is running here.
  model.ensureModelMetadata = async () => entry
  model.selectedModel = 'model-a'
  task.applySelectedModelDefaults(resolution.defaults, 'bs_roformer', undefined, model.getModelInferenceOverrides('model-a'))
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

test('workflow submission uses the supplied input snapshot without changing the current selection', async () => {
  storage()
  const task = await newStore()
  const inputFiles = ['D:/Audio/original.wav', 'D:/Audio/second.wav']
  const submitted = await task.startWorkflowInference(workflow, { inputFiles })
  inputFiles.push('D:/Audio/later.wav')
  assert.deepEqual(submitted.tasks.map(item => item.input), ['D:/Audio/original.wav', 'D:/Audio/second.wav'])
  assert.deepEqual(task.inputFiles, ['D:/Audio/song.wav'])
})

test('an explicitly empty workflow input snapshot never falls back to the current selection', async () => {
  storage()
  const task = await newStore()
  await assert.rejects(task.startWorkflowInference(workflow, { inputFiles: [] }), /separate.startHintNoInput/)
  assert.equal(task.tasks.length, 0)
})

test('clearing submitted inputs preserves unsubmitted paths and still supports clearing all', async () => {
  storage()
  const task = await newStore()
  task.addInputFiles(['D:/Audio/next.wav'])
  task.clearInputFiles([])
  assert.deepEqual(task.inputFiles, ['D:/Audio/song.wav', 'D:/Audio/next.wav'])
  task.clearInputFiles(['D:/Audio/song.wav'])
  assert.deepEqual(task.inputFiles, ['D:/Audio/next.wav'])
  task.clearInputFiles()
  assert.deepEqual(task.inputFiles, [])
})

test('new inference tasks freeze resolved parameters; retry and a new run have distinct settings', async () => {
  storage()
  const task = await newStore()
  const model = useModelStore()
  task.selectedStems = ['vocals']
  await task.applyParameterSettings('custom', { chunkMilliseconds: 10000, overlapBasisPoints: 5000 })
  const first = (await task.startSeparation()).tasks[0]
  assert.equal(first.runConfig.inferenceParamsVersion, 4)
  assert.equal(first.runConfig.inferenceParams.chunk_size, 441000)
  first.status = 'failed'
  await model.setModelInferenceOverrides('model-a', { batch_size: 3 })
  await task.applyParameterSettings('custom', { chunkMilliseconds: 7000, overlapBasisPoints: 7500 }, true)
  const retry = await task.retryTask(first.id)
  assert.deepEqual(retry.runConfig, JSON.parse(JSON.stringify(first.runConfig)))
  assert.equal(retry.runConfig.useTta, false)
  const fresh = await task.rerunTaskWithCurrentParameters(first.id)
  assert.equal(fresh.runConfig.inferenceParams.chunk_size, 308700)
  assert.equal(fresh.runConfig.inferenceParams.overlap_size, 231525)
  assert.equal(fresh.runConfig.inferenceParams.batch_size, 3)
  assert.equal(fresh.runConfig.useTta, true)
  assert.deepEqual(fresh.runConfig.selectedStems, ['vocals'])
  assert.equal(first.runConfig.inferenceParams.batch_size, 1)
})

test('audio track choices are frozen on submission and retained by retry and a new-parameter run', async () => {
  storage()
  const task = await newStore()
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    async invoke(command) {
      if (command === 'get_audio_metadata') {
        return {
          audioTrackSelectionSupported: true,
          audioTracksByPath: {
            'D:/Audio/dual.mkv': [
              { index: 1, language: 'jpn', title: '', channels: 2 },
              { index: 2, language: 'eng', title: '', channels: 2 },
            ],
          },
          audioTrackErrorsByPath: {},
        }
      }
      return { started: true }
    },
  }
  task.addInputFiles(['D:/Audio/dual.mkv'])
  await new Promise(resolve => setTimeout(resolve, 10))
  task.inputTrackSelection['D:/Audio/dual.mkv'] = [2]
  const original = (await task.startSeparation()).tasks.find(item => item.input.endsWith('dual.mkv'))
  assert.deepEqual(original.audioTracks, [2])
  task.inputTrackSelection['D:/Audio/dual.mkv'] = [1]
  const retry = await task.retryTask(original.id)
  const fresh = await task.rerunTaskWithCurrentParameters(original.id)
  assert.deepEqual(retry.audioTracks, [2])
  assert.deepEqual(fresh.audioTracks, [2])
})

test('audio-track metadata uses one bounded queue and stale responses cannot affect a re-added path', async () => {
  storage()
  const task = await newStore()
  const calls = []
  const pending = []
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    invoke(command, args) {
      if (command !== 'get_audio_metadata') return Promise.resolve({ started: true })
      calls.push(args.payload.paths)
      return new Promise(resolve => pending.push(resolve))
    },
  }
  const videos = Array.from({ length: 17 }, (_, index) => `D:/Audio/${index}.mkv`)
  task.addInputFiles(videos)
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(calls.length, 1)
  assert.ok(calls[0].length <= 8)
  pending.shift()({ audioTrackSelectionSupported: true, audioTracksByPath: {}, audioTrackErrorsByPath: {} })
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(calls.length, 2)
  assert.ok(calls[1].length <= 8)
  pending.shift()({ audioTrackSelectionSupported: true, audioTracksByPath: {}, audioTrackErrorsByPath: {} })
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(calls.length, 3)
  assert.ok(calls[2].length <= 8)
  pending.shift()({ audioTrackSelectionSupported: true, audioTracksByPath: {}, audioTrackErrorsByPath: {} })

  const path = 'D:/Audio/re-added.mkv'
  task.addInputFiles([path])
  await new Promise(resolve => setTimeout(resolve, 0))
  task.removeInputFile(path)
  task.addInputFiles([path])
  pending.shift()({ audioTrackSelectionSupported: true, audioTracksByPath: { [path]: [{ index: 1, language: 'old', title: '', channels: 2 }, { index: 2, language: 'old', title: '', channels: 2 }] }, audioTrackErrorsByPath: {} })
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(task.inputAudioTracks[path], undefined)
  pending.shift()({ audioTrackSelectionSupported: true, audioTracksByPath: { [path]: [{ index: 1, language: 'new', title: '', channels: 2 }, { index: 2, language: 'new', title: '', channels: 2 }] }, audioTrackErrorsByPath: {} })
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.deepEqual(task.inputTrackSelection[path], [1, 2])
  assert.equal(task.inputAudioTracks[path][0].language, 'new')
})

test('removing queued videos stops their metadata work before a worker is started', async () => {
  storage()
  const task = await newStore()
  const calls = []
  let finishCurrent
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    invoke(command, args) {
      if (command !== 'get_audio_metadata') return Promise.resolve({ started: true })
      calls.push(args.payload.paths)
      return new Promise(resolve => { finishCurrent = resolve })
    },
  }
  task.addInputFiles(Array.from({ length: 20 }, (_, index) => `D:/Video/remove-${index}.mkv`))
  assert.equal(calls.length, 1)
  task.clearInputFiles()
  finishCurrent({ audioTrackSelectionSupported: true, audioTracksByPath: {}, audioTrackErrorsByPath: {} })
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(calls.length, 1)
  assert.deepEqual(task.audioTrackProbeState, {})
  assert.deepEqual(task.inputTrackSelection, {})
})

test('submission waits for unresolved audio track discovery before preparing the model', async () => {
  storage()
  const task = await newStore()
  const model = useModelStore()
  let finishProbe
  let prepared = 0
  const ensureMetadata = model.ensureModelMetadata
  model.ensureModelMetadata = async (...args) => { prepared++; return ensureMetadata(...args) }
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    invoke(command) {
      if (command === 'get_audio_metadata') return new Promise(resolve => { finishProbe = resolve })
      return Promise.resolve({ started: true })
    },
  }
  const path = 'D:/Video/wait-for-probe.mkv'
  task.addInputFiles([path])
  const pendingStart = task.startSeparation()
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(task.tasks.length, 0)
  assert.equal(prepared, 0)
  finishProbe({ audioTrackSelectionSupported: true, audioTracksByPath: { [path]: [
    { index: 1, language: 'jpn', title: '', channels: 2 },
    { index: 2, language: 'eng', title: '', channels: 2 },
  ] }, audioTrackErrorsByPath: {} })
  const run = await pendingStart
  assert.deepEqual(run.tasks.find(item => item.input === path).audioTracks, [1, 2])
})

test('submission freezes known track choices while another file is still being probed', async () => {
  storage()
  const task = await newStore()
  const known = 'D:/Video/known.mkv'
  const waiting = 'D:/Video/waiting.mkv'
  const tracks = [
    { index: 1, language: 'jpn', title: '', channels: 2 },
    { index: 2, language: 'eng', title: '', channels: 2 },
  ]
  let finishProbe
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    invoke(command) {
      if (command === 'get_audio_metadata') return new Promise(resolve => { finishProbe = resolve })
      return Promise.resolve({ started: true })
    },
  }
  task.addInputFiles([known])
  finishProbe({ audioTrackSelectionSupported: true, audioTracksByPath: { [known]: tracks }, audioTrackErrorsByPath: {} })
  await new Promise(resolve => setTimeout(resolve, 0))
  task.inputTrackSelection[known] = [2]
  task.addInputFiles([waiting])
  const start = task.startSeparation()
  task.inputTrackSelection[known] = [1]
  finishProbe({ audioTrackSelectionSupported: true, audioTracksByPath: { [waiting]: tracks }, audioTrackErrorsByPath: {} })
  const run = await start
  assert.deepEqual(run.tasks.find(item => item.input === known).audioTracks, [2])
  assert.deepEqual(run.tasks.find(item => item.input === waiting).audioTracks, [1, 2])
})

test('submission waits for metadata, freezes selection before model metadata, and rejects probe errors', async () => {
  storage()
  const task = await newStore()
  const model = useModelStore()
  let resolveTracks
  let resolveModel
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    invoke(command) {
      if (command === 'get_audio_metadata') return new Promise(resolve => { resolveTracks = resolve })
      return Promise.resolve({ started: true })
    },
  }
  task.addInputFiles(['D:/Audio/wait.mkv'])
  resolveTracks({ audioTrackSelectionSupported: true, audioTracksByPath: { 'D:/Audio/wait.mkv': [{ index: 1, language: 'jpn', title: '', channels: 2 }, { index: 2, language: 'eng', title: '', channels: 2 }] }, audioTrackErrorsByPath: {} })
  await new Promise(resolve => setTimeout(resolve, 0))
  task.inputTrackSelection['D:/Audio/wait.mkv'] = [2]
  model.ensureModelMetadata = () => new Promise(resolve => { resolveModel = resolve })
  const start = task.startSeparation()
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(task.tasks.length, 0)
  // The selection was frozen before the asynchronous model lookup begins.
  task.inputTrackSelection['D:/Audio/wait.mkv'] = [1]
  resolveModel(model.selectedInfo)
  const result = await start
  assert.deepEqual(result.tasks.find(item => item.input.endsWith('wait.mkv')).audioTracks, [2])

  window.__TAURI_INTERNALS__.invoke = (command) => command === 'get_audio_metadata'
    ? Promise.resolve({ audioTrackSelectionSupported: true, audioTracksByPath: { 'D:/Audio/broken.mkv': [] }, audioTrackErrorsByPath: { 'D:/Audio/broken.mkv': 'invalid media' } })
    : Promise.resolve({ started: true })
  model.ensureModelMetadata = async () => model.selectedInfo
  task.addInputFiles(['D:/Audio/broken.mkv'])
  await new Promise(resolve => setTimeout(resolve, 0))
  await assert.rejects(task.startSeparation(), /separate.audioTrackProbeFailedForFile/)
})

test('old kernels keep normal separation without a picker and restored retries preserve explicit and absent choices', async () => {
  storage({
    'task-history': { tasks: [
      { id: 'track-two', model: 'model-a', input: 'D:/Audio/history.mkv', audioTracks: [2], output: 'D:/Outputs/history.wav', status: 'failed', createdAt: 1, updatedAt: 1, progress: 100, files: [], outputs: [], logs: [], runConfig: { outputLayout: 'flat', inferenceParams: {} } },
      { id: 'all-default', model: 'model-a', input: 'D:/Audio/default.mkv', output: 'D:/Outputs/default.wav', status: 'failed', createdAt: 2, updatedAt: 2, progress: 100, files: [], outputs: [], logs: [], runConfig: { outputLayout: 'flat', inferenceParams: {} } },
    ] },
  })
  const task = await newStore()
  task.inputAudioTracks['D:/Audio/history.mkv'] = [{ index: 1, language: 'jpn', title: '', channels: 2 }, { index: 2, language: 'eng', title: '', channels: 2 }]
  task.inputTrackSelection['D:/Audio/history.mkv'] = [1]
  assert.deepEqual((await task.retryTask('track-two')).audioTracks, [2])
  assert.equal((await task.retryTask('all-default')).audioTracks, undefined)

  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    async invoke(command) {
      if (command === 'get_audio_metadata') return { audioTrackSelectionSupported: false, audioTracksByPath: {}, audioTrackErrorsByPath: {} }
      return { started: true }
    },
  }
  task.addInputFiles(['D:/Audio/legacy.mkv'])
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(task.audioTrackSelectionSupported, false)
  assert.equal(task.inputAudioTracks['D:/Audio/legacy.mkv'], undefined)
  const created = await task.startSeparation()
  assert.equal(created.tasks.find(item => item.input.endsWith('legacy.mkv')).audioTracks, undefined)
})

test('upgrading the kernel re-probes already imported videos before submission', async () => {
  storage()
  const task = await newStore()
  task.clearInputFiles()
  const app = useAppStore()
  app.envInfo = { platform: 'win32', pymssVersion: '2.3.5' }
  const path = 'D:/Audio/upgrade.mkv'
  const tracks = [{ index: 1, language: 'jpn', title: 'Original', channels: 2 }, { index: 2, language: 'eng', title: 'English', channels: 2 }]
  let supported = false
  let calls = 0
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    async invoke(command) {
      if (command !== 'get_audio_metadata') return { started: true }
      calls++
      return { audioTrackSelectionSupported: supported, audioTracksByPath: supported ? { [path]: tracks } : {}, audioTrackErrorsByPath: {} }
    },
  }
  task.addInputFiles([path])
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(task.audioTrackSelectionSupported, false)
  supported = true
  app.envInfo = { ...app.envInfo, pymssVersion: '2.4.0' }
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(calls, 2)
  assert.equal(task.audioTrackSelectionSupported, true)
  assert.deepEqual((await task.startSeparation()).tasks[0].audioTracks, [1, 2])
})

test('runtime refresh preserves valid and empty choices and does not silently replace a missing track', async () => {
  storage()
  const task = await newStore()
  task.clearInputFiles()
  const app = useAppStore()
  app.envInfo = { platform: 'win32', pymssVersion: '2.4.0' }
  app.runtimeInfo = { installedBackend: 'cpu', installState: { pythonPath: 'D:/Runtime/old/python.exe' } }
  const paths = ['D:/Audio/chosen.mkv', 'D:/Audio/empty.mkv', 'D:/Audio/replaced.mkv']
  const tracks = [{ index: 1, language: 'jpn', title: 'Original', channels: 2 }, { index: 2, language: 'eng', title: 'English', channels: 2 }]
  let replaced = false
  let calls = 0
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    async invoke(command, args) {
      if (command !== 'get_audio_metadata') return { started: true }
      calls++
      return { audioTrackSelectionSupported: true, audioTracksByPath: Object.fromEntries(args.payload.paths.map(path => [path, replaced && path === paths[2] ? tracks.slice(0, 1) : tracks])), audioTrackErrorsByPath: {} }
    },
  }
  task.addInputFiles(paths)
  await new Promise(resolve => setTimeout(resolve, 0))
  task.inputTrackSelection[paths[0]] = [2]
  task.inputTrackSelection[paths[1]] = []
  task.inputTrackSelection[paths[2]] = [2]
  replaced = true
  app.runtimeInfo = { installedBackend: 'cpu', installState: { pythonPath: 'D:/Runtime/new/python.exe' } }
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.deepEqual(task.inputTrackSelection[paths[0]], [2])
  assert.deepEqual(task.inputTrackSelection[paths[1]], [])
  assert.deepEqual(task.inputTrackSelection[paths[2]], [])
  assert.equal(task.inputAudioTracks[paths[2]].length, 1)
  const afterSwitch = calls
  app.runtimeInfo = { ...app.runtimeInfo }
  app.envInfo = { ...app.envInfo, cudaDeviceCount: 0 }
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(calls, afterSwitch)
})

test('metadata returned by the previous runtime cannot overwrite the new probe', async () => {
  storage()
  const task = await newStore()
  task.clearInputFiles()
  const app = useAppStore()
  app.envInfo = { platform: 'win32', pymssVersion: '2.3.5' }
  const path = 'D:/Audio/runtime-race.mkv'
  const pending = []
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    invoke(command) {
      if (command !== 'get_audio_metadata') return Promise.resolve({ started: true })
      return new Promise(resolve => pending.push(resolve))
    },
  }
  task.addInputFiles([path])
  app.envInfo = { ...app.envInfo, pymssVersion: '2.4.0' }
  pending.shift()({ audioTrackSelectionSupported: false, audioTracksByPath: {}, audioTrackErrorsByPath: {} })
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(task.audioTrackSelectionSupported, null)
  assert.equal(task.audioTrackProbeState[path], 'loading')
  assert.equal(pending.length, 1)
  const tracks = [{ index: 1, language: 'jpn', title: 'Original', channels: 2 }, { index: 2, language: 'eng', title: 'English', channels: 2 }]
  pending.shift()({ audioTrackSelectionSupported: true, audioTracksByPath: { [path]: tracks }, audioTrackErrorsByPath: {} })
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(task.audioTrackSelectionSupported, true)
  assert.deepEqual(task.inputTrackSelection[path], [1, 2])
})

test('a runtime change during submission requires confirmation of the refreshed selection', async () => {
  storage()
  const task = await newStore()
  task.clearInputFiles()
  const app = useAppStore()
  app.envInfo = { platform: 'win32', pymssVersion: '2.3.5' }
  const path = 'D:/Audio/submitting.mkv'
  const pending = []
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    invoke(command) {
      if (command !== 'get_audio_metadata') return Promise.resolve({ started: true })
      return new Promise(resolve => pending.push(resolve))
    },
  }
  task.addInputFiles([path])
  const submitted = assert.rejects(task.startSeparation(), /separate.audioTrackRuntimeChanged/)
  app.envInfo = { ...app.envInfo, pymssVersion: '2.4.0' }
  pending.shift()({ audioTrackSelectionSupported: false, audioTracksByPath: {}, audioTrackErrorsByPath: {} })
  await new Promise(resolve => setTimeout(resolve, 0))
  if (pending.length) pending.shift()({ audioTrackSelectionSupported: true, audioTracksByPath: { [path]: [
    { index: 1, language: 'jpn', title: '', channels: 2 }, { index: 2, language: 'eng', title: '', channels: 2 },
  ] }, audioTrackErrorsByPath: {} })
  await submitted
  assert.equal(task.tasks.length, 0)
})

test('a runtime change while preparing model metadata cannot submit an old selection', async () => {
  storage()
  const task = await newStore()
  task.clearInputFiles()
  const app = useAppStore()
  app.envInfo = { platform: 'win32', pymssVersion: '2.4.0' }
  const path = 'D:/Audio/model-wait.mkv'
  const tracks = [{ index: 1, language: 'jpn', title: '', channels: 2 }, { index: 2, language: 'eng', title: '', channels: 2 }]
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    async invoke(command) {
      if (command === 'get_audio_metadata') return { audioTrackSelectionSupported: true, audioTracksByPath: { [path]: tracks }, audioTrackErrorsByPath: {} }
      return { started: true }
    },
  }
  task.addInputFiles([path])
  await new Promise(resolve => setTimeout(resolve, 0))
  task.inputTrackSelection[path] = [2]
  const model = useModelStore()
  let finishModel
  model.ensureModelMetadata = () => new Promise(resolve => { finishModel = resolve })
  const submitted = assert.rejects(task.startSeparation(), /separate.audioTrackRuntimeChanged/)
  await new Promise(resolve => setTimeout(resolve, 0))
  app.runtimeInfo = { installedBackend: 'cpu', installState: { pythonPath: 'D:/Runtime/new/python.exe' } }
  finishModel(model.selectedInfo)
  await submitted
  assert.equal(task.tasks.length, 0)
})

for (const choice of [[1, 2], [2]]) {
  test(`an unavailable track API rejects retained choices ${choice} before creating batch tasks`, async () => {
    storage()
    const task = await newStore()
    task.clearInputFiles()
    const app = useAppStore()
    app.envInfo = { platform: 'win32', pymssVersion: '2.4.0' }
    const path = 'D:/Audio/track-api.mkv'
    const tracks = [{ index: 1, language: 'jpn', title: '', channels: 2 }, { index: 2, language: 'eng', title: '', channels: 2 }]
    let supported = true
    let started = 0
    window.__TAURI_INTERNALS__ = {
      metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
      async invoke(command) {
        if (command === 'get_audio_metadata') return {
          audioTrackSelectionSupported: supported, audioTracksByPath: supported ? { [path]: tracks } : {}, audioTrackErrorsByPath: {},
        }
        if (command === 'start_separation') started++
        return { started: true }
      },
    }
    task.addInputFiles([path, 'D:/Audio/normal.wav'])
    await new Promise(resolve => setTimeout(resolve, 0))
    if (choice.length === 1) task.inputTrackSelection[path] = [...choice]
    supported = false
    app.runtimeInfo = { installedBackend: 'cpu', installState: { pythonPath: 'D:/Runtime/unavailable/python.exe' } }
    await new Promise(resolve => setTimeout(resolve, 0))
    assert.equal(task.inputAudioTracks[path], undefined)
    assert.deepEqual(task.inputTrackSelection[path], choice)
    await assert.rejects(task.startSeparation(), /separate.audioTrackSelectionUnavailable/)
    assert.equal(task.tasks.length, 0)
    assert.equal(started, 0)
    assert.deepEqual(task.inputFiles, [path, 'D:/Audio/normal.wav'])
  })
}

for (const choice of [[2], [1, 3]]) {
  test(`submission during a re-probe rejects missing tracks in frozen choices ${choice}`, async () => {
    storage()
    const task = await newStore()
    task.clearInputFiles()
    const app = useAppStore()
    app.envInfo = { platform: 'win32', pymssVersion: '2.4.0' }
    const path = 'D:/Audio/refreshed.mkv'
    const tracks = [1, 2, 3].map(index => ({ index, language: 'eng', title: '', channels: 2 }))
    let reprobing = false
    let finishProbe
    window.__TAURI_INTERNALS__ = {
      metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
      invoke(command) {
        if (command !== 'get_audio_metadata') return Promise.resolve({ started: true })
        if (reprobing) return new Promise(resolve => { finishProbe = resolve })
        return Promise.resolve({ audioTrackSelectionSupported: true, audioTracksByPath: { [path]: tracks }, audioTrackErrorsByPath: {} })
      },
    }
    task.addInputFiles([path])
    await new Promise(resolve => setTimeout(resolve, 0))
    task.inputTrackSelection[path] = [...choice]
    reprobing = true
    app.runtimeInfo = { installedBackend: 'cpu', installState: { pythonPath: 'D:/Runtime/refreshed/python.exe' } }
    const submitted = assert.rejects(task.startSeparation(), /separate.audioTrackSelectionInvalidForFile/)
    finishProbe({ audioTrackSelectionSupported: true, audioTracksByPath: { [path]: tracks.slice(0, choice.length) }, audioTrackErrorsByPath: {} })
    await submitted
    assert.equal(task.tasks.length, 0)
    assert.deepEqual(task.inputTrackSelection[path], choice.filter(index => index <= choice.length))
  })
}

test('submission retries a transient metadata failure once and clears its error', async () => {
  storage()
  const task = await newStore()
  task.clearInputFiles()
  const path = 'D:/Audio/busy.mkv'
  let busy = true
  let calls = 0
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    async invoke(command) {
      if (command !== 'get_audio_metadata') return { started: true }
      calls++
      if (busy) throw new Error('RUNTIME_BUSY: The runtime is in use')
      return { audioTrackSelectionSupported: true, audioTracksByPath: { [path]: [
        { index: 1, language: 'jpn', title: '', channels: 2 }, { index: 2, language: 'eng', title: '', channels: 2 },
      ] }, audioTrackErrorsByPath: {} }
    },
  }
  task.addInputFiles([path])
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(task.audioTrackProbeState[path], 'failed')
  assert.match(task.audioTrackProbeErrors[path], /RUNTIME_BUSY/)
  await assert.rejects(task.startSeparation(), /separate.audioTrackProbeFailedForFile/)
  assert.equal(calls, 2)
  assert.equal(task.tasks.length, 0)
  busy = false
  const created = await task.startSeparation()
  assert.equal(calls, 3)
  assert.deepEqual(created.tasks[0].audioTracks, [1, 2])
  assert.equal(task.audioTrackProbeState[path], undefined)
  assert.equal(task.audioTrackProbeErrors[path], undefined)
})

test('file metadata errors require an explicit retry and duplicate retry clicks do not enqueue extra work', async () => {
  storage()
  const task = await newStore()
  task.clearInputFiles()
  const path = 'D:/Audio/repaired.mkv'
  let calls = 0
  let finishRetry
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    invoke(command) {
      if (command !== 'get_audio_metadata') return Promise.resolve({ started: true })
      calls++
      if (calls === 1) return Promise.resolve({ audioTrackSelectionSupported: true, audioTracksByPath: {}, audioTrackErrorsByPath: { [path]: 'Invalid media' } })
      return new Promise(resolve => { finishRetry = resolve })
    },
  }
  task.addInputFiles([path])
  await new Promise(resolve => setTimeout(resolve, 0))
  await assert.rejects(task.startSeparation(), /separate.audioTrackProbeFailedForFile/)
  assert.equal(calls, 1)
  assert.equal(task.retryAudioTrackProbe(path), true)
  assert.equal(task.audioTrackProbeErrors[path], undefined)
  assert.equal(task.retryAudioTrackProbe(path), false)
  assert.equal(calls, 2)
  finishRetry({ audioTrackSelectionSupported: true, audioTracksByPath: { [path]: [
    { index: 1, language: 'jpn', title: '', channels: 2 }, { index: 2, language: 'eng', title: '', channels: 2 },
  ] }, audioTrackErrorsByPath: {} })
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.deepEqual((await task.startSeparation()).tasks[0].audioTracks, [1, 2])
  assert.equal(task.retryAudioTrackProbe(path), false)
})

test('retrying after a runtime probe failure retains explicit track choices', async () => {
  storage()
  const task = await newStore()
  task.clearInputFiles()
  const path = 'D:/Audio/choice.mkv'
  const app = useAppStore()
  app.envInfo = { platform: 'win32', pymssVersion: '2.4.0' }
  let busy = false
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    async invoke(command) {
      if (command !== 'get_audio_metadata') return { started: true }
      if (busy) throw 'RUNTIME_BUSY: Updating the runtime'
      return { audioTrackSelectionSupported: true, audioTracksByPath: { [path]: [
        { index: 1, language: 'jpn', title: '', channels: 2 }, { index: 2, language: 'eng', title: '', channels: 2 },
      ] }, audioTrackErrorsByPath: {} }
    },
  }
  task.addInputFiles([path])
  await new Promise(resolve => setTimeout(resolve, 0))
  task.inputTrackSelection[path] = [2]
  busy = true
  app.runtimeInfo = { installedBackend: 'cpu', installState: { pythonPath: 'D:/Runtime/refreshed/python.exe' } }
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(task.audioTrackProbeState[path], 'failed')
  busy = false
  assert.equal(task.retryAudioTrackProbe(path), true)
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.deepEqual(task.inputTrackSelection[path], [2])
  assert.deepEqual((await task.startSeparation()).tasks[0].audioTracks, [2])
})

test('removing failed inputs clears retry classification before the same path is added again', async () => {
  storage()
  const task = await newStore()
  task.clearInputFiles()
  const path = 'D:/Audio/readded-failure.mkv'
  let calls = 0
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    async invoke(command) {
      if (command !== 'get_audio_metadata') return { started: true }
      calls++
      if (calls === 1) throw new Error('RUNTIME_BUSY')
      return { audioTrackSelectionSupported: true, audioTracksByPath: {}, audioTrackErrorsByPath: { [path]: 'Invalid media' } }
    },
  }
  task.addInputFiles([path])
  await new Promise(resolve => setTimeout(resolve, 0))
  task.removeInputFile(path)
  assert.equal(task.retryAudioTrackProbe(path), false)
  task.addInputFiles([path])
  await new Promise(resolve => setTimeout(resolve, 0))
  await assert.rejects(task.startSeparation(), /separate.audioTrackProbeFailedForFile/)
  assert.equal(calls, 2)
})

test('shared ensemble parameters validate participating models without previous single-model metadata', async () => {
  const values = storage()
  const task = await newStore()
  const model = useModelStore()
  const first = model.models[0]
  model.models = [first, { ...first, name: 'model-b' }]
  model.selectedModel = 'missing'
  model.selectedInfo = null
  task.applySelectedModelDefaults(undefined)
  task.ensembleEnabled = true
  task.ensembleModels = ['model-a', 'model-b']
  await task.applyParameterSettings('custom', { chunkMilliseconds: 8000, overlapBasisPoints: 2000 }, true)
  assert.equal(task.parameterMode, 'custom')
  assert.equal(task.customParameterIntent.chunkMilliseconds, 8000)
  assert.equal(task.useTta, true)
  assert.equal(model.selectedModel, 'missing')
  assert.equal(JSON.parse(values.get('pymss-studio:separate-state')).customParameterIntent.overlapBasisPoints, 2000)

  model.models[1] = { ...model.models[1], inferenceParamMeta: undefined }
  await assert.rejects(task.applyParameterSettings('custom', { chunkMilliseconds: 14000, overlapBasisPoints: 8000 }), /metadataUnavailable/)
  assert.equal(task.customParameterIntent.chunkMilliseconds, 8000)
})

test('legacy enabled TTA is retained as a custom preference and is inactive while following the model', async () => {
  const values = storage({ 'separate-state': { version: 2, parameterMode: 'model', useTta: true } })
  const task = await newStore()
  const bindings = { inputs: { input: { files: ['D:/Audio/song.wav'] } }, outputs: {},
    primaryInput: 'input', outputLayout: 'flat', outputDirectory: 'E:/Output' }
  assert.equal(task.ttaOverride, true)
  assert.equal(task.useTta, false)
  const single = (await task.prepareInferenceSnapshots(['model-a']))['model-a']
  assert.equal(single.useTta, false)
  assert.equal(single.sources.enable_tta, 'runtime-default')
  const inherited = (await task.startWorkbenchInference(workflow, bindings)).tasks[0].runConfig.workflowParameters.nodes.split
  assert.equal(inherited.useTta, false)
  assert.equal(inherited.sources.enable_tta, 'runtime-default')

  await task.applyParameterSettings('custom', {}, task.ttaOverride)
  const custom = (await task.startWorkflowInference(workflow)).tasks[0].runConfig.workflowParameters.nodes.split
  assert.equal(custom.useTta, true)
  assert.equal(custom.sources.enable_tta, 'custom')
  const persisted = JSON.parse(values.get('pymss-studio:separate-state'))
  assert.equal(persisted.ttaOverride, true)
  assert.equal(Object.hasOwn(persisted, 'useTta'), false)
})

test('legacy separation values are archived and cannot silently widen into model-library or workflow defaults', async () => {
  const values = storage({ 'separate-state': { version: 1, inferenceParamsByModel: { 'model-a': { chunk_size: 441000, overlap_size: 220500, normalize: true } } } })
  const task = await newStore()
  const model = useModelStore()
  assert.equal(task.parameterMode, 'model')
  assert.deepEqual(task.customParameterIntent, {})
  assert.equal(task.pendingLegacyParameters('model-a').chunk_size, 441000)
  await assert.rejects(task.startSeparation(), /parameterEditor.migrationRequired/)
  assert.equal(model.getModelInferenceOverrides('model-a'), undefined)
  const ensureMetadata = model.ensureModelMetadata
  model.ensureModelMetadata = async () => ({ ...model.models[0], inferenceParamMeta: { resolution: { ...model.models[0].inferenceParamMeta.resolution, fingerprint: 'changed-after-review' } } })
  await assert.rejects(task.resolveLegacyParameters('model-a', 'import'), /parameterEditor.parametersChanged/)
  assert.equal(model.getModelInferenceOverrides('model-a'), undefined)
  model.ensureModelMetadata = ensureMetadata
  await task.resolveLegacyParameters('model-a', 'keep')
  const run = (await task.startSeparation()).tasks[0]
  assert.equal(run.runConfig.inferenceParams.chunk_size, 573300)
  const saved = JSON.parse(values.get('pymss-studio:separate-state'))
  assert.equal(saved.legacyParameterBackup['model-a'].normalize, true)
  assert.equal(saved.legacyParameterChoices['model-a'], 'keep')
  assert.equal(model.getModelInferenceOverrides('model-a'), undefined)
})

test('batch model overrides persist together, reject stale previews, and roll back failed writes', async () => {
  const values = storage()
  await newStore()
  const model = useModelStore()
  model.models.push({ ...model.models[0], name: 'model-b' })
  const revision = model.modelInferenceRevision
  await model.replaceModelInferenceOverridesBatch([{ name: 'model-a', overrides: { batch_size: 3 } }, { name: 'model-b', overrides: { batch_size: 5 } }], revision)
  assert.equal(JSON.parse(values.get('pymss-studio:model-state')).modelInferenceOverrides['model-b'].batch_size, 5)
  await assert.rejects(model.replaceModelInferenceOverridesBatch([{ name: 'model-a', overrides: { batch_size: 8 } }], revision), /MODEL_PARAMETERS_CHANGED/)
  const before = JSON.parse(JSON.stringify(model.modelInferenceOverrides))
  const setItem = globalThis.localStorage.setItem
  globalThis.localStorage.setItem = (key, value) => { if (key === 'pymss-studio:model-state') throw new Error('write failed'); setItem(key, value) }
  await assert.rejects(model.replaceModelInferenceOverridesBatch([{ name: 'model-a', overrides: { batch_size: 8 } }, { name: 'model-b', overrides: { batch_size: 9 } }], model.modelInferenceRevision), /write failed/)
  assert.deepEqual(JSON.parse(JSON.stringify(model.modelInferenceOverrides)), before)
  globalThis.localStorage.setItem = setItem
})

test('batch reset removes explicit overrides while retaining each model default and unselected models', async () => {
  const values = storage({ 'model-state': { modelInferenceOverrides: {
    'model-a': { batch_size: 3 }, 'model-b': { batch_size: 2 }, 'model-c': { batch_size: 7 },
  } } })
  await newStore()
  const model = useModelStore()
  const defaults = { ...model.models[0].inferenceParamMeta.resolution.defaults, batch_size: 2, chunk_size: 882000 }
  model.models.push({
    ...model.models[0], name: 'model-b', defaultInferenceParams: defaults,
    inferenceParamMeta: { resolution: { ...model.models[0].inferenceParamMeta.resolution, defaults } },
  })
  await model.replaceModelInferenceOverridesBatch([{ name: 'model-a' }, { name: 'model-b' }], model.modelInferenceRevision)
  assert.equal(model.getModelInferenceOverrides('model-a'), undefined)
  assert.equal(model.getModelInferenceOverrides('model-b'), undefined)
  assert.equal(model.models[0].defaultInferenceParams.batch_size, 1)
  assert.equal(model.models[1].defaultInferenceParams.batch_size, 2)
  assert.equal(model.models[1].defaultInferenceParams.chunk_size, 882000)
  assert.deepEqual(JSON.parse(values.get('pymss-studio:model-state')).modelInferenceOverrides, { 'model-c': { batch_size: 7 } })
})

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

for (const mode of ['model', 'workflow']) {
  for (const count of [1, 2]) {
    test(`${mode} keeps the default model directory for ${count} restored queued file(s)`, async () => {
      storage()
      const task = await newStore()
      task.inputFiles = Array.from({ length: count }, (_, index) => `D:/Audio/input-${index}.wav`)
      const { tasks } = mode === 'model' ? await task.startSeparation() : await task.startWorkflowInference(workflow)
      assert.equal(tasks.length, count)
      assert.ok(tasks.every(item => item.runConfig.modelDir === null))

      settings.modelDir = 'D:/NewModels'
      const { calls } = await resumeRestoredQueue(tasks)
      assert.equal(calls.length, 1)
      assert.equal(calls[0].modelDir, null)
    })
  }
}

test('a restored workflow without a recorded model directory uses the current setting', async () => {
  settings.modelDir = 'D:/CurrentModels'
  const { calls } = await restoreWorkflowQueue(1, [1])
  assert.equal(calls.length, 1)
  assert.equal(calls[0].modelDir, 'D:/CurrentModels')
})

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

test('task notifications distinguish outcomes and mention only counts that occurred', t => {
  const outcomes = [
    [{ done: 3, failed: 0, cancelled: 0 }, '分离任务已完成', '已成功处理 3 项。'],
    [{ done: 2, failed: 1, cancelled: 0 }, '分离任务部分失败', '成功 2 项，失败 1 项。可在任务队列查看原因。'],
    [{ done: 0, failed: 3, cancelled: 0 }, '分离任务失败', '3 项处理失败。可在任务队列查看原因。'],
    [{ done: 2, failed: 0, cancelled: 1 }, '分离任务已结束', '成功 2 项，已取消 1 项。'],
    [{ done: 0, failed: 1, cancelled: 1 }, '分离任务失败', '失败 1 项，已取消 1 项。可在任务队列查看原因。'],
  ]
  for (const [counts, title, body] of outcomes) {
    assert.deepEqual(describeTaskReminder('model', counts), { title, body })
  }
  t.after(() => { i18n.global.locale.value = 'zh-CN' })
  i18n.global.locale.value = 'en'
  assert.deepEqual(describeTaskReminder('workflow', { done: 1, failed: 0, cancelled: 0 }), {
    title: 'Workflow completed', body: '1 item processed successfully.',
  })
})

test('system task notifications fire once per batch when enabled and supported', async () => {
  const { task } = await resumeRestoredQueue([
    ...queuedBatch('workflow', 1), ...queuedBatch('model', 1, 'model', 10),
    ...queuedBatch('cancelled', 1, 'model', 20),
    ...queuedBatch('disabled', 1, 'model', 30),
  ], 2)
  settings.taskSystemNotifications = true
  settings.appPaths = { systemNotificationSupported: true }
  const notifications = []
  const invoke = window.__TAURI_INTERNALS__.invoke
  window.__TAURI_INTERNALS__.invoke = (command, args) => {
    if (command === 'show_task_notification') notifications.push(args)
    return invoke(command, args)
  }
  const finish = taskId => task.handleWorkerEvent({ type: 'task_done', taskId, payload: { files: [], outputs: [] } })
  finish('workflow-0')
  assert.equal(notifications.length, 0)
  task.handleWorkerEvent({ type: 'error', taskId: 'workflow-1', payload: { message: 'Failed' } })
  finish('workflow-1')
  assert.deepEqual(notifications, [{ title: '工作流任务部分失败', body: '成功 1 项，失败 1 项。可在任务队列查看原因。' }])

  for (const taskId of ['cancelled-0', 'cancelled-1']) {
    task.handleWorkerEvent({ type: 'task_cancelled', taskId, payload: {} })
  }
  assert.equal(notifications.length, 1)

  settings.appPaths.systemNotificationSupported = false
  finish('model-0')
  finish('model-1')
  assert.equal(notifications.length, 1)

  settings.appPaths.systemNotificationSupported = true
  settings.taskSystemNotifications = false
  finish('disabled-0')
  finish('disabled-1')
  assert.equal(notifications.length, 1)
})

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

test('separation page and workbench freeze the same per-step values and replay them after the global setting changes', async () => {
  storage()
  const task = await newStore()
  const model = useModelStore()
  await model.setModelInferenceOverrides('model-a', { batch_size: 4 })
  const definition = {
    version: 1,
    steps: [
      { ...workflow.definition.steps[0], id: 'A' },
      { ...workflow.definition.steps[0], id: 'B', inference_params: { batch_size: 1, enable_tta: false } },
    ],
  }
  const entry = { ...workflow, definition }
  const bindings = { inputs: { input: { files: ['D:/Audio/song.wav'] } }, outputs: {},
    primaryInput: 'input', outputLayout: 'flat', outputDirectory: 'E:/Output' }
  const submitBoth = async () => [
    (await task.startWorkflowInference(entry)).tasks[0],
    (await task.startWorkbenchInference(entry, bindings)).tasks[0],
  ]
  for (const item of await submitBoth()) {
    assert.equal(item.runConfig.workflowParameters.nodes.A.parameters.batch_size, 4)
    assert.equal(item.runConfig.workflowParameters.nodes.B.parameters.batch_size, 1)
  }
  await task.applyParameterSettings('custom', { batchSize: 2 }, true)
  const submitted = await submitBoth()
  for (const item of submitted) {
    const { A, B } = item.runConfig.workflowParameters.nodes
    assert.equal(A.parameters.batch_size, 2)
    assert.equal(A.sources.batch_size, 'custom')
    assert.equal(A.useTta, true)
    assert.equal(B.parameters.batch_size, 1)
    assert.equal(B.sources.batch_size, 'node')
    assert.equal(B.useTta, false)
  }
  await task.applyParameterSettings('custom', { batchSize: 3 }, undefined)
  for (const item of submitted) {
    item.status = 'failed'
    assert.deepEqual((await task.retryTask(item.id)).runConfig.workflowParameters, item.runConfig.workflowParameters)
    assert.equal(item.runConfig.workflowParameters.nodes.A.parameters.batch_size, 2)
  }
  assert.equal(definition.steps[0].inference_params, undefined)
})

test('workflow submission loads a missing catalog config once before applying global custom parameters', async () => {
  storage()
  const task = await newStore()
  const model = useModelStore()
  const resolution = model.selectedInfo.inferenceParamMeta.resolution
  model.models = [{ ...model.models[0], configPath: 'D:/Models/model-a.yaml', inferenceParamMeta: undefined }]
  const lookups = []
  model.getWorkflowParameterMetadata = async (target, directory, fetchMissingConfig) => {
    lookups.push([target.model, Boolean(fetchMissingConfig)])
    return { resolution: fetchMissingConfig ? resolution : undefined, missingCatalogConfig: !fetchMissingConfig }
  }
  await task.applyParameterSettings('custom', { normalize: true })
  const input = workflow.definition.steps[0]
  const entry = { ...workflow, definition: {
    version: 1, steps: [{ ...input, id: ' A ' }, { ...input, id: 'B', inference_params: { normalize: false } }, { ...input, id: 'C' }],
  } }
  const bindings = { inputs: { input: { files: ['D:/Audio/song.wav'] } }, outputs: {},
    primaryInput: 'input', outputLayout: 'flat', outputDirectory: 'E:/Output' }
  const direct = await task.startWorkflowInference(entry)
  const workbench = await task.startWorkbenchInference(entry, bindings)
  assert.deepEqual(lookups, [['model-a', false], ['model-a', true], ['model-a', false], ['model-a', true]])
  for (const result of [direct, workbench]) {
    const { A, B, C } = result.tasks[0].runConfig.workflowParameters.nodes
    assert.equal(A.parameters.normalize, true)
    assert.equal(A.sources.normalize, 'custom')
    assert.equal(B.parameters.normalize, false)
    assert.equal(C.parameters.normalize, true)
    assert.equal(A.fingerprint, resolution.fingerprint)
  }
  assert.equal(entry.definition.steps[0].id, ' A ')
})

test('VR catalog configuration is fetched even when runtime fallback metadata is available', async () => {
  storage()
  const task = await newStore()
  const model = useModelStore()
  const configPath = 'D:/Models/vr.yaml'
  const defaults = { batch_size: 2, normalize: false }
  const resolution = {
    version: 1, fingerprint: 'vr-fallback', defaults, overlap: { kind: 'unknown' },
    capabilities: { batch_size: { kind: 'free' }, normalize: { kind: 'free' } },
  }
  const entry = {
    name: 'vr-model', modelType: 'vr', source: 'catalog', configPath, missingPaths: [configPath],
    inferenceParamMeta: { resolution },
  }
  const calls = []
  model.models = [entry]
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    async invoke(command, { payload } = {}) {
      if (command !== 'get_model_info') return { started: true }
      calls.push([payload.model, Boolean(payload.fetchMissingConfig)])
      return payload.fetchMissingConfig
        ? { ...entry, missingPaths: [], inferenceParamMeta: { resolution: {
          ...resolution, fingerprint: 'vr-config', defaults: { ...defaults, batch_size: 3 },
        } } }
        : entry
    },
  }
  const result = await task.startWorkflowInference({ ...workflow, definition: {
    version: 1, steps: [{ ...workflow.definition.steps[0], model: entry.name, model_type: 'vr' }],
  } })
  const snapshot = result.tasks[0].runConfig.workflowParameters.nodes.split
  assert.deepEqual(calls, [['vr-model', false], ['vr-model', true]])
  assert.equal(snapshot.parameters.batch_size, 3)
  assert.equal(snapshot.fingerprint, 'vr-config')
})

test('workflow metadata reads distinguish a snapshotted default directory from the current setting', async () => {
  storage()
  await newStore()
  const model = useModelStore()
  const entry = model.models[0]
  const dirs = []
  settings.modelDir = 'D:/NewModels'
  window.__TAURI_INTERNALS__ = {
    async invoke(command, { payload }) {
      assert.equal(command, 'get_model_info')
      dirs.push(payload.modelDir)
      return entry
    },
  }

  await model.getWorkflowParameterMetadata({ model: entry.name }, null)
  await model.getWorkflowParameterMetadata({ model: entry.name })
  assert.deepEqual(dirs, [null, 'D:/NewModels'])
})

test('Comfy-MSS export resolves only overridden nodes and never downloads model metadata', async () => {
  storage()
  const task = await newStore()
  const model = useModelStore()
  const original = model.selectedInfo.inferenceParamMeta.resolution
  const resolution = { ...original, defaults: { ...original.defaults, overlap_size: 0 } }
  model.modelInferenceOverrides['model-a'] = { batch_size: 5 }
  await task.applyParameterSettings('custom', { batchSize: 6 }, true)
  const calls = []
  let missingCatalogConfig = true
  model.getWorkflowParameterMetadata = async (target, directory, download) => {
    calls.push([target.id, directory, download])
    return { resolution, missingCatalogConfig }
  }
  const nodes = [1, 2].map(id => ({
    id, type: 'pymss_mss_separate', widgets_values: ['model-a'],
    properties: id === 2 ? { studioInferenceOverrides: { normalize: true } } : {},
  }))
  const definition = { nodes, links: [] }
  await assert.rejects(task.prepareComfyExportParameters(definition), /comfyExportMissingConfig/)
  missingCatalogConfig = false
  const plan = await task.prepareComfyExportParameters(definition)
  assert.deepEqual(calls, [['2', null, undefined], ['2', null, undefined]])
  assert.deepEqual(Object.keys(plan.nodes), ['2'])
  assert.equal(plan.nodes['2'].parameters.batch_size, resolution.defaults.batch_size)
  assert.equal(plan.nodes['2'].modelDefaultOverlap, 0)
  assert.equal(plan.nodes['2'].parameters.normalize, true)
  assert.equal(plan.nodes['2'].useTta, false)
})

test('reads and downloads for one model update metadata in order across sources', async () => {
  storage()
  await newStore()
  const model = useModelStore()
  const original = JSON.parse(JSON.stringify(model.selectedInfo))
  const configPath = 'D:/Models/model-a.yaml'
  const installed = { ...original, configPath, missingPaths: [], inferenceParamMeta: {
    resolution: { ...original.inferenceParamMeta.resolution, fingerprint: 'downloaded-config' },
  } }
  const calls = []
  let finishDownload
  window.__TAURI_INTERNALS__ = {
    async invoke(command, { payload }) {
      assert.equal(command, 'get_model_info')
      calls.push([Boolean(payload.fetchMissingConfig), payload.source])
      if (payload.fetchMissingConfig) return new Promise(resolve => { finishDownload = resolve })
      return installed
    },
  }
  const download = model.getWorkflowParameterMetadata({ model: original.name, downloadSource: 'huggingface' }, null, true)
  const read = model.getWorkflowParameterMetadata({ model: original.name, downloadSource: 'modelscope' }, null)
  assert.deepEqual(calls, [[true, 'huggingface']])
  finishDownload(installed)
  const [downloaded, observed] = await Promise.all([download, read])
  assert.deepEqual(calls, [[true, 'huggingface'], [false, undefined]])
  assert.equal(observed.resolution.fingerprint, downloaded.resolution.fingerprint)
  assert.equal(model.selectedInfo.inferenceParamMeta.resolution.fingerprint, 'downloaded-config')
})

test('a catalog refresh cannot restore stale metadata after a concurrent config download', async () => {
  storage()
  await newStore()
  const model = useModelStore()
  const original = JSON.parse(JSON.stringify(model.selectedInfo))
  const configPath = 'D:/Models/model-a.yaml'
  const installed = { ...original, configPath, missingPaths: [], inferenceParamMeta: {
    resolution: { ...original.inferenceParamMeta.resolution, fingerprint: 'downloaded-config' },
  } }
  let finishList
  window.__TAURI_INTERNALS__ = {
    async invoke(command) {
      if (command === 'list_models') return new Promise(resolve => { finishList = resolve })
      assert.equal(command, 'get_model_info')
      return installed
    },
  }
  const refresh = model.loadModels()
  await model.getWorkflowParameterMetadata({ model: original.name }, null, true)
  finishList({ models: [{ ...original, configPath, missingPaths: [configPath] }], categories: [], categoriesCn: {}, modelDir: null })
  await refresh
  assert.equal(model.models[0].inferenceParamMeta.resolution.fingerprint, 'downloaded-config')
  assert.equal(model.selectedInfo.inferenceParamMeta.resolution.fingerprint, 'downloaded-config')
  assert.deepEqual(model.selectedInfo.missingPaths, [])
})

test('a catalog refresh keeps a completed model download when only the weights were missing', async () => {
  storage()
  await newStore()
  const model = useModelStore()
  const original = JSON.parse(JSON.stringify(model.selectedInfo))
  const modelPath = 'D:/Models/model-a.ckpt'
  const configPath = 'D:/Models/model-a.yaml'
  const beforeDownload = { ...original, modelPath, configPath, downloaded: false, missingPaths: [modelPath] }
  const installed = { ...beforeDownload, downloaded: true, missingPaths: [], inferenceParamMeta: {
    resolution: { ...original.inferenceParamMeta.resolution, fingerprint: 'downloaded-weights' },
  } }
  let finishList
  window.__TAURI_INTERNALS__ = {
    async invoke(command) {
      assert.equal(command, 'list_models')
      return new Promise(resolve => { finishList = resolve })
    },
  }
  const refresh = model.loadModels()
  model.handleWorkerEvent({ type: 'download_done', taskId: 'download_model-a', payload: {
    model: original.name, modelInfo: installed, downloaded: [modelPath], skipped: [configPath],
  } })
  assert.equal(model.selectedInfo.downloaded, true)
  finishList({ models: [beforeDownload], categories: [], categoriesCn: {}, modelDir: 'D:/Models' })
  await refresh
  assert.equal(model.models[0].downloaded, true)
  assert.equal(model.selectedInfo.inferenceParamMeta.resolution.fingerprint, 'downloaded-weights')
  assert.deepEqual(model.selectedInfo.missingPaths, [])
})

for (const fetchMissingConfig of [false, true]) {
  test(`a late metadata query cannot roll back a completed download (fetch config: ${fetchMissingConfig})`, async () => {
    storage()
    await newStore()
    const model = useModelStore()
    const original = JSON.parse(JSON.stringify(model.selectedInfo))
    const modelPath = 'D:/Models/model-a.ckpt'
    const configPath = 'D:/Models/model-a.yaml'
    const beforeDownload = { ...original, modelPath, configPath, downloaded: false, missingPaths: [modelPath] }
    const installed = { ...beforeDownload, downloaded: true, missingPaths: [], inferenceParamMeta: {
      resolution: { ...original.inferenceParamMeta.resolution, fingerprint: 'installed-weights' },
    } }
    let finishList, finishMetadata
    window.__TAURI_INTERNALS__ = {
      async invoke(command) {
        if (command === 'list_models') return new Promise(resolve => { finishList = resolve })
        assert.equal(command, 'get_model_info')
        return new Promise(resolve => { finishMetadata = resolve })
      },
    }
    const refresh = model.loadModels()
    const query = model.getWorkflowParameterMetadata({ model: original.name }, null, fetchMissingConfig)
    model.handleWorkerEvent({ type: 'download_done', taskId: 'download_model-a', payload: {
      model: original.name, modelInfo: installed, downloaded: [modelPath], skipped: [configPath],
    } })
    finishMetadata(beforeDownload)
    assert.equal((await query).resolution.fingerprint, 'installed-weights')
    assert.equal(model.selectedInfo.downloaded, true)
    finishList({ models: [installed], categories: [], categoriesCn: {}, modelDir: 'D:/Models' })
    await refresh
    assert.equal(model.models[0].downloaded, true)
    assert.equal(model.selectedInfo.inferenceParamMeta.resolution.fingerprint, 'installed-weights')
  })
}

test('YAML model names with a category prefix are normalized for submission metadata', async () => {
  storage()
  const task = await newStore()
  const entry = JSON.parse(JSON.stringify(useModelStore().selectedInfo))
  const requests = []
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    async invoke(command, { payload } = {}) {
      if (command !== 'get_model_info') return { started: true }
      requests.push(payload.model)
      assert.equal(payload.model, entry.name)
      return entry
    },
  }
  const displayName = '  [vocals] ' + entry.name + '  '
  const definition = { version: 1, steps: [{ ...workflow.definition.steps[0], model: displayName }] }
  const result = await task.startWorkflowInference({ ...workflow, definition })
  assert.deepEqual(requests, [entry.name])
  assert.equal(result.tasks[0].runConfig.workflowParameters.nodes.split.model, entry.name)
  assert.equal(result.tasks[0].runConfig.workflowDefinition.steps[0].model, displayName)
})

test('advanced workflow preflight looks up the complete registered name of a custom model', async () => {
  storage()
  const task = await newStore()
  const model = useModelStore()
  const name = '[My]-vocals'
  const entry = { ...model.selectedInfo, name, modelPath: 'D:/Models/vocals.ckpt', source: 'user' }
  model.models = [entry]
  const requests = []
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
    async invoke(command, { payload } = {}) {
      if (command !== 'get_model_info') return { started: true }
      requests.push(payload.inferenceTarget.modelName)
      assert.equal(payload.inferenceTarget.modelName, name)
      return { resolution: entry.inferenceParamMeta.resolution }
    },
  }
  const definition = {
    nodes: [
      { id: 1, type: 'pymss_custom_mss_separate_list', widgets_values: [name, 'bs_roformer', 'auto', '0', false] },
      { id: 2, type: 'pymss_save_audio' },
    ],
    links: [],
  }
  const result = await task.startWorkbenchInference({ ...workflow, definition }, {
    inputs: {}, outputs: {}, outputLayout: 'flat', outputDirectory: 'E:/Output',
  })
  assert.deepEqual(requests, [name])
  assert.equal(result.tasks[0].runConfig.workflowParameters.nodes['1'].model, name)
})

test('config preflight respects the advanced separation node download setting', async () => {
  const values = storage()
  const task = await newStore()
  const model = useModelStore()
  const resolution = model.selectedInfo.inferenceParamMeta.resolution
  model.models = [{ ...model.models[0], configPath: 'D:/Models/model-a.yaml', inferenceParamMeta: undefined }]
  const lookups = []
  const downloadSources = []
  model.getWorkflowParameterMetadata = async (target, _directory, fetchMissingConfig) => {
    lookups.push(Boolean(fetchMissingConfig))
    if (fetchMissingConfig) downloadSources.push(target.downloadSource)
    return { resolution: fetchMissingConfig ? resolution : undefined, missingCatalogConfig: !fetchMissingConfig }
  }
  await task.applyParameterSettings('custom', { normalize: true })
  const widgets = ['model-a', 'auto', false]
  const entry = { ...workflow, definition: {
    nodes: [{ id: 1, type: 'mss_separate', widgets_values: widgets }, { id: 2, type: 'pymss_save_audio' }],
    links: [],
  } }
  const bindings = { inputs: {}, outputs: {}, outputLayout: 'flat', outputDirectory: 'E:/Output' }
  await assert.rejects(task.startWorkbenchInference(entry, bindings), /metadataUnavailable/)
  assert.deepEqual(lookups, [false])
  widgets[2] = true
  const result = await task.startWorkbenchInference(entry, bindings)
  assert.deepEqual(lookups, [false, false, true])
  assert.equal(result.tasks[0].runConfig.workflowParameters.nodes['1'].parameters.normalize, true)
  widgets.splice(2, 1, '0', false)
  await assert.rejects(task.startWorkbenchInference(entry, bindings), /metadataUnavailable/)
  widgets[3] = true
  settings.downloadSource = 'huggingface'
  const imported = await task.startWorkbenchInference(entry, bindings)
  assert.deepEqual(lookups.slice(3), [false, false, true])
  assert.deepEqual(downloadSources, ['modelscope', 'huggingface'])
  assert.equal(imported.tasks[0].runConfig.downloadSource, 'huggingface')
  const { workflowParameters, workflowDefinition } = imported.tasks[0].runConfig
  assert.equal(workflowParameters.nodes['1'].parameters.normalize, true)
  assert.equal(workflowParameters.nodes['1'].downloadMissing, true)
  assert.equal(workflowParameters.nodes['1'].downloadSource, undefined)
  assert.deepEqual(workflowDefinition.nodes[0].widgets_values.slice(2, 5), [true, 'Default', '0'])

  const oldTask = JSON.parse(JSON.stringify(imported.tasks[0]))
  oldTask.status = 'failed'
  delete oldTask.runConfig.workflowParameters.nodes['1'].downloadMissing
  oldTask.runConfig.workflowDefinition.nodes[0].widgets_values = ['model-a', 'auto', '0', false]
  values.set('pymss-studio:task-history', JSON.stringify({ tasks: [oldTask] }))
  const restored = await newStore()
  const retry = await restored.retryTask(oldTask.id)
  assert.equal(retry.runConfig.workflowParameters.nodes['1'].downloadMissing, false)
  assert.equal(retry.runConfig.workflowParameters.nodes['1'].fingerprint, resolution.fingerprint)
})
