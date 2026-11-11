import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { isActiveDownloadStatus } from '../src/utils/downloadQueue.ts'

const source = ts.createSourceFile('models.ts', parse(readFileSync(new URL('../src/views/ModelsView.vue', import.meta.url), 'utf8')).descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const functions = new Set(['isCustomModel', 'isDeletingModel', 'isDownloadSelectable', 'isDownloadSelected', 'isQueuedDownload',
  'setDownloadSelected', 'toggleDownloadSelectAll', 'selectDownloadPage', 'clearDownloadSelection', 'toggleDownloadSelectMode',
  'toggleBatchSelectMode', 'isBatchSelected', 'setBatchSelected', 'handleModelCardClick'])
const variables = new Set(['batchSelectMode', 'batchSelection', 'batchSelectionSet', 'downloadSelectMode', 'modelSelectionActive',
  'downloadSelection', 'downloadSelectionSet', 'downloadQueueSet', 'downloadSelectableModels', 'downloadSelectablePageModels',
  'downloadSelectedModels', 'downloadSelectedBytes', 'allDownloadsSelected', 'someDownloadsSelected'])
const statements = source.statements.filter(statement => ts.isFunctionDeclaration(statement)
  ? functions.has(statement.name?.text)
  : ts.isVariableStatement(statement) && statement.declarationList.declarations.some(item => variables.has(item.name.getText(source))))
assert.equal(statements.length, functions.size + variables.size)
const prune = source.statements.find(statement => ts.isExpressionStatement(statement) && statement.getText(source).startsWith('watch(')
  && statement.getText(source).includes('if (!downloadSelection.value.length)'))
assert.ok(prune)
const code = ts.transpileModule([...statements, prune].map(statement => statement.getText(source)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText
const modelSource = ts.createSourceFile('model.ts', readFileSync(new URL('../src/stores/model.ts', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true)
const modelDeclaration = modelSource.statements.find(statement => ts.isVariableStatement(statement)
  && statement.declarationList.declarations.some(item => item.name.getText(modelSource) === 'useModelStore'))
const modelBody = modelDeclaration.declarationList.declarations[0].initializer.arguments[1].body
const eligibilityFunctions = new Set(['isDeleteTaskTerminal', 'isModelDeletionPending', 'canQueueDownload'])
const eligibilityStatements = modelBody.statements.filter(statement => ts.isFunctionDeclaration(statement)
  ? eligibilityFunctions.has(statement.name?.text)
  : ts.isVariableStatement(statement) && statement.declarationList.declarations.some(item => item.name.getText(modelSource) === 'pendingModelDeletions'))
assert.equal(eligibilityStatements.length, eligibilityFunctions.size + 1)
const eligibilityCode = ts.transpileModule(eligibilityStatements.map(statement => statement.getText(modelSource)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function environment(t) {
  const stops = [], opened = []
  const models = ['new', 'error', 'paused', 'cancelled', 'interrupted', 'preparing', 'downloading', 'downloaded', 'custom', 'deleting', 'queued']
    .map(name => ({ name, supported: true, downloaded: name === 'downloaded', source: name === 'custom' ? 'user' : 'catalog', sizeBytes: 100 }))
  const modelStore = reactive({ models })
  let finishedDrags = 0
  const state = { ref, computed, isActiveDownloadStatus, modelStore,
    models: computed(() => modelStore.models),
    sortedModels: computed(() => modelStore.models), pagedModels: ref(models.slice(0, 3)),
    downloadTasks: ref(Object.fromEntries(['error', 'paused', 'cancelled', 'interrupted', 'preparing', 'downloading'].map(status => [status, { status }]))),
    deleteTasks: ref({ deleting: { status: 'deleting' } }), downloadQueue: ref(['queued']), showDetail: ref(true), suppressBatchClick: false,
    canEditModelParameters: model => Boolean(model?.downloaded), selectModel: model => opened.push(model.name),
    finishBatchDragSelection() { finishedDrags++ }, window: { getSelection: () => null },
    watch(...args) { const stop = watch(...args); stops.push(stop); return stop },
  }
  const eligibility = vm.runInNewContext(`${eligibilityCode}\n({canQueueDownload, pendingModelDeletions})`, state)
  modelStore.canQueueDownload = eligibility.canQueueDownload
  const view = vm.runInNewContext(`${code}\n({${[...functions, ...variables].join(',')}})`, state)
  t.after(() => stops.forEach(stop => stop()))
  return { state, view, models, opened, pendingDeletions: eligibility.pendingModelDeletions, finishedDrags: () => finishedDrags }
}

test('failed and interrupted downloads can be selected again while active or ineligible models stay excluded', t => {
  const h = environment(t)
  for (const model of h.models) {
    assert.equal(h.view.isDownloadSelectable(model), ['new', 'error', 'paused', 'cancelled', 'interrupted'].includes(model.name), model.name)
  }
  assert.equal(h.view.isDownloadSelectable({ name: 'unsupported', supported: false }), false)
  h.view.toggleDownloadSelectAll(true)
  assert.deepEqual(Array.from(h.view.downloadSelection.value), ['new', 'error', 'paused', 'cancelled', 'interrupted'])
  assert.equal(h.view.downloadSelectedBytes.value, 500)
})

test('page selection keeps choices from other pages and changing status prunes only ineligible entries', async t => {
  const h = environment(t)
  h.view.setDownloadSelected(h.models[4], true)
  h.view.selectDownloadPage()
  assert.deepEqual(Array.from(h.view.downloadSelection.value), ['interrupted', 'new', 'error', 'paused'])
  h.state.downloadTasks.value.error.status = 'downloading'
  h.state.downloadQueue.value.push('new')
  h.state.deleteTasks.value.paused = { status: 'deleting' }
  await nextTick()
  assert.deepEqual(Array.from(h.view.downloadSelection.value), ['interrupted'])
})

test('download selection and parameter selection are mutually exclusive and stop an old drag', t => {
  const h = environment(t)
  h.view.batchSelectMode.value = true
  h.view.batchSelection.value = ['downloaded']
  h.view.toggleDownloadSelectMode()
  assert.equal(h.view.batchSelectMode.value, false)
  assert.deepEqual(Array.from(h.view.batchSelection.value), [])
  assert.equal(h.view.downloadSelectMode.value, true)
  assert.equal(h.state.showDetail.value, false)
  h.view.setDownloadSelected(h.models[1], true)
  h.view.toggleBatchSelectMode()
  assert.equal(h.view.downloadSelectMode.value, false)
  assert.deepEqual(Array.from(h.view.downloadSelection.value), [])
  assert.equal(h.view.batchSelectMode.value, true)
  assert.equal(h.finishedDrags(), 2)
})

test('card keyboard and click events select within the active mode without opening details', t => {
  const h = environment(t)
  h.view.toggleDownloadSelectMode()
  h.view.handleModelCardClick(h.models[1], { type: 'keydown', key: 'Enter' })
  assert.deepEqual(Array.from(h.view.downloadSelection.value), ['error'])
  h.view.handleModelCardClick(h.models[1], { type: 'keydown', key: ' ' })
  assert.deepEqual(Array.from(h.view.downloadSelection.value), [])
  h.view.handleModelCardClick(h.models[5], { type: 'click' })
  assert.deepEqual(h.opened, [])
  h.view.toggleBatchSelectMode()
  h.view.handleModelCardClick(h.models[7], { type: 'keydown', key: 'Enter' })
  assert.deepEqual(Array.from(h.view.batchSelection.value), ['downloaded'])
  h.view.toggleBatchSelectMode()
  h.view.handleModelCardClick(h.models[0], { type: 'click' })
  assert.deepEqual(h.opened, ['new'])
})

test('pending batch deletion targets become unavailable and clear only their own selections', async t => {
  const h = environment(t)
  h.view.toggleDownloadSelectMode()
  h.view.setDownloadSelected(h.models[1], true)
  h.view.setDownloadSelected(h.models[2], true)
  h.pendingDeletions.value.set('error', 1)
  await nextTick()
  assert.equal(h.view.isDownloadSelectable(h.models[1]), false)
  assert.equal(h.view.isDownloadSelectable(h.models[2]), true)
  assert.deepEqual(Array.from(h.view.downloadSelection.value), ['paused'])
  h.view.setDownloadSelected(h.models[1], true)
  assert.deepEqual(Array.from(h.view.downloadSelection.value), ['paused'])
  h.pendingDeletions.value.delete('error')
  await nextTick()
  assert.equal(h.view.isDownloadSelectable(h.models[1]), true)
  h.view.setDownloadSelected(h.models[1], true)
  assert.deepEqual(Array.from(h.view.downloadSelection.value), ['paused', 'error'])
})
