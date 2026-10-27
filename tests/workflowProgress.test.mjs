import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { appendWorkflowNodeLogs } from '../src/workflows/nodeLogs.ts'
import { normalizeLogRecord, workerLogRecord, formatLogRecord, localLogRecord } from '../src/utils/logRecord.ts'

const source = ts.createSourceFile('task.ts', readFileSync(new URL('../src/stores/task.ts', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true)
const names = new Set(['normalizeStatus', 'clamp', 'resolveStageProgress', 'handleWorkerEvent'])
const selected = []
function collect(node) {
  if (ts.isFunctionDeclaration(node) && names.has(node.name?.text)) selected.push(node.getText(source))
  if (ts.isVariableStatement(node) && node.declarationList.declarations.some(item => item.name.getText(source) === 'STAGE_META')) selected.push(node.getText(source))
  ts.forEachChild(node, collect)
}
collect(source)
assert.equal(selected.length, names.size + 1)
const code = ts.transpileModule(selected.join('\n'), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

function setup() {
  let now = 1000
  class Clock extends Date {
    static now() { return now }
  }
  const task = { id: 'workflow', status: 'separating', progress: 35, createdAt: 1, logs: [], runConfig: {} }
  const terminal = ['done', 'failed', 'cancelled']
  const handle = vm.runInNewContext(`${code}\nhandleWorkerEvent`, {
    Date: Clock,
    tasks: { value: [task] },
    TERMINAL_STATUSES: terminal,
    isTerminalTaskStatus: status => terminal.includes(status),
    markTaskStarted: () => {}, touch: () => {}, queueProgressPersist: () => {},
    appendWorkflowNodeLogs, normalizeLogRecord, workerLogRecord, localLogRecord, useSettingsStore: () => ({ developerMode: false }),
    appendTaskLogs: (target, lines) => target.logs.push(...(Array.isArray(lines) ? lines : [lines])),
    normalizeStemOutputs: () => [], resolveTaskErrorMessage: (_code, message) => message,
    markTaskFinished: () => {}, queuePersist: () => {},
    scheduleQueue: () => {},
  })
  return {
    task,
    advance: milliseconds => { now += milliseconds },
    progress: payload => handle({ type: 'task_progress', taskId: task.id, payload: { stage: 'separating', ...payload } }),
    execution: payload => handle({ type: 'workflow_execution', taskId: task.id, payload }),
    event: (type, payload) => handle({ type, taskId: task.id, payload }),
  }
}

test('normal mode preserves progress units and timestamps only changed valid progress', () => {
  const { task, progress, advance, event } = setup()
  progress({ done: 12, total: 231, unit: 'seconds' })
  assert.equal(task.progressUnit, 'seconds')
  assert.equal(task.lastProgressPercent, 5)
  assert.equal(task.lastProgressAt, 1000)
  advance(10000)
  progress({ done: 12, total: 231, unit: 'seconds' })
  event('task_log', { level: 'info', message: 'Model is running' })
  assert.equal(task.lastProgressAt, 1000)
  progress({ done: 38, total: 231, unit: 'seconds' })
  assert.equal(task.lastProgressAt, 11000)
  assert.equal(task.lastProgressPercent, 16)
  advance(1000)
  progress({ done: NaN, total: 0 })
  assert.equal(task.lastProgressAt, 11000)
  assert.equal(task.lastProgressPercent, 16)
})

test('counterless workflow repeats do not refresh time while sub-percent node changes do', () => {
  const { task, progress, advance } = setup()
  const payload = { progress: 44, nodeId: 'split', nodeProgress: .4, message: 'Running workflow' }
  progress(payload)
  assert.equal(task.lastProgressAt, 1000)
  advance(45000)
  progress(payload)
  assert.equal(task.lastProgressAt, 1000)
  assert.equal(task.lastProgressPercent, 44)
  advance(1000)
  progress({ ...payload, nodeProgress: .41 })
  assert.equal(task.lastProgressAt, 47000)
  assert.equal(task.lastProgressPercent, 44)
  advance(1000)
  progress({ ...payload, nodeProgress: .41 })
  assert.equal(task.lastProgressAt, 47000)
})

for (const terminal of ['error', 'task_cancelled']) {
  test(`${terminal} preserves the last measured percentage and rejects late progress`, () => {
    const { task, progress, event } = setup()
    progress({ done: 4, total: 10, unit: 'patches' })
    event(terminal, { message: 'Stopped' })
    assert.equal(task.lastProgressPercent, 40)
    assert.equal(task.lastProgressAt, 1000)
    progress({ done: 9, total: 10, unit: 'patches' })
    assert.equal(task.lastProgressPercent, 40)
    assert.equal(task.lastProgressAt, 1000)
  })
}

for (const protocol of ['progress', 'execution']) {
  test(`${protocol} events retain first-seen node order across updates and failure`, () => {
    const { task, progress, execution, event } = setup()
    task.runConfig.runMode = 'workflow'
    const send = (nodeId, fraction) => protocol === 'progress'
      ? progress({ nodeId, nodeProgress: fraction, message: 'Processing audio' })
      : execution({ version: 1, event: fraction === 0 ? 'node_started' : fraction === 1 ? 'node_completed' : 'node_progress',
        nodeId, nodeProgress: fraction })
    for (const id of ['10', '2', '3']) {
      send(id, 0)
      send(id, 0.5)
      send(id, 1)
    }
    send('4', 0)
    event('error', { message: 'Save failed' })
    send('20', 0)
    assert.deepEqual(Array.from(task.nodeExecutionOrder || []), ['10', '2', '3', '4'])
  })
}

test('workflow percentage uses overall progress while audio time stays local to the node', () => {
  const { task, progress } = setup()
  progress({ done: 192, total: 192, progress: 44, message: 'node=41 Processing audio' })
  assert.equal(task.progress, 44)
  progress({ progress: 44, message: 'node=42 type=pymss_mss_separate' })
  assert.equal(task.progress, 44)
  assert.equal(task.progressCurrent, undefined)
  assert.equal(task.progressTotal, undefined)
  progress({ done: 0, total: 192, progress: 44, message: 'node=42 Processing audio' })
  assert.equal(task.progress, 44)
  assert.equal(task.progressCurrent, 0)
  assert.equal(task.progressTotal, 192)
})

test('single-model events retain audio percentage and reject invalid explicit percentages', () => {
  const { task, progress } = setup()
  for (const explicit of [undefined, null, '60', NaN, Infinity]) {
    progress({ done: 25, total: 100, progress: explicit })
    assert.equal(task.progress, 25)
  }
  progress({ done: 0, total: 100, progress: 120 })
  assert.equal(task.progress, 99)
  progress({ done: 0, total: 100, progress: -2 })
  assert.equal(task.progress, 0)
})

test('late workflow progress cannot resurrect a cancelled task', () => {
  const { task, progress } = setup()
  task.status = 'cancelled'
  task.progress = 100
  progress({ done: 0, total: 192, progress: 44 })
  assert.equal(task.status, 'cancelled')
  assert.equal(task.progress, 100)
})

test('only explicit node ids route logs, even while another node is active', () => {
  const { task, progress, event } = setup()
  progress({ nodeId: 'first', nodeProgress: 0, message: 'Loading audio' })
  progress({ nodeId: 'first', nodeProgress: .2, message: 'Loading audio' })
  assert.equal(task.nodeLogs.first.length, 1)
  event('task_log', { message: 'Unattributed stderr', level: 'warning' })
  assert.equal(task.nodeLogs.first.length, 1)
  assert.equal(task.logs[0].message, 'Unattributed stderr')
  assert.equal(task.logs[0].level, 'warn')
  event('task_log', { nodeId: 'second', message: 'Model log', level: 'info' })
  assert.equal(task.nodeLogs.second[0].message, 'Model log')
  assert.equal(task.nodeLogs.second[0].level, 'info')
})

test('failed nodes retain their progress, diagnostics and tracebacks', () => {
  const { task, progress, event } = setup()
  progress({ nodeId: 'first', nodeProgress: .4, message: 'Processing audio' })
  event('error', { message: 'Model failed', detail: 'Traceback:\n  File "model.py", line 1\nValueError: invalid' })
  assert.equal(task.nodeStates.first.status, 'failed')
  assert.equal(task.nodeStates.first.progress, 40)
  assert.ok(task.nodeLogs.first.some(line => formatLogRecord(normalizeLogRecord(line)).includes('ValueError: invalid')))
})

test('node cancellation is recorded and late progress cannot overwrite it', () => {
  const { task, progress, event } = setup()
  progress({ nodeId: 'first', nodeProgress: .4, message: 'Processing audio' })
  event('task_cancelled', { message: 'Cancelled by user' })
  progress({ nodeId: 'first', nodeProgress: 1, message: 'Completed' })
  assert.equal(task.nodeStates.first.status, 'cancelled')
  assert.equal(task.nodeLogs.first.at(-1).message, 'Cancelled by user')
})

test('finishing audio does not complete a node before its output handling returns', () => {
  const { task, progress, event } = setup()
  progress({ nodeId: 'first', nodeProgress: 1, audioIndex: 1, audioCount: 1, unit: 'seconds', message: 'Audio processed' })
  assert.equal(task.nodeStates.first.status, 'running')
  assert.equal(task.currentNodeId, 'first')
  event('error', { message: 'Missing output stem' })
  assert.equal(task.nodeStates.first.status, 'failed')
  assert.ok(task.nodeLogs.first.some(line => formatLogRecord(normalizeLogRecord(line)).includes('Missing output stem')))
})

test('workflow execution uses explicit node lifecycle without making task completion terminal', () => {
  const { task, execution } = setup()
  task.runConfig = { runMode: 'workflow', workbench: { nativeGraph: true } }
  execution({ version: 1, event: 'node_started', nodeId: 'save', nodeProgress: 0, overallFraction: 0, message: 'Writing output' })
  assert.equal(task.nodeStates.save.status, 'running')
  assert.equal(task.currentNodeId, 'save')
  assert.equal(task.progress, 35)
  execution({ version: 1, event: 'node_completed', nodeId: 'save', nodeProgress: 1, overallFraction: 1, message: 'Saved' })
  assert.equal(task.nodeStates.save.status, 'done')
  assert.equal(task.nodeStates.save.progress, 100)
  assert.equal(task.currentNodeId, undefined)
  assert.equal(task.status, 'separating')
  assert.equal(task.progress, 90)
  assert.ok(task.nodeLogs.save.some(line => formatLogRecord(normalizeLogRecord(line)).includes('Node completed: Saved')))
})

test('workflow node failure remains failed through execution failure until the error detail arrives', () => {
  const { task, execution, event } = setup()
  task.runConfig = { runMode: 'workflow', workbench: { nativeGraph: true } }
  execution({ version: 1, event: 'node_failed', nodeId: 'split', overallFraction: .3, message: 'Model execution failed' })
  assert.equal(task.nodeStates.split.status, 'failed')
  assert.equal(task.currentNodeId, 'split')
  execution({ version: 1, event: 'execution_failed', nodeId: 'split', overallFraction: .3, message: 'Workflow failed' })
  assert.equal(task.nodeStates.split.status, 'failed')
  assert.equal(task.currentNodeId, 'split')
  event('error', { message: 'Model execution failed', detail: 'Traceback: detail' })
  assert.equal(task.nodeStates.split.status, 'failed')
  assert.ok(task.nodeLogs.split.some(line => formatLogRecord(normalizeLogRecord(line)).includes('Traceback: detail')))
})

test('workflow execution ignores non-second counters and late events after cancellation', () => {
  const { task, execution, event } = setup()
  task.runConfig = { runMode: 'workflow', workbench: { nativeGraph: true } }
  execution({ version: 1, event: 'node_progress', nodeId: 'split', nodeProgress: .4, overallFraction: .4, done: 4, total: 10, unit: 'chunks' })
  assert.equal(task.progressCurrent, undefined)
  assert.equal(task.progressTotal, undefined)
  execution({ version: 1, event: 'node_progress', nodeId: 'split', nodeProgress: .5, overallFraction: .5, done: 2, total: 5, unit: 'seconds' })
  assert.equal(task.progressCurrent, 2)
  assert.equal(task.progressTotal, 5)
  event('task_cancelled', { message: 'Cancelled' })
  execution({ version: 1, event: 'node_completed', nodeId: 'split', overallFraction: 1, message: 'Late success' })
  assert.equal(task.status, 'cancelled')
  assert.equal(task.nodeStates.split.status, 'cancelled')
})

test('execution cancellation and unknown payloads do not overwrite an explicit cancelled node', () => {
  const { task, execution } = setup()
  task.runConfig = { runMode: 'workflow' }
  execution({ version: 1, event: 'node_cancelled', nodeId: 'split', overallFraction: .4, message: 'Node cancelled' })
  assert.equal(task.nodeStates.split.status, 'cancelled')
  execution({ version: 1, event: 'execution_cancelled', nodeId: 'split', overallFraction: .4, message: 'Workflow cancelled' })
  assert.equal(task.nodeStates.split.status, 'cancelled')
  const before = task.progress
  execution({ version: 99, event: 'node_progress', nodeId: 'split', overallFraction: .8 })
  execution({ version: 1, event: 'future_event', nodeId: 'split', overallFraction: .8 })
  assert.equal(task.nodeStates.split.status, 'cancelled')
  assert.equal(task.progress, before)
})
