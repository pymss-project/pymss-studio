import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const vite = await createServer({
  configFile: false,
  server: { middlewareMode: true, hmr: false },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  resolve: {
    alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) },
  },
})
after(() => vite.close())

const {
  detectWorkflowFormat,
  isGraphWorkflowDefinition,
  isSimpleWorkflowDefinition,
  normalizeGraphWorkflowDefinition,
  normalizeSimpleWorkflowDefinition,
} = await vite.ssrLoadModule('/src/workflows/formats.ts')
const {
  countWorkflowSaveOutputs,
  getWorkflowDefinitionIssue,
  mergeSimpleInferenceParams,
  prepareWorkflowDefinitionForRun,
  resolveWorkflowRuntimeDefaults,
} = await vite.ssrLoadModule('/src/workflows/runtimeDefinition.ts')
const {
  applyGraphDefaultWidgets,
  storeGraphDefaults,
} = await vite.ssrLoadModule('/src/workflows/graphDefaults.ts')
const {
  analyzeSimpleWorkflow,
  buildSimpleWorkflowDefinition,
  createDefaultSimpleEditorUi,
  fitSimpleEditorViewport,
  hydrateSimpleWorkflow,
  renderSimpleOutputFilename,
  resolveWorkflowOpenMode,
} = await vite.ssrLoadModule('/src/utils/workflowSimple.ts')
const {
  canConnectSimple,
  cleanupSimpleDraft,
  connectSimple,
  disconnectSimple,
  simpleOutputRef,
  simpleSaveTarget,
  simpleStepInputTarget,
} = await vite.ssrLoadModule('/src/utils/simpleWorkflowEditor.ts')

function simpleFixture() {
  return {
    version: 1,
    defaults: {
      device: 'cuda',
      output_format: 'flac',
      inference_params: { normalize: true },
    },
    steps: [
      {
        id: 'vocals',
        model: 'first.ckpt',
        input: 'input',
        stems: ['vocals', 'instrumental'],
        save: { vocals: 'vocals' },
      },
      {
        id: 'cleanup',
        model: 'second.pth',
        input: 'vocals.vocals',
        stems: ['clean', 'noise'],
        save: { clean: 'clean' },
      },
    ],
  }
}

test('workflow format detection rejects ambiguous definitions', () => {
  assert.equal(detectWorkflowFormat({ steps: [] }), 'simple')
  assert.equal(detectWorkflowFormat({ nodes: [], links: [] }), 'graph')
  assert.equal(detectWorkflowFormat({ nodes: [] }), 'graph')
  assert.equal(detectWorkflowFormat({ nodes: [], links: null }), 'graph')
  assert.equal(detectWorkflowFormat({ nodes: [], links: 'invalid' }), 'unknown')
  assert.equal(detectWorkflowFormat({ nodes: [], links: [], steps: [] }), 'unknown')
  assert.equal(detectWorkflowFormat({}), 'unknown')
  assert.equal(isSimpleWorkflowDefinition({ steps: [] }), true)
  assert.equal(isGraphWorkflowDefinition({ nodes: [], links: [] }), true)
})

test('legacy custom separator widgets migrate without mutating stored input', () => {
  const legacy = {
    nodes: [{
      id: 1,
      type: 'custom_mss_separate',
      widgets_values: ['custom.ckpt', 'mel_band_roformer', 'cuda', false, 'modelscope', '0,1', true],
    }],
    links: [],
  }
  const normalized = normalizeGraphWorkflowDefinition(legacy)

  assert.notEqual(normalized, legacy)
  assert.deepEqual(legacy.nodes[0].widgets_values, [
    'custom.ckpt', 'mel_band_roformer', 'cuda', false, 'modelscope', '0,1', true,
  ])
  assert.deepEqual(normalized.nodes[0].widgets_values, [
    'custom.ckpt', 'mel_band_roformer', 'cuda', '0,1', true,
  ])
  assert.equal(normalizeGraphWorkflowDefinition(normalized), normalized)

  const withoutTrailingDebug = {
    nodes: [{
      type: 'custom_mss_separate',
      widgets_values: ['custom.ckpt', 'mel_band_roformer', 'cpu', true, 'huggingface', '0'],
    }],
  }
  assert.deepEqual(
    normalizeGraphWorkflowDefinition(withoutTrailingDebug).nodes[0].widgets_values,
    ['custom.ckpt', 'mel_band_roformer', 'cpu', '0', false],
  )

  const currentWithTrailingValue = {
    nodes: [{
      type: 'custom_mss_separate',
      widgets_values: ['custom.ckpt', 'mel_band_roformer', 'cuda', '0,1', true, null],
    }],
  }
  assert.equal(normalizeGraphWorkflowDefinition(currentWithTrailingValue), currentWithTrailingValue)
})

