import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, ref } from 'vue'
import { parse } from 'vue/compiler-sfc'

const { descriptor } = parse(readFileSync(new URL('../src/views/SeparateView.vue', import.meta.url), 'utf8'))
const source = ts.createSourceFile('SeparateView.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const names = new Set(['formatProgressTime', 'progressDetail', 'logProgressPercent', 'logProgressIndeterminate', 'logProgressUpdateLabel'])
const statements = source.statements.filter(node => ts.isFunctionDeclaration(node)
  ? names.has(node.name?.text)
  : ts.isVariableStatement(node) && node.declarationList.declarations.some(item => names.has(item.name.getText(source))))
const code = ts.transpileModule(statements.map(node => node.getText(source)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function setup(overrides = {}) {
  const currentTask = ref({ status: 'separating', progress: 0, ...overrides })
  const taskClockNow = ref(10000)
  const helpers = vm.runInNewContext(`${code}\n({ progressDetail, logProgressPercent, logProgressIndeterminate, logProgressUpdateLabel })`, {
    computed, currentTask, taskClockNow,
    padNumber: value => String(value).padStart(2, '0'),
    t: (key, params) => `${key}${params ? ':' + JSON.stringify(params) : ''}`,
  })
  return { ...helpers, currentTask, taskClockNow }
}

test('log footer waits for real progress and updates without developer logs', () => {
  const state = setup()
  assert.equal(state.logProgressPercent.value, undefined)
  assert.equal(state.logProgressIndeterminate.value, true)
  assert.equal(state.logProgressUpdateLabel.value, 'tasks.progressWaitingFirst')
  Object.assign(state.currentTask.value, { lastProgressPercent: 38, lastProgressAt: 9000 })
  assert.equal(state.logProgressPercent.value, 38)
  assert.equal(state.logProgressIndeterminate.value, false)
  assert.equal(state.logProgressUpdateLabel.value, 'tasks.progressUpdatedAgo:{"seconds":1}')
  state.taskClockNow.value = 54000
  assert.equal(state.logProgressUpdateLabel.value, 'tasks.progressUpdatedAgo:{"seconds":45}')
})

test('only successful completion displays 100 percent; failure and cancellation freeze measured progress', () => {
  for (const status of ['failed', 'cancelled']) {
    const state = setup({ status, progress: 100, lastProgressPercent: 38, lastProgressAt: 9000 })
    assert.equal(state.logProgressPercent.value, 38)
    assert.equal(state.logProgressIndeterminate.value, false)
    assert.equal(state.logProgressUpdateLabel.value, '')
    assert.equal(setup({ status, progress: 100 }).logProgressPercent.value, undefined)
  }
  const state = setup({ status: 'writing_output', lastProgressPercent: 99 })
  assert.equal(state.logProgressPercent.value, 99)
  state.currentTask.value.status = 'done'
  assert.equal(state.logProgressPercent.value, 100)
})

test('audio seconds and VR patches retain different display units; unknown counters remain counts', () => {
  const { progressDetail } = setup()
  const task = { status: 'separating', progressCurrent: 12, progressTotal: 231 }
  assert.equal(progressDetail({ ...task, progressUnit: 'seconds' }),
    'tasks.progressAudioAmount:{"done":"00:00:12","total":"00:03:51"}')
  assert.equal(progressDetail({ ...task, progressUnit: 'patches' }),
    'tasks.progressPatchAmount:{"done":12,"total":231}')
  assert.equal(progressDetail(task), 'tasks.progressCountAmount:{"done":12,"total":231}')
  assert.equal(progressDetail({ ...task, progressCurrent: -1 }), '')
  assert.equal(progressDetail({ ...task, progressTotal: Infinity }), '')
})
