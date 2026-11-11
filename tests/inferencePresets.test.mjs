import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'
import { computed, reactive, ref } from 'vue'
import { INFERENCE_PRESETS, matchingInferencePreset, supportsInferencePresets } from '../src/features/inference/presets.ts'
import { resolveParameters } from '../src/features/inference/parameters.ts'

const source = ts.createSourceFile('separate.ts', parse(readFileSync(new URL('../src/views/SeparateView.vue', import.meta.url), 'utf8')).descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const apply = source.statements.find(statement => ts.isFunctionDeclaration(statement) && statement.name?.text === 'applyParameterPreset')
assert.ok(apply)
const code = ts.transpileModule(apply.getText(source), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

function metadata(sampleRate = 44100, lattice = false) {
  const chunk = lattice ? 457728 : sampleRate * 10
  return { version: 1, fingerprint: 'preset-test', sampleRate,
    defaults: { chunk_size: chunk, overlap_size: Math.floor(chunk / 2), batch_size: 1, standardize: false, normalize: true },
    capabilities: { chunk_size: lattice ? { kind: 'lattice', min: 31744, step: 32768, offset: 31744 } : { kind: 'free', min: 1025 },
      overlap_size: { kind: 'free', min: 0 }, batch_size: { kind: 'free', min: 1 }, standardize: { kind: 'free' }, normalize: { kind: 'free' } },
    overlap: { kind: 'samples' },
  }
}

function draft(intent = {}) {
  const state = { parameterDraftIntent: { value: intent }, showParameterPresets: { value: true }, parameterPresetsDisabled: { value: false } }
  const view = vm.runInNewContext(`${code}\n({applyParameterPreset})`, state)
  return { state, apply: view.applyParameterPreset }
}

test('presets update the two draft values atomically without changing other settings or the original draft', () => {
  const original = Object.freeze({ batchSize: 4, standardize: true, normalize: false, vr: { aggression: 0 }, chunkMilliseconds: 11000 })
  const h = draft(original)
  for (const preset of INFERENCE_PRESETS) {
    h.apply(preset)
    const next = h.state.parameterDraftIntent.value
    assert.equal(next.chunkMilliseconds, preset.chunkMilliseconds)
    assert.equal(next.overlapBasisPoints, preset.overlapBasisPoints)
    assert.equal(next.batchSize, 4)
    assert.equal(next.standardize, true)
    assert.equal(next.normalize, false)
    assert.equal(next.vr, original.vr)
    assert.equal(matchingInferencePreset(next), preset.id)
  }
  assert.equal(original.chunkMilliseconds, 11000)
  assert.equal(original.overlapBasisPoints, undefined)
})

test('read-only and busy states cannot apply a preset', () => {
  const h = draft()
  h.state.showParameterPresets.value = false
  h.apply(INFERENCE_PRESETS[0])
  assert.deepEqual(h.state.parameterDraftIntent.value, {})
  h.state.showParameterPresets.value = true
  h.state.parameterPresetsDisabled.value = true
  h.apply(INFERENCE_PRESETS[0])
  assert.deepEqual(h.state.parameterDraftIntent.value, {})
})

test('selection follows exact shared intent and clears when either value is manually changed', () => {
  assert.equal(matchingInferencePreset({}), undefined)
  assert.equal(matchingInferencePreset({ chunkMilliseconds: 10000 }), undefined)
  const intent = { chunkMilliseconds: 10000, overlapBasisPoints: 5000, batchSize: 8 }
  assert.equal(matchingInferencePreset(intent), 'balanced')
  assert.equal(matchingInferencePreset({ ...intent, overlapBasisPoints: 4900 }), undefined)
  assert.equal(matchingInferencePreset({ ...intent, chunkMilliseconds: 10500 }), undefined)
})

test('presets require both editable parameters and a known sample rate', () => {
  const meta = metadata()
  assert.equal(supportsInferencePresets(meta), true)
  assert.equal(supportsInferencePresets(metadata(48000, true)), true)
  assert.equal(supportsInferencePresets(undefined), false)
  assert.equal(supportsInferencePresets({ ...meta, sampleRate: undefined }), false)
  assert.equal(supportsInferencePresets({ ...meta, defaults: { batch_size: 1 } }), false)
  for (const kind of ['fixed', 'unsupported', 'unknown']) {
    for (const key of ['chunk_size', 'overlap_size']) {
      assert.equal(supportsInferencePresets({ ...meta, capabilities: { ...meta.capabilities, [key]: { kind } } }), false)
    }
  }
})

for (const sampleRate of [44100, 48000]) {
  for (const lattice of [false, true]) {
    test(`preset intent uses the existing resolver at ${sampleRate} Hz (lattice=${lattice})`, () => {
      const meta = metadata(sampleRate, lattice)
      const h = draft({ batchSize: 4, normalize: false })
      for (const preset of INFERENCE_PRESETS) {
        h.apply(preset)
        const result = resolveParameters(meta, undefined, 'custom', h.state.parameterDraftIntent.value)
        assert.deepEqual(result.issues, [])
        assert.equal(result.parameters.batch_size, 4)
        assert.equal(result.parameters.normalize, false)
        assert.equal(result.parameters.standardize, false)
        assert.ok(Math.abs(result.parameters.overlap_size / result.parameters.chunk_size - preset.overlapBasisPoints / 10000) < 1 / result.parameters.chunk_size)
        if (lattice) assert.equal((result.parameters.chunk_size - 31744) % 32768, 0)
        else assert.equal(result.parameters.chunk_size, sampleRate * preset.chunkMilliseconds / 1000)
        assert.equal(matchingInferencePreset(h.state.parameterDraftIntent.value), preset.id)
      }
    })
  }
}

function parameterContext(singleName, selectedNames, pendingNames = []) {
  const vr = { name: 'vr', inferenceParamMeta: { resolution: { version: 1, sampleRate: 44100,
    fingerprint: 'vr', defaults: { window_size: 512 }, capabilities: { chunk_size: { kind: 'unsupported' }, overlap_size: { kind: 'unsupported' } }, overlap: { kind: 'unknown' } } } }
  const models = [vr, { name: 'mss-a', inferenceParamMeta: { resolution: metadata(48000) } },
    { name: 'mss-b', inferenceParamMeta: { resolution: metadata() } }]
  const names = new Set(['selectedModelName', 'selectedModelListItem', 'currentModelInfo', 'ensembleModelEntries',
    'parameterReferenceModel', 'parameterDraftResolution', 'showParameterPresets'])
  const declarations = source.statements.filter(statement => ts.isVariableStatement(statement)
    && statement.declarationList.declarations.some(item => ts.isIdentifier(item.name) && names.has(item.name.text)))
  const script = ts.transpileModule(declarations.map(item => item.getText(source)).join('\n'), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  const state = { computed, supportsInferencePresets, resolveParameters,
    selectedModel: ref(singleName), model: reactive({ selectedInfo: models.find(item => item.name === singleName), getModelInferenceOverrides: () => ({ batch_size: 4 }) }),
    listedDownloadedModels: ref(models), ensembleEnabled: ref(true), ensembleModels: ref(selectedNames),
    parameterDraftMode: ref('custom'), parameterDraftIntent: ref({ chunkMilliseconds: 8000, overlapBasisPoints: 2000 }),
    pending: reactive(new Set(pendingNames)),
  }
  state.task = { pendingLegacyParameters: name => state.pending.has(name) ? {} : undefined }
  const view = vm.runInNewContext(`${script}\n({showParameterPresets, parameterDraftResolution,
    reference: typeof parameterReferenceModel === 'undefined' ? currentModelInfo : parameterReferenceModel})`, state)
  return { state, view }
}

test('ensemble presets and preview use an applicable participating model instead of the previous VR selection', () => {
  const { state, view } = parameterContext('vr', ['mss-a', 'mss-b'])
  assert.equal(view.showParameterPresets.value, true)
  assert.equal(view.reference.value.name, 'mss-a')
  assert.equal(view.parameterDraftResolution.value.parameters.chunk_size, 384000)
  assert.equal(view.parameterDraftResolution.value.parameters.batch_size, 4)
  assert.equal(state.selectedModel.value, 'vr')
  assert.equal(state.model.selectedInfo.name, 'vr')
})

test('ensemble presets do not require any previous single-model metadata', () => {
  const { view } = parameterContext('missing', ['mss-a', 'mss-b'])
  assert.equal(view.showParameterPresets.value, true)
  assert.equal(view.parameterDraftResolution.value.issues.length, 0)
})

test('unsupported ensembles ignore an applicable old single model and switching back restores its context', () => {
  const { state, view } = parameterContext('mss-a', ['vr'])
  assert.equal(view.showParameterPresets.value, false)
  assert.equal(view.reference.value.name, 'vr')
  state.ensembleModels.value = ['vr', 'mss-b']
  assert.equal(view.showParameterPresets.value, true)
  assert.equal(view.reference.value.name, 'mss-b')
  state.ensembleModels.value = []
  assert.equal(view.showParameterPresets.value, false)
  state.ensembleEnabled.value = false
  assert.equal(view.reference.value.name, 'mss-a')
  assert.equal(view.showParameterPresets.value, true)
})

test('legacy parameter decisions stay reachable for participating models and ignore an unrelated single model', () => {
  const { state, view } = parameterContext('vr', ['mss-a', 'mss-b'], ['vr', 'mss-b'])
  assert.equal(view.reference.value.name, 'mss-b')
  state.pending.delete('mss-b')
  assert.equal(view.reference.value.name, 'mss-a')
})
