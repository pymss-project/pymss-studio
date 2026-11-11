import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import { randomUUID } from 'node:crypto'
import ts from 'typescript'
import { nextTick, ref, watch } from 'vue'
import { BATCH_DOWNLOAD_CONCURRENCY, enqueueDownloads, isActiveDownloadStatus, takeQueuedDownloads } from '../src/utils/downloadQueue.ts'

const source = ts.createSourceFile('model.ts', readFileSync(new URL('../src/stores/model.ts', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true)
const declaration = source.statements.find(statement => ts.isVariableStatement(statement)
  && statement.declarationList.declarations.some(item => item.name.getText(source) === 'useModelStore'))
const body = declaration.declarationList.declarations[0].initializer.arguments[1].body
const names = new Set(['downloadModel', 'deleteModel', 'deleteStorageModels', 'isDeleteTaskTerminal', 'isModelDeletionPending', 'canQueueDownload',
  'pumpDownloadQueue', 'enqueueBatchDownload', 'removeQueuedDownload', 'clearDownloadQueue'])
const functions = body.statements.filter(statement => ts.isFunctionDeclaration(statement) && names.has(statement.name?.text))
assert.equal(functions.length, names.size)
const pendingDeletions = body.statements.find(statement => ts.isVariableStatement(statement)
  && statement.declarationList.declarations.some(item => item.name.getText(source) === 'pendingModelDeletions'))
assert.ok(pendingDeletions)
const queueWatch = body.statements.find(statement => ts.isExpressionStatement(statement)
  && statement.getText(source).startsWith('watch(downloadTasks,') && statement.getText(source).includes('pumpDownloadQueue'))
assert.ok(queueWatch)
const code = ts.transpileModule([pendingDeletions, ...functions, queueWatch].map(statement => statement.getText(source)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText
const flush = async () => { for (let i = 0; i < 6; i++) { await nextTick(); await new Promise(resolve => setImmediate(resolve)) } }

function environment(t) {
  const calls = [], stops = [], waiting = new Map()
  const state = {
    Error, ref, crypto: { randomUUID }, BATCH_DOWNLOAD_CONCURRENCY, enqueueDownloads, isActiveDownloadStatus, takeQueuedDownloads,
    models: ref(['a', 'b', 'c', 'd'].map(name => ({ name, supported: true, downloaded: false, source: 'catalog' }))),
    downloadTasks: ref({}), downloadQueue: ref([]), deleteTasks: ref({}), deleteTaskIndex: ref({}),
    downloadStates: ref({}), downloadErrors: ref({}), downloadTaskIndex: ref({}),
    batchDeleteState: ref({ active: false, totalModels: 0, completedModels: 0, currentModel: '', failedModels: [] }),
    useAppStore: () => ({ runtimeInfo: { ready: true }, async checkRuntimeInfo() {} }),
    useSettingsStore: () => ({ modelDir: 'C:/Models', downloadSource: 'modelscope', downloadMethod: 'requests' }),
    watch(...args) { const stop = watch(...args); stops.push(stop); return stop },
    async invoke(command, args) { calls.push({ command, ...args }); return { started: true } },
    waitForDeleteTask: name => new Promise((resolve, reject) => waiting.set(name, { resolve, reject })),
    finalizeDeletedModel() {}, clearDeleteTask: name => { delete state.deleteTasks.value[name] },
    async deleteToolModel() {}, async loadModelStorageSummary() {},
    studioLog: { warn() {} }, appendLog: (logs, record) => [...logs, record],
  }
  const store = vm.runInNewContext(`${code}\n({${[...names].join(',')}})`, state)
  t.after(() => stops.forEach(stop => stop()))
  return { state, store, calls, finishDeletion: (name, error) => error
    ? waiting.get(name).reject(error) : waiting.get(name).resolve({ resultModelInfo: null }),
    started: () => calls.filter(call => call.command === 'start_model_download').map(call => call.payload.model) }
}

test('queued downloads use two slots and advance after completion', async t => {
  const h = environment(t)
  assert.equal(h.store.enqueueBatchDownload(['a', 'b', 'c']), 3)
  await flush()
  assert.deepEqual(h.started(), ['a', 'b'])
  assert.deepEqual(Array.from(h.state.downloadQueue.value), ['c'])
  h.state.downloadTasks.value.a.status = 'done'
  await flush()
  assert.deepEqual(h.started(), ['a', 'b', 'c'])
})

test('deleting a queued model removes it before a slot can start another download', async t => {
  const h = environment(t)
  h.store.enqueueBatchDownload(['a', 'b', 'c', 'd'])
  await flush()
  await h.store.deleteModel('c', 'batch')
  assert.equal(h.state.deleteTasks.value.c.status, 'deleting')
  assert.deepEqual(Array.from(h.state.downloadQueue.value), ['d'])
  assert.equal(h.store.enqueueBatchDownload(['c']), 0)
  h.state.downloadTasks.value.a.status = 'done'
  await flush()
  assert.deepEqual(h.started(), ['a', 'b', 'd'])
  h.state.deleteTasks.value.c.status = 'done'
  h.state.downloadTasks.value.b.status = 'done'
  await flush()
  assert.deepEqual(h.started(), ['a', 'b', 'd'])
})

test('the scheduler rechecks deletions when a queued model reaches a free slot', async t => {
  const h = environment(t)
  h.store.enqueueBatchDownload(['a', 'b', 'c', 'd'])
  await flush()
  h.state.deleteTasks.value.c = { status: 'deleting' }
  h.state.downloadTasks.value.a.status = 'done'
  await flush()
  assert.deepEqual(h.started(), ['a', 'b', 'd'])
})

test('batch deletion removes all targeted downloads before waiting for the first deletion', async t => {
  const h = environment(t)
  h.store.enqueueBatchDownload(['a', 'b', 'c', 'd'])
  await flush()
  const deleting = h.store.deleteStorageModels(['c', 'd'].map(id => ({ kind: 'catalog', id, name: id })))
  await flush()
  assert.equal(h.state.deleteTasks.value.c.status, 'deleting')
  assert.deepEqual(Array.from(h.state.downloadQueue.value), [])
  h.state.downloadTasks.value.a.status = 'done'
  await flush()
  assert.deepEqual(h.started(), ['a', 'b'])
  h.finishDeletion('c')
  await flush()
  assert.equal(h.state.deleteTasks.value.d.status, 'deleting')
  h.finishDeletion('d')
  await deleting
  assert.deepEqual(h.started(), ['a', 'b'])
})

test('terminal deletion states allow an explicit new download request', async t => {
  for (const status of ['done', 'error', 'cancelled']) {
    const h = environment(t)
    h.state.deleteTasks.value.a = { status }
    assert.equal(h.store.enqueueBatchDownload(['a']), 1, status)
    await flush()
    assert.deepEqual(h.started(), ['a'], status)
  }
})

test('later batch deletion targets stay blocked until their deletion has completed', async t => {
  const h = environment(t)
  const deleting = h.store.deleteStorageModels(['c', 'd'].map(id => ({ kind: 'catalog', id, name: id })))
  await flush()
  assert.equal(h.state.deleteTasks.value.c.status, 'deleting')
  assert.equal(h.state.deleteTasks.value.d, undefined)
  const added = h.store.enqueueBatchDownload(['d', 'a'])
  await flush()
  const started = h.started()
  h.finishDeletion('c')
  await flush()
  h.finishDeletion('d')
  await deleting
  assert.equal(added, 1)
  assert.deepEqual(started, ['a'])
  assert.equal(h.store.enqueueBatchDownload(['d']), 1)
  await flush()
  assert.deepEqual(h.started(), ['a', 'd'])
})

test('manual downloads cannot start a later batch deletion target', async t => {
  const h = environment(t)
  const deleting = h.store.deleteStorageModels(['c', 'd'].map(id => ({ kind: 'catalog', id, name: id })))
  await flush()
  const started = await h.store.downloadModel('d')
  h.finishDeletion('c')
  await flush()
  h.finishDeletion('d')
  await deleting
  assert.equal(started, false)
  assert.deepEqual(h.started(), [])
})

test('failed and cancelled deletions release their targets while later deletions remain protected', async t => {
  const h = environment(t)
  const deleting = h.store.deleteStorageModels(['c', 'd'].map(id => ({ kind: 'catalog', id, name: id })))
  await flush()
  h.finishDeletion('c', new Error('Delete failed'))
  await flush()
  assert.equal(h.store.canQueueDownload('c'), true)
  assert.equal(h.store.canQueueDownload('d'), false)
  h.finishDeletion('d', new Error('Delete cancelled'))
  await deleting
  assert.equal(h.store.canQueueDownload('d'), true)
  assert.deepEqual(Array.from(h.state.batchDeleteState.value.failedModels), ['c', 'd'])
})

test('a failed storage refresh does not leave completed deletion targets reserved', async t => {
  const h = environment(t)
  h.state.loadModelStorageSummary = async () => { throw new Error('Refresh failed') }
  const deleting = h.store.deleteStorageModels([{ kind: 'catalog', id: 'c', name: 'c' }])
  const rejected = assert.rejects(deleting, /Refresh failed/)
  await flush()
  h.finishDeletion('c')
  await rejected
  assert.equal(h.store.canQueueDownload('c'), true)
})
