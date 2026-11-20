import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test, { after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { setupLitegraphEnvDom } from './_litegraphEnv.mjs'

await setupLitegraphEnvDom()
const vite = await createServer({
  configFile: false,
  server: { watch: null, middlewareMode: true, hmr: false, ws: false },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
  ssr: { noExternal: ['@comfyorg/litegraph'] },
})
after(() => vite.close())

const { analyzeComfyOverview, buildSimpleWorkflowDefinition, hydrateSimpleWorkflow, setSimpleStepInferenceOverrides } =
  await vite.ssrLoadModule('/src/utils/workflowSimple.ts')
const { workflowParameterTargets, resolveWorkflowNode } = await vite.ssrLoadModule('/src/workflows/inferencePolicy.ts')
const { buildWorkflowPreview } = await vite.ssrLoadModule('/src/workflows/preview.ts')
const { buildComfyWorkflowExport } = await vite.ssrLoadModule('/src/workflows/comfyExport.ts')
const { LGraph, LiteGraph } = await vite.ssrLoadModule('@comfyorg/litegraph')
const { registerPymssNodes } = await vite.ssrLoadModule('/src/litegraph/registerNodes.ts')
const { comfyToLitegraph, litegraphToComfy, setGraphNodeInferenceOverrides } =
  await vite.ssrLoadModule('/src/litegraph/graphAdapter.ts')
const { createWorkflowHistory, recordWorkflowSnapshot, undoWorkflowHistory, redoWorkflowHistory } =
  await vite.ssrLoadModule('/src/litegraph/history.ts')
registerPymssNodes()

test('imported graph previews distinguish a modified native parameter node from individual consumers', () => {
  const graph = JSON.parse(readFileSync(new URL('./fixtures/comfy-mss/example_vr_separate.json', import.meta.url), 'utf8'))
  const source = graph.nodes.find(node => node.type === 'pymss_vr_params')
  const consumer = graph.nodes.find(node => node.type === 'vr_separate')
  const marked = () => buildWorkflowPreview(graph, key => key).nodes.filter(node => node.customParameters).map(node => node.id).sort()
  assert.deepEqual(marked(), [])
  assert.equal(buildWorkflowPreview(graph, key => key).nodes.find(node => node.id === String(consumer.id)).detail,
    consumer.widgets_values[0])
  source.widgets_values[6] = 0.20000000000000004
  assert.deepEqual(marked(), [])
  source.widgets_values[2] = 0
  assert.deepEqual(marked(), [String(source.id)])
  consumer.type = 'pymss_vr_separate_list'
  assert.equal(buildWorkflowPreview(graph, key => key).nodes.find(node => node.id === String(consumer.id)).detail,
    consumer.widgets_values[0])
  consumer.properties.studioInferenceOverrides = { enable_tta: false }
  assert.deepEqual(marked(), [String(source.id), String(consumer.id)].sort())
  source.widgets_values[2] = 5
  assert.deepEqual(marked(), [String(consumer.id)])
  delete consumer.properties.studioInferenceOverrides
  assert.deepEqual(marked(), [])
})

test('first simple step edit saves only explicit fields even when equal to the workflow default', () => {
  const draft = hydrateSimpleWorkflow({
    version: 1,
    defaults: { inference_params: { normalize: false } },
    steps: [{ id: 'vocals', model: 'vocals.ckpt', stems: ['vocals'] }],
  })
  const step = draft.steps[0]
  const [inherited] = workflowParameterTargets(buildSimpleWorkflowDefinition(draft))
  assert.deepEqual(inherited.overrides, {})
  assert.deepEqual(inherited.defaults, { normalize: false })

  setSimpleStepInferenceOverrides(draft, step, { normalize: false })
  const saved = buildSimpleWorkflowDefinition(draft)
  assert.deepEqual(saved.steps[0].inference_params, { normalize: false })
  const [explicit] = workflowParameterTargets(saved)
  const context = { mode: 'model', intent: {} }
  assert.equal(resolveWorkflowNode(explicit, undefined, undefined, context).sources.normalize, 'node')
  assert.equal(resolveWorkflowNode({ ...explicit, overrides: {} }, undefined, undefined, context).sources.normalize, 'workflow-default')

  setSimpleStepInferenceOverrides(draft, step, {})
  assert.equal(Object.hasOwn(buildSimpleWorkflowDefinition(draft).steps[0], 'inference_params'), false)
})

test('simple draft preserves legacy overlap zero and distinguishes explicitly chosen zero', () => {
  const definition = {
    version: 1,
    steps: [{ id: 'vocals', model: 'vocals.ckpt', inference_params: { overlap_size: 0 } }],
  }
  const draft = hydrateSimpleWorkflow(definition)
  const step = draft.steps[0]
  assert.equal(buildWorkflowPreview(definition, key => key).nodes.find(node => node.id === 'step:vocals').customParameters, undefined)
  setSimpleStepInferenceOverrides(draft, step, { overlap_size: 0, enable_tta: false }, ['overlap_size'])
  const legacy = buildSimpleWorkflowDefinition(draft)
  assert.deepEqual(legacy.steps[0].inference_params, { overlap_size: 0, enable_tta: false })
  assert.equal(workflowParameterTargets(legacy)[0].overrides.overlap_size, undefined)
  assert.equal(legacy.studio.nativeInferenceSteps, undefined)

  setSimpleStepInferenceOverrides(draft, step, { overlap_size: 0, enable_tta: false })
  const explicit = buildSimpleWorkflowDefinition(hydrateSimpleWorkflow(JSON.parse(JSON.stringify(buildSimpleWorkflowDefinition(draft)))))
  assert.deepEqual(explicit.studio.nativeInferenceSteps, ['vocals'])
  assert.deepEqual(workflowParameterTargets(explicit)[0].overrides, { overlap_size: 0, enable_tta: false })
  assert.equal(buildWorkflowPreview(explicit, key => key).nodes.find(node => node.id === 'step:vocals').customParameters, true)
})

test('editing a legacy download checkbox persists the same value that the workflow will run', () => {
  const legacy = { nodes: [{
    id: 1, type: 'mss_separate', widgets_values: ['model', 'cpu', '1', false, 'huggingface', false],
  }], links: [] }
  const directlyExported = litegraphToComfy(legacy)
  assert.equal(directlyExported.nodes[0].type, 'pymss_mss_separate')
  assert.deepEqual(directlyExported.nodes[0].widgets_values, ['model', 'cpu', false, 'huggingface', '1', false])
  assert.equal(workflowParameterTargets(directlyExported)[0].downloadMissing, false)
  assert.equal(workflowParameterTargets(directlyExported)[0].downloadSource, 'huggingface')

  const graph = new LGraph()
  graph.configure(comfyToLitegraph(legacy))
  const toggle = graph.getNodeById(1).widgets.find(widget => widget.name === 'download_missing')
  assert.equal(toggle.value, false)
  toggle.value = true
  const saved = litegraphToComfy(graph.serialize())
  assert.equal(saved.nodes[0].type, 'pymss_mss_separate')
  assert.deepEqual(saved.nodes[0].widgets_values, ['model', 'cpu', true, 'huggingface', '1', false])
  const [target] = workflowParameterTargets(saved)
  assert.equal(resolveWorkflowNode(target, undefined, {}, { mode: 'model', intent: {} }).downloadMissing, true)
})

test('Comfy-MSS export converts only the overridden consumer to a native params link', () => {
  const graph = new LGraph()
  const source = LiteGraph.createNode('pymss_mss_params')
  const first = LiteGraph.createNode('pymss_mss_separate')
  const second = LiteGraph.createNode('pymss_mss_separate')
  for (const node of [source, first, second]) graph.add(node)
  source.connect(0, first, 1)
  source.connect(0, second, 1)
  setGraphNodeInferenceOverrides(first, { batch_size: 4 })
  const definition = litegraphToComfy(graph.serialize())
  const [target] = workflowParameterTargets(definition)
  const context = { mode: 'model', intent: {} }
  const resolved = resolveWorkflowNode(target, undefined, undefined, context)
  const plan = { version: 1, context, nodes: { [first.id]: resolved } }
  const exported = buildComfyWorkflowExport(definition, plan)
  const [portable, sibling] = workflowParameterTargets(exported)
  assert.equal(portable.linkValues.batch_size, 4)
  assert.equal(sibling.linkValues.batch_size, 1)
  assert.deepEqual(portable.overrides, {})
  assert.deepEqual(workflowParameterTargets(definition)[0].overrides, { batch_size: 4 })
  const imported = new LGraph()
  imported.configure(comfyToLitegraph(exported))
  assert.equal(imported.links.size, 2)
  assert.equal(imported.nodes.length, 4)

  const zero = { ...resolved, parameters: { ...resolved.parameters, overlap_size: 0 } }
  assert.throws(() => buildComfyWorkflowExport(definition, { ...plan, nodes: { [first.id]: zero } }), /overlap_size/)
})

test('a missing legacy download source still inherits after a Studio round trip', () => {
  const definition = { nodes: [
    { id: 1, type: 'mss_separate', widgets_values: ['model', 'cpu', '0', true] },
    { id: 2, type: 'pymss_vr_separate', widgets_values: ['model', 'cpu', false] },
  ], links: [] }
  const graph = new LGraph()
  graph.configure(comfyToLitegraph(definition))
  const studio = litegraphToComfy(graph.serialize())
  for (const result of [definition, studio]) {
    assert.deepEqual(workflowParameterTargets(result).map(target => target.downloadSource), [undefined, undefined])
  }
  assert.deepEqual(studio.nodes.map(node => node.widgets_values[3]), ['Default', 'Default'])
  const comfy = buildComfyWorkflowExport(definition)
  assert.deepEqual(comfy.nodes.map(node => node.widgets_values[3]), ['modelscope', 'modelscope'])
})

test('Comfy-MSS export rejects zero overlap on an existing linked MSS parameter node', () => {
  const graph = new LGraph()
  const source = LiteGraph.createNode('pymss_mss_params')
  const consumer = LiteGraph.createNode('pymss_mss_separate')
  graph.add(source)
  graph.add(consumer)
  source.connect(0, consumer, 1)
  for (const zero of ['0', 0]) {
    source.widgets[1].value = zero
    const definition = litegraphToComfy(graph.serialize())
    assert.equal(workflowParameterTargets(definition)[0].linkValues.overlap_size, 0)
    assert.throws(() => buildComfyWorkflowExport(definition), /overlap_size/)
  }
  source.widgets[1].value = 'Default'
  source.widgets[2].value = '0'
  const definition = litegraphToComfy(graph.serialize())
  const portable = buildComfyWorkflowExport(definition)
  assert.deepEqual(portable.nodes[0].widgets_values.slice(1, 3), ['Default', 'Default'])
  assert.equal(definition.nodes[0].widgets_values[2], '0')
})

test('Comfy-MSS export preserves a model-configured zero overlap using Default', () => {
  const graph = new LGraph()
  const node = LiteGraph.createNode('pymss_mss_separate')
  graph.add(node)
  setGraphNodeInferenceOverrides(node, { batch_size: 4 })
  const definition = litegraphToComfy(graph.serialize())
  const defaults = { batch_size: 1, chunk_size: 8000, overlap_size: 0, normalize: false, standardize: false }
  const metadata = {
    version: 1, fingerprint: 'model-defaults', sampleRate: 44100, defaults,
    capabilities: Object.fromEntries(Object.keys(defaults).map(key => [key, { kind: 'free' }])),
    overlap: { kind: 'samples' },
  }
  const context = { mode: 'model', intent: {} }
  const [target] = workflowParameterTargets(definition)
  const resolved = resolveWorkflowNode(target, metadata, undefined, context)
  assert.equal(resolved.parameters.overlap_size, 0)
  const exported = buildComfyWorkflowExport(definition, { version: 1, context, nodes: { [node.id]: resolved } })
  assert.deepEqual(exported.nodes.find(node => node.type === 'pymss_mss_params').widgets_values,
    [4, 'Default', 'Default', false, false, false])

  setGraphNodeInferenceOverrides(node, { chunk_size: 4000 })
  const withChunk = litegraphToComfy(graph.serialize())
  const [changed] = workflowParameterTargets(withChunk)
  const changedPlan = resolveWorkflowNode(changed, metadata, undefined, context)
  assert.equal(changedPlan.parameters.overlap_size, 0)
  const portable = buildComfyWorkflowExport(withChunk, {
    version: 1, context, nodes: { [node.id]: { ...changedPlan, modelDefaultOverlap: metadata.defaults.overlap_size } },
  })
  assert.deepEqual(portable.nodes.find(node => node.type === 'pymss_mss_params').widgets_values,
    [1, 'Default', '4000', false, false, false])

  setGraphNodeInferenceOverrides(node, { chunk_size: 4000, overlap_size: 0 })
  const withExplicitZero = litegraphToComfy(graph.serialize())
  const [explicit] = workflowParameterTargets(withExplicitZero)
  const explicitPlan = resolveWorkflowNode(explicit, metadata, undefined, context)
  assert.throws(() => buildComfyWorkflowExport(withExplicitZero, {
    version: 1, context, nodes: { [node.id]: { ...explicitPlan, modelDefaultOverlap: metadata.defaults.overlap_size } },
  }), /overlap_size/)
})

test('replacing a graph node model restores parameter inheritance and undo restores its overrides', () => {
  const graph = new LGraph()
  const consumer = LiteGraph.createNode('pymss_mss_separate')
  graph.add(consumer)
  const select = consumer.widgets[0]
  select.value = 'first'
  select.callback('first')
  setGraphNodeInferenceOverrides(consumer, { chunk_size: 8000, batch_size: 2 })
  const before = JSON.stringify(litegraphToComfy(graph.serialize()))
  select.value = '[vocals] first'
  select.callback('[vocals] first')
  assert.ok(consumer.properties.studioInferenceOverrides)

  select.value = 'second'
  select.callback('second')
  const after = JSON.stringify(litegraphToComfy(graph.serialize()))
  const [changed] = workflowParameterTargets(JSON.parse(after))
  assert.equal(changed.model, 'second')
  assert.deepEqual(changed.overrides, {})

  const history = recordWorkflowSnapshot(createWorkflowHistory(before), after)
  graph.configure(comfyToLitegraph(JSON.parse(undoWorkflowHistory(history).snapshot)))
  const [restored] = workflowParameterTargets(litegraphToComfy(graph.serialize()))
  assert.equal(restored.model, 'first')
  assert.deepEqual(restored.overrides, { chunk_size: 8000, batch_size: 2 })
})

test('replacing one bracketed custom model with another clears its node overrides', () => {
  const graph = new LGraph()
  const consumer = LiteGraph.createNode('pymss_custom_mss_separate')
  graph.add(consumer)
  const select = consumer.widgets[0]
  select.value = '[My]-vocals'
  select.callback(select.value)
  setGraphNodeInferenceOverrides(consumer, { batch_size: 2 })
  select.value = '[Other]-vocals'
  select.callback(select.value)
  const definition = litegraphToComfy(graph.serialize())
  const [target] = workflowParameterTargets(definition)
  assert.equal(target.model, '[Other]-vocals')
  assert.deepEqual(analyzeComfyOverview(definition).models, ['[Other]-vocals'])
  assert.deepEqual(target.overrides, {})
})

test('graph node overrides survive JSON and history without changing linked siblings or widgets', () => {
  const graph = new LGraph()
  const source = LiteGraph.createNode('pymss_mss_params')
  const first = LiteGraph.createNode('mss_separate')
  const second = LiteGraph.createNode('mss_separate')
  graph.add(source)
  graph.add(first)
  graph.add(second)
  source.connect(0, first, 1)
  source.connect(0, second, 1)
  first.properties.keep = 'unrelated value'
  const before = JSON.stringify(litegraphToComfy(graph.serialize()))
  const historyStart = createWorkflowHistory(before)
  const values = { overlap_size: 0, normalize: false, enable_tta: false }
  setGraphNodeInferenceOverrides(first, values)
  const after = JSON.stringify(litegraphToComfy(graph.serialize()))
  const history = recordWorkflowSnapshot(historyStart, after)
  const undo = undoWorkflowHistory(history)
  const redo = redoWorkflowHistory(undo.history)

  graph.configure(comfyToLitegraph(JSON.parse(undo.snapshot)))
  assert.equal(graph.getNodeById(first.id).properties.studioInferenceOverrides, undefined)
  graph.configure(comfyToLitegraph(JSON.parse(redo.snapshot)))
  const restored = graph.getNodeById(first.id)
  const [target, sibling] = workflowParameterTargets(litegraphToComfy(graph.serialize()))
  assert.equal(target.linked, true)
  assert.equal(target.linkSource, sibling.linkSource)
  assert.deepEqual(target.overrides, values)
  assert.deepEqual(sibling.overrides, {})
  assert.deepEqual(graph.getNodeById(source.id).serialize().widgets_values, JSON.parse(before).nodes[0].widgets_values)
  setGraphNodeInferenceOverrides(restored, {})
  assert.equal(Object.hasOwn(restored.properties, 'studioInferenceOverrides'), false)
  assert.equal(restored.properties.keep, 'unrelated value')
})
