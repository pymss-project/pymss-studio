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
  // Retries of one input form a lineage rooted at its first task. A queued, active or successful
  // task anywhere in that lineage already covers every failure in it, including sibling retries
  // started from an older batch.
  const byId = new Map(allTasks.map(task => [task.id, task]))
  const lineageRoot = (task: T) => {
    let root = task.id
    const visited = new Set<string>([root])
    let parent = task.retryOf
    for (; parent && !visited.has(parent); parent = byId.get(parent)?.retryOf) {
      visited.add(parent)
      root = parent
    }
    // A corrupted cyclic link has no first task; every member resolves to the same one.
    return parent ? [...visited].sort()[0]! : root
  }
  const coveredRoots = new Set(allTasks.filter(task => !['failed', 'cancelled'].includes(task.status)).map(lineageRoot))
  const seen = new Set<string>()
  return candidates.filter(task => {
    if (task.status !== 'failed' || coveredRoots.has(lineageRoot(task)) || seen.has(task.id)) return false
    seen.add(task.id)
    return true
  })
}
