import { isStudioHost } from '@/sdk'
import { ref, type Ref } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import { registerWindowCloseGuard } from '../../utils/windowCloseGuards.ts'
import { workerLogRecord, type LogRecord } from '../../utils/logRecord.ts'

export type OptionalPackageStatus = {
  package: string
  requirement: string
  installed: boolean
  present?: boolean
  version: string | null
  source: 'managed' | 'environment' | null
  canUninstall?: boolean
  issue?: 'version_mismatch' | null
  runtimeIdentity: string
}

export type OptionalPackageAction = 'install' | 'uninstall'

export type OptionalPackageLogEntry = {
  id: number
  timestamp: number
  level: 'stage' | 'output' | 'success' | 'error'
  message: string
  record?: LogRecord
}

type OptionalPackageEvent = {
  type: string
  taskId?: string | null
  payload?: OptionalPackageStatus & {
    package?: string
    action?: OptionalPackageAction
    message?: string
    logRecord?: LogRecord
  }
}

type ManageOptions = {
  mirror: string
  locale: string
  startedMessage: string
}

export type OptionalRuntimePackage = {
  status: Ref<OptionalPackageStatus | null>
  checking: Ref<boolean>
  busy: Ref<boolean>
  error: Ref<string>
  action: Ref<OptionalPackageAction | null>
  taskId: Ref<string | null>
  cancelling: Ref<boolean>
  logs: Ref<OptionalPackageLogEntry[]>
  showLog: Ref<boolean>
  start: () => Promise<void>
  refresh: () => Promise<OptionalPackageStatus | null>
  manage: (action: OptionalPackageAction, options: ManageOptions) => Promise<OptionalPackageStatus>
  cancel: () => Promise<boolean>
  appendLog: (message: string, level: OptionalPackageLogEntry['level']) => void
}

export class OptionalPackageCancelledError extends Error {
  constructor() {
    super('Optional package operation cancelled')
    this.name = 'OptionalPackageCancelledError'
  }
}

const MAX_LOGS = 160
const packages = new Map<string, OptionalRuntimePackage>()

function createOptionalRuntimePackage(packageName: string): OptionalRuntimePackage {
  const status = ref<OptionalPackageStatus | null>(null)
  const checking = ref(true)
  const busy = ref(false)
  const error = ref('')
  const action = ref<OptionalPackageAction | null>(null)
  const taskId = ref<string | null>(null)
  const cancelling = ref(false)
  const logs = ref<OptionalPackageLogEntry[]>([])
  const showLog = ref(false)
  let logSequence = 0
  let refreshSequence = 0
  let startPromise: Promise<void> | undefined
  let unlistenWorker: UnlistenFn | undefined
  let pending: {
    resolve: (status: OptionalPackageStatus) => void
    reject: (error: Error) => void
  } | undefined

  function appendLog(messageText: string, level: OptionalPackageLogEntry['level']) {
    const message = messageText.trim()
    if (!message) return
    logSequence += 1
    logs.value = [
      ...logs.value,
      { id: logSequence, timestamp: Date.now(), level, message },
    ].slice(-MAX_LOGS)
  }

  async function start() {
    if (typeof window === 'undefined' || !(isStudioHost()) || unlistenWorker) return
    if (!startPromise) {
      startPromise = listen<OptionalPackageEvent>('pymss://worker-event', (event) => {
        const workerEvent = event.payload
        if (!busy.value || !taskId.value || workerEvent.taskId !== taskId.value) return
        const payload = workerEvent.payload
        if (workerEvent.type === 'optional_package_stage' || workerEvent.type === 'optional_package_log') {
          if (payload?.package !== packageName || payload.action !== action.value) return
          appendLog(
            String(payload.message || ''),
            workerEvent.type === 'optional_package_stage' ? 'stage' : 'output',
          )
          const last = logs.value.at(-1)
          if (last && payload.logRecord) last.record = workerLogRecord({ ...workerEvent, taskId: workerEvent.taskId || undefined, payload })
          return
        }
        if (workerEvent.type === 'optional_package_status' && payload?.package === packageName) {
          status.value = payload
          const completion = pending
          finishOperation()
          completion?.resolve(payload)
          void refresh()
          return
        }
        if (workerEvent.type === 'task_cancelled') {
          const completion = pending
          finishOperation()
          completion?.reject(new OptionalPackageCancelledError())
          return
        }
        if (workerEvent.type === 'error') {
          const detail = String(payload?.message || 'Optional package operation failed')
          error.value = detail
          appendLog(detail, 'error')
          const completion = pending
          finishOperation()
          completion?.reject(new Error(detail))
        }
      }).then((unlisten) => {
        unlistenWorker = unlisten
      }).catch((listenerError) => {
        startPromise = undefined
        throw listenerError
      })
    }
    await startPromise
  }

  function finishOperation() {
    pending = undefined
    busy.value = false
    action.value = null
    taskId.value = null
    cancelling.value = false
  }

  async function refresh() {
    if (busy.value) return status.value
    const sequence = ++refreshSequence
    checking.value = true
    error.value = ''
    status.value = null
    try {
      const nextStatus = await invoke<OptionalPackageStatus>('optional_runtime_package_status', {
        package: packageName,
      })
      if (sequence !== refreshSequence) return status.value
      status.value = nextStatus
      return nextStatus
    } catch (refreshError) {
      if (sequence !== refreshSequence) return status.value
      status.value = null
      error.value = String(refreshError)
      return null
    } finally {
      if (sequence === refreshSequence) checking.value = false
    }
  }

  async function manage(packageAction: OptionalPackageAction, options: ManageOptions) {
    if (busy.value) throw new Error(`${packageName} package management is already running`)
    refreshSequence += 1
    checking.value = false
    if (packageAction === 'install') status.value = null
    logs.value = []
    action.value = packageAction
    taskId.value = `optional_package_${packageName}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
    busy.value = true
    error.value = ''
    showLog.value = true
    appendLog(options.startedMessage, 'stage')
    const completion = new Promise<OptionalPackageStatus>((resolve, reject) => {
      pending = { resolve, reject }
    })
    try {
      await start()
      await invoke<{ taskId: string; started: boolean }>('manage_optional_runtime_package', {
        payload: {
          package: packageName,
          action: packageAction,
          mirror: options.mirror,
          locale: options.locale,
          taskId: taskId.value,
        },
      })
    } catch (startError) {
      const detail = String(startError)
      error.value = detail
      appendLog(detail, 'error')
      const failed = pending
      finishOperation()
      failed?.reject(startError instanceof Error ? startError : new Error(detail))
    }
    return completion
  }

  async function cancel() {
    if (!busy.value || !taskId.value || cancelling.value) return false
    cancelling.value = true
    try {
      const accepted = await invoke<boolean>('cancel_task', { taskId: taskId.value })
      if (!accepted) {
        const completion = pending
        finishOperation()
        completion?.reject(new Error('Optional package task is no longer running'))
      }
      return accepted
    } finally {
      cancelling.value = false
    }
  }

  registerWindowCloseGuard(async () => {
    if (busy.value) await cancel()
  }, 70)

  return {
    status, checking, busy, error, action, taskId, cancelling, logs, showLog,
    start, refresh, manage, cancel, appendLog,
  }
}

export function useOptionalRuntimePackage(packageName: string) {
  const normalized = packageName.trim().toLowerCase()
  const existing = packages.get(normalized)
  if (existing) return existing
  const runtimePackage = createOptionalRuntimePackage(normalized)
  packages.set(normalized, runtimePackage)
  return runtimePackage
}
