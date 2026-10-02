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
  createAudioOperationDraft,
  fitSimpleEditorViewport,
  hydrateSimpleWorkflow,
  normalizeSimpleInferenceParams,
  renderSimpleOutputFilename,
} = await vite.ssrLoadModule('/src/utils/workflowSimple.ts')
const {
  analyzeSimpleModelChangeImpact,
  analyzeSimpleNodeRemovalImpact,
  canMoveSimpleStep,
  canConnectSimple,
  cleanupSimpleDraft,
  connectSimple,
  disconnectSimple,
  moveSimpleStep,
  simpleEnsembleInputTarget,
  simpleOutputRef,
  simpleSaveTarget,
  simpleStepInputTarget,
  updateSimpleEnsembleOutputStem,
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

  const advancedInference = structuredClone(editable)
  advancedInference.steps[0].inference_params = { chunk_size: 4096 }
  assert.deepEqual(analyzeSimpleWorkflow(advancedInference), { editable: true, reasonCodes: [] })

  const unsupportedInference = structuredClone(editable)
  unsupportedInference.steps[0].inference_params = { future_parameter: 4096 }
  assert.deepEqual(analyzeSimpleWorkflow(unsupportedInference), {
    editable: false,
    reasonCodes: ['advanced_parameters'],
  })

  const unsupportedWorkflowDefault = structuredClone(editable)
  unsupportedWorkflowDefault.defaults.inference_params.batch_size = 2
  assert.deepEqual(analyzeSimpleWorkflow(unsupportedWorkflowDefault), {
    editable: false,
    reasonCodes: ['advanced_parameters'],
  })

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

test('simple editor reports collateral links and saved outputs before destructive changes', () => {
  const draft = hydrateSimpleWorkflow({
    version: 1,
    defaults: { device: 'auto', output_format: 'wav', inference_params: { normalize: true } },
    steps: [
      {
        id: 'source', model: 'four-stem.ckpt', input: 'input',
        stems: ['Vocals', 'Instrumental'],
        save: { Vocals: 'Default', Instrumental: 'Default' },
        output_names: {},
      },
      { id: 'cleanup', model: 'cleanup.ckpt', input: 'source.Vocals', stems: ['Voice'], save: {}, output_names: {} },
      { id: 'master', model: 'master.ckpt', input: 'blend.Mix', stems: ['Master'], save: {}, output_names: {} },
    ],
    ensembles: [{
      id: 'blend',
      inputs: [{ source: 'input', weight: 1 }, { source: 'source.Instrumental', weight: 1 }],
      algorithm: 'avg_wave',
      output_stem: 'Mix',
      save: 'Default',
      output_name: '%filename%_%stem%',
    }],
  })

  assert.deepEqual(
    analyzeSimpleModelChangeImpact(draft, 'source', ['Instrumental']),
    { connections: 1, savedOutputs: 1 },
  )
  assert.deepEqual(
    analyzeSimpleNodeRemovalImpact(draft, 'source'),
    { connections: 2, savedOutputs: 2 },
  )
  assert.deepEqual(
    analyzeSimpleNodeRemovalImpact(draft, 'blend'),
    { connections: 1, savedOutputs: 1 },
  )

  const rebuilt = buildSimpleWorkflowDefinition(draft)
  assert.equal(rebuilt.defaults.inference_params.normalize, true)

  draft.steps[0].outputNames.Vocals = ''
  cleanupSimpleDraft(draft)
  assert.equal(draft.steps[0].outputNames.Vocals, '%filename%_%stem%_%model%')
})

test('simple editor can reorder independent steps without breaking dependencies', () => {
  const draft = hydrateSimpleWorkflow({
    version: 1,
    steps: [
      { id: 'source', model: 'a.ckpt', input: 'input', stems: ['Vocals'], save: {} },
      { id: 'cleanup', model: 'b.ckpt', input: 'source.Vocals', stems: ['Clean'], save: {} },
      { id: 'inserted', model: 'c.ckpt', input: 'source.Vocals', stems: ['Processed'], save: {} },
    ],
  })

  assert.equal(canMoveSimpleStep(draft, 'source', 1), false)
  assert.equal(canMoveSimpleStep(draft, 'inserted', -1), true)
  assert.equal(moveSimpleStep(draft, 'inserted', -1), true)
  assert.deepEqual(draft.steps.map(step => step.id), ['source', 'inserted', 'cleanup'])
  assert.equal(
    canConnectSimple(draft, 'inserted.Processed', simpleStepInputTarget('cleanup')).ok,
    true,
  )
})

test('simple editor round-trips Ensemble nodes and validates their connections', () => {
  const definition = {
    version: 1,
    defaults: { device: 'cuda', output_format: 'wav', inference_params: { normalize: false } },
    steps: [
      { id: 'modelA', model: 'a.ckpt', input: 'input', stems: ['Vocals'], save: {}, output_names: {} },
      { id: 'modelB', model: 'b.ckpt', input: 'input', stems: ['Vocals'], save: {}, output_names: {} },
      { id: 'cleanup', model: 'cleanup.ckpt', input: 'input', stems: ['Voice'], save: { Voice: 'Default' }, output_names: {} },
    ],
    ensembles: [{
      id: 'blend',
      inputs: [
        { source: 'input', weight: 1 },
        { source: 'modelB.Vocals', weight: 0.75 },
      ],
      algorithm: 'avg_fft',
      output_stem: 'Vocals',
      save: false,
      output_name: '%filename%_%stem%_Ensemble',
    }],
  }

  const draft = hydrateSimpleWorkflow(definition)
  assert.equal(draft.ensembles.length, 1)
  assert.equal(draft.ensembles[0].algorithm, 'avg_fft')
  assert.equal(draft.ensembles[0].inputs[1].weight, 0.75)
  assert.equal(canConnectSimple(draft, 'input', simpleEnsembleInputTarget('blend', 0)).ok, true)
  assert.deepEqual(
    canConnectSimple(draft, 'input', simpleEnsembleInputTarget('blend', 1)),
    { ok: false, reason: 'duplicate-source' },
  )
  assert.equal(canConnectSimple(draft, 'blend.Vocals', 'save').ok, true)
  assert.equal(canConnectSimple(draft, 'blend.Vocals', simpleStepInputTarget('modelB')).ok, false)
  assert.equal(canConnectSimple(draft, 'blend.Vocals', simpleStepInputTarget('cleanup')).ok, true)
  connectSimple(draft, 'blend.Vocals', simpleStepInputTarget('cleanup'))
  cleanupSimpleDraft(draft)
  assert.equal(draft.steps[2].input, 'blend.Vocals')
  assert.equal(countWorkflowSaveOutputs(definition), 1)
  assert.deepEqual(analyzeSimpleWorkflow(definition), { editable: true, reasonCodes: [] })

  const invalidAlgorithm = structuredClone(definition)
  invalidAlgorithm.ensembles[0].algorithm = 'unknown'
  assert.equal(getWorkflowDefinitionIssue(invalidAlgorithm), 'invalid-definition')
  const missingSource = structuredClone(definition)
  missingSource.ensembles[0].inputs[1].source = 'missing.Vocals'
  assert.equal(getWorkflowDefinitionIssue(missingSource), 'invalid-definition')

  const rebuilt = buildSimpleWorkflowDefinition(draft)
  assert.deepEqual(rebuilt.ensembles, definition.ensembles)
  assert.equal(rebuilt.steps[2].input, 'blend.Vocals')
  assert.equal(getWorkflowDefinitionIssue(rebuilt), null)
  assert.equal(rebuilt.studio.nodes.blend.x, draft.ui.nodes.blend.x)

  updateSimpleEnsembleOutputStem(draft, draft.ensembles[0], '')
  assert.equal(draft.steps[2].input, 'blend.Vocals')
  cleanupSimpleDraft(draft)
  assert.equal(draft.steps[2].input, 'blend.Vocals')
  updateSimpleEnsembleOutputStem(draft, draft.ensembles[0], 'Lead')
  assert.equal(draft.steps[2].input, 'blend.Lead')
  assert.equal(canConnectSimple(draft, 'blend.Lead', simpleStepInputTarget('cleanup')).ok, true)
  const rebuiltAfterRename = buildSimpleWorkflowDefinition(draft)
  assert.equal(rebuiltAfterRename.ensembles[0].output_stem, 'Lead')
  assert.equal(rebuiltAfterRename.steps[2].input, 'blend.Lead')
  assert.equal(getWorkflowDefinitionIssue(rebuiltAfterRename), null)

  const cyclic = structuredClone(rebuilt)
  cyclic.steps[1].input = 'blend.Vocals'
  assert.equal(getWorkflowDefinitionIssue(cyclic), 'invalid-definition')

  draft.steps.splice(0, 1)
  cleanupSimpleDraft(draft)
  assert.equal(draft.ensembles[0].inputs[0].source, 'input')
  assert.equal(draft.ensembles[0].inputs[1].source, 'modelB.Vocals')
  assert.equal(draft.steps[1].input, 'blend.Lead')
  draft.ensembles = []
  cleanupSimpleDraft(draft)
  assert.equal(draft.steps[1].input, '')
})

test('simple editor supports forward Ensemble chains and keeps renamed references valid', () => {
  const definition = {
    version: 1,
    defaults: { device: 'auto', output_format: 'wav' },
    steps: [
      { id: 'First', model: 'a.ckpt', input: 'INPUT', stems: ['Vocals'], save: {}, output_names: {} },
      { id: 'second', model: 'b.ckpt', input: 'input', stems: ['Vocals'], save: {}, output_names: {} },
      { id: 'cleanup', model: 'c.ckpt', input: 'polish.Final', stems: ['Clean'], save: { Clean: 'Default' }, output_names: {} },
    ],
    ensembles: [
      {
        id: 'Blend',
        inputs: [{ source: 'first.vocals', weight: 1 }, { source: 'second.Vocals', weight: 1 }],
        algorithm: 'avg_wave', output_stem: 'Vocals', save: false,
      },
      {
        id: 'polish',
        inputs: [{ source: 'blend.vocals', weight: 1 }, { source: 'input', weight: 0.25 }],
        algorithm: 'avg_fft', output_stem: 'Final', save: false,
      },
    ],
  }

  assert.deepEqual(analyzeSimpleWorkflow(definition), { editable: true, reasonCodes: [] })
  const draft = hydrateSimpleWorkflow(definition)
  assert.equal(
    canConnectSimple(draft, 'blend.vocals', simpleEnsembleInputTarget('polish', 0)).ok,
    true,
  )
  assert.deepEqual(
    canConnectSimple(draft, 'polish.Final', simpleEnsembleInputTarget('Blend', 0)),
    { ok: false, reason: 'forward-link' },
  )
  assert.deepEqual(
    canConnectSimple(draft, 'polish.Final', simpleStepInputTarget('First')),
    { ok: false, reason: 'forward-link' },
  )

  cleanupSimpleDraft(draft)
  assert.equal(draft.steps[0].input, 'input')
  assert.equal(draft.ensembles[0].inputs[0].source, 'First.Vocals')
  assert.equal(draft.ensembles[1].inputs[0].source, 'Blend.Vocals')
  updateSimpleEnsembleOutputStem(draft, draft.ensembles[0], 'Lead')
  assert.equal(draft.ensembles[1].inputs[0].source, 'Blend.Lead')
  assert.equal(getWorkflowDefinitionIssue(buildSimpleWorkflowDefinition(draft)), null)
})

function audioOperationsFixture() {
  return {
    version: 1,
    defaults: { device: 'auto', output_format: 'wav', inference_params: { normalize: false } },
    steps: [
      { id: 'sw', model: 'sw.ckpt', input: 'input', stems: ['Vocals', 'Drums', 'Bass', 'Guitar', 'Piano', 'Other'], save: {}, output_names: {} },
      { id: 'gabox', model: 'gabox.ckpt', input: 'input', stems: ['Instrumental'], save: {}, output_names: {} },
      { id: 'cleanup', model: 'cleanup.ckpt', input: 'phase.Inverted', stems: ['Clean'], save: {}, output_names: {} },
    ],
    ensembles: [
      {
        id: 'sum', algorithm: 'sum', output_stem: 'Instrumental', save: false,
        output_name: '%filename%_%stem%_%step%',
        inputs: ['Drums', 'Bass', 'Guitar', 'Piano', 'Other'].map(stem => ({ source: `sw.${stem}`, weight: 1 })),
      },
      {
        id: 'difference', algorithm: 'subtract', output_stem: 'Instrumental', save: false,
        output_name: '%filename%_%stem%_%step%',
        inputs: [{ source: 'input', weight: 1 }, { source: 'sw.Vocals', weight: 1 }],
      },
      {
        id: 'phase', algorithm: 'invert', output_stem: 'Inverted', save: 'Default',
        output_name: '%filename%_%stem%_%step%',
        inputs: [{ source: 'difference.Instrumental', weight: 1 }],
      },
      {
        id: 'blend', algorithm: 'avg_fft', output_stem: 'Instrumental', save: 'Default',
        output_name: '%filename%_%stem%_Ensemble',
        inputs: [{ source: 'sum.Instrumental', weight: 1 }, { source: 'gabox.Instrumental', weight: 0.75 }],
      },
    ],
  }
}

test('simple audio operations round-trip multi-stem sum, original-minus-vocals, and inversion', () => {
  const definition = audioOperationsFixture()
  assert.deepEqual(analyzeSimpleWorkflow(definition), { editable: true, reasonCodes: [] })
  assert.equal(getWorkflowDefinitionIssue(definition), null)
  assert.equal(countWorkflowSaveOutputs(definition), 2)
  const draft = hydrateSimpleWorkflow(definition)
  cleanupSimpleDraft(draft)
  const rebuilt = buildSimpleWorkflowDefinition(draft)
  assert.deepEqual(rebuilt.ensembles, definition.ensembles)
  assert.deepEqual(rebuilt.steps, definition.steps)
  assert.equal(getWorkflowDefinitionIssue(rebuilt), null)
  assert.deepEqual(prepareWorkflowDefinitionForRun(rebuilt, { device: 'cpu', outputFormat: 'flac' }).ensembles, definition.ensembles)
  assert.equal(canConnectSimple(draft, 'sum.Instrumental', simpleEnsembleInputTarget('blend', 0)).ok, true)
  assert.equal(canConnectSimple(draft, 'difference.Instrumental', simpleEnsembleInputTarget('blend', 0)).ok, true)
  assert.equal(canConnectSimple(draft, 'phase.Inverted', simpleStepInputTarget('cleanup')).ok, true)
})

test('audio processing outputs retain rename, save, deletion, and dependency checks', () => {
  const draft = hydrateSimpleWorkflow(audioOperationsFixture())
  updateSimpleEnsembleOutputStem(draft, draft.ensembles[1], 'Residual')
  assert.equal(draft.ensembles[2].inputs[0].source, 'difference.Residual')
  updateSimpleEnsembleOutputStem(draft, draft.ensembles[2], 'Reversed')
  assert.equal(draft.steps[2].input, 'phase.Reversed')
  assert.equal(disconnectSimple(draft, simpleSaveTarget('phase', 'Reversed')), true)
  assert.equal(connectSimple(draft, 'phase.Reversed', 'save').ok, true)
  assert.deepEqual(analyzeSimpleNodeRemovalImpact(draft, 'phase'), { connections: 1, savedOutputs: 1 })
  assert.deepEqual(analyzeSimpleModelChangeImpact(draft, 'sw', ['Vocals']), { connections: 5, savedOutputs: 0 })
  assert.deepEqual(canConnectSimple(draft, 'sw.Drums', simpleEnsembleInputTarget('sum', 1)), { ok: false, reason: 'duplicate-source' })
  assert.deepEqual(canConnectSimple(draft, 'phase.Reversed', simpleEnsembleInputTarget('phase', 0)), { ok: false, reason: 'self-link' })
  assert.deepEqual(canConnectSimple(draft, 'blend.Instrumental', simpleEnsembleInputTarget('sum', 0)), { ok: false, reason: 'forward-link' })
  assert.deepEqual(canConnectSimple(draft, 'phase.Reversed', simpleStepInputTarget('sw')), { ok: false, reason: 'forward-link' })
  assert.deepEqual(canConnectSimple(draft, 'cleanup.Clean', simpleEnsembleInputTarget('difference', 0)), { ok: false, reason: 'forward-link' })
  draft.ensembles = draft.ensembles.filter(node => node.id !== 'phase')
  cleanupSimpleDraft(draft)
  assert.equal(draft.steps[2].input, '')
})

test('audio operation validation rejects invalid arity, weighted inputs, duplicate sources and cycles', () => {
  const base = audioOperationsFixture()
  const invalidChanges = [
    definition => { definition.ensembles[0].inputs = definition.ensembles[0].inputs.slice(0, 1) },
    definition => { definition.ensembles[1].inputs.pop() },
    definition => { definition.ensembles[1].inputs.push({ source: 'gabox.Instrumental', weight: 1 }) },
    definition => { definition.ensembles[2].inputs.push({ source: 'input', weight: 1 }) },
    definition => { definition.ensembles[2].inputs = [] },
    definition => { definition.ensembles[0].inputs[0].weight = 0.5 },
    definition => { definition.ensembles[1].inputs[1].weight = -1 },
    definition => { definition.ensembles[2].inputs[0].weight = 2 },
    definition => { definition.ensembles[1].inputs[1].source = 'INPUT' },
    definition => { definition.ensembles[2].inputs[0].source = 'missing.Audio' },
    definition => { definition.ensembles[0].inputs[0].source = 'blend.Instrumental' },
    definition => { definition.steps[0].input = 'phase.Inverted' },
    definition => { definition.ensembles[1].inputs[0].source = 'cleanup.Clean' },
  ]
  for (const change of invalidChanges) {
    const definition = structuredClone(base)
    change(definition)
    assert.equal(getWorkflowDefinitionIssue(definition), 'invalid-definition')
    assert.deepEqual(analyzeSimpleWorkflow(definition), { editable: false, reasonCodes: ['invalid_definition'] })
  }
})

test('audio processing-only workflows save and reopen without inserting a separation model', () => {
  const invert = createAudioOperationDraft('invert', 0)
  assert.equal(invert.inputs.length, 1)
  assert.equal(createAudioOperationDraft('subtract', 1).inputs.length, 2)
  const draft = hydrateSimpleWorkflow({ steps: [] })
  draft.ensembles = [invert]
  assert.equal(connectSimple(draft, 'input', simpleEnsembleInputTarget(invert.id, 0)).ok, true)
  cleanupSimpleDraft(draft)
  const definition = buildSimpleWorkflowDefinition(draft)
  assert.equal(getWorkflowDefinitionIssue(definition), null)
  assert.equal(countWorkflowSaveOutputs(definition), 1)
  assert.deepEqual(analyzeSimpleWorkflow(definition), { editable: true, reasonCodes: [] })
  assert.deepEqual(buildSimpleWorkflowDefinition(hydrateSimpleWorkflow(definition)), definition)
  assert.equal(getWorkflowDefinitionIssue({ ...definition, version: 2 }), 'invalid-definition')
  assert.equal(getWorkflowDefinitionIssue({ ...definition, ensembles: [] }), 'steps-required')
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

test('simple step inference parameters round-trip and absent values keep global inheritance', () => {
  const source = simpleFixture()
  source.steps[0].model_type = 'mel_band_roformer'
  source.steps[0].inference_params = {
    batch_size: 3,
    overlap_size: 44100,
    normalize: false,
    enable_tta: true,
  }
  const draft = hydrateSimpleWorkflow(source)

  assert.deepEqual(draft.steps[0].inferenceParams, source.steps[0].inference_params)
  assert.equal(draft.steps[0].modelType, 'mel_band_roformer')
  assert.equal(draft.steps[1].inferenceParams, undefined)

  const rebuilt = buildSimpleWorkflowDefinition(draft)
  assert.deepEqual(rebuilt.steps[0].inference_params, source.steps[0].inference_params)
  assert.equal(rebuilt.steps[0].model_type, 'mel_band_roformer')
  assert.equal(Object.hasOwn(rebuilt.steps[1], 'inference_params'), false)
})

test('invalid known inference numbers reject editing and running without changing imported values', () => {
  const invalidValues = {
    batch_size: [0, -1, 1.5, NaN, Infinity, -Infinity, '2'],
    window_size: [0, -1, 1024.5, NaN, Infinity, -Infinity, '512'],
    aggression: [-1, 1.5, NaN, Infinity, -Infinity, '5'],
    overlap_size: [-1, 44100.5, NaN, Infinity, -Infinity, '44100'],
    chunk_size: [-1, 352800.5, NaN, Infinity, -Infinity, '352800'],
    post_process_threshold: [-0.01, 1.01, NaN, Infinity, -Infinity, '0.2'],
  }
  for (const [field, values] of Object.entries(invalidValues)) {
    for (const value of values) {
      const params = { [field]: value }
      assert.equal(normalizeSimpleInferenceParams(params), undefined, `${field}: ${value}`)
      for (const location of ['step', 'defaults']) {
        const definition = simpleFixture()
        if (location === 'step') definition.steps[0].inference_params = params
        else definition.defaults.inference_params = params
        assert.equal(getWorkflowDefinitionIssue(definition), 'invalid-definition', `${location} ${field}: ${value}`)
        assert.deepEqual(analyzeSimpleWorkflow(definition), { editable: false, reasonCodes: ['invalid_definition'] })
        assert.deepEqual(params, { [field]: value })
      }
    }
  }
})

test('zero sample settings and VR zero values remain explicit and survive a simple workflow round-trip', () => {
  const definition = simpleFixture()
  definition.steps[0].inference_params = { batch_size: 1, overlap_size: 0, chunk_size: 0 }
  definition.steps[1].model_type = 'vr'
  definition.steps[1].inference_params = { window_size: 1, aggression: 0, post_process_threshold: 0 }
  assert.equal(getWorkflowDefinitionIssue(definition), null)
  assert.deepEqual(analyzeSimpleWorkflow(definition), { editable: true, reasonCodes: [] })
  const rebuilt = buildSimpleWorkflowDefinition(hydrateSimpleWorkflow(definition))
  assert.deepEqual(rebuilt.steps[0].inference_params, definition.steps[0].inference_params)
  assert.deepEqual(rebuilt.steps[1].inference_params, definition.steps[1].inference_params)
  assert.deepEqual(normalizeSimpleInferenceParams({ post_process_threshold: 0.25 }), { post_process_threshold: 0.25 })
  assert.deepEqual(normalizeSimpleInferenceParams({ post_process_threshold: 1 }), { post_process_threshold: 1 })
})

test('native Default placeholders and unknown advanced inference fields remain protected without data loss', () => {
  for (const params of [
    { overlap_size: 'Default', chunk_size: ' default ' },
    { batch_size: 2, future_parameter: { mode: 'adaptive' } },
  ]) {
    const definition = simpleFixture()
    definition.steps[0].inference_params = params
    assert.equal(getWorkflowDefinitionIssue(definition), null)
    assert.deepEqual(analyzeSimpleWorkflow(definition), { editable: false, reasonCodes: ['advanced_parameters'] })
    const prepared = prepareWorkflowDefinitionForRun(definition, { device: 'cpu', outputFormat: 'wav' })
    assert.deepEqual(prepared.steps[0].inference_params, { normalize: true, ...params })
    assert.deepEqual(definition.steps[0].inference_params, params)
  }
})

test('native null number placeholders run while remaining protected from simple editing', () => {
  for (const key of ['batch_size', 'window_size', 'aggression', 'overlap_size', 'chunk_size', 'post_process_threshold']) {
    for (const scope of ['step', 'defaults']) {
      const definition = simpleFixture()
      const params = scope === 'step'
        ? (definition.steps[0].inference_params = { [key]: null })
        : Object.assign(definition.defaults.inference_params, { [key]: null })
      const original = structuredClone(definition)
      assert.equal(getWorkflowDefinitionIssue(definition), null, `${scope}.${key}`)
      assert.deepEqual(analyzeSimpleWorkflow(definition), { editable: false, reasonCodes: ['advanced_parameters'] })
      const prepared = prepareWorkflowDefinitionForRun(definition, {
        device: 'cpu', outputFormat: 'wav', modelInferenceParams: { 'first.ckpt': { [key]: 1 } },
      })
      assert.equal(prepared.steps[0].inference_params[key], null)
      assert.equal(params[key], null)
      assert.deepEqual(definition, original)
    }
  }
})

test('simple workflow normalization can explicitly follow the model global setting', () => {
  const source = simpleFixture()
  delete source.defaults.inference_params
  const draft = hydrateSimpleWorkflow(source)

  assert.equal(draft.defaultNormalize, null)
  const rebuilt = buildSimpleWorkflowDefinition(draft)
  assert.equal(Object.hasOwn(rebuilt.defaults, 'inference_params'), false)
})

test('explicit nullable native normalization is protected from lossy simple editing', () => {
  const definition = simpleFixture()
  definition.defaults.inference_params.normalize = null
  assert.deepEqual(analyzeSimpleWorkflow(definition), { editable: false, reasonCodes: ['advanced_parameters'] })
  const prepared = prepareWorkflowDefinitionForRun(definition, {
    device: 'cpu', outputFormat: 'wav', modelInferenceParams: { 'first.ckpt': { normalize: true } },
  })
  assert.equal(prepared.steps[0].inference_params.normalize, null)
  assert.equal(definition.defaults.inference_params.normalize, null)
})

test('simple runtime inference precedence is model global, workflow default, then step custom', () => {
  const source = simpleFixture()
  source.defaults.inference_params = { normalize: true, batch_size: 2 }
  source.steps[0].inference_params = { batch_size: 5, chunk_size: 8192 }

  const prepared = prepareWorkflowDefinitionForRun(source, {
    device: 'cpu',
    outputFormat: 'wav',
    modelInferenceParams: {
      'first.ckpt': { batch_size: 3, overlap_size: 44100, normalize: false },
      'second.pth': { batch_size: 4, chunk_size: 4096 },
    },
  })

  assert.deepEqual(prepared.steps[0].inference_params, {
    batch_size: 5,
    overlap_size: 44100,
    normalize: true,
    chunk_size: 8192,
  })
  assert.deepEqual(prepared.steps[1].inference_params, {
    batch_size: 2,
    chunk_size: 4096,
    normalize: true,
  })
  assert.equal(source.steps[1].inference_params, undefined)
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
