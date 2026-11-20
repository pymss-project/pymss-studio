import assert from 'node:assert/strict'
import test from 'node:test'
import { createWorkflowBindings, prepareWorkbenchRun, visibleWorkflowNodeId, workflowOutputNodes, workbenchOutputDirectory, workflowExecutionSignature, workflowBindingsEqual } from '../src/workflows/workbench.ts'

const graph = {
  nodes: [
    { id: 1, type: 'pymss_load_audio', widgets_values: ['old.wav', 'shared'] },
    { id: 2, type: 'pymss_load_audio_batch', widgets_values: ['old-folder', true, true, 'shared'] },
    { id: 3, type: 'pymss_save_audio', widgets_values: ['wav', 'Default', '44100'] },
    { id: 4, type: 'SaveAudio', widgets_values: ['output'] },
  ], links: [],
}

test('native graphs retain their paths, batch options and save parameters without Studio binding overrides', () => {
  const bindings = createWorkflowBindings()
  bindings.inputs = { '1': { files: ['C:/music/one.wav', 'C:/music/two.wav'] }, '2': { files: ['C:/music/reference.wav'] } }
  bindings.primaryInput = '1'
  bindings.outputs = { '4': 'D:/other' }
  bindings.outputLayout = 'folders'
  const original = JSON.stringify(graph)
  const prepared = prepareWorkbenchRun(graph, bindings, 'D:/output')
  assert.equal(JSON.stringify(graph), original)
  assert.deepEqual(prepared.definition, graph)
  assert.deepEqual(prepared.runs, [{ input: '', inputs: {}, outputs: {} }])
  bindings.inputs['1'].files[0] = 'changed.wav'
  ;(prepared.definition.nodes as any[])[0].widgets_values[0] = 'next.wav'
  assert.equal(JSON.stringify(graph), original)
})

test('simple workflows require input bindings and retain per-file batching', () => {
  const definition = { version: 1, steps: [{ id: 'split', model: 'model', stems: ['Vocals'], save: { Vocals: 'Default' } }] }
  const bindings = createWorkflowBindings()
  assert.throws(() => prepareWorkbenchRun(definition, bindings, 'out'), /missingInput/)
  bindings.inputs.input = { files: ['first.wav', 'second.wav'] }
  const prepared = prepareWorkbenchRun(definition, bindings, 'out')
  assert.deepEqual(prepared.runs.map(run => run.input), ['first.wav', 'second.wav'])
  assert.deepEqual(prepared.definition, definition)
  assert.deepEqual(prepared.runs.map(run => run.outputs), [{ 'save:split:Vocals': 'out' }, { 'save:split:Vocals': 'out' }])
})

test('POSIX workbench directories preserve literal backslashes and spaces', () => {
  const definition = { version: 1, steps: [{ id: 'split', model: 'model', save: { Vocals: 'Default' } }] }
  const bindings = createWorkflowBindings()
  bindings.inputs.input = { files: ['/audio/mix\\take.wav', '/audio/ song .wav'] }
  bindings.outputDirectory = '/tmp/results\\'
  bindings.outputLayout = 'folders'
  const prepared = prepareWorkbenchRun(definition, bindings, '/tmp/default', false)
  assert.deepEqual(prepared.runs.map(run => run.outputs['save:split:Vocals']), [
    '/tmp/results\\/mix\\take', '/tmp/results\\/ song ',
  ])
  assert.equal(workbenchOutputDirectory({ id: 'save', label: '', subdirectory: 'child' }, '/tmp/results\\', '/tmp/default'), '/tmp/results\\/child')
})

test('workbench directories retain Windows drive and UNC roots', () => {
  const definition = { version: 1, steps: [{ id: 'split', model: 'model', save: { Vocals: 'Default' } }] }
  const bindings = createWorkflowBindings()
  bindings.inputs.input = { files: ['C:\\audio\\mix.wav'] }
  bindings.outputLayout = 'folders'
  for (const root of ['D:\\', '\\\\server\\share\\']) {
    bindings.outputDirectory = root
    const prepared = prepareWorkbenchRun(definition, bindings, 'D:\\default', true)
    assert.equal(prepared.runs[0].outputs['save:split:Vocals'], `${root}mix`)
  }
})

test('workbench input basenames cannot escape their output directory', () => {
  const definition = { version: 1, steps: [{ id: 'split', model: 'model', save: { Vocals: 'Default' } }] }
  const bindings = createWorkflowBindings()
  bindings.inputs.input = { files: ['/audio/...wav', '/audio/../song.wav'] }
  bindings.outputDirectory = '/tmp/results'
  bindings.outputLayout = 'folders'
  const prepared = prepareWorkbenchRun(definition, bindings, '/tmp/default', false)
  assert.deepEqual(prepared.runs.map(run => run.outputs['save:split:Vocals']), ['/tmp/results/workflow', '/tmp/results/song'])
})

