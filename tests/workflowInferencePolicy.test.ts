import assert from 'node:assert/strict'
import test from 'node:test'
import { graphNodeHasCustomParameters, replayWorkflowParameters, resolveWorkflowNode, workflowParameterTargets } from '../src/workflows/inferencePolicy.ts'
import type { ParameterResolutionMetadata } from '../src/features/inference/parameters.ts'

const metadata: ParameterResolutionMetadata = {
  version: 1, fingerprint: 'fixture', sampleRate: 1000,
  defaults: { batch_size: 4, chunk_size: 8000, overlap_size: 2000, normalize: false },
  capabilities: Object.fromEntries(['batch_size', 'chunk_size', 'overlap_size', 'normalize'].map(key => [key, { kind: 'free' }])),
  overlap: { kind: 'samples' },
}
const context = { mode: 'custom' as const, intent: { batchSize: 2 }, ttaOverride: true }

test('sparse decisions belong to each occurrence of a model, including equal, false and zero', () => {
  const definition = { steps: [
    { id: 'A', model: 'model' },
    { id: 'B', model: 'model', inference_params: { batch_size: 2, enable_tta: false, overlap_size: 0 } },
  ], studio: { nativeInferenceSteps: ['B'] } }
  const [a, b] = workflowParameterTargets(definition)
  const follow = { ...context, mode: 'model' as const }
  assert.equal(resolveWorkflowNode(a, metadata, {}, follow).parameters.batch_size, 4)
  assert.equal(resolveWorkflowNode(a, metadata, {}, follow).useTta, false)
  assert.equal(resolveWorkflowNode(b, metadata, {}, context).sources.batch_size, 'node')
  const changed = { ...context, intent: { batchSize: 3 } }
  assert.equal(resolveWorkflowNode(a, metadata, {}, changed).parameters.batch_size, 3)
  const result = resolveWorkflowNode(b, metadata, {}, changed)
  assert.equal(result.parameters.batch_size, 2)
  assert.equal(result.parameters.overlap_size, 0)
  assert.equal(result.useTta, false)
  assert.equal(result.sources.enable_tta, 'node')
  assert.equal(resolveWorkflowNode({ ...b, overrides: {} }, metadata, {}, changed).parameters.batch_size, 3)
})

test('old YAML sentinel masks its workflow default and follows the separation settings', () => {
  const [target] = workflowParameterTargets({ defaults: { inference_params: { normalize: true, overlap_size: 3000 } },
    steps: [{ id: 'legacy', model: 'model', inference_params: { overlap_size: 0 } }] })
  const result = resolveWorkflowNode(target, metadata, {}, { ...context, intent: { normalize: false, overlapBasisPoints: 7500 } })
  assert.equal(result.parameters.overlap_size, 6000)
  assert.equal(result.sources.overlap_size, 'custom')
  assert.equal(result.parameters.normalize, true)
  assert.equal(result.sources.normalize, 'workflow-default')
})

test('step Default masks workflow sizes until the step inherits them again', () => {
  for (const sentinel of [0, 'Default', null]) {
    const [target] = workflowParameterTargets({
      defaults: { inference_params: { chunk_size: 9000, overlap_size: 3000 } },
      steps: [{ id: 'legacy', model: 'model', inference_params: { chunk_size: sentinel, overlap_size: sentinel } }],
    })
    const follow = { mode: 'model' as const, intent: {} }
    const legacy = resolveWorkflowNode(target, metadata, {}, follow)
    assert.equal(legacy.parameters.chunk_size, 8000)
    assert.equal(legacy.parameters.overlap_size, 2000)
    assert.equal(legacy.sources.overlap_size, 'model-config')
    const inherited = resolveWorkflowNode({ ...target, modelDefaultKeys: [] }, metadata, {}, follow)
    assert.equal(inherited.parameters.chunk_size, 9000)
    assert.equal(inherited.parameters.overlap_size, 3000)
    assert.equal(inherited.sources.overlap_size, 'workflow-default')
  }
})

