import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { formatBytes, formatSpeedMBps } from '../src/utils/format.ts'

const { descriptor } = parse(readFileSync(new URL('../src/components/DownloadDetailModal.vue', import.meta.url), 'utf8'))
const script = ts.createSourceFile('DownloadDetailModal.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const names = new Set([
  'logContainerRef', 'autoScroll', 'statusType', 'statusLabel', 'statusIcon', 'displayProgress',
  'filesText', 'progressMessage', 'speedText', 'bytesText', 'canCancel', 'canResume', 'canDelete', 'logs',
  'formatTime', 'scrollToBottom', 'resumeAutoScroll', 'onScroll', 'copyAllLogs',
  'handleCancel', 'handleResume', 'handleDelete', 'handleClose',
])
const statements = script.statements.filter(statement => (
  ts.isFunctionDeclaration(statement) ? names.has(statement.name?.text)
    : ts.isVariableStatement(statement) ? statement.declarationList.declarations.some(item => names.has(item.name.getText(script)))
      : ts.isExpressionStatement(statement) && statement.expression.expression?.getText(script) === 'watch'
))
const code = ts.transpileModule(statements.map(statement => statement.getText(script)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function createModal(t, task = {}) {
  const props = reactive({ show: false, modelName: 'model.ckpt', task: task === null ? null : {
    taskId: 'download-1', status: 'downloading', progress: 8, message: '',
    totalFiles: 2, completedFiles: 0, logs: [], ...task,
  } })
  const events = [], messages = [], copied = []
  const scope = effectScope()
  t.after(() => scope.stop())
  const modal = scope.run(() => vm.runInNewContext(`${code}\n({ ${[...names].join(', ')} })`, {
    props, computed, ref, watch, nextTick, formatBytes, formatSpeedMBps,
    CloudDownloadOutline: 'download', CheckmarkCircleOutline: 'done', AlertCircleOutline: 'error', PauseCircleOutline: 'paused',
    emit: (...event) => events.push(event),
    t: (key, values) => values ? `${values.completed} / ${values.total} files` : key,
    message: Object.fromEntries(['success', 'error', 'info'].map(level => [level, text => messages.push([level, text])])),
    navigator: { clipboard: { writeText: async text => { copied.push(text) } } },
  }))
  return { modal, props, events, messages, copied }
}

test('transfer metrics distinguish zero bytes, unknown totals and inactive speeds', t => {
  const { modal, props } = createModal(t, { downloadedBytes: 0, totalBytes: 1024, speedBytesPerSecond: 1024 * 1024 })
  assert.equal(modal.bytesText.value, '0 B / 1.0 KB')
  assert.equal(modal.speedText.value, '1.0 MB/s')
  assert.equal(modal.filesText.value, '0 / 2 files')
  props.task.status = 'paused'
  assert.equal(modal.speedText.value, '—')
  props.task.downloadedBytes = undefined
  props.task.totalBytes = undefined
  assert.equal(modal.bytesText.value, '—')
  props.task.totalFiles = 1
  assert.equal(modal.filesText.value, '0 / 1 files')
})

test('download status controls preserve cancel, resume and delete availability', t => {
  const { modal, props } = createModal(t)
  props.task.status = 'preparing'
  assert.equal(modal.statusLabel.value, 'tasks.statusPreparing')
  props.task.message = modal.statusLabel.value
  assert.equal(modal.progressMessage.value, '')
  props.task.message = 'Getting download links'
  assert.equal(modal.progressMessage.value, 'Getting download links')
  for (const status of ['preparing', 'downloading']) {
    props.task.status = status
    assert.equal(modal.canCancel.value, true)
    assert.equal(Boolean(modal.canResume.value), false)
  }
  for (const status of ['paused', 'cancelled', 'error', 'interrupted']) {
    props.task.status = status
    assert.equal(modal.canCancel.value, false)
    assert.equal(Boolean(modal.canResume.value), true)
    assert.equal(Boolean(modal.canDelete.value), true)
  }
  props.task.status = 'done'
  assert.equal(modal.statusType.value, 'success')
  assert.equal(modal.statusIcon.value, 'done')
  assert.equal(modal.displayProgress.value, 100)
  assert.equal(Boolean(modal.canResume.value), false)
  props.task.status = 'idle'
  assert.equal(modal.statusType.value, 'default')
})

test('progress remains finite and within bounds for persisted or partial records', t => {
  const { modal, props } = createModal(t)
  for (const [value, expected] of [[8.4, 8], [-10, 0], [120, 100], [Number.NaN, 0], [Number.POSITIVE_INFINITY, 0]]) {
    props.task.progress = value
    assert.equal(modal.displayProgress.value, expected)
  }
  props.task = null
  assert.equal(modal.displayProgress.value, 0)
  assert.equal(modal.bytesText.value, '—')
  assert.equal(modal.canCancel.value, false)
})

test('known worker stages are localized without replacing diagnostic messages or logs', t => {
  const originalLogs = [{ ts: 1, level: 'error', message: 'Connection reset by peer' }]
  const { modal, props } = createModal(t, { logs: originalLogs })
  for (const [detail, key] of [
    ['Started', 'models.downloadStarted'],
    ['resolving_files', 'models.downloadPreparing'],
    ['Downloading', 'models.downloadTransferring'],
    ['Downloading model files', 'models.downloadTransferring'],
    ['Verifying downloaded files', 'models.downloadVerifying'],
  ]) {
    props.task.message = detail
    assert.equal(modal.progressMessage.value, key)
  }
  props.task.message = 'Connection reset by peer'
  assert.equal(modal.progressMessage.value, 'Connection reset by peer')
  assert.equal(props.task.logs[0].message, originalLogs[0].message)
  props.task.status = 'done'
  props.task.message = 'Done'
  assert.equal(modal.progressMessage.value, '')
})

test('logs follow new entries at the cap but preserve manual reading and reset for a new task', async t => {
  const { modal, props } = createModal(t, { logs: [{ ts: 1, level: 'info', message: 'Starting' }] })
  modal.logContainerRef.value = { scrollHeight: 400, clientHeight: 100, scrollTop: 300 }
  props.task.logs = [{ ts: 2, level: 'info', message: 'Downloading' }]
  await nextTick(); await new Promise(setImmediate)
  assert.equal(modal.logContainerRef.value.scrollTop, 400)
  modal.onScroll({ target: { scrollHeight: 400, clientHeight: 100, scrollTop: 0 } })
  modal.logContainerRef.value.scrollTop = 0
  props.task.logs.push({ ts: 3, level: 'warn', message: 'Retrying' })
  await nextTick(); await new Promise(setImmediate)
  assert.equal(modal.logContainerRef.value.scrollTop, 0)
  modal.resumeAutoScroll()
  await nextTick(); await new Promise(setImmediate)
  assert.equal(modal.logContainerRef.value.scrollTop, 400)
  modal.autoScroll.value = false
  props.task.taskId = 'download-2'
  await nextTick(); await new Promise(setImmediate)
  assert.equal(modal.autoScroll.value, true)
})

test('copy and footer actions preserve original log content and emitted events', async t => {
  const logs = [{ ts: 0, level: 'warn', message: 'Retrying\nConnection reset' }]
  const { modal, events, copied, messages } = createModal(t, { logs })
  await modal.copyAllLogs()
  assert.equal(copied[0], `[${modal.formatTime(0)}] [warn] Retrying\nConnection reset`)
  assert.deepEqual(messages, [['success', 'models.downloadLogsCopied']])
  modal.handleCancel(); modal.handleResume(); modal.handleDelete(); modal.handleClose()
  assert.deepEqual(events, [['cancel'], ['resume'], ['delete'], ['update:show', false], ['close']])
})
