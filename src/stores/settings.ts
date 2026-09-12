import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import {
  applyTheme,
  DEFAULT_THEME_ACCENT,
  DEFAULT_THEME_MODE,
  normalizeThemeAccent,
  type ThemeAccent,
  type ThemeMode,
} from '@/utils/theme'
import {
  applyScaleFactor,
  DEFAULT_SCALE_FACTOR,
  normalizeScaleFactor,
  SCALE_FACTOR_MAX,
  SCALE_FACTOR_MIN,
  SCALE_FACTOR_STEP,
} from '@/utils/appZoom'
import { normalizeLocaleSetting, setLocale, type LocaleSetting } from '@/i18n'
import { isTauriRuntime, loadAppStore, saveAppStore } from '@/utils/appStore'
import {
  DEFAULT_CONCURRENT_SEPARATIONS,
  MAX_CONCURRENT_SEPARATIONS,
  normalizeConcurrentSeparations,
} from '@/features/tasks/concurrency'
import type { EnvInfo } from '@/stores/app'

type AppPathsPayload = {
  dataRoot: string
  settingsDir: string
  modelsDir: string
  outputsDir: string
  editorProjectsDir: string
  logsDir: string
  tempDir: string
}

export type ModelDirMigrationConflict = {
  sourcePath: string
  relativePath: string
  destinationPath: string
  existingSizeBytes: number
  incomingSizeBytes: number
}

export type PrepareModelDirChangePayload = {
  currentModelDir: string
  targetModelDir: string
  sameAsCurrent: boolean
  sourceDirExists: boolean
  targetDirExists: boolean
  targetDirEmpty: boolean
  fileCount: number
  totalBytes: number
  conflictCount: number
  diskAvailableBytes: number | null
  diskInsufficient: boolean
}

export type ModelDirMigrationResolution = 'overwrite' | 'skip' | 'abort'
export type ModelDirMigrationStatus = 'idle' | 'confirm' | 'running' | 'conflict' | 'ready_to_switch' | 'finalizing_cleanup' | 'success' | 'failed' | 'aborted'
export type ModelDirMigrationState = {
  status: ModelDirMigrationStatus
  taskId: string
  sourceModelDir: string
  targetModelDir: string
  previousModelDir: string
  totalFiles: number
  completedFiles: number
  totalBytes: number
  copiedBytes: number
  currentPath: string
  message: string
  skippedFiles: string[]
  overwrittenFiles: string[]
  cleanupFailedFiles: string[]
  conflict: ModelDirMigrationConflict | null
  error: string
  preparation: PrepareModelDirChangePayload | null
  resolvingConflict: boolean
}

type StoredSettings = {
  themeMode?: ThemeMode
  themeAccent?: ThemeAccent
  scaleFactor?: number
  locale?: LocaleSetting
  animationsEnabled?: boolean
  developerMode?: boolean
  modelDir?: string
  outputDir?: string
  defaultDevice?: string
  defaultFormat?: string
  downloadSource?: string
  downloadMethod?: DownloadMethod
  maxConcurrentSeparations?: number
  clearInputAfterSubmit?: boolean
  wavBitDepth?: string
  flacBitDepth?: string
  mp3BitRate?: string
  m4aBitRate?: string
  m4aCodec?: string
  startupOnboardingSeen?: boolean
  startupOnboardingSeenVersion?: string
  proxyMode?: ProxyMode
  proxyUrl?: string
  proxyBypass?: string
  updateChannel?: UpdateChannel
  updateEndpointOverride?: string
}

export type ProxyMode = 'system' | 'none' | 'custom'
export type DownloadMethod = 'aria2c' | 'urllib'
export type UpdateChannel = 'stable' | 'prerelease'

const DEFAULT_LOCALE: LocaleSetting = 'system'
const DEFAULT_DOWNLOAD_SOURCE = 'modelscope'
const DEFAULT_DOWNLOAD_METHOD: DownloadMethod = 'aria2c'
const DEFAULT_DEFAULT_DEVICE = 'auto'
const DEFAULT_DEFAULT_FORMAT = 'wav'
const DEFAULT_CLEAR_INPUT_AFTER_SUBMIT = true
const DEFAULT_PROXY_MODE: ProxyMode = 'system'
const DEFAULT_PROXY_URL = ''
const DEFAULT_UPDATE_CHANNEL: UpdateChannel = 'stable'

