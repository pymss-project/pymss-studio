import { watch } from 'vue'
import { emitTo, listen } from '@tauri-apps/api/event'
import { getCurrentWindow } from '@tauri-apps/api/window'
import { useTaskStore } from '@/stores/task'
import { useWorkbenchStore, type WorkflowWorkbenchSession } from '@/stores/workbench'
import { useWorkflowStore } from '@/stores/workflow'
import { cloneWorkflow } from '@/workflows/workbench'
import { createWorkbenchBridge, type WorkbenchAction } from '@/workflows/workbenchBridge'
import { isMainTaskOwner } from '@/utils/taskOwner'
import { hasBackendBridge as isTauriRuntime } from '@/utils/appStore'

let bridge: ReturnType<typeof createWorkbenchBridge> | undefined
export function initializeWorkbenchBridge() {
  if (!isTauriRuntime()) return Promise.resolve(null)
  if (bridge) return bridge
  const workbench = useWorkbenchStore()
  const tasks = useTaskStore()
  const workflows = useWorkflowStore()
  const label = getCurrentWindow().label
  bridge = createWorkbenchBridge({
    label,
    transport: { listen: (name, handler) => listen(name, event => handler(event.payload)), emitTo },
    ...(isMainTaskOwner() ? { host: {
      snapshot: (key: string, includeSession: boolean) => cloneWorkflow({
        workflowKey: key,
        jobs: tasks.allJobs.filter(job => job.primary.runConfig?.workbench && job.primary.runConfig.workflowId === key),
        ...(includeSession ? { session: workbench.session(key) } : {}),
      }),
      remember: (key: string, session: WorkflowWorkbenchSession) => {
        if (session) workbench.sessions[key] = cloneWorkflow(session)
      },
      perform: async (action: WorkbenchAction) => {
        if (action.action === 'run') {
          await workflows.reload()
          const entry = workflows.workflows.find(item => item.id === action.workflowKey)
          if (!entry || !action.entry || entry.updatedAt !== action.entry.updatedAt) throw new Error('workflows.revisionConflictTitle')
          if (!action.bindings) throw new Error('workbench.missingInput')
          const result = await tasks.startWorkbenchInference(action.entry, action.bindings)
          const session = workbench.session(entry.id)
          session.bindings = cloneWorkflow(action.bindings)
          session.selectedJobId = result.jobId
          session.selectedTaskId = ''
          session.viewMode = 'edit'
          return { jobId: result.jobId, total: result.total }
        }
        const target = tasks.tasks.find(task => task.runConfig?.workbench && task.runConfig.workflowId === action.workflowKey
          && (action.action === 'cancel' || action.action === 'remove-run' ? (task.jobId || task.id) === action.jobId : task.id === action.taskId))
        if (!target) throw new Error('workbench.runNotFound')
        if (action.action === 'cancel') { await tasks.cancelWorkbenchJob(action.jobId!); return {} }
        if (action.action === 'remove-run') { await tasks.removeWorkflowRun(action.workflowKey, action.jobId!); return {} }
        const retry = await tasks.retryTask(action.taskId!)
        return { taskId: retry?.id, jobId: retry?.jobId || retry?.id }
      },
      watchTasks: (notify: () => void) => watch(() => tasks.tasks, notify, { deep: true }),
    } } : { applyState: state => workbench.applyRemoteState(state, label.startsWith('workflow-simple-editor-')), bindingFingerprint: (key: string) => JSON.stringify(workbench.session(key).bindings) }),
  }).catch(error => { bridge = undefined; throw error })
  return bridge
}

export async function requestWorkbenchAction(action: WorkbenchAction) {
  const connected = await initializeWorkbenchBridge()
  if (!connected) throw new Error('workbench.hostUnavailable')
  return connected.request(action)
}
