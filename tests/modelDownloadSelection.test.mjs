import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'
import { computed, h, nextTick, reactive, ref, watch } from 'vue'
import { BATCH_DOWNLOAD_CONCURRENCY, isActiveDownloadStatus } from '../src/utils/downloadQueue.ts'

const source = ts.createSourceFile('models.ts', parse(readFileSync(new URL('../src/views/ModelsView.vue', import.meta.url), 'utf8')).descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const functions = new Set(['isDeletingModel', 'isDownloadSelectable', 'isQueuedDownload', 'canSelectBatchModel', 'canEditModelParameters',
  'toggleBatchSelectMode', 'isBatchSelected', 'setBatchSelected', 'selectBatchPage', 'selectBatchFiltered', 'clearBatchSelection',
  'handleModelCardClick', 'confirmBatchDownload', 'openBatchEditor', 'closeBatchEditor', 'buildBatchPreview', 'applyBatch',
  'mapWithConcurrency', 'toLibraryOverrides', 'errorText', 'isRevisionConflict'])
const variables = new Set(['batchSelectMode', 'batchSelection', 'batchSelectionSet', 'downloadQueueSet', 'downloadSelectedModels',
  'editableBatchSelection', 'batchSelectableOnPage', 'batchSelectedOnPage', 'batchOutsidePage', 'batchOutsideFilter',
  'allBatchPageSelected', 'someBatchPageSelected', 'batchSelectionOptions', 'batchEditorSelection', 'batchEditorSkipped',
  'batchSession', 'showBatchEditor', 'batchApplying', 'batchIntent', 'batchPreview', 'batchExcluded', 'batchStaleNames',
  'batchLoadState', 'batchReferenceName', 'batchReferenceOptions', 'batchMetadataLoading', 'batchIntentEmpty', 'BATCH_METADATA_CONCURRENCY'])
const statements = source.statements.filter(statement => ts.isFunctionDeclaration(statement)
  ? functions.has(statement.name?.text)
  : ts.isVariableStatement(statement) && statement.declarationList.declarations.some(item => variables.has(item.name.getText(source))))
assert.equal(statements.length, functions.size + variables.size)
const watchers = source.statements.filter(statement => ts.isExpressionStatement(statement) && statement.getText(source).startsWith('watch(')
  && (statement.getText(source).includes('const known = new Set(names)') || statement.getText(source).startsWith('watch(modelDir,')))
assert.equal(watchers.length, 2)
const code = ts.transpileModule([...statements, ...watchers].map(statement => statement.getText(source)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText
const modelSource = ts.createSourceFile('model.ts', readFileSync(new URL('../src/stores/model.ts', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true)
const modelDeclaration = modelSource.statements.find(statement => ts.isVariableStatement(statement)
  && statement.declarationList.declarations.some(item => item.name.getText(modelSource) === 'useModelStore'))
const modelBody = modelDeclaration.declarationList.declarations[0].initializer.arguments[1].body
const eligibilityFunctions = new Set(['isDeleteTaskTerminal', 'isModelDeletionPending', 'canQueueDownload'])
const eligibilityStatements = modelBody.statements.filter(statement => ts.isFunctionDeclaration(statement)
  ? eligibilityFunctions.has(statement.name?.text)
  : ts.isVariableStatement(statement) && statement.declarationList.declarations.some(item => ['pendingModelDeletions', 'downloadsClosing'].includes(item.name.getText(modelSource))))
assert.equal(eligibilityStatements.length, eligibilityFunctions.size + 2)
const eligibilityCode = ts.transpileModule(eligibilityStatements.map(statement => statement.getText(modelSource)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function environment(t) {
  const stops = [], opened = [], queued = [], saved = [], metadata = [], messages = []
  const names = ['new', 'error', 'paused', 'cancelled', 'interrupted', 'preparing', 'downloading', 'downloaded', 'custom', 'deleting', 'queued', 'unsupported']
  const modelStore = reactive({ models: names.map(name => ({ name, supported: name !== 'unsupported',
    downloaded: ['downloaded', 'custom', 'deleting'].includes(name), source: name === 'custom' ? 'user' : 'catalog', sizeBytes: 100,
    inferenceParamMeta: { resolution: { fingerprint: name + '-fp' } },
  })), modelInferenceRevision: 1 })
  let view, confirmation, finishedDrags = 0
  const state = { ref, computed, h, isActiveDownloadStatus, BATCH_DOWNLOAD_CONCURRENCY, modelStore,
    models: computed(() => modelStore.models),
    filter: ref(''), sortedModels: computed(() => modelStore.models.filter(model => !state.filter.value || model.name === state.filter.value)),
    pagedModels: ref(modelStore.models.slice(0, 3)), modelDir: ref('/models'),
    downloadTasks: ref(Object.fromEntries(['error', 'paused', 'cancelled', 'interrupted', 'preparing', 'downloading'].map(status => [status, { status }]))),
    deleteTasks: ref({ deleting: { status: 'deleting' } }), downloadQueue: ref(['queued']), showDetail: ref(true), suppressBatchClick: false,
    findModelEntry: name => modelStore.models.find(model => model.name === name), selectModel: model => opened.push(model.name),
    resolutionOf: model => model?.inferenceParamMeta?.resolution,
    finishBatchDragSelection() { finishedDrags++ }, window: { getSelection: () => null },
    settings: reactive({ modelDir: '/models' }), dialog: { info: options => { confirmation = options } },
    t: (key, args = {}) => key + ':' + JSON.stringify(args), formatBytes: bytes => String(bytes),
    message: Object.fromEntries(['success', 'info', 'warning', 'error'].map(level => [level, text => messages.push({ level, text })])),
    studioLog: { error() {} },
    loadBatchMetadata: names => { metadata.push([...names]); return Promise.resolve([]) }, pickBatchReference() {},
    buildBatchRow: name => ({ name, status: 'ok', profile: { batch_size: 2 }, fingerprint: name + '-fp' }),
    canApplyBatch: ref(true), batchChangedRows: computed(() => view.batchPreview.value?.rows || []),
    watch(...args) { const stop = watch(...args); stops.push(stop); return stop },
  }
  const eligibility = vm.runInNewContext(`${eligibilityCode}\n({canQueueDownload, pendingModelDeletions, downloadsClosing})`, state)
  modelStore.canQueueDownload = eligibility.canQueueDownload
  modelStore.enqueueBatchDownload = names => { queued.push([...names]); state.downloadQueue.value.push(...names); return names.length }
  modelStore.ensureModelMetadata = async name => modelStore.models.find(model => model.name === name)
  modelStore.replaceModelInferenceOverridesBatch = async rows => { saved.push(rows) }
  view = vm.runInNewContext(`${code}\n({${[...functions, ...variables].join(',')}})`, state)
  t.after(() => stops.forEach(stop => stop()))
  return { state, view, modelStore, models: modelStore.models, opened, queued, saved, metadata, messages,
    confirmation: () => confirmation, pendingDeletions: eligibility.pendingModelDeletions,
    downloadsClosing: eligibility.downloadsClosing, finishedDrags: () => finishedDrags }
}

const selected = h => Array.from(h.view.batchSelection.value)
const downloadable = h => Array.from(h.view.downloadSelectedModels.value, model => model.name)

test('one selection accepts downloadable and installed models and derives eligible action subsets', t => {
  const h = environment(t)
  h.view.toggleBatchSelectMode()
  h.view.selectBatchFiltered()
  assert.deepEqual(selected(h), ['new', 'error', 'paused', 'cancelled', 'interrupted', 'downloaded', 'custom'])
  assert.deepEqual(downloadable(h), ['new', 'error', 'paused', 'cancelled', 'interrupted'])
  assert.deepEqual(Array.from(h.view.editableBatchSelection.value), ['downloaded', 'custom'])
  assert.equal(h.state.showDetail.value, false)
  assert.equal(h.view.canSelectBatchModel(h.models.find(model => model.name === 'unsupported')), false)
})

test('page select and deselect preserve selections from other pages and filtered-out models', t => {
  const h = environment(t)
  h.view.setBatchSelected('custom', true)
  h.view.selectBatchPage(true)
  assert.deepEqual(selected(h), ['custom', 'new', 'error', 'paused'])
  assert.equal(h.view.allBatchPageSelected.value, true)
  assert.equal(h.view.batchOutsidePage.value, 1)
  h.view.selectBatchPage(false)
  assert.deepEqual(selected(h), ['custom'])
  h.view.setBatchSelected('new', true)
  h.state.filter.value = 'new'
  assert.equal(h.view.batchOutsideFilter.value, 1)
  assert.deepEqual(selected(h), ['custom', 'new'])
})

test('download state changes update action counts without dropping checked models', async t => {
  const h = environment(t)
  h.view.setBatchSelected('new', true)
  h.view.setBatchSelected('error', true)
  h.state.downloadTasks.value.error.status = 'downloading'
  h.state.downloadQueue.value.push('new')
  await nextTick()
  assert.deepEqual(selected(h), ['new', 'error'])
  assert.deepEqual(downloadable(h), [])
  h.modelStore.models.find(model => model.name === 'error').downloaded = true
  h.state.downloadTasks.value.error.status = 'done'
  await nextTick()
  assert.deepEqual(Array.from(h.view.editableBatchSelection.value), ['error'])
  h.view.setBatchSelected('new', false)
  assert.deepEqual(selected(h), ['error'])
})

test('model removal prunes only missing IDs and a directory change clears the shared selection', async t => {
  const h = environment(t)
  h.view.setBatchSelected('new', true)
  h.view.setBatchSelected('custom', true)
  h.modelStore.models = h.modelStore.models.filter(model => model.name !== 'new')
  await nextTick()
  assert.deepEqual(selected(h), ['custom'])
  h.state.modelDir.value = '/replacement'
  await nextTick()
  assert.deepEqual(selected(h), [])
})

test('card keyboard and click events use shared selection and only normal mode opens details', t => {
  const h = environment(t)
  h.view.toggleBatchSelectMode()
  h.view.handleModelCardClick(h.models[1], { type: 'keydown', key: 'Enter' })
  h.view.handleModelCardClick(h.models[7], { type: 'click' })
  h.view.handleModelCardClick(h.models[5], { type: 'click' })
  assert.deepEqual(selected(h), ['error', 'downloaded'])
  assert.deepEqual(h.opened, [])
  h.view.handleModelCardClick(h.models[1], { type: 'keydown', key: ' ' })
  assert.deepEqual(selected(h), ['downloaded'])
  h.view.toggleBatchSelectMode()
  assert.deepEqual(selected(h), [])
  h.view.handleModelCardClick(h.models[0], { type: 'click' })
  assert.deepEqual(h.opened, ['new'])
})

test('pending deletions and window closure disable download targets without silently removing selections', async t => {
  const h = environment(t)
  h.view.setBatchSelected('error', true)
  h.pendingDeletions.value.set('error', 1)
  await nextTick()
  assert.deepEqual(downloadable(h), [])
  assert.deepEqual(selected(h), ['error'])
  h.view.setBatchSelected('paused', true)
  h.downloadsClosing.value = true
  await nextTick()
  assert.deepEqual(downloadable(h), [])
  h.view.setBatchSelected('new', true)
  assert.deepEqual(selected(h), ['error', 'paused'])
  h.downloadsClosing.value = false
  h.pendingDeletions.value.delete('error')
  await nextTick()
  assert.deepEqual(downloadable(h), ['error', 'paused'])
})

test('download confirmation freezes targets, rechecks eligibility and preserves shared selection after queueing', t => {
  const h = environment(t)
  h.view.toggleBatchSelectMode()
  h.view.setBatchSelected('new', true)
  h.view.setBatchSelected('error', true)
  h.view.setBatchSelected('downloaded', true)
  h.view.confirmBatchDownload()
  const content = h.confirmation().content()
  assert.match(JSON.stringify(content.children), /batchSkippedList/)
  h.view.setBatchSelected('paused', true)
  h.modelStore.models.find(model => model.name === 'error').downloaded = true
  h.confirmation().onPositiveClick()
  assert.deepEqual(h.queued, [['new']])
  assert.equal(h.view.batchSelectMode.value, true)
  assert.deepEqual(selected(h), ['new', 'error', 'downloaded', 'paused'])
})

test('a pending download confirmation cannot enqueue models into a different directory', t => {
  const h = environment(t)
  h.view.setBatchSelected('new', true)
  h.view.confirmBatchDownload()
  h.state.settings.modelDir = '/elsewhere'
  h.confirmation().onPositiveClick()
  assert.deepEqual(h.queued, [])
  assert.match(h.messages.at(-1).text, /batchDirectoryChanged/)
})

test('parameter editor and reset preview keep the opening target scope as models finish downloading', t => {
  const h = environment(t)
  h.view.setBatchSelected('new', true)
  h.view.setBatchSelected('downloaded', true)
  h.view.openBatchEditor()
  assert.deepEqual(Array.from(h.view.batchEditorSelection.value), ['downloaded'])
  assert.deepEqual(Array.from(h.view.batchEditorSkipped.value), ['new'])
  assert.deepEqual(h.metadata, [['downloaded']])
  h.modelStore.models.find(model => model.name === 'new').downloaded = true
  assert.deepEqual(Array.from(h.view.editableBatchSelection.value), ['new', 'downloaded'])
  h.view.buildBatchPreview('reset')
  assert.deepEqual(Array.from(h.view.batchPreview.value.rows, row => row.name), ['downloaded'])
  assert.deepEqual(Array.from(h.view.batchReferenceOptions.value, option => option.value), ['downloaded'])
  h.view.closeBatchEditor()
  assert.deepEqual(selected(h), ['new', 'downloaded'])
})

test('successful parameter application closes the editor but keeps multi-selection available', async t => {
  const h = environment(t)
  h.view.toggleBatchSelectMode()
  h.view.setBatchSelected('downloaded', true)
  h.view.openBatchEditor()
  h.view.buildBatchPreview('reset')
  await h.view.applyBatch()
  assert.equal(h.saved.length, 1)
  assert.equal(h.view.showBatchEditor.value, false)
  assert.equal(h.view.batchSelectMode.value, true)
  assert.deepEqual(selected(h), ['downloaded'])
})
