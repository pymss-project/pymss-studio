import type { SeparationTask } from '../../stores/task'

export type TaskRecoveryAction = 'runtime' | 'parameters' | 'models' | 'input'

export function taskRecoveryAction(task: Pick<SeparationTask, 'status' | 'errorCode' | 'runConfig'>): TaskRecoveryAction | null {
  if (task.status !== 'failed') return null
  const code = task.errorCode || ''
  if (['INPUT_NOT_FOUND', 'INPUT_AUDIO_STREAM_MISSING', 'INPUT_MEDIA_UNSUPPORTED'].includes(code)) return 'input'
  if (['MODEL_NOT_FOUND', 'MODEL_DOWNLOAD_FAILED'].includes(code)) return 'models'
  // Parameters belong to a single model. Workflow runs, including ensembles, fall through to the runtime.
  if (['ROCM_OUT_OF_MEMORY', 'INFERENCE_PARAMS_INVALID'].includes(code) && task.runConfig?.runMode !== 'workflow') return 'parameters'
  if (['RUNTIME_OUTDATED', 'DEVICE_CONFIG_INVALID', 'WORKER_NATIVE_CRASH', 'ROCM_CONTEXT_LOST',
    'ROCM_OUT_OF_MEMORY', 'ROCM_TARGET_MISSING', 'ROCM_RUNTIME_INCOMPATIBLE', 'ROCM_NATIVE_CHECK_FAILED'].includes(code)) return 'runtime'
  return null
}

export function taskRecoveryActions<T extends Pick<SeparationTask, 'status' | 'errorCode' | 'runConfig'>>(tasks: readonly T[]): { action: TaskRecoveryAction; task: T }[] {
  const result = new Map<TaskRecoveryAction, T>()
  for (const task of tasks) {
    const action = taskRecoveryAction(task)
    if (action && !result.has(action)) result.set(action, task)
  }
  return [...result].map(([action, task]) => ({ action, task }))
}

export function failedTasksToRetry<T extends Pick<SeparationTask, 'id' | 'status' | 'retryOf'>>(candidates: readonly T[], allTasks: readonly T[]): T[] {
  // A queued, active or successful replacement already covers its original failure.
  const byId = new Map(allTasks.map(task => [task.id, task]))
  const covered = new Set<string>()
  for (const task of allTasks) {
    if (['failed', 'cancelled'].includes(task.status)) continue
    let parent = task.retryOf
    const visited = new Set<string>()
    while (parent && !visited.has(parent)) {
      visited.add(parent)
      covered.add(parent)
      parent = byId.get(parent)?.retryOf
    }
  }
  const seen = new Set<string>()
  return candidates.filter(task => {
    if (task.status !== 'failed' || covered.has(task.id) || seen.has(task.id)) return false
    seen.add(task.id)
    return true
  })
}
