import assert from 'node:assert/strict'
import test from 'node:test'
import type { SeparationTask } from '../src/stores/task.ts'
import { buildWorkflowResultSnapshot } from '../src/utils/workflowResultDiagnostics.ts'

const workflow = { initialized: true, workflows: [{ id: 'flow', updatedAt: 1 }], selectedWorkflowId: 'flow',
  isSaving: false, recoveryBackupPath: '', lastMutationError: null, openedEditors: [] }
function task(id: string, fields: Partial<SeparationTask> = {}): SeparationTask {
  return { id, model: 'model', input: '/private/input.wav', output: '/outputs', status: 'done', message: '',
    createdAt: 1, updatedAt: 1, progress: 100, stageLabel: '', files: [], outputs: [], logs: [], ...fields }
}

test('result visibility comes from the result store and distinguishes hidden and empty tasks', () => {
  const tasks = [task('text', { files: ['/outputs/text.txt'] }), task('hidden', { resultHidden: true, files: ['/outputs/hidden.txt'] }), task('empty')]
  const snapshot = buildWorkflowResultSnapshot(workflow, tasks, new Set(['text']))
  assert.equal(snapshot.results.visibleCount, 1)
  assert.equal(snapshot.results.hiddenCount, 1)
  assert.deepEqual(snapshot.results.tasks.map(item => item.reason), ['visible', 'hidden', 'no_outputs'])
})

test('node values remain valid results without being interpreted as file paths', () => {
  const result = task('measurement', { nodeValues: [{ nodeId: 'node', nodeType: 'analysis', slot: 0, value: '/not/a/file/value' }] })
  const snapshot = buildWorkflowResultSnapshot(workflow, [result], new Set(['measurement']))
  assert.equal(snapshot.results.tasks[0].reason, 'visible')
  assert.deepEqual(snapshot.results.paths, [])
})

test('output inspection is bounded, deduplicated and limited to recent completed tasks', () => {
  const tasks = Array.from({ length: 13 }, (_, index) => task(String(index), { updatedAt: index, files: [`/output/${index}`] }))
  tasks.push(task('running', { status: 'separating', files: ['/not-complete'] }))
  const snapshot = buildWorkflowResultSnapshot(workflow, tasks, new Set())
  assert.equal(snapshot.results.tasks.length, 12)
  assert.equal(snapshot.results.omittedTasks, 1)
  assert.ok(!snapshot.results.paths.includes('/output/0'))
  assert.ok(!snapshot.results.paths.includes('/not-complete'))
  const many = buildWorkflowResultSnapshot(workflow, [task('many', {
    files: Array.from({ length: 70 }, (_, i) => `/output/${i}`), outputs: [{ stem: 'text', kind: 'file', path: '/output/0' }],
  })], new Set())
  assert.equal(many.results.paths.length, 64)
  assert.equal(many.results.omittedPaths, 6)
})

test('snapshots retain save failure and recovery evidence without graph or media contents', () => {
  const snapshot = buildWorkflowResultSnapshot({ ...workflow, recoveryBackupPath: '/backup/workflow.json',
    lastMutationError: { message: 'Revision changed', code: 'WORKFLOW_REVISION_CONFLICT', at: 'now' } }, [task('example')], new Set())
  assert.equal(snapshot.workflow.lastMutationError?.code, 'WORKFLOW_REVISION_CONFLICT')
  assert.equal(snapshot.workflow.recoveryBackupPath, '/backup/workflow.json')
  assert.ok(!JSON.stringify(snapshot).includes('/private/input.wav'))
})