test('native graphs validate the selected output root without injecting it into the graph', () => {
  const bindings = createWorkflowBindings()
  assert.throws(() => prepareWorkbenchRun(graph, bindings, ''), /missingOutputDirectory/)
  bindings.outputDirectory = 'E:/Selected'
  const prepared = prepareWorkbenchRun(graph, bindings, '')
  assert.deepEqual(prepared.definition, graph)
  assert.deepEqual(prepared.runs, [{ input: '', inputs: {}, outputs: {} }])
  bindings.outputDirectory = '   '
  assert.throws(() => prepareWorkbenchRun(graph, bindings, ''), /missingOutputDirectory/)
  assert.doesNotThrow(() => prepareWorkbenchRun(graph, bindings, 'D:/Default'))
})

test('simple output identities and generated operation nodes map to their editor identities', () => {
  const definition = { version: 1, steps: [{ id: 'split', stems: ['Vocals'], save: { Vocals: 'Default' } }], ensembles: [{ id: 'mix', save: 'Default', output_stem: 'Instrumental' }] }
  assert.deepEqual(workflowOutputNodes(definition).map(node => node.id), ['save:split:Vocals', 'studio:ensemble-save:mix'])
  assert.equal(visibleWorkflowNodeId('step:split', definition), 'split')
  assert.equal(visibleWorkflowNodeId('step:split', { ...definition, steps: [{ id: ' split ' }] }), ' split ')
  assert.equal(visibleWorkflowNodeId('studio:ensemble:mix:mix:2', definition), 'mix')
  assert.equal(visibleWorkflowNodeId('studio:ensemble-save:mix', definition), 'save')
  assert.equal(visibleWorkflowNodeId('unknown-helper', definition), '')
})

test('the simple save node uses one runtime directory for all stems and future output nodes', () => {
  const definition = { version: 1, steps: [{ id: 'split', stems: ['Vocals', 'Instrumental'], save: { Vocals: 'Default', Instrumental: 'Default' } }] }
  const bindings = createWorkflowBindings()
  bindings.inputs.input = { files: ['C:/music/one.wav'] }
  bindings.outputDirectory = 'D:/selected'
  bindings.outputs['save:split:Vocals'] = 'D:/previous'
  const prepared = prepareWorkbenchRun(definition, bindings, 'D:/default')
  assert.deepEqual(Object.values(prepared.runs[0].outputs), ['D:/selected', 'D:/selected'])
  assert.equal('outputDirectory' in prepared.definition, false)
})

test('simple layout changes preserve execution highlighting while processing changes invalidate it', () => {
  const definition = { version: 1, studio: { viewport: { zoom: 1 } }, steps: [], ensembles: [{ id: 'phase', algorithm: 'invert', output_stem: 'Result' }] }
  const moved = structuredClone(definition)
  moved.studio.viewport.zoom = .5
  assert.equal(workflowExecutionSignature(definition), workflowExecutionSignature(moved))
  moved.ensembles[0].algorithm = 'sum'
  assert.notEqual(workflowExecutionSignature(definition), workflowExecutionSignature(moved))
})

test('graph camera, appearance and layering do not change its processing signature', () => {
  const definition = structuredClone(graph)
  const moved = { ...structuredClone(graph), extra: { ds: { scale: .5, offset: [30, 40] } } }
  Object.assign(moved.nodes[0], { pos: [100, 200], size: [320, 400], flags: { collapsed: true }, order: 8 })
  moved.nodes.reverse()
  assert.equal(workflowExecutionSignature(definition), workflowExecutionSignature(moved))
  moved.nodes[0].widgets_values[0] = 'changed'
  assert.notEqual(workflowExecutionSignature(definition), workflowExecutionSignature(moved))
})

test('changing input files or output settings invalidates matching execution bindings', () => {
  const original = createWorkflowBindings()
  original.inputs = { input: { files: ['first.wav', 'second.wav'] } }
  original.outputDirectory = 'D:/selected'
  assert.equal(workflowBindingsEqual(original, structuredClone(original)), true)
  for (const change of [
    (value: typeof original) => { value.inputs.input.files.reverse() },
    (value: typeof original) => { value.outputDirectory = 'D:/next' },
    (value: typeof original) => { value.outputLayout = 'folders' },
  ]) {
    const changed = structuredClone(original)
    change(changed)
    assert.equal(workflowBindingsEqual(original, changed), false)
  }
})