test('explicit legacy null values preserve the native compiler defaults', () => {
  for (const [type, expected] of [
    ['bs_roformer', { batch_size: 1, normalize: false, standardize: false }],
    ['VR', { batch_size: 1, normalize: false, window_size: 512, aggression: 5, enable_post_process: false,
      post_process_threshold: 0.2, high_end_process: false }],
  ] as const) {
    const configured = { batch_size: 4, normalize: true, enable_tta: true,
      ...(type === 'VR' ? { window_size: 1024, aggression: 9, enable_post_process: true,
        post_process_threshold: 0.75, high_end_process: true } : { standardize: true }) }
    const keys = [...Object.keys(expected), 'enable_tta']
    const [target] = workflowParameterTargets({
      defaults: { inference_params: configured },
      steps: [{ id: 'legacy', model: 'model', model_type: type, use_tta: true,
        inference_params: Object.fromEntries(keys.map(key => [key, null])) }],
    })
    const result = resolveWorkflowNode(target, undefined, undefined, { mode: 'custom', intent: {}, ttaOverride: true })
    assert.deepEqual(result.parameters, expected)
    assert.equal(result.useTta, false)
    for (const key of keys) assert.equal(result.sources[key], 'node', key)
  }
})

test('explicit null in legacy workflow defaults keeps the native default values', () => {
  const [target] = workflowParameterTargets({
    defaults: { inference_params: { batch_size: null, normalize: null, enable_tta: null } },
    steps: [{ id: 'legacy', model: 'model' }],
  })
  const result = resolveWorkflowNode(target, metadata, undefined, {
    mode: 'custom', intent: { batchSize: 6, normalize: true }, ttaOverride: true,
  })
  assert.equal(result.parameters.batch_size, 1)
  assert.equal(result.parameters.normalize, false)
  assert.equal(result.useTta, false)
  for (const key of ['batch_size', 'normalize', 'enable_tta']) assert.equal(result.sources[key], 'workflow-default')
})

test('a VR model without a YAML model type keeps model defaults for explicitly null VR fields', () => {
  const vrMetadata: ParameterResolutionMetadata = {
    ...metadata, defaults: { aggression: 7, window_size: 768 },
    capabilities: { aggression: { kind: 'free' }, window_size: { kind: 'free' } },
  }
  const [target] = workflowParameterTargets({
    defaults: { inference_params: { aggression: 9, window_size: null } },
    steps: [{ id: 'legacy', model: '3_HP-Vocal-UVR.pth', inference_params: { aggression: null } }],
  })
  const result = resolveWorkflowNode(target, vrMetadata, { aggression: 6, window_size: 1024 }, {
    mode: 'custom', intent: { vr: { aggression: 12, window_size: 1536 } },
  })
  assert.deepEqual(result.parameters, vrMetadata.defaults)
  assert.equal(result.sources.aggression, 'model-config')
  assert.equal(result.sources.window_size, 'model-config')
})

test('legacy overlap defaults follow the final block length without storing an absolute override', () => {
  const countMetadata: ParameterResolutionMetadata = {
    ...metadata, defaults: { ...metadata.defaults, overlap_size: 6000 }, overlap: { kind: 'count', count: 4 },
  }
  for (const overlap_size of [0, 'Default']) {
    const [target] = workflowParameterTargets({
      steps: [{ id: 'legacy', model: 'model', inference_params: { chunk_size: 2000, overlap_size } }],
    })
    const result = resolveWorkflowNode(target, countMetadata, {}, { mode: 'model', intent: {} })
    assert.equal(result.parameters.overlap_size, 1500)
    assert.equal(result.sources.overlap_size, 'model-config')
    assert.deepEqual(result.issues, [])
  }
})

test('simple targets use the same trimmed IDs and local model paths as the native YAML loader', () => {
  const [target] = workflowParameterTargets({
    steps: [{ id: ' vocals ', model_path: ' D:/Models/local.ckpt ', model_type: ' bs_roformer ',
      config_path: ' D:/Models/local.yaml ', model_dir: ' D:/Models ', inference_params: { overlap_size: 0 } }],
    studio: { nativeInferenceSteps: [' vocals '] },
  })
  assert.equal(target.id, 'vocals')
  assert.equal(target.model, 'D:/Models/local.ckpt')
  assert.equal(target.modelPath, 'D:/Models/local.ckpt')
  assert.equal(target.configPath, 'D:/Models/local.yaml')
  assert.equal(target.modelType, 'bs_roformer')
  assert.equal(target.modelDir, 'D:/Models')
  assert.deepEqual(target.overrides, { overlap_size: 0 })
})

