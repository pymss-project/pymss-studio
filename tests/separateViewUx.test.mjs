import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'

const path = new URL('../src/views/SeparateView.vue', import.meta.url)
const { descriptor } = parse(readFileSync(path, 'utf8'))
const template = descriptor.template?.content || ''
const style = descriptor.styles.map(item => item.content).join('\n')
const modelsPath = new URL('../src/views/ModelsView.vue', import.meta.url)
const modelsTemplate = parse(readFileSync(modelsPath, 'utf8')).descriptor.template?.content || ''
const scriptSource = descriptor.scriptSetup.content
const script = ts.createSourceFile('SeparateView.ts', scriptSource, ts.ScriptTarget.Latest, true)
const names = new Set([
  'modelPanelHasModels',
  'modelPanelLoading',
  'getOutputPlayback',
  'setOutputPlayback',
  'touchPreviewAudio',
  'releasePreviewAudio',
  'trimPreviewAudioCache',
  'getAudio',
  'syncOutputPreviewAudio',
  'collectCurrentModelInferenceDefaults',
  'applyCurrentModelInferenceDefaults',
  'saveCurrentModelInferenceDefaults',
  'resetCurrentModelInferenceDefaults',
])
const selected = script.statements.filter(statement => (
  ts.isFunctionDeclaration(statement) ? names.has(statement.name?.text)
    : ts.isVariableStatement(statement) && statement.declarationList.declarations.some(item => names.has(item.name.getText(script)))
))
assert.equal(selected.length, names.size)
const code = ts.transpileModule(selected.map(statement => statement.getText(script)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText
const logLevelFunctions = script.statements.filter(statement => (
  ts.isFunctionDeclaration(statement) && ['getLogLineLevel', 'getLogLineLevels'].includes(statement.name?.text)
))
assert.equal(logLevelFunctions.length, 2)
const logLevelCode = ts.transpileModule(logLevelFunctions.map(statement => statement.getText(script)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText
const stemSelectionNames = new Set([
  'outputStemSelectionCleared',
  'checkedOutputStems',
  'selectedOutputStemCount',
  'hasOutputStemSelection',
  'selectAllOutputStems',
  'clearOutputStems',
])
const stemSelectionStatements = script.statements.filter(statement => (
  ts.isFunctionDeclaration(statement) ? stemSelectionNames.has(statement.name?.text)
    : ts.isVariableStatement(statement) && statement.declarationList.declarations.some(item => stemSelectionNames.has(item.name.getText(script)))
))
assert.equal(stemSelectionStatements.length, stemSelectionNames.size)
const stemSelectionCode = ts.transpileModule(stemSelectionStatements.map(statement => statement.getText(script)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function computed(source) {
  if (typeof source === 'function') return { get value() { return source() } }
  return {
    get value() { return source.get() },
    set value(value) { source.set(value) },
  }
}

function ref(value) {
  return { value }
}

test('model and workflow targets keep their keyed transition boundary', () => {
  const transitionTag = '<transition name="stage-swap" mode="out-in">'
  const modelBranch = '<div v-if="runMode === \'model\'" key="model"'
  const workflowBranch = '<div v-else key="workflow"'
  const modelIndex = template.indexOf(modelBranch)
  const transitionIndex = template.lastIndexOf(transitionTag, modelIndex)
  const readyIndex = template.lastIndexOf('<section v-else key="ready"', modelIndex)
  const workflowIndex = template.indexOf(workflowBranch, modelIndex)
  const closingIndex = template.indexOf('</transition>', workflowIndex)

  assert.equal(template.split(transitionTag).length - 1, 2)
  assert.ok(modelIndex > 0)
  assert.ok(transitionIndex > readyIndex)
  assert.ok(workflowIndex > modelIndex)
  assert.ok(closingIndex > workflowIndex)
})

test('model panel shows cached rows while refreshing them in the background', () => {
  const context = {
    computed,
    modelsLoaded: { value: false },
    downloadedModels: { value: [{ name: 'cached-model' }] },
    isLoading: { value: true },
    modelError: { value: null },
    app: { envLoading: false },
  }
  const result = vm.runInNewContext(`${code}\n({ modelPanelHasModels, modelPanelLoading })`, context)

  assert.equal(result.modelPanelHasModels.value, true)
  assert.equal(result.modelPanelLoading.value, false)
  context.downloadedModels.value = []
  assert.equal(result.modelPanelHasModels.value, false)
  assert.equal(result.modelPanelLoading.value, true)
  context.modelsLoaded.value = true
  context.isLoading.value = false
  assert.equal(result.modelPanelHasModels.value, false)
  assert.equal(result.modelPanelLoading.value, false)
})

test('model library keeps cached cards visible while refreshing them in the background', () => {
  assert.ok(modelsTemplate.includes('v-if="isLoading && !modelStore.models.length"'))
})

test('running batches can move to the background while a new batch is composed', () => {
  assert.ok(scriptSource.includes('const composingNewJob = ref(false)'))
  assert.ok(scriptSource.includes('const showQueueModal = ref(false)'))
  assert.ok(scriptSource.includes('composingNewJob.value ? null : (focusedJob.value || newestRunningJob.value)'))
  assert.ok(scriptSource.includes('function beginNextSeparation()'))
  assert.ok(scriptSource.includes('function focusQueueJob(target: SeparationJob)'))
  assert.ok(scriptSource.includes('const recentFailedJobs = computed(() => task.sessionJobs'))
  assert.ok(scriptSource.includes('function viewQueueJobLogs(target: SeparationJob)'))
  assert.ok(scriptSource.includes('task.clearFinishedSessionJobs()'))
  assert.ok(template.includes("t('separate.addNextBatch')"))
  assert.ok(template.includes('v-if="hasQueueEntries"'))
  assert.ok(template.includes('v-model:show="showQueueModal"'))
  assert.ok(template.includes('v-for="job in queueJobs"'))
  assert.ok(template.includes("job.status === 'failed'"))
  assert.ok(template.includes(':loading="submittingJob"'))
})

test('terminal task actions use state-specific hierarchy', () => {
  assert.ok(scriptSource.includes('const completedActionOptions = computed<DropdownOption[]>'))
  assert.ok(template.includes('v-if="taskPanelState === \'done\'"'))
  assert.ok(template.includes("t('separate.moreActions')"))
  assert.ok(template.includes("t('separate.viewResults')"))
  assert.ok(template.includes("t('separate.runAgain')"))
  assert.ok(template.includes('class="result-path"'))
  assert.ok(template.includes("@click.stop=\"task.revealPath(currentTask.outputs[0]?.path || currentTask.output)\""))
})

test('output stem picker uses the app scrollbar instead of native browser controls', () => {
  assert.ok(style.includes('.ofield--stems .stem-chips::-webkit-scrollbar'))
  assert.ok(style.includes('.ofield--stems .stem-chips::-webkit-scrollbar-thumb:hover'))
  assert.ok(style.includes('.ofield--stems .stem-chips::-webkit-scrollbar-button'))
  assert.ok(style.includes('scrollbar-width: thin'))
})

test('large output stem lists expose compact bulk selection actions', () => {
  assert.ok(template.includes('availableStemNames.length > 3'))
  assert.ok(template.includes('@click="selectAllOutputStems"'))
  assert.ok(template.includes('@click="clearOutputStems"'))
  assert.ok(template.includes("t('separate.selectAllStems')"))
  assert.ok(template.includes("t('separate.clearStemSelection')"))
  assert.ok(scriptSource.includes('outputStemSelectionCleared.value = true'))
  assert.ok(scriptSource.includes('&& hasOutputStemSelection.value'))
})

test('output stem selection is restored from the model cache', () => {
  assert.ok(scriptSource.includes('persistedOutputStemSelectionCleared'))
  assert.ok(scriptSource.includes('task.getSavedModelState(info.name)'))
  assert.ok(!template.includes('saveCurrentModelOutputStems'))
  assert.ok(!template.includes('resetCurrentModelOutputStems'))
})

test('output stem bulk actions distinguish all stems from an empty selection', () => {
  const context = {
    computed,
    ref,
    availableStemNames: ref(['vocals', 'instrumental', 'drums', 'bass']),
    selectedStems: ref([]),
    persistedOutputStemSelectionCleared: ref(false),
  }
  const result = vm.runInNewContext(`${stemSelectionCode}\n({ checkedOutputStems, selectedOutputStemCount, hasOutputStemSelection, selectAllOutputStems, clearOutputStems })`, context)

  assert.deepEqual(Array.from(result.checkedOutputStems.value), context.availableStemNames.value)
  assert.equal(result.hasOutputStemSelection.value, true)

  result.clearOutputStems()
  assert.deepEqual(Array.from(result.checkedOutputStems.value), [])
  assert.equal(result.selectedOutputStemCount.value, 0)
  assert.equal(result.hasOutputStemSelection.value, false)
  assert.equal(context.persistedOutputStemSelectionCleared.value, true)

  result.checkedOutputStems.value = ['vocals']
  assert.deepEqual(Array.from(context.selectedStems.value), ['vocals'])
  assert.equal(result.hasOutputStemSelection.value, true)

  result.selectAllOutputStems()
  assert.deepEqual(Array.from(result.checkedOutputStems.value), context.availableStemNames.value)
  assert.deepEqual(Array.from(context.selectedStems.value), [])
  assert.equal(context.persistedOutputStemSelectionCleared.value, false)
})

test('advanced inference settings expose model-scoped save and reset actions', async () => {
  assert.ok(template.includes('@click="saveCurrentModelInferenceDefaults"'))
  assert.ok(template.includes('@click="resetCurrentModelInferenceDefaults"'))
  assert.ok(template.includes("t('separate.saveInferenceDefaults')"))
  assert.ok(template.includes("t('separate.resetInferenceDefaults')"))

  const calls = []
  const info = {
    name: 'test-model',
    modelType: 'bs_roformer',
    defaultInferenceParams: { batch_size: 1, overlap_size: 1024, chunk_size: 8192 },
  }
  let storedOverrides
  const context = {
    computed,
    modelsLoaded: { value: true },
    downloadedModels: { value: [] },
    isLoading: { value: false },
    modelError: { value: null },
    app: { envLoading: false },
    currentModelDefaults: {
      value: {
        batch_size: 1,
        overlap_size: 1024,
        num_overlap: 4,
        chunk_size: 8192,
        window_size: 512,
        aggression: 5,
        enable_post_process: false,
        post_process_threshold: 0.2,
        high_end_process: false,
      },
    },
    batch_size: { value: 2 },
    overlap_size: { value: 2048 },
    num_overlap: { value: 8 },
    chunk_size: { value: 16384 },
    window_size: { value: 1024 },
    aggression: { value: 7 },
    enable_post_process: { value: true },
    post_process_threshold: { value: 0.35 },
    high_end_process: { value: true },
    standardize: { value: true },
    normalize: { value: true },
    isApolloModel: { value: false },
    showStandardizeField: { value: true },
    showNormalizeField: { value: true },
    currentModelInfo: { value: info },
    model: {
      models: [info],
      getModelBaseInferenceDefaults: () => info.defaultInferenceParams,
      getModelInferenceOverrides: () => storedOverrides,
      async setModelInferenceOverrides(name, overrides) {
        storedOverrides = overrides
        calls.push(['save', name, overrides])
      },
      async resetModelInferenceOverrides(name) {
        storedOverrides = undefined
        calls.push(['reset', name])
      },
    },
    task: {
      normalizeInferenceInputsBeforeSubmit: () => calls.push(['normalize']),
      getSavedModelState: () => ({ selectedStems: ['vocals'] }),
      applySelectedModelDefaults: (...args) => calls.push(['apply', ...args]),
    },
    message: { success: value => calls.push(['success', value]) },
    t: key => key,
  }
  const result = vm.runInNewContext(
    `${code}\n({ collectCurrentModelInferenceDefaults, saveCurrentModelInferenceDefaults, resetCurrentModelInferenceDefaults })`,
    context,
  )

  assert.deepEqual(
    { ...result.collectCurrentModelInferenceDefaults() },
    {
      batch_size: 2,
      overlap_size: 2048,
      num_overlap: 8,
      chunk_size: 16384,
      window_size: 1024,
      aggression: 7,
      enable_post_process: true,
      post_process_threshold: 0.35,
      high_end_process: true,
      standardize: true,
      normalize: true,
    },
  )
  context.isApolloModel.value = true
  assert.equal(result.collectCurrentModelInferenceDefaults().num_overlap, undefined)
  context.isApolloModel.value = false

  await result.saveCurrentModelInferenceDefaults()
  assert.equal(calls[0][0], 'normalize')
  assert.deepEqual(calls[1].slice(0, 2), ['save', 'test-model'])
  assert.equal(calls[2][0], 'apply')
  assert.deepEqual(calls[2][3], { selectedStems: ['vocals'] })
  assert.equal(calls[2][5].force, true)
  assert.deepEqual(calls[3], ['success', 'models.inferenceDefaultsSaved'])

  calls.length = 0
  await result.resetCurrentModelInferenceDefaults()
  assert.deepEqual(calls[0], ['reset', 'test-model'])
  assert.equal(calls[1][0], 'apply')
  assert.equal(calls[1][4], undefined)
  assert.equal(calls[1][5].force, true)
  assert.deepEqual(calls[2], ['success', 'models.inferenceDefaultsReset'])
})

test('task log levels recognize structured prefixes and actual embedded pymss levels', () => {
  const getLogLineLevel = vm.runInNewContext(`${logLevelCode}\ngetLogLineLevel`)
  const cases = [
    ['[info] Loading model', 'info'],
    ['info: Loading model', 'info'],
    ['[debug] Runtime parameters', 'debug'],
    ['trace: Device details', 'debug'],
    ['[warning] No outputs', 'warn'],
    ['warn: No outputs', 'warn'],
    ['error: [INFERENCE_FAILED] Separation did not produce outputs', 'error'],
    ['[critical] Worker exited', 'error'],
    ['[warning] 00:26:41 | DBG | separator.py:1171 | Runtime parameters', 'debug'],
    ['warning: 00:26:41 | INF | separator.py:1136 | Loading model completed', 'info'],
    ['[warning] 00:26:44 | WAR | separator.py:1524 | Cannot separate track', 'warn'],
    ['[info] 00:26:44 | ERR | separator.py:1524 | Cannot separate track', 'error'],
    ['00:26:44 | WRN | An output was skipped', 'warn'],
    ['FTL | Worker exited', 'error'],
    ['\u001b[33m[warning] 00:26:41 | INF | Model loaded\u001b[0m', 'info'],
  ]
  for (const [line, expected] of cases) assert.equal(getLogLineLevel(line), expected, line)
})

test('task log levels keep tracebacks distinct without treating message keywords as severity', () => {
  const getLogLineLevel = vm.runInNewContext(`${logLevelCode}\ngetLogLineLevel`)
  for (const line of ['traceback:\nRuntimeError: mismatched tensors', 'Traceback (most recent call last):', '[warning] Traceback (most recent call last):', 'ValueError: invalid shape']) {
    assert.equal(getLogLineLevel(line), 'error', line)
  }
  for (const line of ['', '12:26:42 AM Separating', 'Reading D:/Music/error-warning.wav', '[info] Previous error was recovered']) {
    assert.equal(getLogLineLevel(line), 'info', line)
  }
})

test('explicit log levels are preserved when the message starts with an exception name', () => {
  const { getLogLineLevel, getLogLineLevels } = vm.runInNewContext(`${logLevelCode}\n({ getLogLineLevel, getLogLineLevels })`)
  const cases = [
    ['[info] ValueError: this was recovered', 'info'],
    ['info: RuntimeError: previous failure was handled', 'info'],
    ['[debug] TypeError: retry details', 'debug'],
    ['warning: ValueError: optional setting was ignored', 'warn'],
    ['[info] Traceback (most recent call last):', 'info'],
  ]
  for (const [line, expected] of cases) {
    assert.equal(getLogLineLevel(line), expected, line)
    assert.deepEqual(Array.from(getLogLineLevels([line])), [expected], line)
  }
})

test('a new warning directly after a traceback retains its own level', () => {
  const getLogLineLevels = vm.runInNewContext(`${logLevelCode}\ngetLogLineLevels`)
  const traceback = [
    'Traceback (most recent call last):',
    '  File "worker.py", line 10, in run',
    '    raise TypeError("bad argument")',
    'TypeError: bad argument',
  ]
  const warnings = [
    'warning: File "optional-model.yaml" is missing',
    '[warning] File "optional-model.yaml" is missing',
    '[warning] D:/runtime/transformer.py:49: UserWarning: ComplexHalf support is experimental',
    '[warning]   rot = torch.complex(cos, sin)',
    'warning: ValueError: optional setting was ignored',
  ]
  for (const warning of warnings) {
    assert.deepEqual(Array.from(getLogLineLevels([...traceback, warning, '[info] Cleanup completed'])), [
      ...Array(traceback.length).fill('error'), 'warn', 'info',
    ], warning)
  }
})

test('a split traceback keeps its frames, source and caret markers at error level', () => {
  const getLogLineLevels = vm.runInNewContext(`${logLevelCode}\ngetLogLineLevels`)
  const lines = [
    'error: [INFERENCE_FAILED] Invalid model arguments',
    'traceback:',
    'Traceback (most recent call last):',
    '  File "worker_infer.py", line 911, in cmd_infer',
    '    separator = _prepare_separator(',
    '                ^^^^^^^^^^^^^^^^^^^',
    '    flags | DEFAULT_FLAGS',
    '  File "worker_infer.py", line 164, in __init__',
    '    super().__init__(*args, **kwargs)',
    "TypeError: unexpected keyword argument 'target_instrument_override'",
    '[warning] 00:51:00 | DBG | Closing separator',
    '[info] Cleanup completed',
  ]
  const original = [...lines]

  assert.deepEqual(Array.from(getLogLineLevels(lines)), [
    'error', 'error', 'error', 'error', 'error', 'error', 'error', 'error', 'error', 'error', 'debug', 'info',
  ])
  assert.deepEqual(lines, original)
})

test('stderr wrappers and chained exceptions retain traceback severity until the next log', () => {
  const getLogLineLevels = vm.runInNewContext(`${logLevelCode}\ngetLogLineLevels`)
  const lines = [
    '[warning] Traceback (most recent call last):',
    '[warning]   File "worker.py", line 10, in run',
    '[warning]     raise ValueError("invalid config")',
    '[warning] ValueError: invalid config',
    'During handling of the above exception, another exception occurred:',
    'Traceback (most recent call last):',
    '  File "worker.py", line 12, in run',
    '    raise RuntimeError("failed") from error',
    'RuntimeError: failed',
    'The above exception was the direct cause of the following exception:',
    'Traceback (most recent call last):',
    '  File "worker.py", line 14, in run',
    '    raise ModelLoadFailure("missing weights")',
    'ModelLoadFailure: missing weights',
    '00:51:01 Finished',
    '[warning] Optional file missing',
  ]

  assert.deepEqual(Array.from(getLogLineLevels(lines)), [
    ...Array(14).fill('error'), 'info', 'warn',
  ])
})

test('traceback state ends at normal output and does not leak into another task', () => {
  const getLogLineLevels = vm.runInNewContext(`${logLevelCode}\ngetLogLineLevels`)
  assert.deepEqual(Array.from(getLogLineLevels([
    'traceback:', '  File "worker.py", line 10', '[info]   Recovering runtime',
    '  Runtime parameters', 'Reading D:/Music/error-warning.wav',
  ])), ['error', 'error', 'info', 'info', 'info'])
  assert.deepEqual(Array.from(getLogLineLevels(['info: Model loaded', '  Runtime parameters'])), ['info', 'info'])
})

test('preview audio requests metadata before playback', () => {
  const calls = []
  class FakeAudio {
    listeners = new Map()
    duration = 70.471
    currentTime = 0
    paused = true

    set preload(value) { calls.push(['preload', value]) }
    set src(value) { calls.push(['src', value]) }
    addEventListener(name, callback) { this.listeners.set(name, callback) }
    pause() { calls.push(['pause']) }
    removeAttribute(name) { calls.push(['removeAttribute', name]) }
    load() {
      calls.push(['load'])
      this.listeners.get('loadedmetadata')?.()
    }
  }
  const context = {
    computed,
    modelsLoaded: { value: true },
    downloadedModels: { value: [] },
    isLoading: { value: false },
    modelError: { value: null },
    app: { envLoading: false },
    Audio: FakeAudio,
    audioElements: new Map(),
    audioAccessOrder: [],
    PREVIEW_AUDIO_CACHE_LIMIT: 8,
    outputPlayback: { value: {} },
    playingOutputPath: { value: '' },
    convertFileSrc: value => `asset:${value}`,
  }
  const result = vm.runInNewContext(`${code}\n({ getAudio, getOutputPlayback })`, context)
  result.getAudio('output.wav')

  assert.deepEqual(calls, [
    ['preload', 'metadata'],
    ['src', 'asset:output.wav'],
    ['load'],
  ])
  assert.equal(result.getOutputPlayback('output.wav').duration, 70.471)
})

test('preview audio preload is bounded and stale players are released', () => {
  class FakeAudio {
    listeners = new Map()
    duration = 12
    currentTime = 0
    paused = true

    set preload(_value) {}
    set src(_value) {}
    addEventListener(name, callback) { this.listeners.set(name, callback) }
    pause() { this.paused = true }
    removeAttribute(_name) {}
    load() { this.listeners.get('loadedmetadata')?.() }
  }
  const context = {
    computed,
    modelsLoaded: { value: true },
    downloadedModels: { value: [] },
    isLoading: { value: false },
    modelError: { value: null },
    app: { envLoading: false },
    Audio: FakeAudio,
    audioElements: new Map(),
    audioAccessOrder: [],
    PREVIEW_AUDIO_CACHE_LIMIT: 8,
    outputPlayback: { value: {} },
    playingOutputPath: { value: '' },
    convertFileSrc: value => `asset:${value}`,
  }
  const result = vm.runInNewContext(`${code}\n({ syncOutputPreviewAudio })`, context)
  result.syncOutputPreviewAudio(Array.from({ length: 20 }, (_, index) => ({ path: `output-${index}.wav` })))
  assert.equal(context.audioElements.size, 8)
  assert.deepEqual([...context.audioElements.keys()], Array.from({ length: 8 }, (_, index) => `output-${index}.wav`))

  result.syncOutputPreviewAudio([{ path: 'replacement.wav' }])
  assert.deepEqual([...context.audioElements.keys()], ['replacement.wav'])
  assert.deepEqual(context.audioAccessOrder, ['replacement.wav'])
})

test('released audio cannot restore stale playback state', () => {
  const instances = []
  class FakeAudio {
    listeners = new Map()
    duration = 12
    currentTime = 0
    paused = true

    constructor() { instances.push(this) }
    set preload(_value) {}
    set src(_value) {}
    addEventListener(name, callback) { this.listeners.set(name, callback) }
    pause() { this.paused = true }
    removeAttribute(_name) {}
    load() {}
  }
  const context = {
    computed,
    modelsLoaded: { value: true },
    downloadedModels: { value: [] },
    isLoading: { value: false },
    modelError: { value: null },
    app: { envLoading: false },
    Audio: FakeAudio,
    audioElements: new Map(),
    audioAccessOrder: [],
    PREVIEW_AUDIO_CACHE_LIMIT: 8,
    outputPlayback: { value: {} },
    playingOutputPath: { value: '' },
    convertFileSrc: value => `asset:${value}`,
  }
  const result = vm.runInNewContext(`${code}\n({ syncOutputPreviewAudio })`, context)
  result.syncOutputPreviewAudio([{ path: 'old.wav' }])
  const oldAudio = instances[0]
  result.syncOutputPreviewAudio([{ path: 'replacement.wav' }])

  oldAudio.duration = 99
  oldAudio.currentTime = 42
  oldAudio.listeners.get('loadedmetadata')?.()
  oldAudio.listeners.get('timeupdate')?.()
  assert.equal(context.outputPlayback.value['old.wav'], undefined)
  assert.deepEqual([...context.audioElements.keys()], ['replacement.wav'])
})
