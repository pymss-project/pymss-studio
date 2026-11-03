import type { SeparationTask } from '../stores/task'

type WorkflowState = {
  initialized: boolean
  workflows: { id: string; updatedAt: number }[]
  selectedWorkflowId: string
  isSaving: boolean
  recoveryBackupPath: string
  lastMutationError: { message: string; code: string; at: string } | null
  openedEditors: { label: string; workflowId: string; kind: string }[]
}

export function buildWorkflowResultSnapshot(workflow: WorkflowState, tasks: SeparationTask[], visibleIds: ReadonlySet<string>) {
  const completed = tasks.filter(task => task.status === 'done')
  const recent = [...completed].sort((a, b) => b.updatedAt - a.updatedAt).slice(0, 12)
  const paths = [...new Set(recent.flatMap(task => [...task.outputs.map(output => output.path), ...task.files]).filter(Boolean))]
  return {
    workflow: {
      initialized: workflow.initialized, count: workflow.workflows.length,
      selectedWorkflowId: workflow.selectedWorkflowId, isSaving: workflow.isSaving,
      recoveryBackupPath: workflow.recoveryBackupPath,
      lastMutationError: workflow.lastMutationError ? { ...workflow.lastMutationError } : null,
      editors: workflow.openedEditors.map(editor => ({ ...editor })),
    },
    results: {
      completedCount: completed.length, visibleCount: completed.filter(task => visibleIds.has(task.id)).length,
      hiddenCount: completed.filter(task => task.resultHidden).length,
      omittedTasks: Math.max(0, completed.length - recent.length),
      tasks: recent.map(task => ({
        id: task.id, visible: visibleIds.has(task.id), resultHidden: task.resultHidden === true,
        outputCount: task.outputs.length, fileCount: task.files.length, nodeValueCount: task.nodeValues?.length || 0,
        reason: visibleIds.has(task.id) ? 'visible' : task.resultHidden ? 'hidden'
          : !task.outputs.length && !task.files.length && !task.nodeValues?.length ? 'no_outputs' : 'unknown',
      })),
      paths: paths.slice(0, 64), omittedPaths: Math.max(0, paths.length - 64),
    },
  }
}

export type WorkflowResultSnapshot = ReturnType<typeof buildWorkflowResultSnapshot>