test('legacy simple save filename templates migrate to flat output names', () => {
  const legacy = simpleFixture()
  legacy.save_intermediate = false
  legacy.steps[0].save.vocals = '%filename%_vocals_first.wav'
  legacy.steps[1].save.clean = 'mastered'
  const normalized = normalizeSimpleWorkflowDefinition(legacy)

  assert.notEqual(normalized, legacy)
  assert.equal(Object.hasOwn(normalized, 'save_intermediate'), false)
  assert.equal(legacy.steps[0].save.vocals, '%filename%_vocals_first.wav')
  assert.equal(normalized.steps[0].save.vocals, 'Default')
  assert.equal(normalized.steps[0].output_names.vocals, '%filename%_vocals_first.wav')
  assert.equal(normalized.steps[1].save.clean, 'mastered')
  assert.equal(normalizeSimpleWorkflowDefinition(normalized), normalized)
})

test('legacy simple custom audio filenames migrate regardless of template prefix', () => {
  const legacy = simpleFixture()
  legacy.steps[0].save.vocals = 'lead-vocal.flac'
  const normalized = normalizeSimpleWorkflowDefinition(legacy)

  assert.equal(normalized.steps[0].save.vocals, 'Default')
  assert.equal(normalized.steps[0].output_names.vocals, 'lead-vocal.flac')
})

test('simple creator stem directories migrate to the default file template', () => {
  const legacy = simpleFixture()
  legacy.steps[0].save.vocals = 'vocals'
  const normalized = normalizeSimpleWorkflowDefinition(legacy)

  assert.equal(normalized.steps[0].save.vocals, 'Default')
  assert.equal(normalized.steps[0].output_names.vocals, '%filename%_%stem%_%model%.flac')
})

test('workflow validation follows the detected definition format', () => {
  assert.equal(getWorkflowDefinitionIssue(simpleFixture()), null)
  assert.equal(getWorkflowDefinitionIssue({ steps: [] }), 'steps-required')
  assert.equal(getWorkflowDefinitionIssue({ version: 1, steps: [{ id: 'empty', save: {} }] }), 'no-save-outputs')
  assert.equal(getWorkflowDefinitionIssue({ steps: [{ save: { vocals: 'vocals' } }] }), 'invalid-definition')
  assert.equal(getWorkflowDefinitionIssue({ steps: [], defaults: 'cuda' }), 'invalid-definition')
  assert.equal(getWorkflowDefinitionIssue({ steps: [{}], defaults: { inference_params: [] } }), 'invalid-definition')
  assert.equal(getWorkflowDefinitionIssue({ steps: [{ save: [] }] }), 'invalid-definition')
  assert.equal(getWorkflowDefinitionIssue({ nodes: [{ type: 'pymss_save_audio' }], links: [] }), null)
  assert.equal(getWorkflowDefinitionIssue({ nodes: [{ type: 'mss_separate' }], links: [] }), 'no-save-outputs')
  assert.equal(getWorkflowDefinitionIssue({}), 'invalid-format')
  assert.equal(countWorkflowSaveOutputs(simpleFixture()), 2)
  assert.equal(countWorkflowSaveOutputs({
    steps: [{ save: { vocals: false, other: '', backing: ['mix-a', '', 'mix-b'] } }],
  }), 1)
  assert.equal(getWorkflowDefinitionIssue({
    version: 1,
    steps: [{ save: { vocals: false } }],
  }), 'no-save-outputs')
  assert.equal(countWorkflowSaveOutputs({
    nodes: [
      { type: 'pymss_save_audio' },
      { type: 'mss_separate' },
      { type: 'SaveAudio' },
    ],
    links: [],
  }), 2)
  assert.equal(countWorkflowSaveOutputs({}), 0)
})