test('a shared producer has a separate consumer override and dimensions use the effective chunk', () => {
  const definition = { nodes: [
    { id: 1, type: 'pymss_mss_params', widgets_values: [4, 'Default', '4000', false, false, false] },
    ...[2, 3].map(id => ({ id, type: 'mss_separate', widgets_values: ['[Type] model'],
      inputs: [{ name: 'params', link: id }], properties: id === 3 ? { studioInferenceOverrides: { batch_size: 1 } } : {} })),
  ], links: [[2, 1, 0, 2, 1, 'PYMSS_MSS_PARAMS'], [3, 1, 0, 3, 1, 'PYMSS_MSS_PARAMS']] }
  const [a,b] = workflowParameterTargets(definition)
  const intent = { ...context, intent: { chunkMilliseconds: 9000, overlapBasisPoints: 5000, batchSize: 2 } }
  const left = resolveWorkflowNode(a, metadata, {}, intent)
  const right = resolveWorkflowNode(b, metadata, {}, intent)
  assert.equal(a.model, 'model')
  assert.equal(left.parameters.chunk_size, 4000)
  assert.equal(left.parameters.overlap_size, 2000)
  assert.equal(left.parameters.batch_size, 4)
  assert.equal(left.sources.batch_size, 'link')
  assert.equal(right.parameters.batch_size, 1)
  assert.equal(right.sources.batch_size, 'node')
  assert.equal(definition.nodes[0].widgets_values[0], 4)
})

test('blank MSS size widgets inherit, while an explicit zero overlap remains an override', () => {
  const type = 'pymss_mss_params'
  for (const [overlap, expected, source] of [
    [' \t ', 2000, 'model-config'], ['', 2000, 'model-config'], [' 0 ', 0, 'link'],
  ] as const) {
    const widgets = [1, overlap, ' ', false, false, false]
    const [target] = workflowParameterTargets({ nodes: [
      { id: 1, type, widgets_values: widgets },
      { id: 2, type: 'pymss_mss_separate', widgets_values: ['model'], inputs: [{ name: 'params', link: 1 }] },
    ], links: [[1, 1, 0, 2, 1, 'PYMSS_MSS_PARAMS']] })
    assert.equal(target.linkValues?.chunk_size, undefined)
    const result = resolveWorkflowNode(target, metadata, undefined, { mode: 'model', intent: {} })
    assert.equal(result.parameters.overlap_size, expected)
    assert.equal(result.sources.overlap_size, source)
    const custom = resolveWorkflowNode(target, metadata, undefined, { mode: 'custom', intent: { overlapBasisPoints: 5000 } })
    assert.equal(custom.parameters.overlap_size, source === 'link' ? 0 : 4000)
    assert.equal(graphNodeHasCustomParameters(type, undefined, index => widgets[index]), source === 'link')
  }
})

test('historical retries retain observed dynamic parameters without mutating original snapshots', () => {
  const [target] = workflowParameterTargets({ steps: [{ id: 'A', model: 'm' }] })
  const node = { ...resolveWorkflowNode(target, metadata, {}, context), dynamic: true }
  const plan = { version: 1 as const, context, nodes: { A: node } }
  const replay = replayWorkflowParameters(plan, { A: { parameters: { batch_size: 7 }, sources: { batch_size: 'link' }, useTta: false, fingerprint: 'installed' } })
  assert.equal(replay.nodes.A.replay, true)
  assert.equal(replay.nodes.A.fingerprint, 'installed')
  assert.equal(replay.nodes.A.parameters.batch_size, 7)
  assert.equal(node.parameters.batch_size, 2)
  assert.equal(node.dynamic, true)
})

test('only unmasked global intent requires metadata, and fixed native fields are checked before submitting', () => {
  const [target] = workflowParameterTargets({ steps: [{ id: 'A', model: 'm', inference_params: { batch_size: 2 } }] })
  assert.deepEqual(resolveWorkflowNode(target, undefined, {}, context).issues, [])
  assert.ok(resolveWorkflowNode({ ...target, overrides: {} }, undefined, {}, context).issues.length)
  const fixed = { ...metadata, capabilities: { ...metadata.capabilities, batch_size: { kind: 'fixed' as const } } }
  assert.deepEqual(resolveWorkflowNode(target, fixed, {}, context).issues, [{ key: 'batch_size', code: 'invalidValue' }])
})