function displayModelDirPath(path: unknown): string {
  const value = String(path || '').trim()
  if (!value) return ''
  return value
    .replace(/^\\\\\?\\UNC\\/i, '\\\\')
    .replace(/^\\\\\?\\/, '')
    .replace(/^\/\/\?\/UNC\//i, '//')
    .replace(/^\/\/\?\//, '')
}

function normalizePrepareModelDirChangePayload(payload: PrepareModelDirChangePayload): PrepareModelDirChangePayload {
  return {
    ...payload,
    currentModelDir: displayModelDirPath(payload.currentModelDir),
    targetModelDir: displayModelDirPath(payload.targetModelDir),
  }
}

const DEFAULT_WAV_BIT_DEPTH = 'FLOAT'
const DEFAULT_FLAC_BIT_DEPTH = 'PCM_24'
const DEFAULT_MP3_BIT_RATE = '320k'
const DEFAULT_M4A_BIT_RATE = '512k'
const DEFAULT_M4A_CODEC = 'aac'

function normalizeM4aCodec(value: unknown): string {
  return String(value || '').trim().toLowerCase() === 'aac' ? 'aac' : DEFAULT_M4A_CODEC
}

function createEmptyModelDirMigrationState(): ModelDirMigrationState {
  return {
    status: 'idle',
    taskId: '',
    sourceModelDir: '',
    targetModelDir: '',
    previousModelDir: '',
    totalFiles: 0,
    completedFiles: 0,
    totalBytes: 0,
    copiedBytes: 0,
    currentPath: '',
    message: '',
    skippedFiles: [],
    overwrittenFiles: [],
    cleanupFailedFiles: [],
    conflict: null,
    error: '',
    preparation: null,
    resolvingConflict: false,
  }
}

function normalizeDownloadMethod(value: unknown): DownloadMethod {
  return value === 'urllib' ? 'urllib' : DEFAULT_DOWNLOAD_METHOD
}

function browserAppPaths(): AppPathsPayload {
  return {
    dataRoot: '',
    settingsDir: '',
    modelsDir: 'models',
    outputsDir: 'outputs',
    editorProjectsDir: 'editor-projects',
    logsDir: 'logs',
    tempDir: 'temp',
  }
}

export type AudioParams = {
  wavBitDepth: string
  flacBitDepth: string
  mp3BitRate: string
  m4aBitRate: string
  m4aCodec: string
}

/**
 * What the worker is told to run on. ROCm is deliberately absent: a ROCm torch build exposes
 * its GPUs through the same `cuda` device API, so it resolves to 'cuda' — sending 'rocm' would
 * fail inside pymss. See getRuntimeDeviceConfig().
 */
export type RuntimeDeviceConfig = {
  device: 'auto' | 'cpu' | 'cuda' | 'mps' | 'mlx'
  deviceIds: number[]
}

/** A pickable entry in the device dropdown. `type` is the UI kind, which does tell ROCm apart. */
export type DeviceOption = {
  label: string
  value: string
  type: RuntimeDeviceConfig['device'] | 'rocm'
  deviceIds?: number[]
}

export const useSettingsStore = defineStore('settings', () => {
  const initialized = ref(false)
  const appPaths = ref<AppPathsPayload | null>(null)
  const themeMode = ref<ThemeMode>(DEFAULT_THEME_MODE)
  const themeAccent = ref<ThemeAccent>(DEFAULT_THEME_ACCENT)
  const scaleFactor = ref(DEFAULT_SCALE_FACTOR)
  const locale = ref<LocaleSetting>(DEFAULT_LOCALE)
  const animationsEnabled = ref(true)
  const developerMode = ref(false)
  const modelDir = ref('')
  const outputDir = ref('')
  const defaultDevice = ref(DEFAULT_DEFAULT_DEVICE)
  const defaultFormat = ref(DEFAULT_DEFAULT_FORMAT)
  const downloadSource = ref(DEFAULT_DOWNLOAD_SOURCE)
  const downloadMethod = ref<DownloadMethod>(DEFAULT_DOWNLOAD_METHOD)
  const maxConcurrentSeparations = ref(DEFAULT_CONCURRENT_SEPARATIONS)
  const clearInputAfterSubmit = ref(DEFAULT_CLEAR_INPUT_AFTER_SUBMIT)
  const wavBitDepth = ref(DEFAULT_WAV_BIT_DEPTH)
  const flacBitDepth = ref(DEFAULT_FLAC_BIT_DEPTH)
  const mp3BitRate = ref(DEFAULT_MP3_BIT_RATE)
  const m4aBitRate = ref(DEFAULT_M4A_BIT_RATE)
  const m4aCodec = ref(DEFAULT_M4A_CODEC)
  const proxyMode = ref<ProxyMode>(DEFAULT_PROXY_MODE)
  const proxyUrl = ref(DEFAULT_PROXY_URL)
  const proxyBypass = ref('')
  const updateChannel = ref<UpdateChannel>(DEFAULT_UPDATE_CHANNEL)
  const updateEndpointOverride = ref('')
  const startupOnboardingSeen = ref(false)
  const modelDirMigrationState = ref<ModelDirMigrationState>(createEmptyModelDirMigrationState())

  const dataRoot = computed(() => appPaths.value?.dataRoot || '')
  const settingsDir = computed(() => appPaths.value?.settingsDir || '')
  const editorProjectsDir = computed(() => appPaths.value?.editorProjectsDir || '')
  const logsDir = computed(() => appPaths.value?.logsDir || '')
  const tempDir = computed(() => appPaths.value?.tempDir || '')
  const isModelDirMigrating = computed(() => ['running', 'conflict', 'ready_to_switch', 'finalizing_cleanup'].includes(modelDirMigrationState.value.status))
  const shouldShowStartupOnboarding = computed(() => initialized.value && !startupOnboardingSeen.value)

  const persistable = computed<StoredSettings>(() => ({
    themeMode: themeMode.value,
    themeAccent: themeAccent.value,
    scaleFactor: scaleFactor.value,
    locale: locale.value,
    animationsEnabled: animationsEnabled.value,
    developerMode: developerMode.value,
    modelDir: modelDir.value,
    outputDir: outputDir.value,
    defaultDevice: defaultDevice.value,
    defaultFormat: defaultFormat.value,
    downloadSource: downloadSource.value,
    downloadMethod: downloadMethod.value,
    maxConcurrentSeparations: maxConcurrentSeparations.value,
    clearInputAfterSubmit: clearInputAfterSubmit.value,
    wavBitDepth: wavBitDepth.value,
    flacBitDepth: flacBitDepth.value,
    mp3BitRate: mp3BitRate.value,
    m4aBitRate: m4aBitRate.value,
    m4aCodec: m4aCodec.value,
    startupOnboardingSeen: startupOnboardingSeen.value,
    proxyMode: proxyMode.value,
    proxyUrl: proxyUrl.value,
    proxyBypass: proxyBypass.value,
    updateChannel: updateChannel.value,
    updateEndpointOverride: updateEndpointOverride.value.trim() || undefined,
  }))

  let saveTimer: ReturnType<typeof setTimeout> | null = null
  let scaleApplyTimer: ReturnType<typeof setTimeout> | null = null

  // 防抖应用缩放：滑块位于它自身控制的 WebView 内，立即 setZoom 会在交互过程中
  // 改变滑块几何，导致指针位置重新映射、数值连跳两格。延迟到交互稳定后再缩放即可避免。
  function queueApplyScaleFactor(value: number) {
    if (scaleApplyTimer) clearTimeout(scaleApplyTimer)
    scaleApplyTimer = setTimeout(() => {
      scaleApplyTimer = null
      void applyScaleFactor(value).catch((error) => console.warn('Failed to apply scale factor', error))
    }, 140)
  }

  function queuePersist() {
    if (!initialized.value) return
    if (saveTimer) clearTimeout(saveTimer)
    saveTimer = setTimeout(() => {
      saveTimer = null
      void saveAppStore('app-settings', persistable.value)
    }, 80)
  }

  async function initialize() {
    if (initialized.value) return
    const [paths, stored] = await Promise.all([
      isTauriRuntime() ? invoke<AppPathsPayload>('get_app_paths') : Promise.resolve(browserAppPaths()),
      loadAppStore<StoredSettings>('app-settings'),
    ])

    appPaths.value = paths
    themeMode.value = stored?.themeMode || DEFAULT_THEME_MODE
    themeAccent.value = normalizeThemeAccent(stored?.themeAccent, DEFAULT_THEME_ACCENT)
    scaleFactor.value = normalizeScaleFactor(stored?.scaleFactor)
    locale.value = normalizeLocaleSetting(stored?.locale, DEFAULT_LOCALE)
    animationsEnabled.value = stored?.animationsEnabled ?? true
    developerMode.value = stored?.developerMode === true
    modelDir.value = (stored?.modelDir || paths.modelsDir).trim() || paths.modelsDir
    outputDir.value = (stored?.outputDir || paths.outputsDir).trim() || paths.outputsDir
    defaultDevice.value = stored?.defaultDevice || DEFAULT_DEFAULT_DEVICE
    defaultFormat.value = stored?.defaultFormat || DEFAULT_DEFAULT_FORMAT
    downloadSource.value = stored?.downloadSource || DEFAULT_DOWNLOAD_SOURCE
    downloadMethod.value = normalizeDownloadMethod(stored?.downloadMethod)
    maxConcurrentSeparations.value = normalizeConcurrentSeparations(stored?.maxConcurrentSeparations)
    clearInputAfterSubmit.value = stored?.clearInputAfterSubmit ?? DEFAULT_CLEAR_INPUT_AFTER_SUBMIT
    wavBitDepth.value = stored?.wavBitDepth || DEFAULT_WAV_BIT_DEPTH
    flacBitDepth.value = stored?.flacBitDepth || DEFAULT_FLAC_BIT_DEPTH
    mp3BitRate.value = stored?.mp3BitRate || DEFAULT_MP3_BIT_RATE
    m4aBitRate.value = stored?.m4aBitRate || DEFAULT_M4A_BIT_RATE
    m4aCodec.value = normalizeM4aCodec(stored?.m4aCodec)
    proxyMode.value = (stored?.proxyMode && ['system', 'none', 'custom'].includes(stored.proxyMode))
      ? stored.proxyMode
      : DEFAULT_PROXY_MODE
    proxyUrl.value = stored?.proxyUrl || DEFAULT_PROXY_URL
    proxyBypass.value = stored?.proxyBypass || ''
    updateChannel.value = stored?.updateChannel === 'prerelease' ? 'prerelease' : DEFAULT_UPDATE_CHANNEL
    updateEndpointOverride.value = String(stored?.updateEndpointOverride || '').trim()
    startupOnboardingSeen.value = stored?.startupOnboardingSeen === true
      || String(stored?.startupOnboardingSeenVersion || '').trim().length > 0
    const shouldPersistNormalizedOnboarding =
      !stored
      || typeof stored.startupOnboardingSeen !== 'boolean'
      || String(stored.startupOnboardingSeenVersion || '').trim().length > 0

    applyTheme(themeMode.value, themeAccent.value)
    await applyScaleFactor(scaleFactor.value).catch((error) => console.warn('Failed to apply scale factor', error))
    setLocale(locale.value)
    initialized.value = true
    if (shouldPersistNormalizedOnboarding) queuePersist()
    await syncProxyToBackend().catch((error) => console.warn('Failed to sync proxy settings', error))
  }

  watch(themeMode, (value) => {
    applyTheme(value, themeAccent.value)
    queuePersist()
  })
  watch(themeAccent, (value) => {
    applyTheme(themeMode.value, value)
    queuePersist()
  })
  watch(scaleFactor, (value) => {
    const normalized = normalizeScaleFactor(value)
    if (normalized !== value) scaleFactor.value = normalized
    queueApplyScaleFactor(normalized)
    queuePersist()
  })
  watch(locale, (value) => {
    setLocale(value)
    queuePersist()
  })
  watch(animationsEnabled, () => {
    queuePersist()
  })
  watch(developerMode, async () => {
    if (!initialized.value) return
    try {
      await saveAppStore('app-settings', persistable.value)
    } catch (error) {
      console.warn('Failed to persist app settings', error)
    }
  })
  watch(
    [
      modelDir,
      outputDir,
      defaultDevice,
      defaultFormat,
      downloadSource,
      downloadMethod,
      maxConcurrentSeparations,
      clearInputAfterSubmit,
      wavBitDepth,
      flacBitDepth,
      mp3BitRate,
      m4aBitRate,
      m4aCodec,
      proxyMode,
      proxyUrl,
      proxyBypass,
      updateChannel,
      updateEndpointOverride,
    ],
    () => queuePersist(),
    { deep: true },
  )

  watch(
    [proxyMode, proxyUrl, proxyBypass],
    () => {
      void syncProxyToBackend().catch((error) => console.warn('Failed to sync proxy settings', error))
    },
  )

  async function syncProxyToBackend() {
    if (!initialized.value || !isTauriRuntime()) return
    await invoke('update_proxy_settings', {
      payload: {
        mode: proxyMode.value,
        url: proxyUrl.value,
        bypass: proxyBypass.value,
      },
    })
  }

  function deviceOptions(env?: EnvInfo | null): DeviceOption[] {
    const options: DeviceOption[] = [
      { label: 'Auto (优先使用可用显卡)', value: 'auto', type: 'auto' },
      { label: 'CPU', value: 'cpu', type: 'cpu', deviceIds: [0] },
    ]
    // A ROCm build reports its GPUs through torch's cuda API, so they arrive in cudaDevices and
    // only the naming differs. Each device still gets its own value: sharing one across cards
    // would make every card past the first unselectable and pin inference to GPU 0.
    const isRocm = env?.torchBackend === 'rocm'
    const gpuKind = isRocm ? 'rocm' : 'cuda'
    const gpuName = isRocm ? 'ROCm' : 'CUDA'
    for (const gpu of env?.cudaDevices || []) {
      const memory = gpu.totalMemoryBytes
        ? ` · ${(gpu.totalMemoryBytes / 1024 / 1024 / 1024).toFixed(1)} GB`
        : ''
      options.push({
        label: `${gpuName} ${gpu.id}: ${gpu.name}${memory}`,
        value: `${gpuKind}:${gpu.id}`,
        type: gpuKind,
        deviceIds: [gpu.id],
      })
    }
    // Enumeration can come back empty while the accelerator works; fall back to device count.
    if (env?.cudaAvailable && !(env.cudaDevices || []).length) {
      const count = Math.max(1, env.cudaDeviceCount || 1)
      for (let id = 0; id < count; id += 1) {
        options.push({ label: `${gpuName} ${id}`, value: `${gpuKind}:${id}`, type: gpuKind, deviceIds: [id] })
      }
    }
    if (env?.mlxAvailable || env?.mpsAvailable) options.push({ label: 'Apple MLX', value: 'mlx', type: 'mlx', deviceIds: [0] })
    if (env?.mpsAvailable) options.push({ label: 'Apple MPS', value: 'mps', type: 'mps', deviceIds: [0] })
    return options
  }

  function getRuntimeDeviceConfig(env?: EnvInfo | null): RuntimeDeviceConfig {
    const selected = defaultDevice.value
    // Both prefixes resolve to 'cuda' — that is the device API a ROCm torch build speaks.
    const gpuPrefix = ['cuda:', 'rocm:'].find((prefix) => selected.startsWith(prefix))
    if (gpuPrefix) {
      const id = parseInt(selected.slice(gpuPrefix.length), 10)
      return { device: 'cuda', deviceIds: Number.isFinite(id) ? [id] : [0] }
    }
    // Bare 'rocm' is what settings saved before ROCm gained per-device values.
    if (selected === 'cuda' || selected === 'rocm') return { device: 'cuda', deviceIds: [0] }
    if (selected === 'auto' && env?.mlxAvailable) {
      return { device: 'mlx', deviceIds: [0] }
    }
    if (selected === 'cpu' || selected === 'mps' || selected === 'mlx' || selected === 'auto') {
      return { device: selected, deviceIds: [0] }
    }
    return { device: 'auto', deviceIds: [0] }
  }

  function getAudioParams(): Record<string, string | number> {
    return {
      wav_bit_depth: wavBitDepth.value,
      flac_bit_depth: flacBitDepth.value,
      mp3_bit_rate: mp3BitRate.value,
      m4a_bit_rate: m4aBitRate.value,
      m4a_codec: m4aCodec.value,
    }
  }

  async function pickModelDir() {
    return invoke<string | null>('pick_output_folder')
  }

  async function pickOutputDir() {
    const folder = await invoke<string | null>('pick_output_folder')
    if (folder) outputDir.value = folder
  }

  async function pickFolder() {
    return invoke<string | null>('pick_output_folder')
  }

  async function refreshModelDataAfterDirChange() {
    const { useModelStore } = await import('@/stores/model')
    const modelStore = useModelStore()
    try {
      await modelStore.loadModels()
    } catch (error) {
      console.warn('Failed to refresh models after model directory change', error)
    }
    try {
      await modelStore.loadModelStorageSummary({ force: true })
    } catch (error) {
      console.warn('Failed to refresh model storage after model directory change', error)
    }
    const selectedModelName = modelStore.selectedModel
    if (selectedModelName) {
      const nextSelected = modelStore.models.find((item) => item.name === selectedModelName) || null
      modelStore.selectedInfo = nextSelected
      if (nextSelected) {
        await modelStore.selectModel(selectedModelName).catch(() => {})
      }
    } else {
      modelStore.selectedInfo = null
    }
  }

  async function applyModelDirWithoutMigration(targetModelDir: string) {
    const previousModelDir = modelDir.value
    modelDir.value = displayModelDirPath(targetModelDir)
    try {
      await refreshModelDataAfterDirChange()
      return { targetModelDir }
    } catch (error) {
      modelDir.value = previousModelDir
      await refreshModelDataAfterDirChange().catch(() => {})
      throw error
    }
  }

  async function prepareModelDirChange(targetDir: string) {
    const payload = normalizePrepareModelDirChangePayload(await invoke<PrepareModelDirChangePayload>('prepare_model_dir_change', {
      payload: {
        currentModelDir: modelDir.value,
        targetModelDir: targetDir,
      },
    }))

    if (payload.sameAsCurrent) {
      return { outcome: 'noop' as const, payload }
    }

    if (payload.fileCount <= 0) {
      await applyModelDirWithoutMigration(payload.targetModelDir)
      return { outcome: 'switched' as const, payload }
    }

    modelDirMigrationState.value = {
      ...createEmptyModelDirMigrationState(),
      status: 'confirm',
      sourceModelDir: payload.currentModelDir,
      targetModelDir: payload.targetModelDir,
      previousModelDir: modelDir.value,
      totalFiles: payload.fileCount,
      totalBytes: payload.totalBytes,
      preparation: payload,
      message: '',
    }
    return { outcome: 'confirm' as const, payload }
  }

  function cancelModelDirChangeConfirmation() {
    modelDirMigrationState.value = createEmptyModelDirMigrationState()
  }

  async function confirmModelDirMigration() {
    const preparation = modelDirMigrationState.value.preparation
    if (!preparation) {
      throw new Error('No model directory migration is pending confirmation')
    }
    const taskId = `model_dir_migration_${Date.now()}`
    modelDirMigrationState.value = {
      ...modelDirMigrationState.value,
      status: 'running',
      taskId,
      sourceModelDir: preparation.currentModelDir,
      targetModelDir: preparation.targetModelDir,
      previousModelDir: modelDir.value,
      totalFiles: preparation.fileCount,
      totalBytes: preparation.totalBytes,
      conflict: null,
      error: '',
      message: '正在复制模型目录',
      resolvingConflict: false,
    }
    try {
      await invoke('start_model_dir_migration', {
        payload: {
          taskId,
          currentModelDir: preparation.currentModelDir,
          targetModelDir: preparation.targetModelDir,
        },
      })
    } catch (error) {
      modelDirMigrationState.value = {
        ...modelDirMigrationState.value,
        status: 'confirm',
        taskId: '',
        message: '',
      }
      throw error
    }
  }

  async function resolveModelDirConflict(resolution: ModelDirMigrationResolution) {
    const taskId = modelDirMigrationState.value.taskId
    if (!taskId) {
      throw new Error('No model directory migration is waiting for conflict resolution')
    }
    modelDirMigrationState.value = {
      ...modelDirMigrationState.value,
      resolvingConflict: true,
    }
    try {
      await invoke('respond_model_dir_migration_conflict', {
        payload: {
          taskId,
          resolution,
        },
      })
      modelDirMigrationState.value = {
        ...modelDirMigrationState.value,
        status: 'running',
        conflict: null,
        resolvingConflict: false,
        message: resolution === 'overwrite'
          ? '已选择覆盖同名文件，正在继续复制'
          : resolution === 'skip'
            ? '已选择跳过同名文件，正在继续复制'
            : '正在终止模型目录迁移',
      }
    } catch (error) {
      modelDirMigrationState.value = {
        ...modelDirMigrationState.value,
        resolvingConflict: false,
      }
      throw error
    }
  }

  async function finalizeModelDirSwitch(taskId: string, payload: any) {
    const previousModelDir = modelDirMigrationState.value.previousModelDir || modelDir.value
    const targetModelDir = displayModelDirPath(payload.targetModelDir || modelDirMigrationState.value.targetModelDir || '')
    modelDirMigrationState.value = {
      ...modelDirMigrationState.value,
      status: 'finalizing_cleanup',
      message: payload.message || '',
      currentPath: '',
      conflict: null,
      resolvingConflict: false,
      error: '',
    }
    modelDir.value = targetModelDir
    try {
      await refreshModelDataAfterDirChange()
      await invoke('confirm_model_dir_migration_switch', {
        payload: { taskId },
      })
    } catch (error) {
      await invoke('cancel_model_dir_migration', {
        payload: { taskId },
      }).catch(() => {})
      modelDir.value = previousModelDir
      await refreshModelDataAfterDirChange().catch(() => {})
      modelDirMigrationState.value = {
        ...modelDirMigrationState.value,
        status: 'failed',
        message: error instanceof Error ? error.message : String(error),
        error: error instanceof Error ? error.message : String(error),
        currentPath: '',
        resolvingConflict: false,
        conflict: null,
      }
    }
  }

  async function handleWorkerEvent(event: any) {
    const type = event?.type as string | undefined
    if (!type?.startsWith('model_dir_migration_')) return
    const payload = event?.payload || {}
    const taskId = String(event?.taskId || '')
    if (modelDirMigrationState.value.taskId && taskId && modelDirMigrationState.value.taskId !== taskId) return

    if (type === 'model_dir_migration_started') {
      modelDirMigrationState.value = {
        ...modelDirMigrationState.value,
        status: 'running',
        taskId,
        sourceModelDir: displayModelDirPath(payload.sourceModelDir || modelDirMigrationState.value.sourceModelDir),
        targetModelDir: displayModelDirPath(payload.targetModelDir || modelDirMigrationState.value.targetModelDir),
        totalFiles: Number(payload.totalFiles || modelDirMigrationState.value.totalFiles || 0),
        completedFiles: Number(payload.completedFiles || 0),
        totalBytes: Number(payload.totalBytes || modelDirMigrationState.value.totalBytes || 0),
        copiedBytes: Number(payload.copiedBytes || 0),
        currentPath: displayModelDirPath(payload.currentPath || ''),
        message: payload.message || '',
        conflict: null,
        error: '',
        resolvingConflict: false,
      }
      return
    }

    if (type === 'model_dir_migration_progress') {
      modelDirMigrationState.value = {
        ...modelDirMigrationState.value,
        status: 'running',
        taskId,
        completedFiles: Number(payload.completedFiles || 0),
        totalFiles: Number(payload.totalFiles || modelDirMigrationState.value.totalFiles || 0),
        totalBytes: Number(payload.totalBytes || modelDirMigrationState.value.totalBytes || 0),
        copiedBytes: Number(payload.copiedBytes || 0),
        currentPath: displayModelDirPath(payload.currentPath || ''),
        message: payload.message || '',
        skippedFiles: Array.isArray(payload.skippedFiles) ? payload.skippedFiles : modelDirMigrationState.value.skippedFiles,
        overwrittenFiles: Array.isArray(payload.overwrittenFiles) ? payload.overwrittenFiles : modelDirMigrationState.value.overwrittenFiles,
        conflict: null,
        resolvingConflict: false,
      }
      return
    }

    if (type === 'model_dir_migration_conflict') {
      modelDirMigrationState.value = {
        ...modelDirMigrationState.value,
        status: 'conflict',
        taskId,
        completedFiles: Number(payload.completedFiles || modelDirMigrationState.value.completedFiles || 0),
        totalFiles: Number(payload.totalFiles || modelDirMigrationState.value.totalFiles || 0),
        totalBytes: Number(payload.totalBytes || modelDirMigrationState.value.totalBytes || 0),
        copiedBytes: Number(payload.copiedBytes || modelDirMigrationState.value.copiedBytes || 0),
        currentPath: displayModelDirPath(payload.currentPath || ''),
        message: payload.message || '',
        conflict: payload.conflict ? { ...payload.conflict, sourcePath: displayModelDirPath(payload.conflict.sourcePath), destinationPath: displayModelDirPath(payload.conflict.destinationPath) } : null,
        resolvingConflict: false,
      }
      return
    }

    if (type === 'model_dir_migration_ready_to_switch') {
      modelDirMigrationState.value = {
        ...modelDirMigrationState.value,
        status: 'ready_to_switch',
        taskId,
        completedFiles: Number(payload.completedFiles || modelDirMigrationState.value.totalFiles || 0),
        totalFiles: Number(payload.totalFiles || modelDirMigrationState.value.totalFiles || 0),
        totalBytes: Number(payload.totalBytes || modelDirMigrationState.value.totalBytes || 0),
        copiedBytes: Number(payload.copiedBytes || modelDirMigrationState.value.totalBytes || 0),
        currentPath: '',
        message: payload.message || '',
        skippedFiles: Array.isArray(payload.skippedFiles) ? payload.skippedFiles : modelDirMigrationState.value.skippedFiles,
        overwrittenFiles: Array.isArray(payload.overwrittenFiles) ? payload.overwrittenFiles : modelDirMigrationState.value.overwrittenFiles,
        conflict: null,
        resolvingConflict: false,
      }
      await finalizeModelDirSwitch(taskId, payload)
      return
    }

    if (type === 'model_dir_migration_done') {
      modelDirMigrationState.value = {
        ...modelDirMigrationState.value,
        status: 'success',
        taskId,
        sourceModelDir: displayModelDirPath(payload.sourceModelDir || modelDirMigrationState.value.sourceModelDir),
        targetModelDir: displayModelDirPath(payload.targetModelDir || modelDirMigrationState.value.targetModelDir),
        completedFiles: Number(payload.completedFiles || modelDirMigrationState.value.completedFiles || 0),
        totalFiles: Number(payload.totalFiles || modelDirMigrationState.value.totalFiles || 0),
        totalBytes: Number(payload.totalBytes || modelDirMigrationState.value.totalBytes || 0),
        copiedBytes: Number(payload.copiedBytes || modelDirMigrationState.value.copiedBytes || 0),
        currentPath: '',
        message: payload.message || '',
        skippedFiles: Array.isArray(payload.skippedFiles) ? payload.skippedFiles : [],
        overwrittenFiles: Array.isArray(payload.overwrittenFiles) ? payload.overwrittenFiles : [],
        cleanupFailedFiles: Array.isArray(payload.cleanupFailedFiles) ? payload.cleanupFailedFiles : [],
        conflict: null,
        error: '',
        resolvingConflict: false,
      }
      modelDir.value = displayModelDirPath(payload.targetModelDir || modelDirMigrationState.value.targetModelDir)
      // Custom model registrations store absolute paths, so anything that lived under the old
      // model directory now points at files the migration has moved away. Rebase them.
      // Fire-and-forget: a failure here leaves those models showing as missing with a relink
      // action, which must not turn a completed migration into a failed one.
      if (payload.sourceModelDir && payload.targetModelDir) {
        void invoke('remap_custom_model_paths', {
          payload: { fromRoot: payload.sourceModelDir, toRoot: payload.targetModelDir },
        }).then(() => import('@/stores/model'))
          .then(({ useModelStore }) => useModelStore().loadModels())
          .catch(() => {})
      }
      return
    }

    if (type === 'model_dir_migration_failed') {
      modelDirMigrationState.value = {
        ...modelDirMigrationState.value,
        status: 'failed',
        taskId,
        currentPath: '',
        message: payload.message || '',
        error: payload.message || '',
        conflict: null,
        resolvingConflict: false,
      }
      return
    }

    if (type === 'model_dir_migration_aborted') {
      modelDirMigrationState.value = {
        ...modelDirMigrationState.value,
        status: 'aborted',
        taskId,
        currentPath: '',
        message: payload.message || '',
        error: '',
        conflict: null,
        resolvingConflict: false,
      }
    }
  }

  function clearModelDirMigrationState() {
    modelDirMigrationState.value = createEmptyModelDirMigrationState()
  }

  async function markStartupOnboardingCompleted() {
    startupOnboardingSeen.value = true
    await saveAppStore('app-settings', persistable.value)
  }

  return {
    initialized,
    appPaths,
    dataRoot,
    settingsDir,
    editorProjectsDir,
    logsDir,
    tempDir,
    isModelDirMigrating,
    shouldShowStartupOnboarding,
    themeMode,
    themeAccent,
    scaleFactor,
    locale,
    animationsEnabled,
    developerMode,
    modelDir,
    outputDir,
    defaultDevice,
    defaultFormat,
    downloadSource,
    downloadMethod,
    maxConcurrentSeparations,
    clearInputAfterSubmit,
    MAX_CONCURRENT_SEPARATIONS,
    SCALE_FACTOR_MIN,
    SCALE_FACTOR_MAX,
    SCALE_FACTOR_STEP,
    wavBitDepth,
    flacBitDepth,
    mp3BitRate,
    m4aBitRate,
    m4aCodec,
    proxyMode,
    proxyUrl,
    proxyBypass,
    updateChannel,
    updateEndpointOverride,
    startupOnboardingSeen,
    modelDirMigrationState,
    initialize,
    markStartupOnboardingCompleted,
    pickModelDir,
    pickOutputDir,
    pickFolder,
    prepareModelDirChange,
    cancelModelDirChangeConfirmation,
    confirmModelDirMigration,
    resolveModelDirConflict,
    clearModelDirMigrationState,
    handleWorkerEvent,
    deviceOptions,
    getRuntimeDeviceConfig,
    getAudioParams,
  }
})
