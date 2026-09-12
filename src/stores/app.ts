import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { createRuntimeQueryQueue } from '@/utils/runtimeQueries'
import { isTauriRuntime } from '@/utils/appStore'
import { registerWindowCloseGuard } from '@/utils/windowCloseGuards'

export type EnvInfo = {
  pythonVersion?: string
  platform?: string
  workerVersion?: string
  pymssAvailable?: boolean
  /** Whether the active runtime's pymss exposes the user-model registry (2.0.15+). */
  customModelsSupported?: boolean
  pymssPath?: string | null
  pymssVersion?: string | null
  pymssError?: string
  torchAvailable?: boolean
  torchVersion?: string | null
  torchError?: string
  torchBackend?: 'cpu' | 'cuda' | 'rocm' | string
  hipVersion?: string | null
  cudaAvailable?: boolean
  cudaAvailableError?: string | null
  cudaDeviceCount?: number
  cudaDevices?: CudaDeviceInfo[]
  cudaDeviceCountError?: string | null
  cudaDeviceNamesError?: string | null
  mpsAvailable?: boolean
  mlxAvailable?: boolean
  avAvailable?: boolean
  librosaAvailable?: boolean
}

export type RuntimeBackend = 'cpu' | 'cuda' | 'rocm' | 'mlx'
export type RuntimeInfo = {
  manifestVersion?: string
  pythonVersion?: string
  platform?: string
  machine?: string
  bootstrapPython?: string
  runtimeEnvsDir?: string
  activeRuntimeFile?: string
  backend?: RuntimeBackend | null
  installedBackend?: RuntimeBackend | string | null
  installState?: {
    backend?: RuntimeBackend | string
    manifestVersion?: string
    installedAt?: string
    pythonVersion?: string
    torchVersion?: string | null
    torchBackend?: string | null
    pythonPath?: string
    logPath?: string
    packages?: Record<string, boolean>
    packageVersions?: Record<string, string | null>
    pymssVersion?: string | null
    pymssCoreVersion?: string | null
    pymssGraphAvailable?: boolean | null
  } | null
  installedEnvironments?: InstalledRuntime[]
  /** GPU vendors detected without torch ('nvidia' | 'amd' | 'intel'); empty when undetectable. */
  gpuVendors?: string[]
  statePath?: string
  logPath?: string
  torchVersion?: string | null
  torchBackend?: string
  acceleratorAvailable?: boolean
  packages?: Record<string, boolean>
  packageVersions?: Record<string, string | null>
  pymssVersion?: string | null
  pymssCoreVersion?: string | null
  /** Whether the pymss graph API required by advanced workflows is importable. */
  pymssGraphAvailable?: boolean | null
  ready?: boolean
}

export type InstalledRuntime = {
  backend?: RuntimeBackend | string
  source?: 'managed' | 'bundled' | string
  health?: 'ready' | 'degraded' | 'broken' | 'unknown' | string
  manifestVersion?: string
  installedAt?: string
  pythonVersion?: string
  torchVersion?: string | null
  torchBackend?: string | null
  acceleratorAvailable?: boolean
  pythonPath?: string
  logPath?: string
  packages?: Record<string, boolean>
  packageVersions?: Record<string, string | null>
  pymssVersion?: string | null
  pymssCoreVersion?: string | null
  /** Whether the pymss graph API required by advanced workflows is importable. */
  pymssGraphAvailable?: boolean | null
  coreUpdateSupported?: boolean
}

export type RuntimeCoreVersions = {
  packages?: Record<string, {
    latestVersion?: string | null
    error?: string
  }>
}

export type CudaDeviceInfo = {
  id: number
  name: string
  totalMemoryBytes?: number
  major?: number
  minor?: number
}

export type DiagnosticLevel = 'ok' | 'warn' | 'error'
export type DiagnosticItem = {
  key: string
  level: DiagnosticLevel
  label: string
  value: string
  detail?: string
}

export type WorkerEventConnectionStatus = 'idle' | 'connecting' | 'connected' | 'error'