test('simple editor opens only definitions it can round-trip without data loss', () => {
  const editable = simpleFixture()
  assert.deepEqual(analyzeSimpleWorkflow(editable), { editable: true, reasonCodes: [] })
  assert.equal(resolveWorkflowOpenMode(editable), 'simple')

  const advancedInference = structuredClone(editable)
  advancedInference.steps[0].inference_params = { chunk_size: 4096 }
  assert.deepEqual(analyzeSimpleWorkflow(advancedInference), {
    editable: false,
    reasonCodes: ['advanced_parameters'],
  })
  assert.equal(resolveWorkflowOpenMode(advancedInference), 'advanced')

  const customModel = structuredClone(editable)
  customModel.steps[0].model_path = 'D:/Models/custom.ckpt'
  customModel.steps[0].model_type = 'bs_roformer'
  assert.equal(analyzeSimpleWorkflow(customModel).editable, false)

  const extraMetadata = structuredClone(editable)
  extraMetadata.runtime_extension = { enabled: true }
  assert.deepEqual(analyzeSimpleWorkflow(extraMetadata), {
    editable: false,
    reasonCodes: ['advanced_parameters'],
  })

  const malformed = structuredClone(editable)
  malformed.defaults = 'cuda'
  assert.deepEqual(analyzeSimpleWorkflow(malformed), {
    editable: false,
    reasonCodes: ['invalid_definition'],
  })

  const malformedStepInference = structuredClone(editable)
  malformedStepInference.steps[0].inference_params = []
  assert.deepEqual(analyzeSimpleWorkflow(malformedStepInference), {
    editable: false,
    reasonCodes: ['invalid_definition'],
  })

  const futureVersion = structuredClone(editable)
  futureVersion.version = 2
  assert.deepEqual(analyzeSimpleWorkflow(futureVersion), {
    editable: false,
    reasonCodes: ['invalid_definition'],
  })

  assert.deepEqual(analyzeSimpleWorkflow({ nodes: [], links: [] }), {
    editable: false,
    reasonCodes: ['graph_workflow'],
  })
  assert.deepEqual(analyzeSimpleWorkflow({}), {
    editable: false,
    reasonCodes: ['invalid_definition'],
  })
})

test('simple editor layout metadata round-trips and legacy definitions receive defaults', () => {
  const legacy = simpleFixture()
  const hydratedLegacy = hydrateSimpleWorkflow(legacy)
  assert.equal(hydratedLegacy.ui.editor, 'simple')
  assert.ok(hydratedLegacy.ui.nodes.input)
  assert.ok(hydratedLegacy.ui.nodes[legacy.steps[0].id])

  const ui = createDefaultSimpleEditorUi(hydratedLegacy.steps)
  ui.viewport = { x: 18, y: -24, zoom: 1.25 }
  ui.nodes[legacy.steps[0].id] = { x: 512, y: 96 }
  const definition = buildSimpleWorkflowDefinition({ ...hydratedLegacy, ui })
  const restored = hydrateSimpleWorkflow(definition)
  assert.deepEqual(restored.ui.viewport, ui.viewport)
  assert.deepEqual(restored.ui.nodes[legacy.steps[0].id], { x: 512, y: 96 })
  assert.equal(analyzeSimpleWorkflow(definition).editable, true)
})

test('simple editor fit view centers the complete node bounds instead of resetting zoom', () => {
  const viewport = fitSimpleEditorViewport([
    { x: 120, y: 80, width: 100, height: 80 },
    { x: 520, y: 260, width: 140, height: 100 },
  ], 800, 600, { padding: 40 })

  assert.ok(Math.abs(viewport.zoom - 4 / 3) < 1e-9)
  assert.ok(Math.abs(viewport.x + 120) < 1e-9)
  assert.ok(Math.abs(viewport.y - 20 / 3) < 1e-9)
})

test('simple filename preview follows edited template and output format', () => {
  assert.equal(
    renderSimpleOutputFilename('%filename%_%stem%_%model%', {
      inputName: '小蓝背心 - 灯火通明.mp3',
      stem: 'Instrumental',
      model: 'melband_roformer_instvox_duality_v2.ckpt',
      stepId: 'step1',
      index: 1,
      outputFormat: 'wav',
    }),
    '小蓝背心 - 灯火通明_Instrumental_melband_roformer_instvox_duality_v2.wav',
  )
  assert.equal(
    renderSimpleOutputFilename('mix-%index%-%track%.flac', {
      inputName: 'input.wav',
      stem: 'vocals',
      model: 'model.pth',
      stepId: 'step2',
      index: 2,
      outputFormat: 'mp3',
    }),
    'mix-2-input.mp3',
  )
})

