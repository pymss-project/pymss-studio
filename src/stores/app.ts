import { createLogger } from '@/utils/logger'
import { workerLogRecord, type LogEntry } from '@/utils/logRecord'
import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { createRuntimeQueryQueue, createRuntimeReadCache } from '@/utils/runtimeQueries'
import { activeRuntimeEnvironment, rocmStableDeviceIds, supportsRuntimeCoreUpdate, supportsRuntimeManagement } from '@/utils/runtime'
import { runtimeOperationError, type RuntimeOperationError } from '@/utils/runtimeErrors'
import { isTauriRuntime } from '@/utils/appStore'
import { registerWindowCloseGuard } from '@/utils/windowCloseGuards'
import { useSettingsStore } from '@/stores/settings'

const studioLog = createLogger('ui.stores.app')

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
export type RuntimeGpuProbeCheck = {
  id: string; status: 'passed' | 'failed' | 'unavailable' | 'timeout' | 'skipped'
  message?: string; precision?: string; requiredForBasic?: boolean; errorCode?: string
  exceptionType?: string; detail?: string
}
export type RuntimeGpuProbeMode = {
  mode: 'miopen' | 'nativeHip' | string; status: string; basicAvailable: boolean; checks: RuntimeGpuProbeCheck[]
  message?: string; errorCode?: string; failureStage?: string; exitCode?: number
}
export type RuntimeGpuDeviceCapability = {
  index: number; name: string; rawArchitecture?: string; gfx?: string; stableId?: string; driverVersion?: string
  basicAvailable: boolean; modes: RuntimeGpuProbeMode[]
  failureStage?: string; exitCode?: number
}
export type RuntimeGpuProbeReport = {
  probeVersion: string | number; profileId?: string; baseFingerprint?: string; targetFingerprint?: string
  checkedAt: string; cacheReusable: boolean; status: 'completed' | 'partial' | 'unavailable' | 'cancelled'
  message?: string; errorCode?: string
  fromCache?: boolean; matchesCurrentRuntime?: boolean
  deviceCapabilities: RuntimeGpuDeviceCapability[]
}
export type RuntimeInstallAction = 'install' | 'addTargets' | 'upgrade' | 'prepareSeed'
export type RuntimeInstallOptions = {
  profileId?: string; targets?: string[]; selectedDeviceIds?: string[]; action?: RuntimeInstallAction; acceptPreview?: boolean
  expectedProfileId?: string; expectedProfileRevision?: string | number
  expectedBaseFingerprint?: string; expectedTargetFingerprint?: string; replacePrevious?: boolean
}
export type RuntimeHardwareDevice = {
  id: string; vendor: string; name: string; hardwareId?: string; driverVersion?: string
  gfx?: string | null; architectureSource: 'native' | 'runtime' | 'manual' | 'unknown'
  architectureConfidence: 'confirmed' | 'hint' | 'unknown'; memoryBytes?: number | null; isDiscrete?: boolean | null
}
export type RuntimeHardwareProfile = {
  id: string; revision: string | number; releaseState: 'legacy' | 'disabled' | 'preview' | 'stable' | 'revoked'
  installationEnabled: boolean; validationStatus?: string; availableTargets: string[]; validatedTargets: string[]; torchVersion?: string
}
export type RuntimeHardwareInventory = {
  schemaVersion: number; platform: string; devices: RuntimeHardwareDevice[]; profiles: RuntimeHardwareProfile[]
  defaultProfileId?: string | null
  errors: Array<{ code?: string; message?: string } | string>; detectorStatus?: string
}
export type RuntimeInstallPlan = RuntimeInstallOptions & {
  backend: RuntimeBackend; profileRevision?: string | number; releaseState?: string
  installationEnabled: boolean; requiresPreviewAcceptance?: boolean
  validationStatus?: string; packages?: Record<string, string>; requiresPreviousReplacement?: boolean
  requestedTargets?: string[]; resolvedTargets?: string[]; installedTargets?: string[]; missingTargets?: string[]
  seedPreparationRequired?: boolean; downloadBytes?: number | null
  environmentRollbackAvailable?: boolean; previousProfile?: { id?: string; packages?: Record<string, string> } | null
}
export type RuntimeActivationValidation = {
  status: 'passed' | 'failed' | 'notRun'; reason?: string; message?: string; exitCode?: number
  devices?: Array<{ index: number; name?: string; gfx?: string; status: 'passed' | 'failed' | 'targetMissing'; message?: string }>
}
export type RuntimeProfileState = {
  profileId?: string; profileRevision?: string | number; installedTargets?: string[]; baseFingerprint?: string; targetFingerprint?: string
  selectedDeviceIds?: string[]
  gpuProbeReport?: RuntimeGpuProbeReport | null
  activationValidation?: RuntimeActivationValidation | null
  seedPreparationRequired?: boolean
}
export type RuntimeCoreMode = 'update' | 'sync' | 'repair' | 'rollback'
export type RuntimeCorePlan = {
  id: string
  backend: RuntimeBackend
  pythonPath: string
  mode: RuntimeCoreMode
  channel: 'stable' | 'prerelease'
  mirror: string
  currentVersions: Record<string, string | null>
  targetVersions: Record<string, string>
  dependencyCount: number
}
type RuntimeDebugCommand = 'debug_runtime_override_active' | 'debug_runtime_write_file' | 'debug_runtime_restore_file'
export type RuntimeInfo = RuntimeProfileState & {
  environmentRollbackAvailable?: boolean
  seedProfileId?: string | null
  seedInstalledTargets?: string[]
  seedPreparationReason?: string | null
  runtimeMaintenancePending?: boolean
  recoveryRequired?: boolean
  recoveryError?: { code?: string; message?: string } | null
  previousRuntime?: { profileId?: string; torchVersion?: string; installedAt?: string } | null
  rollbackAvailable?: boolean
  manifestVersion?: string
  pythonVersion?: string
  platform?: string
  machine?: string
  bootstrapPython?: string
  runtimeEnvsDir?: string
  activeRuntimeFile?: string
  backend?: RuntimeBackend | null
  installedBackend?: RuntimeBackend | string | null
  installState?: RuntimeProfileState & {
    backend?: RuntimeBackend | string
    manifestVersion?: string
    installedAt?: string
    pythonVersion?: string
    torchVersion?: string | null
    torchBackend?: string | null
    pythonPath?: string
    overlayPath?: string
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
  liveProbeError?: { code: string; message: string } | null
  ready?: boolean
}

export type InstalledRuntime = RuntimeProfileState & {
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
  overlayPath?: string
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
  channel?: 'stable' | 'prerelease'
  packages?: Record<string, {
    latestVersion?: string | null
    currentVersion?: string | null
    updateAvailable?: boolean
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

function readSelectedRocmDeviceIds(): string[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem('pymss-studio.rocm-device-ids') || '[]')
    return Array.isArray(value) ? rocmStableDeviceIds(value.filter((id): id is string => typeof id === 'string')).slice(0, 16) : []
  } catch { return [] }
}

export const useAppStore = defineStore('app', () => {
  const settings = useSettingsStore()
  const envInfo = ref<EnvInfo | null>(null)
  const envLoading = ref(false)
  const envError = ref<string | null>(null)
  const envCheckedOnce = ref(false)
  let envRequestId: string | null = null
  const workerEvents = ref<any[]>([])
  const workerEventConnectionStatus = ref<WorkerEventConnectionStatus>('idle')
  const workerEventConnectionError = ref('')
  const lastError = ref<string | null>(null)
  const runtimeInfo = ref<RuntimeInfo | null>(null)
  const runtimeHardwareInventory = ref<RuntimeHardwareInventory | null>(null)
  const runtimeHardwareInventoryLoading = ref(false)
  const runtimeHardwareInventoryError = ref<RuntimeOperationError | null>(null)
  const selectedRocmDeviceIds = ref<string[]>(readSelectedRocmDeviceIds())
  watch(selectedRocmDeviceIds, (ids) => {
    try { localStorage.setItem('pymss-studio.rocm-device-ids', JSON.stringify(rocmStableDeviceIds(ids))) } catch { /* Storage is optional. */ }
  }, { deep: true })
  const runtimeInfoLoading = ref(false)
  const runtimeInfoError = ref('')
  const runtimeInfoFailure = ref<RuntimeOperationError | null>(null)
  const runtimeInstallTaskId = ref<string | null>(null)
  const runtimeInstallStatus = ref<'idle' | 'installing' | 'success' | 'error' | 'cancelled'>('idle')
  const runtimeInstallBackend = ref<string | null>(null)
  const runtimeInstallMessage = ref('')
  const runtimeInstallActivationValidation = ref<RuntimeActivationValidation | null>(null)
  const runtimeInstallLogs = ref<LogEntry[]>([])
  const runtimeGpuProbeTaskId = ref<string | null>(null)
  const runtimeGpuProbeStatus = ref<'idle' | 'running' | 'success' | 'error' | 'cancelled'>('idle')
  const runtimeGpuProbeStage = ref('')
  const runtimeGpuProbeError = ref<RuntimeOperationError | null>(null)
  const runtimeGpuProbeReport = ref<RuntimeGpuProbeReport | null>(null)
  const runtimeMaintenanceTaskId = ref<string | null>(null)
  const runtimeMaintenanceStatus = ref<'idle' | 'running' | 'success' | 'error' | 'cancelled'>('idle')
  const runtimeMaintenanceMode = ref<'rollback' | 'recover'>('recover')
  const runtimeMaintenanceError = ref<RuntimeOperationError | null>(null)
  const runtimeCoreVersions = ref<RuntimeCoreVersions | null>(null)
  const runtimeCoreVersionsLoading = ref(false)
  const runtimeCoreUpdateTaskId = ref<string | null>(null)
  const runtimeCoreUpdateStatus = ref<'idle' | 'preparing' | 'prepared' | 'updating' | 'success' | 'error' | 'cancelled'>('idle')
  const runtimeCoreUpdateMessage = ref('')
  const runtimeCoreUpdateError = ref<RuntimeOperationError | null>(null)
  const runtimeCoreUpdateMode = ref<RuntimeCoreMode>('update')
  const runtimeCoreUpdateStage = ref('')
  const corePlanWaiters = new Map<string, { resolve: (plan: RuntimeCorePlan) => void; reject: (error: Error) => void }>()
  let preparedRuntimeCorePlanId: string | null = null
  const runtimeEnvSizes = ref<Record<string, number>>({})
  const runtimeRollbackBytes = ref<number | null>(null)
  const runtimeEnvSizesLoading = ref(false)
  const runtimeEnvSizesError = ref('')
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
    const envCheckFailed = event?.type === 'error' && event?.payload?.code === 'ENV_CHECK_FAILED'
    if ((event?.type === 'env_info' || envCheckFailed) && (!envRequestId || event.requestId !== envRequestId)) return
    if (event?.type === 'env_info') {
      envInfo.value = event.payload
      envError.value = null
      envLoading.value = false
      envCheckedOnce.value = true
      envRequestId = null
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
    if (envCheckFailed) {
      envError.value = lastError.value
      envLoading.value = false
      envCheckedOnce.value = true
      envRequestId = null
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
    const requestId = crypto.randomUUID()
    envRequestId = requestId
    envLoading.value = true
    envError.value = null
    lastError.value = null
    try {
      const result: EnvInfo = isTauriRuntime()
        ? await runRuntimeQuery(() => invoke<EnvInfo>('get_env_info'))
        : { platform: navigator.platform }
      if (envRequestId === requestId) {
        envInfo.value = result
        envError.value = null
        envCheckedOnce.value = true
      }
      return result
    } catch (error) {
      if (envRequestId === requestId) {
        envError.value = error instanceof Error ? error.message : String(error)
        lastError.value = envError.value
      }
      throw error
    } finally {
      if (envRequestId === requestId) {
        envRequestId = null
        envLoading.value = false
      }
    }
  }

  async function checkEnvInBackground() {
    if (envLoading.value) return
    if (!isTauriRuntime()) {
      envCheckedOnce.value = true
      return
    }
    const requestId = crypto.randomUUID()
    envRequestId = requestId
    envLoading.value = true
    envError.value = null
    lastError.value = null
    try {
      await import('@/utils/events').then(({ registerWorkerEvents }) => registerWorkerEvents())
      if (envRequestId !== requestId) return
      await invoke('start_env_check', { requestId })
    } catch (error) {
      if (envRequestId === requestId) {
        envRequestId = null
        envLoading.value = false
        envError.value = error instanceof Error ? error.message : String(error)
        lastError.value = envError.value
      }
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
  const runtimeInfoCache = createRuntimeReadCache<RuntimeInfo>(2 * 60_000)
  const runtimeSizesCache = createRuntimeReadCache<Record<string, number>>(10 * 60_000)
  const runtimeVersionsCache = createRuntimeReadCache<RuntimeCoreVersions | null>(15 * 60_000)
  let runtimeVersionsRequest = 0

  function invalidateRuntimeReads() {
    runtimeInfoCache.invalidate()
    runtimeSizesCache.invalidate()
    runtimeVersionsCache.invalidate()
    runtimeVersionsRequest += 1
  }

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
    invalidateRuntimeReads()
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
        await withRuntimeMutation(async () => {
          operation.dispatched = true
          await invoke(command, { payload })
          resolveStarted()
          await completed
        })
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
        runtimeRollbackBytes.value = null
        runtimeIncompleteBackends.value = []
        return runtimeEnvSizes.value
      }
      const result = await invoke<{
        sizes?: Record<string, number>
        incompleteBackends?: string[]
        environmentRollbackBytes?: number | null
      }>('runtime_env_sizes')
      runtimeEnvSizes.value = result?.sizes || {}
      const rollbackBytes = result?.environmentRollbackBytes
      runtimeRollbackBytes.value = typeof rollbackBytes === 'number' && Number.isFinite(rollbackBytes) && rollbackBytes >= 0 ? rollbackBytes : null
      runtimeIncompleteBackends.value = result?.incompleteBackends || []
      return runtimeEnvSizes.value
    })
  }

  let runtimeEnvSizesWaiting = 0

  async function queryRuntimeEnvSizes() {
    runtimeEnvSizesWaiting += 1
    runtimeEnvSizesLoading.value = true
    runtimeEnvSizesError.value = ''
    try {
      const result = await measureRuntimeEnvSizes()
      runtimeEnvSizesError.value = ''
      return result
    } catch (error) {
      runtimeEnvSizesError.value = error instanceof Error ? error.message : String(error)
      throw error
    } finally {
      runtimeEnvSizesWaiting -= 1
      // Only the last caller clears the flag; queued callers are still measuring.
      if (runtimeEnvSizesWaiting === 0) runtimeEnvSizesLoading.value = false
    }
  }

  async function readRuntimeEnvSizes(fresh: boolean) {
    try {
      return await runtimeSizesCache.read(queryRuntimeEnvSizes, fresh)
    } catch {
      // Keep the last displayed size on failure, without marking it fresh.
      return runtimeEnvSizes.value
    }
  }

  const loadRuntimeEnvSizes = () => readRuntimeEnvSizes(true)
  const ensureRuntimeEnvSizes = () => readRuntimeEnvSizes(false)
  let runtimeInfoWaiting = 0

  async function queryRuntimeInfo(backend?: RuntimeBackend) {
    runtimeInfoWaiting += 1
    runtimeInfoLoading.value = true
    runtimeInfoError.value = ''
    runtimeInfoFailure.value = null
    try {
      return await runRuntimeQuery(async () => {
        if (!isTauriRuntime()) {
          const result: RuntimeInfo = { ready: false, backend: backend || null, platform: navigator.platform }
          runtimeInfo.value = result
          runtimeInfoError.value = ''
          return result
        }
        const result = await invoke<RuntimeInfo>('runtime_info', { payload: backend ? { backend } : {} })
        runtimeInfo.value = result
        runtimeInfoError.value = ''
        return result
      })
    } catch (error) {
      runtimeInfoError.value = error instanceof Error ? error.message : String(error)
      runtimeInfoFailure.value = runtimeOperationError(error)
      throw error
    } finally {
      runtimeInfoWaiting -= 1
      runtimeInfoLoading.value = runtimeInfoWaiting > 0
    }
  }

  function checkRuntimeInfo(backend?: RuntimeBackend) {
    if (backend) {
      runtimeInfoCache.invalidate()
      return queryRuntimeInfo(backend)
    }
    return runtimeInfoCache.read(() => queryRuntimeInfo(), true)
  }

  const ensureRuntimeInfo = () => runtimeInfoCache.read(() => queryRuntimeInfo())
  let runtimeVersionsWaiting = 0

  async function queryRuntimeCoreVersions() {
    const request = ++runtimeVersionsRequest
    const channel = settings.updateChannel
    runtimeVersionsWaiting += 1
    runtimeCoreVersionsLoading.value = true
    try {
      if (!isTauriRuntime()) {
        runtimeCoreVersions.value = null
        return runtimeCoreVersions.value
      }
      const info = runtimeInfo.value
      const result = await invoke<RuntimeCoreVersions>('runtime_core_versions', { payload: {
        channel,
        versions: { pymss: info?.pymssVersion || info?.installState?.pymssVersion, 'pymss-core': info?.pymssCoreVersion || info?.installState?.pymssCoreVersion },
      } })
      if (request === runtimeVersionsRequest && channel === settings.updateChannel) runtimeCoreVersions.value = result
      return result
    } finally {
      runtimeVersionsWaiting -= 1
      runtimeCoreVersionsLoading.value = runtimeVersionsWaiting > 0
    }
  }

  const loadRuntimeCoreVersions = () => runtimeVersionsCache.read(queryRuntimeCoreVersions, true)
  const ensureRuntimeCoreVersions = () => runtimeVersionsCache.read(queryRuntimeCoreVersions)
  watch(() => settings.updateChannel, () => {
    runtimeVersionsRequest += 1
    runtimeVersionsCache.invalidate()
    runtimeCoreVersions.value = null
    if (runtimeCoreUpdateSupported.value && runtimeInfo.value) void loadRuntimeCoreVersions().catch(() => {})
  })

  const runtimeManagementSupported = computed(() => supportsRuntimeManagement({
    platform: runtimeInfo.value?.platform || envInfo.value?.platform,
  }))
  const runtimeCoreUpdateSupported = computed(() => supportsRuntimeCoreUpdate({
    platform: runtimeInfo.value?.platform || envInfo.value?.platform,
  }))
  const runtimeDebugSupported = computed(() => {
    const platform = String(runtimeInfo.value?.platform || envInfo.value?.platform || '').toLowerCase()
    return platform ? ['win32', 'windows'].includes(platform)
      : typeof navigator !== 'undefined' && /^win/i.test(navigator.platform)
  })

  function requireRuntimeManagement() {
    if (!runtimeManagementSupported.value) throw new Error('Runtime management is only available on Windows and Linux.')
  }

  async function installRuntime(backend: RuntimeBackend, mirror = 'auto', locale = '', target: RuntimeInstallOptions = {}) {
    requireRuntimeManagement()
    const taskId = `runtime_install_${crypto.randomUUID()}`
    runtimeInstallTaskId.value = taskId
    runtimeInstallStatus.value = 'installing'
    runtimeInstallBackend.value = backend
    runtimeInstallMessage.value = ''
    runtimeInstallLogs.value = []
    runtimeInstallActivationValidation.value = null
    try {
      await startRuntimeBackground('start_runtime_install', taskId, { taskId, backend, mirror, locale, ...target,
        ...(target.selectedDeviceIds ? { selectedDeviceIds: rocmStableDeviceIds(target.selectedDeviceIds) } : {}),
      }, 'runtime_install_finished')
    } catch (error) {
      runtimeInstallStatus.value = 'error'
      runtimeInstallMessage.value = error instanceof Error ? error.message : String(error)
      throw error
    }
    return taskId
  }

  async function runtimeInstallPlan(backend: RuntimeBackend, target: RuntimeInstallOptions = {}) {
    return runRuntimeQuery(() => invoke<RuntimeInstallPlan>('runtime_install_plan', { payload: { backend, ...target,
      ...(target.selectedDeviceIds ? { selectedDeviceIds: rocmStableDeviceIds(target.selectedDeviceIds) } : {}),
    } }))
  }

  async function loadRuntimeHardwareInventory() {
    runtimeHardwareInventoryLoading.value = true
    runtimeHardwareInventoryError.value = null
    try {
      const result = await runRuntimeQuery(() => invoke<RuntimeHardwareInventory>('runtime_hardware_inventory'))
      runtimeHardwareInventory.value = result
      return result
    } catch (error) {
      runtimeHardwareInventoryError.value = runtimeOperationError(error)
      throw error
    } finally { runtimeHardwareInventoryLoading.value = false }
  }

  async function startRuntimeGpuProbe(target: { deviceIds?: number[]; modes?: Array<'miopen' | 'nativeHip'>; includeFp16?: boolean } = {}) {
    const taskId = `runtime_gpu_probe_${crypto.randomUUID()}`
    runtimeGpuProbeTaskId.value = taskId
    runtimeGpuProbeStatus.value = 'running'
    runtimeGpuProbeStage.value = ''
    runtimeGpuProbeError.value = null
    runtimeGpuProbeReport.value = null
    try {
      await startRuntimeBackground('start_runtime_gpu_probe', taskId, { taskId, ...target }, 'runtime_gpu_probe_finished')
    } catch (error) {
      if (runtimeGpuProbeTaskId.value === taskId) {
        runtimeGpuProbeStatus.value = 'error'
        runtimeGpuProbeError.value = runtimeOperationError(error)
        runtimeGpuProbeTaskId.value = null
      }
      throw error
    }
    return taskId
  }

  async function cancelRuntimeGpuProbe() {
    if (!runtimeGpuProbeTaskId.value) return false
    return cancelRuntimeBackground('cancel_runtime_gpu_probe', runtimeGpuProbeTaskId.value)
  }

  async function rollbackRuntime() {
    requireRuntimeManagement()
    const taskId = `runtime_rollback_${crypto.randomUUID()}`
    runtimeMaintenanceTaskId.value = taskId
    runtimeMaintenanceMode.value = 'rollback'
    runtimeMaintenanceStatus.value = 'running'
    runtimeMaintenanceError.value = null
    try {
      await startRuntimeBackground('start_runtime_rollback', taskId, { taskId, backend: 'rocm' }, 'runtime_rollback_finished')
    } catch (error) {
      if (runtimeMaintenanceTaskId.value === taskId) {
        runtimeMaintenanceStatus.value = 'error'
        runtimeMaintenanceError.value = runtimeOperationError(error)
        runtimeMaintenanceTaskId.value = null
      }
      throw error
    }
    return taskId
  }

  async function cancelRuntimeMaintenance() {
    if (!runtimeMaintenanceTaskId.value || runtimeMaintenanceMode.value !== 'rollback') return false
    return cancelRuntimeBackground('cancel_runtime_install', runtimeMaintenanceTaskId.value)
  }

  async function recoverRuntime() {
    requireRuntimeManagement()
    runtimeMaintenanceMode.value = 'recover'
    runtimeMaintenanceStatus.value = 'running'
    runtimeMaintenanceError.value = null
    try {
      await mutateRuntime(() => invoke('recover_runtime', { payload: {} }))
      runtimeMaintenanceStatus.value = 'success'
      runtimeGpuProbeReport.value = null
      await Promise.allSettled([checkRuntimeInfo(), checkEnv(), loadRuntimeEnvSizes()])
    } catch (error) {
      runtimeMaintenanceStatus.value = 'error'
      runtimeMaintenanceError.value = runtimeOperationError(error)
      throw error
    }
  }

  async function updateRuntimeCore(
    backend: RuntimeBackend,
    mirror = 'auto',
    locale = '',
    target: { pythonPath?: string; repairDependencies?: boolean; planId?: string; mode?: RuntimeCoreMode; prepareOnly?: boolean } = {},
  ) {
    if (!runtimeCoreUpdateSupported.value) throw new Error('Runtime core updates are unavailable on this platform.')
    preparedRuntimeCorePlanId = null
    const taskId = `runtime_core_update_${crypto.randomUUID()}`
    runtimeCoreUpdateTaskId.value = taskId
    runtimeCoreUpdateStatus.value = 'updating'
    runtimeCoreUpdateMessage.value = ''
    runtimeCoreUpdateError.value = null
    runtimeCoreUpdateStage.value = ''
    runtimeCoreUpdateMode.value = target.mode || (target.repairDependencies ? 'repair' : 'update')
    if (target.prepareOnly) runtimeCoreUpdateStatus.value = 'preparing'
    try {
      await startRuntimeBackground('start_runtime_core_update', taskId, { taskId, backend, mirror, locale, channel: settings.updateChannel, ...target }, target.prepareOnly ? 'runtime_core_update_plan' : 'runtime_core_update_finished')
    } catch (error) {
      const failure = runtimeOperationError(error)
      if (runtimeCoreUpdateTaskId.value === taskId) {
        runtimeCoreUpdateStatus.value = 'error'
        runtimeCoreUpdateError.value = failure
        runtimeCoreUpdateMessage.value = failure.message
        runtimeCoreUpdateTaskId.value = null
      }
      throw error instanceof Error ? error : failure
    }
    return taskId
  }

  async function prepareRuntimeCoreUpdate(backend: RuntimeBackend, mirror: string, locale: string, target: { pythonPath?: string; mode: RuntimeCoreMode }) {
    // The waiter must be registered before dispatch: a cached plan may complete
    // before the start acknowledgement is delivered to the webview.
    const taskId = `runtime_core_update_${crypto.randomUUID()}`
    let resolvePlan!: (plan: RuntimeCorePlan) => void
    let rejectPlan!: (error: Error) => void
    const completion = new Promise<RuntimeCorePlan>((resolve, reject) => { resolvePlan = resolve; rejectPlan = reject })
    corePlanWaiters.set(taskId, { resolve: resolvePlan, reject: rejectPlan })
    if (!runtimeCoreUpdateSupported.value) {
      corePlanWaiters.delete(taskId)
      throw new Error('Runtime core updates are unavailable on this platform.')
    }
    preparedRuntimeCorePlanId = null
    runtimeCoreUpdateTaskId.value = taskId
    runtimeCoreUpdateStatus.value = 'preparing'
    runtimeCoreUpdateMode.value = target.mode
    runtimeCoreUpdateMessage.value = ''
    runtimeCoreUpdateError.value = null
    runtimeCoreUpdateStage.value = ''
    void startRuntimeBackground('start_runtime_core_update', taskId, {
      taskId, backend, mirror, locale, ...target, channel: settings.updateChannel, prepareOnly: true,
    }, 'runtime_core_update_plan').catch(error => {
      const failure = runtimeOperationError(error)
      corePlanWaiters.delete(taskId)
      if (runtimeCoreUpdateTaskId.value === taskId) {
        runtimeCoreUpdateStatus.value = 'error'
        runtimeCoreUpdateError.value = failure
        runtimeCoreUpdateMessage.value = failure.message
        runtimeCoreUpdateTaskId.value = null
      }
      rejectPlan(failure)
    })
    return completion
  }

  function discardRuntimeCorePlan(planId: string) {
    if (runtimeCoreUpdateStatus.value !== 'prepared' || preparedRuntimeCorePlanId !== planId) return false
    preparedRuntimeCorePlanId = null
    runtimeCoreUpdateStatus.value = 'idle'
    return true
  }

  async function activateRuntime(
    backend: RuntimeBackend,
    target: { pythonPath?: string; selectedDeviceIds?: string[] } = {},
    options: { refreshCoreVersions?: boolean; onlyIfNoActive?: boolean } = {},
  ) {
    requireRuntimeManagement()
    const payload = {
      backend,
      ...target,
      ...(target.selectedDeviceIds ? { selectedDeviceIds: rocmStableDeviceIds(target.selectedDeviceIds) } : {}),
      ...(options.onlyIfNoActive ? { onlyIfNoActive: true } : {}),
    }
    await mutateRuntime(() => invoke('activate_runtime', { payload }))
    runtimeGpuProbeReport.value = null
    const checks: Promise<unknown>[] = [checkRuntimeInfo(), checkEnv()]
    if (options.refreshCoreVersions !== false) checks.push(loadRuntimeCoreVersions())
    await Promise.all(checks)
  }

  function mutateRuntime<T>(mutation: () => Promise<T>): Promise<T> {
    invalidateRuntimeReads()
    return runRuntimeQuery(() => withRuntimeMutation(mutation), { retryBusy: false })
  }

  async function withRuntimeMutation<T>(mutation: () => Promise<T>): Promise<T> {
    // Invalidate probes before changing the interpreter or its configuration.
    envRequestId = null
    envLoading.value = true
    try {
      return await mutation()
    } finally {
      if (envRequestId === null) envLoading.value = false
    }
  }

  async function updateDebugRuntime<T>(command: RuntimeDebugCommand, payload: Record<string, unknown>): Promise<T> {
    if (!runtimeDebugSupported.value) throw new Error('Runtime file editing is only available on Windows.')
    const result = await mutateRuntime(() => invoke<T>(command, { payload }))
    // A debug write may deliberately leave the interpreter unusable. Preserve the
    // successful file-write result while recording probe failures in diagnostics.
    await Promise.allSettled([checkRuntimeInfo(), checkEnv()])
    return result
  }

  async function cancelRuntimeInstall() {
    if (!runtimeInstallTaskId.value) return false
    return cancelRuntimeBackground('cancel_runtime_install', runtimeInstallTaskId.value)
  }

  async function cancelRuntimeCoreUpdate() {
    if (runtimeCoreUpdateStage.value === 'activate') return false
    if (!runtimeCoreUpdateTaskId.value) return false
    return cancelRuntimeBackground('cancel_runtime_core_update', runtimeCoreUpdateTaskId.value)
  }

  // Runtime installation is a background worker, not a separation task, so the
  // title bar's task counter does not cover it. Cancel it before the window exits
  // to avoid leaving pip/download processes behind.
  registerWindowCloseGuard(async () => {
    if (!isTauriRuntime()) return
    if (runtimeMaintenanceStatus.value === 'running') {
      if (runtimeMaintenanceMode.value === 'recover') throw new Error('Runtime recovery is in progress. Wait for the operation to finish.')
      await cancelRuntimeMaintenance()
    }
    if (runtimeGpuProbeStatus.value === 'running') await cancelRuntimeGpuProbe()
    if (runtimeInstallStatus.value === 'installing') await cancelRuntimeInstall()
    if (runtimeCoreUpdateStatus.value === 'updating' || runtimeCoreUpdateStatus.value === 'preparing') {
      if (runtimeCoreUpdateStage.value === 'activate') throw new Error('The runtime is being activated. Wait for the operation to finish.')
      await cancelRuntimeCoreUpdate()
    }
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
    requireRuntimeManagement()
    await mutateRuntime(() => invoke('delete_runtime', { payload: { backend, ...target } }))
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
    if (runtimeGpuProbeTaskId.value && taskId === runtimeGpuProbeTaskId.value) {
      if (event.type === 'runtime_gpu_probe_finished') {
        const report = (event.payload?.report || event.payload) as RuntimeGpuProbeReport
        runtimeGpuProbeReport.value = report.status === 'cancelled' ? null : report
        runtimeGpuProbeStatus.value = report.status === 'cancelled' ? 'cancelled' : 'success'
        runtimeGpuProbeTaskId.value = null
        runtimeGpuProbeStage.value = ''
      } else if (event.type === 'runtime_gpu_probe_stage') {
        runtimeGpuProbeStage.value = event.payload?.message || event.payload?.stage || event.payload?.phase || ''
      } else if (event.type === 'error') {
        runtimeGpuProbeStatus.value = 'error'
        runtimeGpuProbeError.value = runtimeOperationError(event.payload)
        runtimeGpuProbeTaskId.value = null
      } else if (event.type === 'task_cancelled') {
        runtimeGpuProbeStatus.value = 'cancelled'
        runtimeGpuProbeTaskId.value = null
        runtimeGpuProbeStage.value = ''
      }
      return
    }
    if (runtimeMaintenanceTaskId.value && taskId === runtimeMaintenanceTaskId.value) {
      if (event.type === 'runtime_rollback_finished') {
        runtimeMaintenanceStatus.value = 'success'
        runtimeMaintenanceTaskId.value = null
        runtimeGpuProbeReport.value = null
        void Promise.allSettled([checkRuntimeInfo(), checkEnv(), loadRuntimeEnvSizes()])
        void import('@/stores/model').then(({ useModelStore }) => useModelStore().loadModels()).catch(() => {})
      } else if (event.type === 'error') {
        runtimeMaintenanceStatus.value = 'error'
        runtimeMaintenanceError.value = runtimeOperationError(event.payload)
        runtimeMaintenanceTaskId.value = null
        void checkRuntimeInfo().catch(() => {})
      } else if (event.type === 'task_cancelled') {
        runtimeMaintenanceStatus.value = 'cancelled'
        runtimeMaintenanceTaskId.value = null
        void checkRuntimeInfo().catch(() => {})
      }
      return
    }
    if (runtimeCoreUpdateTaskId.value && taskId === runtimeCoreUpdateTaskId.value) {
      const waiter = corePlanWaiters.get(taskId)
      if (event?.type === 'runtime_core_update_plan') {
        preparedRuntimeCorePlanId = (event.payload as RuntimeCorePlan).id
        runtimeCoreUpdateStatus.value = 'prepared'
        runtimeCoreUpdateTaskId.value = null
        runtimeCoreUpdateMessage.value = ''
        runtimeCoreUpdateStage.value = ''
        corePlanWaiters.delete(taskId)
        waiter?.resolve(event.payload as RuntimeCorePlan)
      } else if (event?.type === 'runtime_core_update_finished') {
        invalidateRuntimeReads()
        runtimeGpuProbeReport.value = null
        runtimeCoreVersions.value = null
        runtimeCoreUpdateStatus.value = 'success'
        runtimeCoreUpdateMessage.value = ''
        runtimeCoreUpdateTaskId.value = null
        if (event.payload?.state || event.payload?.logPath) {
          const state = event.payload?.state
          const previousActive = activeRuntimeEnvironment(runtimeInfo.value)
          runtimeInfo.value = {
            ...(runtimeInfo.value || {}),
            ...(state ? {
              pymssVersion: state.pymssVersion,
              pymssCoreVersion: state.pymssCoreVersion,
              packageVersions: state.packageVersions,
              rollbackAvailable: event.payload?.rollbackAvailable === true,
              installedEnvironments: runtimeInfo.value?.installedEnvironments?.map(env => env === previousActive ? { ...env, ...state, health: 'ready' } : env),
            } : {}),
            installState: state,
            logPath: event.payload?.logPath,
          }
        }
        void checkEnv().catch((error) => {
          studioLog.warn('Failed to refresh environment details after a core update', error)
        })
        void checkRuntimeInfo().then(() => loadRuntimeCoreVersions()).catch((error) => {
          studioLog.warn('Failed to refresh runtime versions after a core update', error)
        })
        void import('@/stores/model').then(({ useModelStore }) => useModelStore().loadModels()).catch((error) => {
          studioLog.warn('Failed to refresh models after a runtime change', error)
        })
      } else if (event?.type === 'runtime_core_update_started') {
        if (runtimeCoreUpdateStatus.value !== 'preparing') runtimeCoreUpdateStatus.value = 'updating'
        runtimeCoreUpdateMode.value = event.payload?.mode || 'update'
        runtimeCoreUpdateMessage.value = event.payload?.backend || ''
        if (event.payload?.logPath) runtimeInfo.value = { ...(runtimeInfo.value || {}), logPath: event.payload.logPath }
      } else if (event?.type === 'runtime_core_update_stage' || event?.type === 'runtime_core_update_log') {
        if (event?.type === 'runtime_core_update_stage') runtimeCoreUpdateStage.value = event.payload?.stage || ''
        runtimeCoreUpdateMessage.value = event.payload?.message || event.payload?.stage || ''
      } else if (event?.type === 'error') {
        const failure = runtimeOperationError(event.payload || 'Runtime core update failed')
        runtimeCoreUpdateStatus.value = 'error'
        runtimeCoreUpdateError.value = failure
        runtimeCoreUpdateMessage.value = failure.message
        runtimeCoreUpdateTaskId.value = null
        corePlanWaiters.delete(taskId)
        waiter?.reject(failure)
      } else if (event?.type === 'task_cancelled') {
        runtimeCoreUpdateStatus.value = 'cancelled'
        runtimeCoreUpdateError.value = null
        runtimeCoreUpdateMessage.value = ''
        runtimeCoreUpdateTaskId.value = null
        corePlanWaiters.delete(taskId)
        waiter?.reject(runtimeOperationError({ code: 'TASK_CANCELLED', message: 'Runtime operation cancelled' }))
        void checkRuntimeInfo().catch(() => {})
      }
      return
    }
    if (!runtimeInstallTaskId.value || taskId !== runtimeInstallTaskId.value) return
    if (event?.type === 'runtime_install_finished') {
      runtimeGpuProbeReport.value = null
      runtimeInstallStatus.value = 'success'
      runtimeInstallActivationValidation.value = event.payload?.activationValidation || null
      runtimeInstallMessage.value = ''
      if (event.payload?.activated !== false && (event.payload?.state || event.payload?.logPath)) {
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
      if (line) runtimeInstallLogs.value = [...runtimeInstallLogs.value, workerLogRecord(event)].slice(-300)
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
    envError,
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
    runtimeInfo, runtimeHardwareInventory, runtimeHardwareInventoryLoading, runtimeHardwareInventoryError, selectedRocmDeviceIds, loadRuntimeHardwareInventory,
    runtimeManagementSupported,
    runtimeDebugSupported,
    runtimeCoreUpdateSupported,
    runtimeInfoLoading,
    runtimeInfoError,
    runtimeInfoFailure,
    runtimeInstallTaskId,
    runtimeInstallStatus,
    runtimeInstallBackend,
    runtimeInstallMessage, runtimeInstallActivationValidation,
    runtimeInstallLogs,
    runtimeGpuProbeTaskId, runtimeGpuProbeStatus, runtimeGpuProbeStage, runtimeGpuProbeError, runtimeGpuProbeReport,
    startRuntimeGpuProbe, cancelRuntimeGpuProbe, runtimeInstallPlan,
    runtimeMaintenanceTaskId, runtimeMaintenanceStatus, runtimeMaintenanceMode, runtimeMaintenanceError,
    rollbackRuntime, recoverRuntime, cancelRuntimeMaintenance,
    runtimeCoreVersions,
    runtimeCoreVersionsLoading,
    runtimeCoreUpdateTaskId,
    runtimeCoreUpdateStatus,
    runtimeCoreUpdateMessage,
    runtimeCoreUpdateError,
    runtimeCoreUpdateMode,
    runtimeCoreUpdateStage,
    prepareRuntimeCoreUpdate,
    discardRuntimeCorePlan,
    runtimeEnvSizes,
    runtimeRollbackBytes,
    runtimeEnvSizesLoading,
    runtimeEnvSizesError,
    runtimeIncompleteBackends,
    buildInfoVersion,
    buildInfoVariant,
    buildInfoUpdateSupported,
    loadRuntimeEnvSizes,
    checkRuntimeInfo,
    loadRuntimeCoreVersions,
    ensureRuntimeInfo,
    ensureRuntimeEnvSizes,
    ensureRuntimeCoreVersions,
    installRuntime,
    cancelRuntimeCoreUpdate,
    updateRuntimeCore,
    activateRuntime,
    updateDebugRuntime,
    cancelRuntimeInstall,
    waitForRuntimeInstall,
    deleteRuntime,
    handleRuntimeEvent,
  }
})
