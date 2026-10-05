import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

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
  const task = { id: 'workflow', status: 'separating', progress: 35, createdAt: 1 }
  const terminal = ['done', 'failed', 'cancelled']
  const handle = vm.runInNewContext(`${code}\nhandleWorkerEvent`, {
    tasks: { value: [task] },
    TERMINAL_STATUSES: terminal,
    isTerminalTaskStatus: status => terminal.includes(status),
    markTaskStarted: () => {}, touch: () => {}, queueProgressPersist: () => {},
  })
  return {
    task,
    progress: payload => handle({ type: 'task_progress', taskId: task.id, payload: { stage: 'separating', ...payload } }),
  }
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