test('simple node editor enforces forward-only links and cleans deleted references', () => {
  const draft = hydrateSimpleWorkflow({
    version: 1,
    defaults: { device: 'cpu', output_format: 'wav' },
    save_intermediate: false,
    steps: [
      { id: 'step1', model: 'one', input: 'input', stems: ['vocals', 'music'], save: {} },
      { id: 'step2', model: 'two', input: 'step1.vocals', stems: ['clean'], save: {} },
    ],
  })
  assert.equal(canConnectSimple(draft, 'input', simpleStepInputTarget('step2')).ok, true)
  assert.equal(canConnectSimple(draft, 'step1.music', simpleStepInputTarget('step1')).ok, false)
  assert.equal(canConnectSimple(draft, 'step2.clean', simpleStepInputTarget('step1')).ok, false)
  assert.equal(canConnectSimple(draft, 'step1.vocals', 'save').ok, true)
  assert.equal(canConnectSimple(draft, 'step1.music', 'save').ok, true)
  connectSimple(draft, 'step1.music', 'save')
  assert.equal(draft.steps[0].save.music, 'Default')
  assert.equal(draft.steps[0].outputNames.music, '%filename%_%stem%_%model%')
  assert.equal(disconnectSimple(draft, simpleSaveTarget('step1', 'music')), true)
  assert.deepEqual(draft.steps[0].save, {})
  draft.steps.splice(0, 1)
  cleanupSimpleDraft(draft)
  assert.equal(draft.steps[0].input, '')
})

test('simple node editor keeps unselected stems available for downstream steps', () => {
  const draft = hydrateSimpleWorkflow({
    version: 1,
    defaults: { device: 'cpu', output_format: 'wav' },
    save_intermediate: false,
    steps: [
      { id: 'step1', model: 'one', input: 'input', stems: ['vocals', 'music'], save: { music: 'Default' } },
      { id: 'step2', model: 'two', input: 'input', stems: ['clean'], save: {} },
    ],
  })
  assert.equal(canConnectSimple(draft, simpleOutputRef('step1', 'vocals'), simpleStepInputTarget('step2')).ok, true)
  connectSimple(draft, simpleOutputRef('step1', 'vocals'), simpleStepInputTarget('step2'))
  cleanupSimpleDraft(draft)
  assert.equal(draft.steps[1].input, 'step1.vocals')
  assert.deepEqual(draft.steps[0].save, { music: 'Default' })
  assert.equal(canConnectSimple(draft, simpleOutputRef('step1', 'vocals'), 'save').ok, true)
})

test('simple runtime preparation materializes defaults without mutating the stored workflow', () => {
  const source = simpleFixture()
  source.defaults.inference_params.batch_size = 2
  source.steps[1].device = 'cpu'
  source.steps[1].output_format = 'mp3'
  source.steps[1].inference_params = { normalize: false, chunk_size: 4096 }
  const before = structuredClone(source)
  const prepared = prepareWorkflowDefinitionForRun(source, {
    device: 'cpu',
    outputFormat: 'wav',
  })

  assert.deepEqual(source, before)
  assert.equal(prepared.steps[0].device, 'cuda')
  assert.equal(prepared.steps[0].output_format, 'flac')
  assert.deepEqual(prepared.steps[0].inference_params, { normalize: true, batch_size: 2 })
  assert.equal(prepared.steps[1].device, 'cpu')
  assert.equal(prepared.steps[1].output_format, 'mp3')
  assert.deepEqual(prepared.steps[1].inference_params, {
    normalize: false,
    batch_size: 2,
    chunk_size: 4096,
  })

  const withoutDefaults = {
    version: 1,
    steps: [{ id: 'step1', model: 'first.ckpt', stems: ['vocals'], save: { vocals: 'vocals' } }],
  }
  const withRuntimeFallbacks = prepareWorkflowDefinitionForRun(withoutDefaults, {
    device: 'cpu',
    outputFormat: 'mp3',
  })
  assert.equal(withRuntimeFallbacks.steps[0].device, 'cpu')
  assert.equal(withRuntimeFallbacks.steps[0].output_format, 'mp3')
})