export const useAppStore = defineStore('app', () => {
  const envInfo = ref<EnvInfo | null>(null)
  const envLoading = ref(false)
  const envCheckedOnce = ref(false)
  const workerEvents = ref<any[]>([])
  const workerEventConnectionStatus = ref<WorkerEventConnectionStatus>('idle')
  const workerEventConnectionError = ref('')
  const lastError = ref<string | null>(null)
  const runtimeInfo = ref<RuntimeInfo | null>(null)
  const runtimeInstallTaskId = ref<string | null>(null)
  const runtimeInstallStatus = ref<'idle' | 'installing' | 'success' | 'error' | 'cancelled'>('idle')
  const runtimeInstallBackend = ref<string | null>(null)
  const runtimeInstallMessage = ref('')
  const runtimeInstallLogs = ref<string[]>([])
  const runtimeCoreVersions = ref<RuntimeCoreVersions | null>(null)
  const runtimeCoreVersionsLoading = ref(false)
  const runtimeCoreUpdateTaskId = ref<string | null>(null)
  const runtimeCoreUpdateStatus = ref<'idle' | 'updating' | 'success' | 'error' | 'cancelled'>('idle')
  const runtimeCoreUpdateMessage = ref('')
  const runtimeEnvSizes = ref<Record<string, number>>({})
  const runtimeEnvSizesLoading = ref(false)
  // Backends whose venv exists but never finished installing — leftover disk usage the user
  // can reclaim.
  const runtimeIncompleteBackends = ref<string[]>([])
  const buildInfoVersion = ref('')
  const buildInfoVariant = ref('')
  const buildInfoUpdateSupported = ref(false)

  const diagnostics = computed<DiagnosticItem[]>(() => {
    const env = envInfo.value
    if (!env) return []
    return [
      {
        key: 'python',
        level: env.pythonVersion ? 'ok' : 'error',
        label: 'Python',
        value: env.pythonVersion || 'Not detected',
        detail: env.platform,
      },
      {
        key: 'pymss',
        level: env.pymssAvailable ? 'ok' : 'error',
        label: 'pymss',
        value: env.pymssAvailable ? 'Available' : 'Unavailable',
        detail: env.pymssPath || env.pymssError,
      },
      {
        key: 'torch',
        level: env.torchAvailable ? 'ok' : 'error',
        label: 'Torch',
        value: env.torchVersion || 'Unavailable',
        detail: env.torchError,
      },
      {
        key: 'accelerator',
        level: env.cudaAvailable || env.mpsAvailable || env.mlxAvailable ? 'ok' : 'warn',
        label: 'Accelerator',
        value: env.cudaAvailable
          ? `${env.torchBackend === 'rocm' ? 'ROCm' : 'CUDA'} (${env.cudaDeviceCount || 0})`
          : env.mlxAvailable
              ? 'MLX'
              : env.mpsAvailable
                ? 'MPS'
              : 'CPU only',
        detail: env.cudaAvailable || env.mpsAvailable || env.mlxAvailable
          ? undefined
          : 'No hardware accelerator detected. Separation still works, but can be slower.',
      },
      {
        key: 'av',
        level: env.avAvailable ? 'ok' : 'warn',
        label: 'PyAV',
        value: env.avAvailable ? 'Available' : 'Unavailable',
        detail: env.avAvailable ? undefined : 'Some audio formats may require extra codecs or PyAV.',
      },
    ]
  })

  const envReady = computed(() => {
    const env = envInfo.value
    return Boolean(env?.pythonVersion && env?.pymssAvailable && env?.torchAvailable)
  })

  const runtimeInstalledBackend = computed(() => {
    const info = runtimeInfo.value
    if (!info?.ready) return null
    const recorded = info.installedBackend || info.installState?.backend
    if (recorded === 'mlx' && info.packages?.mlx) return recorded
    if (recorded && recorded === info.torchBackend) return recorded
    if (info.packages?.mlx) return 'mlx'
    return info.torchBackend || null
  })

  const envIssueCount = computed(() => diagnostics.value.filter((item) => item.level !== 'ok').length)

  function runtimeReadyForBackend(backend: RuntimeBackend) {
    const info = runtimeInfo.value
    if (!info?.ready) return false
    if (backend === 'mlx') return Boolean(info.packages?.mlx)
    if (info.torchBackend !== backend) return false
    if (backend === 'cuda' || backend === 'rocm') return Boolean(info.acceleratorAvailable)
    return true
  }

  function recordWorkerEvent(event: any) {
    workerEvents.value.unshift(event)
    workerEvents.value = workerEvents.value.slice(0, 100)
  }

  function handleWorkerEvent(event: any) {
    if (event?.type === 'env_info') {
      envInfo.value = event.payload
      envLoading.value = false
      envCheckedOnce.value = true
    }
    if (event?.type === 'error') {
      lastError.value = event.payload?.message || 'Unknown error'
      if (event.payload?.logPath) {
        runtimeInfo.value = { ...(runtimeInfo.value || {}), logPath: event.payload.logPath }
      }
      if (event.payload?.backend && event.payload?.logPath && runtimeInstallBackend.value === event.payload.backend) {
        runtimeInstallMessage.value = event.payload.message || runtimeInstallMessage.value
      }
    }
    if (event?.type === 'error' && event?.payload?.code === 'ENV_CHECK_FAILED') {
      envLoading.value = false
      envCheckedOnce.value = true
    }
  }

  function clearWorkerEvents() {
    workerEvents.value = []
  }

  function setWorkerEventConnection(status: WorkerEventConnectionStatus, error = '') {
    workerEventConnectionStatus.value = status
    workerEventConnectionError.value = status === 'error' ? error : ''
  }

  async function checkEnv() {
    envLoading.value = true
    lastError.value = null
    if (!isTauriRuntime()) {
      const result: EnvInfo = { platform: navigator.platform }
      envInfo.value = result
      envCheckedOnce.value = true
      envLoading.value = false
      return result
    }
    try {
      const result = await invoke<EnvInfo>('get_env_info')
      envInfo.value = result
      envCheckedOnce.value = true
      return result
    } catch (error) {
      lastError.value = error instanceof Error ? error.message : String(error)
      throw error
    } finally {
      envLoading.value = false
    }
  }

  async function checkEnvInBackground() {
    if (envLoading.value) return
    if (!isTauriRuntime()) {
      envCheckedOnce.value = true
      return
    }
    envLoading.value = true
    lastError.value = null
    try {
      await invoke('start_env_check')
    } catch (error) {
      envLoading.value = false
      lastError.value = error instanceof Error ? error.message : String(error)
      throw error
    }
  }

  // Disk usage is a separate worker call: walking multi-GB venvs is too slow to fold into
  // runtime_info, which runs on startup and after every runtime operation.
  // Callers refresh right after an install or a delete, so a caller must never be handed the
  // result of a walk that started before the change it is refreshing for.
  // Queries and writes take the same worker-side lock. Each enqueue supplies a fresh
  // callback, and background writes keep their queue slot until a terminal event.
  const runRuntimeQuery = createRuntimeQueryQueue()

  type RuntimeBackgroundOperation = {
    dispatched: boolean
    cancelled: boolean
    completed: boolean
    finishedEvent: string
    started: Promise<void>
    resolveStarted: () => void
    complete: () => void
  }
  const runtimeBackgroundOperations = new Map<string, RuntimeBackgroundOperation>()

  function startRuntimeBackground(command: string, taskId: string, payload: Record<string, unknown>, finishedEvent: string) {
    let resolveStarted!: () => void
    let rejectStarted!: (error: unknown) => void
    let resolveCompleted!: () => void
    const started = new Promise<void>((resolve, reject) => {
      resolveStarted = resolve
      rejectStarted = reject
    })
    const completed = new Promise<void>(resolve => { resolveCompleted = resolve })
    const operation: RuntimeBackgroundOperation = {
      dispatched: false,
      cancelled: false,
      completed: false,
      finishedEvent,
      started,
      resolveStarted,
      complete() {
        operation.completed = true
        resolveCompleted()
      },
    }
    runtimeBackgroundOperations.set(taskId, operation)
    void runRuntimeQuery(async () => {
      try {
        if (operation.cancelled) return
        // Onboarding can start before bootstrap has registered the event listener.
        // Subscribe before dispatch so even an immediate terminal event releases the queue.
        await import('@/utils/events').then(({ registerWorkerEvents }) => registerWorkerEvents())
        if (operation.cancelled) return
        operation.dispatched = true
        await invoke(command, { payload })
        resolveStarted()
        await completed
      } catch (error) {
        rejectStarted(error)
        throw error
      } finally {
        runtimeBackgroundOperations.delete(taskId)
      }
    }, { retryBusy: false }).catch(rejectStarted)
    // Preserve the public contract: acknowledge dispatch without waiting for installation.
    return started
  }

  async function cancelRuntimeBackground(command: string, taskId: string) {
    const operation = runtimeBackgroundOperations.get(taskId)
    if (operation && !operation.dispatched) {
      operation.cancelled = true
      handleRuntimeEvent({ type: 'task_cancelled', taskId })
      operation.resolveStarted()
      return true
    }
    if (operation) {
      // Cancellation must not race ahead of the start IPC registering its worker in Rust.
      try {
        await operation.started
      } catch {
        return false
      }
      if (operation.completed) return false
    }
    const cancelled = await invoke<boolean>(command, { taskId })
    if (cancelled && operation && !operation.completed) {
      // The acknowledged cancellation also releases the queue if its event was missed.
      handleRuntimeEvent({ type: 'task_cancelled', taskId })
    }
    return cancelled
  }

  async function measureRuntimeEnvSizes() {
    return runRuntimeQuery(async () => {
      if (!isTauriRuntime()) {
        runtimeEnvSizes.value = {}
        runtimeIncompleteBackends.value = []
        return runtimeEnvSizes.value
      }
      const result = await invoke<{
        sizes?: Record<string, number>
        incompleteBackends?: string[]
      }>('runtime_env_sizes')
      runtimeEnvSizes.value = result?.sizes || {}
      runtimeIncompleteBackends.value = result?.incompleteBackends || []
      return runtimeEnvSizes.value
    })
  }

  let runtimeEnvSizesWaiting = 0

  async function loadRuntimeEnvSizes() {
    runtimeEnvSizesWaiting += 1
    runtimeEnvSizesLoading.value = true
    try {
      return await measureRuntimeEnvSizes()
    } catch {
      // Sizes are supplementary — a failure must not break the settings page.
      return runtimeEnvSizes.value
    } finally {
      runtimeEnvSizesWaiting -= 1
      // Only the last caller clears the flag; queued callers are still measuring.
      if (runtimeEnvSizesWaiting === 0) runtimeEnvSizesLoading.value = false
    }
  }

  async function checkRuntimeInfo(backend?: RuntimeBackend) {
    return runRuntimeQuery(async () => {
      if (!isTauriRuntime()) {
        const result: RuntimeInfo = { ready: false, backend: backend || null, platform: navigator.platform }
        runtimeInfo.value = result
        return result
      }
      const result = await invoke<RuntimeInfo>('runtime_info', { payload: backend ? { backend } : {} })
      runtimeInfo.value = result
      return result
    })
  }

  async function loadRuntimeCoreVersions() {
    runtimeCoreVersionsLoading.value = true
    try {
      if (!isTauriRuntime()) {
        runtimeCoreVersions.value = null
        return runtimeCoreVersions.value
      }
      runtimeCoreVersions.value = await invoke<RuntimeCoreVersions>('runtime_core_versions')
      return runtimeCoreVersions.value
    } finally {
      runtimeCoreVersionsLoading.value = false
    }
  }

  async function installRuntime(backend: RuntimeBackend, mirror = 'auto', locale = '') {
    const taskId = `runtime_install_${crypto.randomUUID()}`
    runtimeInstallTaskId.value = taskId
    runtimeInstallStatus.value = 'installing'
    runtimeInstallBackend.value = backend
    runtimeInstallMessage.value = ''
    runtimeInstallLogs.value = []
    try {
      await startRuntimeBackground('start_runtime_install', taskId, { taskId, backend, mirror, locale }, 'runtime_install_finished')
    } catch (error) {
      runtimeInstallStatus.value = 'error'
      runtimeInstallMessage.value = error instanceof Error ? error.message : String(error)
      throw error
    }
    return taskId
  }

  async function updateRuntimeCore(
    backend: RuntimeBackend,
    mirror = 'auto',
    locale = '',
    target: { pythonPath?: string } = {},
  ) {
    const taskId = `runtime_core_update_${crypto.randomUUID()}`
    runtimeCoreUpdateTaskId.value = taskId
    runtimeCoreUpdateStatus.value = 'updating'
    runtimeCoreUpdateMessage.value = ''
    try {
      await startRuntimeBackground('start_runtime_core_update', taskId, { taskId, backend, mirror, locale, ...target }, 'runtime_core_update_finished')
    } catch (error) {
      runtimeCoreUpdateStatus.value = 'error'
      runtimeCoreUpdateMessage.value = error instanceof Error ? error.message : String(error)
      throw error
    }
    return taskId
  }

  async function activateRuntime(
    backend: RuntimeBackend,
    target: { pythonPath?: string } = {},
    options: { refreshCoreVersions?: boolean; onlyIfNoActive?: boolean } = {},
  ) {
    const payload = {
      backend,
      ...target,
      ...(options.onlyIfNoActive ? { onlyIfNoActive: true } : {}),
    }
    await runRuntimeQuery(() => invoke('activate_runtime', { payload }), { retryBusy: false })
    const checks: Promise<unknown>[] = [checkRuntimeInfo(), checkEnv()]
    if (options.refreshCoreVersions !== false) checks.push(loadRuntimeCoreVersions())
    await Promise.all(checks)
  }

  async function cancelRuntimeInstall() {
    if (!runtimeInstallTaskId.value) return false
    return cancelRuntimeBackground('cancel_runtime_install', runtimeInstallTaskId.value)
  }

  async function cancelRuntimeCoreUpdate() {
    if (!runtimeCoreUpdateTaskId.value) return false
    return cancelRuntimeBackground('cancel_runtime_core_update', runtimeCoreUpdateTaskId.value)
  }

  // Runtime installation is a background worker, not a separation task, so the
  // title bar's task counter does not cover it. Cancel it before the window exits
  // to avoid leaving pip/download processes behind.
  registerWindowCloseGuard(async () => {
    if (!isTauriRuntime()) return
    if (runtimeInstallStatus.value === 'installing') await cancelRuntimeInstall()
    if (runtimeCoreUpdateStatus.value === 'updating') await cancelRuntimeCoreUpdate()
  }, 80)

  async function waitForRuntimeInstall(timeoutMs = 30 * 60 * 1000) {
    const startedAt = Date.now()
    while (runtimeInstallStatus.value === 'installing') {
      if (Date.now() - startedAt > timeoutMs) {
        throw new Error('Runtime installation timed out')
      }
      await new Promise((resolve) => window.setTimeout(resolve, 250))
    }
    if (runtimeInstallStatus.value !== 'success') {
      throw new Error(runtimeInstallMessage.value || 'Runtime installation failed')
    }
  }

  async function deleteRuntime(backend: RuntimeBackend, target: { pythonPath?: string } = {}) {
    await runRuntimeQuery(() => invoke('delete_runtime', { payload: { backend, ...target } }), { retryBusy: false })
    await checkRuntimeInfo()
  }

  function handleRuntimeEvent(event: any) {
    const taskId = event?.taskId
    const operation = runtimeBackgroundOperations.get(taskId)
    if (operation && (
      event?.type === operation.finishedEvent || event?.type === 'error'
      || event?.type === 'task_cancelled' || event?.type === 'runtime_install_failed'
    )) {
      // Release before terminal handlers enqueue their post-write refreshes.
      operation.complete()
    }
    if (runtimeCoreUpdateTaskId.value && taskId === runtimeCoreUpdateTaskId.value) {
      if (event?.type === 'runtime_core_update_finished') {
        runtimeCoreUpdateStatus.value = 'success'
        runtimeCoreUpdateMessage.value = ''
        runtimeCoreUpdateTaskId.value = null
        if (event.payload?.state || event.payload?.logPath) {
          runtimeInfo.value = {
            ...(runtimeInfo.value || {}),
            installState: event.payload?.state,
            logPath: event.payload?.logPath,
          }
        }
        void checkEnv()
        void checkRuntimeInfo()
        void loadRuntimeCoreVersions()
      } else if (event?.type === 'runtime_core_update_started') {
        runtimeCoreUpdateStatus.value = 'updating'
        runtimeCoreUpdateMessage.value = event.payload?.backend || ''
        if (event.payload?.logPath) runtimeInfo.value = { ...(runtimeInfo.value || {}), logPath: event.payload.logPath }
      } else if (event?.type === 'runtime_core_update_stage' || event?.type === 'runtime_core_update_log') {
        runtimeCoreUpdateMessage.value = event.payload?.message || event.payload?.stage || ''
      } else if (event?.type === 'error') {
        runtimeCoreUpdateStatus.value = 'error'
        runtimeCoreUpdateMessage.value = event.payload?.message || 'Runtime core update failed'
        runtimeCoreUpdateTaskId.value = null
      } else if (event?.type === 'task_cancelled') {
        runtimeCoreUpdateStatus.value = 'cancelled'
        runtimeCoreUpdateTaskId.value = null
      }
      return
    }
    if (!runtimeInstallTaskId.value || taskId !== runtimeInstallTaskId.value) return
    if (event?.type === 'runtime_install_finished') {
      runtimeInstallStatus.value = 'success'
      runtimeInstallMessage.value = ''
      if (event.payload?.state || event.payload?.logPath) {
        runtimeInfo.value = {
          ...(runtimeInfo.value || {}),
          installedBackend: event.payload?.state?.backend,
          installState: event.payload?.state,
          logPath: event.payload?.logPath,
        }
      }
      void checkEnv()
      void checkRuntimeInfo()
      void loadRuntimeCoreVersions()
      void import('@/stores/model').then(({ useModelStore }) => useModelStore().loadModels())
    } else if (event?.type === 'runtime_install_started') {
      runtimeInstallStatus.value = 'installing'
      if (event.payload?.backend) runtimeInstallBackend.value = event.payload.backend
      runtimeInstallMessage.value = event.payload?.backend || ''
      if (event.payload?.logPath) runtimeInfo.value = { ...(runtimeInfo.value || {}), logPath: event.payload.logPath }
    } else if (event?.type === 'runtime_install_stage' || event?.type === 'runtime_install_log') {
      runtimeInstallMessage.value = event.payload?.message || event.payload?.stage || ''
      const line = event.payload?.message || event.payload?.stage
      if (line) runtimeInstallLogs.value = [...runtimeInstallLogs.value, String(line)].slice(-300)
    } else if (event?.type === 'runtime_install_failed' || event?.type === 'error') {
      runtimeInstallStatus.value = 'error'
      runtimeInstallMessage.value = event.payload?.message || 'Runtime installation failed'
    } else if (event?.type === 'task_cancelled') {
      runtimeInstallStatus.value = 'cancelled'
    }
  }

  return {
    envInfo,
    envLoading,
    envCheckedOnce,
    workerEvents,
    workerEventConnectionStatus,
    workerEventConnectionError,
    lastError,
    diagnostics,
    envReady,
    envIssueCount,
    runtimeInstalledBackend,
    runtimeReadyForBackend,
    recordWorkerEvent,
    handleWorkerEvent,
    clearWorkerEvents,
    setWorkerEventConnection,
    checkEnv,
    checkEnvInBackground,
    runtimeInfo,
    runtimeInstallTaskId,
    runtimeInstallStatus,
    runtimeInstallBackend,
    runtimeInstallMessage,
    runtimeInstallLogs,
    runtimeCoreVersions,
    runtimeCoreVersionsLoading,
    runtimeCoreUpdateTaskId,
    runtimeCoreUpdateStatus,
    runtimeCoreUpdateMessage,
    runtimeEnvSizes,
    runtimeEnvSizesLoading,
    runtimeIncompleteBackends,
    buildInfoVersion,
    buildInfoVariant,
    buildInfoUpdateSupported,
    loadRuntimeEnvSizes,
    checkRuntimeInfo,
    loadRuntimeCoreVersions,
    installRuntime,
    cancelRuntimeCoreUpdate,
    updateRuntimeCore,
    activateRuntime,
    cancelRuntimeInstall,
    waitForRuntimeInstall,
    deleteRuntime,
    handleRuntimeEvent,
  }
})
