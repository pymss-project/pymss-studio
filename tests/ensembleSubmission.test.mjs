import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'

const descriptor = parse(readFileSync(new URL('../src/views/SeparateView.vue', import.meta.url), 'utf8')).descriptor
const source = ts.createSourceFile('separate.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const names = new Set(['captureEnsembleSubmission', 'buildEnsembleWorkflow', 'startEnsembleJob', 'start'])
const declarations = source.statements.filter(statement => ts.isFunctionDeclaration(statement) && names.has(statement.name?.text))
assert.ok(['buildEnsembleWorkflow', 'startEnsembleJob'].every(name => declarations.some(statement => statement.name.text === name)))
const code = ts.transpileModule(declarations.map(statement => statement.getText(source)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText
const plain = value => JSON.parse(JSON.stringify(value))

function environment() {
  let release, reject, requested, submitted, submitError, cleared
  const uses = []
  const state = {
    task: {
      prepareInferenceSnapshots(names) {
        requested = [...names]
        return new Promise((resolve, fail) => { release = resolve; reject = fail })
      },
      async startWorkflowInference(workflow, options) {
        if (submitError) throw submitError
        submitted = plain({ workflow, options })
        const tasks = (options.inputFiles || state.inputFiles.value).map(input => ({ input }))
        return { jobId: 'ensemble-job', succeeded: tasks.length, failed: 0, tasks }
      },
      clearInputFiles(paths) {
        cleared = paths
        state.inputFiles.value = paths ? state.inputFiles.value.filter(path => !paths.includes(path)) : []
      },
    },
    model: { recordModelUse: name => uses.push(name) },
    ensembleStem: { value: 'vocals' }, ensembleModels: { value: ['A', 'B'] },
    ensembleModelEntries: { value: [] }, ensembleModelStems: { value: { A: 'vocals', B: 'instrumental' } },
    ensembleType: { value: 'avg_wave' }, ensembleWeights: { value: { A: 2, B: 3 } },
    inputFiles: { value: ['D:/Audio/first.wav', 'D:/Audio/second.wav'] },
    settings: { downloadSource: 'modelscope', getRuntimeDeviceConfig: () => ({ device: 'cuda', deviceIds: [0, 1] }), clearInputAfterSubmit: true },
    app: { envInfo: {} }, effectiveFormat: { value: 'wav' }, normalizedOutputDir: { value: 'D:/Output' },
    effectiveOutputLayout: { value: 'folders' },
    outputNamingConfig: { value: { enabled: true, template: '%filename%_%stem%', stemOrder: ['vocals'] } },
    WORKFLOW_FORMAT_VERSION: 1, t: key => key,
    submittingJob: { value: false }, outputDirectoryError: { value: '' }, runMode: { value: 'model' },
    ensembleEnabled: { value: true }, ensembleReady: { value: true }, selectedWorkflow: { value: null },
    workflowValidationError: () => '', focusedSeparationJobId: { value: '' }, newestRunningJob: { value: null },
    composingNewJob: { value: true }, message: { warning() {}, success() {}, error(error) { throw new Error(error) } },
  }
  const view = vm.runInNewContext(`${code}\n({startEnsembleJob, start})`, state)
  return { state, uses, start: view.startEnsembleJob, submit: view.start, submitted: () => submitted, cleared: () => cleared,
    resolve() {
      release(Object.fromEntries(requested.map((name, index) => [name, {
        parameters: { batch_size: index + 1, chunk_size: 441000, overlap_size: 220500, standardize: false, normalize: true },
        useTta: index === 1,
      }])))
    },
    reject(error) { reject(error) }, failSubmission(error) { submitError = error },
  }
}

test('changing ensemble selection and configuration during preparation cannot alter the submitted job', async () => {
  const h = environment()
  const pending = h.start()
  // Change existing objects as well as replacing refs while metadata is still loading.
  h.state.ensembleModels.value.splice(0, 2, 'C', 'B')
  h.state.ensembleModelStems.value.A = 'drums'
  h.state.ensembleModelStems.value.B = 'bass'
  h.state.ensembleWeights.value.A = 9
  h.state.ensembleWeights.value.B = 8
  h.state.ensembleType.value = 'max_spec'
  h.state.ensembleStem.value = 'bass'
  h.state.settings.downloadSource = 'hf'
  h.state.settings.getRuntimeDeviceConfig = () => ({ device: 'cpu', deviceIds: [] })
  h.state.effectiveFormat.value = 'flac'
  h.state.normalizedOutputDir.value = 'E:/Changed'
  h.state.effectiveOutputLayout.value = 'flat'
  h.state.outputNamingConfig.value.template = '%index%'
  h.state.outputNamingConfig.value.stemOrder.push('bass')
  h.resolve()
  assert.equal((await pending).jobId, 'ensemble-job')
  const { workflow, options } = h.submitted()
  const nodes = workflow.definition.nodes
  const separate = nodes.filter(node => node.type === 'mss_separate')
  assert.deepEqual(separate.map(node => node.widgets_values[0]), ['A', 'B'])
  assert.deepEqual(Object.keys(options.resolvedInferences), ['A', 'B'])
  assert.deepEqual(separate.map(node => node.outputs[0].name), ['vocals (Audio)', 'instrumental (Audio)'])
  for (const node of separate) assert.deepEqual(node.widgets_values.slice(1), ['cuda', true, 'modelscope', '0,1', false])
  assert.deepEqual(nodes.filter(node => node.type === 'pymss_mss_params').map(node => node.widgets_values), [
    [1, 220500, 441000, false, true, false], [2, 220500, 441000, false, true, true],
  ])
  assert.deepEqual(nodes.find(node => node.type === 'pymss_audio_ensemble').widgets_values, [2, 'avg_wave', 2, 3])
  assert.equal(nodes.find(node => node.type === 'pymss_save_audio').widgets_values[0], 'wav')
  assert.equal(workflow.definition.extra.studioEnsemble.outputStem, 'vocals')
  assert.equal(options.outputDir, 'D:/Output')
  assert.equal(options.outputLayout, 'folders')
  assert.deepEqual(options.outputNaming, { enabled: true, template: '%filename%_%stem%', stemOrder: ['vocals'] })
  assert.deepEqual(h.uses, ['A', 'B'])
})

test('failed preparation never queues a workflow or records model usage', async () => {
  const h = environment()
  const pending = h.start()
  h.reject(new Error('Metadata unavailable'))
  await assert.rejects(pending, /Metadata unavailable/)
  assert.equal(h.submitted(), undefined)
  assert.deepEqual(h.uses, [])
})

test('model usage is recorded only after the workflow is accepted', async () => {
  const h = environment()
  h.failSubmission(new Error('Queue unavailable'))
  const pending = h.start()
  h.resolve()
  await assert.rejects(pending, /Queue unavailable/)
  assert.deepEqual(h.uses, [])
})

for (const clearInputAfterSubmit of [true, false]) {
  test(`submission freezes input files and preserves newly added files (clear=${clearInputAfterSubmit})`, async () => {
    const h = environment()
    h.state.settings.clearInputAfterSubmit = clearInputAfterSubmit
    const initial = [...h.state.inputFiles.value]
    const pending = h.submit()
    h.state.inputFiles.value.splice(0, 1, 'D:/Audio/new.wav')
    h.resolve()
    await pending
    assert.deepEqual(h.submitted().options.inputFiles, initial)
    assert.deepEqual(h.state.inputFiles.value, clearInputAfterSubmit
      ? ['D:/Audio/new.wav'] : ['D:/Audio/new.wav', 'D:/Audio/second.wav'])
    if (clearInputAfterSubmit) assert.deepEqual(plain(h.cleared()), initial)
    else assert.equal(h.cleared(), undefined)
    assert.equal(h.state.submittingJob.value, false)
  })
}