test('runtime defaults resolve consistently for simple and graph workflows', () => {
  const fallback = { device: 'cpu', outputFormat: 'wav', modelDir: 'D:/Models' }
  assert.deepEqual(resolveWorkflowRuntimeDefaults(simpleFixture(), fallback), {
    device: 'cuda',
    outputFormat: 'flac',
    modelDir: 'D:/Models',
  })
  assert.deepEqual(resolveWorkflowRuntimeDefaults({
    nodes: [],
    links: [],
    extra: { appDefaults: { device: 'mps', output_format: 'MP3', model_dir: 'E:/Models' } },
  }, fallback), {
    device: 'mps',
    outputFormat: 'mp3',
    modelDir: 'E:/Models',
  })
  assert.deepEqual(resolveWorkflowRuntimeDefaults({}, fallback), fallback)
})

test('simple inference precedence is defaults, step use_tta, then step inference params', () => {
  assert.deepEqual(
    mergeSimpleInferenceParams({ batch_size: 4, enable_tta: true }, {}, false),
    { batch_size: 4, enable_tta: false },
  )
  assert.deepEqual(
    mergeSimpleInferenceParams({ enable_tta: true }, { enable_tta: true }, false),
    { enable_tta: true },
  )
})

test('graph runtime preparation preserves node formats and fills missing values', () => {
  const source = {
    nodes: [
      { type: 'pymss_save_audio', widgets_values: ['wav', 'Default'] },
      { type: 'pymss_save_audio', widgets_values: ['', 'Default'] },
    ],
    links: [],
    extra: { appDefaults: { device: 'cuda' } },
  }
  const prepared = prepareWorkflowDefinitionForRun(source, {
    device: 'cuda',
    outputFormat: 'FLAC',
  })

  assert.equal(prepared.nodes[0].widgets_values[0], 'wav')
  assert.equal(prepared.nodes[1].widgets_values[0], 'flac')
  assert.deepEqual(prepared.extra.appDefaults, { device: 'cuda' })
  assert.equal(source.nodes[0].widgets_values[0], 'wav')
})

test('graph defaults metadata and live widgets update independently', () => {
  const source = {
    nodes: [
      { type: 'mss_separate', widgets_values: ['model.ckpt', 'cpu'] },
      { type: 'custom_mss_separate_list', widgets_values: ['custom.ckpt', 'bs_roformer', 'cpu', '0,1', false] },
      { type: 'pymss_save_audio', widgets_values: ['flac', 'Default'] },
    ],
    extra: { editor: { scale: 1 } },
  }
  const metadataOnly = storeGraphDefaults(source, { device: 'cuda', outputFormat: 'mp3' })
  assert.equal(metadataOnly.nodes[0].widgets_values[1], 'cpu')
  assert.equal(metadataOnly.nodes[2].widgets_values[0], 'flac')
  assert.deepEqual(metadataOnly.extra, {
    editor: { scale: 1 },
    appDefaults: { device: 'cuda', output_format: 'mp3' },
  })

  const nodes = [
    {
      type: 'mss_separate',
      widgets: [
        { name: 'model_name', value: 'model.ckpt' },
        { name: 'device', value: 'cpu' },
      ],
    },
    {
      type: 'custom_mss_separate_list',
      widgets: [
        { name: 'model_name', value: 'custom.ckpt' },
        { name: 'model_type', value: 'bs_roformer' },
        { name: 'device', value: 'cpu' },
      ],
    },
    {
      type: 'pymss_save_audio',
      widgets: [{ name: 'output_format', value: 'flac' }],
    },
  ]
  applyGraphDefaultWidgets(nodes, { device: 'cuda', outputFormat: 'mp3' }, {
    device: true,
    outputFormat: false,
  })
  assert.equal(nodes[0].widgets[1].value, 'cuda')
  assert.equal(nodes[1].widgets[1].value, 'bs_roformer')
  assert.equal(nodes[1].widgets[2].value, 'cuda')
  assert.equal(nodes[2].widgets[0].value, 'flac')

  applyGraphDefaultWidgets(nodes, { device: 'cuda', outputFormat: 'mp3' }, {
    device: false,
    outputFormat: true,
  })
  assert.equal(nodes[0].widgets[1].value, 'cuda')
  assert.equal(nodes[2].widgets[0].value, 'mp3')
  assert.equal(source.nodes[0].widgets_values[1], 'cpu')
})
