import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'

const path = new URL('../src/views/SeparateView.vue', import.meta.url)
const { descriptor } = parse(readFileSync(path, 'utf8'))
const script = ts.createSourceFile('SeparateView.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const names = new Set(['buildEnsembleWorkflow', 'normalizeStemOrder', 'orderedOutputStems', 'outputNamingConfig'])
const selected = script.statements.filter(statement => (
  ts.isFunctionDeclaration(statement) ? names.has(statement.name?.text)
    : ts.isVariableStatement(statement) && statement.declarationList.declarations.some(item => names.has(item.name.getText(script)))
))
assert.equal(selected.length, names.size)
const code = ts.transpileModule(selected.map(statement => statement.getText(script)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function setup() {
  const context = {
    computed: read => ({ get value() { return read() } }),
    runMode: { value: 'model' }, ensembleEnabled: { value: true },
    ensembleStem: { value: '  人声  ' }, ensembleModels: { value: ['model-a', 'model-b'] },
    ensembleModelStems: { value: { 'model-a': 'Vocals', 'model-b': 'vocals' } },
    ensembleModelEntries: { value: [] }, ensembleWeights: { value: {} }, ensembleType: { value: 'avg_wave' },
    effectiveFormat: { value: 'flac' },
    customStemOrder: { value: ['Instrumental', 'Vocals'] }, checkedOutputStems: { value: ['Vocals', 'Instrumental'] },
    outputNamingTemplate: { value: '%index%*%filename%*%stem%' },
    settings: { getRuntimeDeviceConfig: () => ({ device: 'cpu', deviceIds: [] }), downloadSource: 'modelscope' },
    app: { envInfo: {} }, WORKFLOW_FORMAT_VERSION: 1, t: key => key,
  }
  const result = vm.runInNewContext(`${code}\n({ buildEnsembleWorkflow, orderedOutputStems, outputNamingConfig })`, context)
  return { context, ...result }
}

test('ensemble graph carries the configured logical output stem without changing model stem inputs', () => {
  const { buildEnsembleWorkflow, outputNamingConfig } = setup()
  const definition = buildEnsembleWorkflow().definition
  assert.equal(definition.extra.studioEnsemble.outputStem, '人声')
  assert.equal(definition.extra.appDefaults.output_format, 'flac')
  const separate = definition.nodes.filter(node => node.type === 'mss_separate')
  assert.deepEqual(Array.from(separate, node => node.outputs[0].name), ['Vocals (Audio)', 'vocals (Audio)'])
  assert.equal(definition.nodes.filter(node => node.type === 'pymss_save_audio').length, 1)
  assert.equal(outputNamingConfig.value.template, '%index%*%filename%*%stem%')
  assert.deepEqual(Array.from(outputNamingConfig.value.stemOrder), ['人声'])
})

test('ensemble naming preview has one output and single-model stem ordering stays unchanged', () => {
  const { context, orderedOutputStems } = setup()
  assert.deepEqual(Array.from(orderedOutputStems.value), ['人声'])
  context.ensembleStem.value = '伴奏'
  assert.deepEqual(Array.from(orderedOutputStems.value), ['伴奏'])
  context.ensembleEnabled.value = false
  assert.deepEqual(Array.from(orderedOutputStems.value), ['Instrumental', 'Vocals'])
  context.ensembleEnabled.value = true
  context.runMode.value = 'workflow'
  assert.deepEqual(Array.from(orderedOutputStems.value), ['Instrumental', 'Vocals'])
})
