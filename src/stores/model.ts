import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { isTauriRuntime, loadAppStore, saveAppStore } from '@/utils/appStore'
import { matchesModelQuery } from '@/utils/modelSearch'
import { MODEL_LIBRARY_PAGE_SIZES, normalizePageSize } from '@/utils/pagination'
import { matchesModelSource, type ModelSourceFilter } from '@/utils/modelSource'
import { useAppStore } from '@/stores/app'
import { registerWindowCloseGuard } from '@/utils/windowCloseGuards'
import { inferenceChunkSizeIssue, normalizeInferenceParamMeta, type InferenceParamMeta } from '@/features/inference/sampleStep'

/** How the model library lays its entries out. */
export type ModelViewMode = 'card' | 'list'
import { useSettingsStore } from '@/stores/settings'

export type ModelEntry = {
  name: string
  aliases: string[]
  modelType: string | null
  architecture: string
  supported: boolean
  unsupportedReason: string
  category: string
  categoryCn: string
  primaryCategory: string
  primaryCategoryCn: string
  secondaryCategory: string
  secondaryCategoryCn: string
  targetStem: string
  configInstruments: string
  configTargetInstrument: string
  classificationConfidence: string
  classificationBasis: string
  sizeBytes: number
  sha256: string
  downloaded: boolean
  missingPaths: string[]
  modelPath: string
  configPath: string | null
  auxiliaryPaths: string[]
  /**
   * Where the model came from. 'user' models were imported from local files and are local-only:
   * pymss refuses to download them, and their weights may live anywhere on disk, so they offer
   * relink/remove where a catalog model offers download/delete.
   * Absent on entries restored from an older persisted cache — treat a missing value as 'catalog'.
   */
  source?: 'catalog' | 'debug' | 'user'
  baseConfigPath?: string | null
  /**
   * How an imported model got here — 'copy' means the app owns its files and removing it deletes
   * them. Null for catalog models. Models registered outside the app read as 'reference', the
   * mode under which nothing is deleted.
   */
  importMode?: 'copy' | 'reference' | null
  defaultInferenceParams?: ModelDefaultInferenceParams
  defaultInferenceParamsResolved?: boolean
  defaultInferenceParamsSource?: 'config' | 'runtime_fallback'
  inferenceParamMeta?: InferenceParamMeta
}

/** One architecture the worker thinks a weights file might be, with why it thinks so. */
export type CustomModelSuggestion = {
  modelType: string
  confidence: 'high' | 'medium' | 'low'
  /** Translated by the UI — the worker must not emit prose it cannot localise. */
  basisCode: 'config_model_key' | 'config_kwargs_section' | 'state_dict_key' | string
  basisDetail: string
}

export type CustomModelInspection = {
  modelPath: string
  configPath: string | null
  sizeBytes: number
  suggestions: CustomModelSuggestion[]
  suggestedModelType: string | null
  suggestedName: string
  instruments: string[]
  targetInstrument: string
  knownModelTypes: string[]
  configOptionalModelTypes: string[]
  /** Null when nothing could be suggested — there is then no type to require a config for. */
  configRequired: boolean | null
  stateDictReadable: boolean
  stateDictError: string | null
}

export type CustomModelImportRequest = {
  name: string
  modelType: string
  modelPath: string
  configPath?: string | null
  aliases?: string[]
  /** 'reference' registers the file where it is; 'copy' puts it under app management. */
  importMode?: 'reference' | 'copy'
  verify?: boolean
  force?: boolean
}

export type CustomModelImportState = {
  taskId: string
  name: string
  status: 'idle' | 'importing' | 'success' | 'error' | 'cancelled'
  stage: string
  progress: number
  message: string
}

export type CustomModelRemoval = {
  name: string
  filesDeleted: string[]
  deletedFiles: boolean
  /** False for referenced models, whose weights the app must never delete. */
  fileDeletionSupported: boolean
  errors: string[]
}

export type ModelDefaultInferenceParams = {
  batch_size?: number
  overlap_size?: number
  num_overlap?: number
  chunk_size?: number
  standardize?: boolean
  normalize?: boolean
  window_size?: number
  aggression?: number
  enable_post_process?: boolean
  post_process_threshold?: number
  high_end_process?: boolean
}

type ModelsPayload = {
  models: ModelEntry[]
  categories: string[]
  categoriesCn: string[]
  count: number
  modelDir: string
  debugStatus?: ModelDebugStatus
}

export type ModelDebugStatus = {
  active: boolean
  catalogActive: boolean
  changedCount: number
  addedCount: number
  removedCount: number
  changedModels?: string[]
  addedModels?: string[]
  removedModels?: string[]
  debugDir?: string
  debugCatalogPath?: string
}

type DownloadStatus = 'idle' | 'preparing' | 'downloading' | 'done' | 'error' | 'cancelled' | 'paused' | 'interrupted'

export type DownloadLogLevel = 'info' | 'warn' | 'error'

export type DownloadLogEntry = {
  ts: number
  level: DownloadLogLevel
  message: string
  fileIndex?: number
  totalFiles?: number
  stage?: string
}

export type DownloadTask = {
  taskId: string
  model: string
  status: DownloadStatus
  progress: number
  message: string
  completedFiles: number
  totalFiles: number
  updatedAt: number
  logs: DownloadLogEntry[]
  errorMessage?: string
  seen?: boolean
  /**
   * Bytes across every file of the model, and the current rate. The worker has always reported
   * these; they were simply not read, so progress could only ever be shown as a percentage.
   */
  downloadedBytes?: number
  totalBytes?: number
  speedBytesPerSecond?: number
}

export type ModelStorageFile = { path: string; sizeBytes: number; exists?: boolean }
export type ModelStorageItem = {
  name: string
  downloaded: boolean
  sizeBytes: number
  expectedSizeBytes: number
  files: ModelStorageFile[]
}

export type ToolModelStorageItem = {
  id: string
  name: string
  tool: 'asr'
  role: 'recognition' | 'vad' | 'punctuation'
  path: string
  sizeBytes: number
  fileCount: number
}

export type ModelStorageDeleteTarget = {
  kind: 'catalog' | 'tool'
  id: string
  name: string
  tool?: ToolModelStorageItem['tool']
}

export type ModelStorageSummary = {
  modelDir: string
  totalBytes: number
  toolModelsBytes?: number
  downloadedCount: number
  models: ModelStorageItem[]
  toolModels: ToolModelStorageItem[]
  residualFiles: ModelStorageFile[]
  residualBytes: number
}

export type ModelPreference = {
  favorite?: boolean
  note?: string
  useCount?: number
  lastUsedAt?: number
  updatedAt?: number
}

export type DeleteTaskStatus = 'deleting' | 'done' | 'error' | 'cancelled'

export type DeleteTask = {
  taskId: string
  model: string
  status: DeleteTaskStatus
  progress: number
  message: string
  completedFiles: number
  totalFiles: number
  updatedAt: number
  source?: 'single' | 'batch'
  resultModelInfo?: ModelEntry | null
}

export type BatchDeleteState = {
  active: boolean
  totalModels: number
  completedModels: number
  currentModel: string
  failedModels: string[]
}

export type ResidualCleanupState = {
  taskId: string
  active: boolean
  status: DeleteTaskStatus | 'idle'
  progress: number
  message: string
  completedFiles: number
  totalFiles: number
  updatedAt: number
  notified?: boolean
}

const DELETE_TASK_TIMEOUT_MS = 5 * 60 * 1000
const STORAGE_SUMMARY_CACHE_TTL_MS = 30 * 1000
const DOWNLOAD_LOG_LIMIT = 200

function appendLog(logs: DownloadLogEntry[], entry: DownloadLogEntry): DownloadLogEntry[] {
  const next = [...logs, entry]
  return next.length > DOWNLOAD_LOG_LIMIT ? next.slice(next.length - DOWNLOAD_LOG_LIMIT) : next
}

type StoredModelState = {
  models?: ModelEntry[]
  categories?: string[]
  categoriesCn?: string[]
  count?: number
  modelDir?: string
  downloadTasks?: Record<string, DownloadTask>
  modelInferenceOverrides?: Record<string, ModelDefaultInferenceParams>
  modelInferenceBaseDefaults?: Record<string, ModelDefaultInferenceParams>
  modelInferenceBaseKnown?: Record<string, boolean>
  modelInferenceBaseSources?: Record<string, ModelEntry['defaultInferenceParamsSource']>
  modelPreferences?: Record<string, ModelPreference>
  modelViewMode?: ModelViewMode
  modelPageSize?: number
}

function normalizeDownloadTasks(input?: Record<string, DownloadTask>) {
  const next: Record<string, DownloadTask> = {}
  Object.entries(input || {}).forEach(([name, task]) => {
    if (!task?.model) return
    next[name] = {
      ...task,
      status: task.status === 'downloading' || task.status === 'preparing' ? 'interrupted' : task.status,
      message: task.status === 'downloading' || task.status === 'preparing' ? '下载已中断' : task.message,
      updatedAt: Date.now(),
      logs: Array.isArray(task.logs) ? task.logs : [],
      seen: true,
    }
  })
  return next
}

function normalizeModelInferenceOverrides(input?: Record<string, ModelDefaultInferenceParams>) {
  const next: Record<string, ModelDefaultInferenceParams> = {}
  Object.entries(input || {}).forEach(([name, value]) => {
    const normalized = normalizeDefaultInferenceParams(value as Record<string, unknown>)
    if (normalized) next[name] = normalized
  })
  return next
}

function normalizeModelPreferences(input?: Record<string, ModelPreference>) {
  const next: Record<string, ModelPreference> = {}
  Object.entries(input || {}).forEach(([name, value]) => {
    if (!name || !value || typeof value !== 'object') return
    const note = typeof value.note === 'string' ? value.note : ''
    const favorite = Boolean(value.favorite)
    const useCount = typeof value.useCount === 'number' && Number.isFinite(value.useCount)
      ? Math.max(0, Math.trunc(value.useCount))
      : 0
    const lastUsedAt = typeof value.lastUsedAt === 'number' && Number.isFinite(value.lastUsedAt)
      ? Math.max(0, value.lastUsedAt)
      : 0
    if (!favorite && !note.trim() && !useCount && !lastUsedAt) return
    next[name] = {
      favorite,
      note,
      useCount,
      lastUsedAt,
      updatedAt: typeof value.updatedAt === 'number' && Number.isFinite(value.updatedAt) ? value.updatedAt : Date.now(),
    }
  })
  return next
}

function normalizeDefaultInferenceParams(input?: Record<string, unknown> | null): ModelDefaultInferenceParams | undefined {
  if (!input || typeof input !== 'object') return undefined

  const next: Partial<ModelDefaultInferenceParams> = {}
  const source = input as Record<string, unknown>

  const assignNumber = (targetKey: keyof ModelDefaultInferenceParams, ...candidateKeys: string[]) => {
    for (const candidateKey of candidateKeys) {
      const value = source[candidateKey]
      if (typeof value === 'number' && Number.isFinite(value)) {
        ;(next as Record<string, number | boolean | undefined>)[targetKey] = value
        return
      }
    }
  }

  const assignBoolean = (targetKey: keyof ModelDefaultInferenceParams, ...candidateKeys: string[]) => {
    for (const candidateKey of candidateKeys) {
      const value = source[candidateKey]
      if (typeof value === 'boolean') {
        ;(next as Record<string, number | boolean | undefined>)[targetKey] = value
        return
      }
    }
  }

  assignNumber('batch_size', 'batch_size', 'batchSize')
  assignNumber('overlap_size', 'overlap_size', 'overlapSize')
  assignNumber('num_overlap', 'num_overlap', 'numOverlap')
  assignNumber('chunk_size', 'chunk_size', 'chunkSize')
  assignBoolean('standardize', 'standardize')
  assignBoolean('normalize', 'normalize')
  assignNumber('window_size', 'window_size', 'windowSize')
  assignNumber('aggression', 'aggression')
  assignBoolean('enable_post_process', 'enable_post_process', 'enablePostProcess')
  assignNumber('post_process_threshold', 'post_process_threshold', 'postProcessThreshold')
  assignBoolean('high_end_process', 'high_end_process', 'highEndProcess')

  return Object.keys(next).length ? next : undefined
}

function normalizeModelEntry(model: ModelEntry): ModelEntry {
  const rawDefaults = model.defaultInferenceParams as Record<string, unknown> | undefined
  return {
    ...model,
    defaultInferenceParams: normalizeDefaultInferenceParams(rawDefaults),
    defaultInferenceParamsResolved: rawDefaults !== undefined,
    inferenceParamMeta: normalizeInferenceParamMeta(model.inferenceParamMeta),
  }
}

function mergeDefaultInferenceParams(
  defaults: ModelDefaultInferenceParams | undefined,
  overrides: ModelDefaultInferenceParams | undefined,
) {
  return normalizeDefaultInferenceParams({
    ...(defaults || {}),
    ...(overrides || {}),
  } as Record<string, unknown>)
}

type NormalizeModelEntryOptions = {
  rememberBase?: boolean
}

/**
 * 后端 worker 事件信封。payload 字段随 type 高度多变（下载/删除/清理各阶段），
 * 故保持宽松结构，仅收敛顶层信封以移除隐式 any。
 */
export type WorkerEvent = {
  type?: string
  taskId?: string
  payload?: Record<string, any>
}

export const useModelStore = defineStore('model', () => {
  const initialized = ref(false)
  const models = ref<ModelEntry[]>([])
  const categories = ref<string[]>([])
  const categoriesCn = ref<string[]>([])
  const modelDir = ref('')
  const debugStatus = ref<ModelDebugStatus | null>(null)
  const selectedModel = ref('bs_roformer_voc_hyperacev2')
  const selectedInfo = ref<ModelEntry | null>(null)
  const isLoading = ref(false)
  const modelsLoaded = ref(false)
  const detailLoading = ref(false)
  const error = ref<string | null>(null)
  const search = ref('')
  const supportedOnly = ref(true)
  const category = ref('')
  /**
   * Which half of the library to show. Session-only, like the other filters.
   * The category filter can already single out `user/custom`, but it cannot express
   * "everything except imported", and it buries the choice among a dozen categories.
   */
  const modelSource = ref<ModelSourceFilter>('all')
  /**
   * Card or list. Persisted rather than session-only: it is a standing preference about how the
   * library reads, not a filter someone re-picks per visit.
   */
  const modelViewMode = ref<ModelViewMode>('list')
  const modelPageSize = ref(24)
  const downloadStates = ref<Record<string, DownloadStatus>>({})
  const downloadErrors = ref<Record<string, string>>({})
  const downloadTasks = ref<Record<string, DownloadTask>>({})
  const modelInferenceOverrides = ref<Record<string, ModelDefaultInferenceParams>>({})
  const modelInferenceBaseDefaults = ref<Record<string, ModelDefaultInferenceParams>>({})
  const modelInferenceBaseKnown = ref<Record<string, boolean>>({})
  const modelInferenceBaseSources = ref<Record<string, ModelEntry['defaultInferenceParamsSource']>>({})
  const modelPreferences = ref<Record<string, ModelPreference>>({})
  const downloadTaskIndex = ref<Record<string, string>>({})
  const deleteTasks = ref<Record<string, DeleteTask>>({})
  const deleteTaskIndex = ref<Record<string, string>>({})
  const modelStorageSummary = ref<ModelStorageSummary | null>(null)
  const modelStorageSummaryLoadedAt = ref(0)
  const storageLoading = ref(false)
  const batchDeleteState = ref<BatchDeleteState>({
    active: false,
    totalModels: 0,
    completedModels: 0,
    currentModel: '',
    failedModels: [],
  })
  const residualCleanupState = ref<ResidualCleanupState>({
    taskId: '',
    active: false,
    status: 'idle',
    progress: 0,
    message: '',
    completedFiles: 0,
    totalFiles: 0,
    updatedAt: 0,
    notified: false,
  })
  const customImportState = ref<CustomModelImportState>({
    taskId: '',
    name: '',
    status: 'idle',
    stage: '',
    progress: 0,
    message: '',
  })

  let saveTimer: ReturnType<typeof setTimeout> | null = null

  const filteredModels = computed(() => {
    const q = search.value.trim().toLowerCase()
    return models.value.filter((model) => {
      const matchesQuery = matchesModelQuery(model, q, modelPreferences.value[model.name]?.note || '')
      const selectedCategory = category.value.trim().toLowerCase()
      const matchesCategory = !selectedCategory
        || model.category.toLowerCase() === selectedCategory
        || model.primaryCategory.toLowerCase() === selectedCategory
        || model.secondaryCategory.toLowerCase() === selectedCategory
      const matchesSupported = !supportedOnly.value || model.supported
      return matchesQuery && matchesCategory && matchesSupported && matchesModelSource(model, modelSource.value)
    })
  })

  const customModelCount = computed(() => models.value.filter((model) => model.source === 'user').length)
  const debugModelCount = computed(() => models.value.filter((model) => model.source === 'debug').length)
  const downloadedModels = computed(() => models.value.filter((model) => model.supported && model.downloaded))

  async function persistState() {
    if (!initialized.value) return
    await saveAppStore('model-state', {
      models: models.value,
      categories: categories.value,
      categoriesCn: categoriesCn.value,
      count: models.value.length,
      modelDir: modelDir.value,
      downloadTasks: downloadTasks.value,
      modelInferenceOverrides: modelInferenceOverrides.value,
      modelInferenceBaseDefaults: modelInferenceBaseDefaults.value,
      modelInferenceBaseKnown: modelInferenceBaseKnown.value,
      modelInferenceBaseSources: modelInferenceBaseSources.value,
      modelPreferences: modelPreferences.value,
      modelViewMode: modelViewMode.value,
      modelPageSize: modelPageSize.value,
    } satisfies StoredModelState)
  }

  function queuePersist() {
    if (!initialized.value) return
    if (saveTimer) clearTimeout(saveTimer)
    saveTimer = setTimeout(() => {
      saveTimer = null
      void persistState()
    }, 120)
  }

  async function flushPersistState() {
    if (saveTimer) {
      clearTimeout(saveTimer)
      saveTimer = null
    }
    await persistState()
  }

  async function initialize() {
    if (initialized.value) return
    const stored = await loadAppStore<StoredModelState>('model-state')
    modelInferenceOverrides.value = normalizeModelInferenceOverrides(stored?.modelInferenceOverrides)
    modelInferenceBaseDefaults.value = normalizeModelInferenceOverrides(stored?.modelInferenceBaseDefaults)
    modelInferenceBaseKnown.value = {
      ...(stored?.modelInferenceBaseKnown || {}),
      ...Object.fromEntries(Object.keys(modelInferenceBaseDefaults.value).map((name) => [name, true])),
    }
    modelInferenceBaseSources.value = stored?.modelInferenceBaseSources || {}
    modelPreferences.value = normalizeModelPreferences(stored?.modelPreferences)
    // Anything unrecognised falls back to the list, which stays readable at any width.
    modelViewMode.value = stored?.modelViewMode === 'card' ? 'card' : 'list'
    modelPageSize.value = normalizePageSize(stored?.modelPageSize, MODEL_LIBRARY_PAGE_SIZES, 24)
    if (stored?.models?.length) {
      models.value = stored.models.map((model) => normalizeModelEntryWithOverrides(model, {
        rememberBase: !modelInferenceOverrides.value[model.name],
      }))
      categories.value = stored.categories || []
      categoriesCn.value = stored.categoriesCn || []
      modelDir.value = stored.modelDir || ''
    }
    downloadTasks.value = normalizeDownloadTasks(stored?.downloadTasks)
    Object.values(downloadTasks.value).forEach((task) => {
      downloadTaskIndex.value[task.taskId] = task.model
      if (task.status === 'downloading') downloadStates.value[task.model] = 'downloading'
      if (task.status === 'error') downloadStates.value[task.model] = 'error'
    })
    initialized.value = true
  }

  watch(downloadTasks, () => queuePersist(), { deep: true })
  watch(modelViewMode, () => queuePersist())
  watch(modelPageSize, () => queuePersist())
  watch(supportedOnly, () => {
    if (selectedInfo.value && !filteredModels.value.some((item) => item.name === selectedInfo.value?.name)) {
      selectedInfo.value = null
    }
  })

  function persistModelCache() {
    queuePersist()
  }

  function rememberModelInferenceBase(
    name: string,
    defaults: ModelDefaultInferenceParams | undefined,
    source: ModelEntry['defaultInferenceParamsSource'],
  ) {
    if (!defaults && !source) return
    const previousSource = modelInferenceBaseSources.value[name]
    const canUpgradeFallback = previousSource === 'runtime_fallback' && source === 'config'
    const canFillMissingDefaults = Boolean(!modelInferenceBaseDefaults.value[name] && defaults)
    if (modelInferenceBaseKnown.value[name] && !canUpgradeFallback && !canFillMissingDefaults) return
    modelInferenceBaseKnown.value = { ...modelInferenceBaseKnown.value, [name]: true }
    modelInferenceBaseSources.value = { ...modelInferenceBaseSources.value, [name]: source }
    if (defaults) {
      modelInferenceBaseDefaults.value = { ...modelInferenceBaseDefaults.value, [name]: { ...defaults } }
    } else if (modelInferenceBaseDefaults.value[name]) {
      const { [name]: _, ...rest } = modelInferenceBaseDefaults.value
      modelInferenceBaseDefaults.value = rest
    }
  }

  function getModelBaseInferenceDefaults(name: string) {
    return modelInferenceBaseDefaults.value[name]
  }

  function hasKnownModelInferenceBase(name: string) {
    return Boolean(modelInferenceBaseKnown.value[name])
  }

  function getKnownModelInferenceBase(name: string, fallback: ModelDefaultInferenceParams | undefined) {
    return modelInferenceBaseKnown.value[name] ? modelInferenceBaseDefaults.value[name] : fallback
  }

  function normalizeModelEntryWithOverrides(model: ModelEntry, options: NormalizeModelEntryOptions = {}) {
    const normalized = normalizeModelEntry(model)
    if (options.rememberBase !== false) {
      rememberModelInferenceBase(
        normalized.name,
        normalized.defaultInferenceParams,
        normalized.defaultInferenceParamsSource,
      )
    }
    const baseDefaults = getKnownModelInferenceBase(normalized.name, normalized.defaultInferenceParams)
    const overrides = modelInferenceOverrides.value[normalized.name]
    if (!overrides) {
      return {
        ...normalized,
        defaultInferenceParams: baseDefaults,
        defaultInferenceParamsResolved: normalized.defaultInferenceParamsResolved || Boolean(baseDefaults),
      }
    }
    return {
      ...normalized,
      defaultInferenceParams: mergeDefaultInferenceParams(baseDefaults, overrides),
      defaultInferenceParamsResolved: true,
    }
  }

  function refreshModelInferenceOverrides(name: string) {
    const index = models.value.findIndex((item) => item.name === name)
    if (index >= 0) models.value[index] = normalizeModelEntryWithOverrides(models.value[index], { rememberBase: false })
    if (selectedInfo.value?.name === name) selectedInfo.value = normalizeModelEntryWithOverrides(selectedInfo.value, { rememberBase: false })
  }

  async function setModelInferenceOverrides(name: string, overrides: ModelDefaultInferenceParams) {
    const normalized = normalizeDefaultInferenceParams({
      ...(modelInferenceOverrides.value[name] || {}),
      ...overrides,
    } as Record<string, unknown>)
    if (!normalized) return
    const entry = selectedInfo.value?.name === name ? selectedInfo.value : models.value.find(item => item.name === name)
    const chunkIssue = inferenceChunkSizeIssue(normalized.chunk_size, entry?.inferenceParamMeta)
    if (chunkIssue) throw new Error(`Invalid chunk size. Use ${chunkIssue.values} samples.`)
    const previousOverrides = modelInferenceOverrides.value
    modelInferenceOverrides.value = {
      ...modelInferenceOverrides.value,
      [name]: normalized,
    }
    refreshModelInferenceOverrides(name)
    try {
      await flushPersistState()
    } catch (error) {
      modelInferenceOverrides.value = previousOverrides
      refreshModelInferenceOverrides(name)
      throw error
    }
  }

  async function resetModelInferenceOverrides(name: string) {
    if (!modelInferenceOverrides.value[name]) return
    const previousOverrides = modelInferenceOverrides.value
    const { [name]: _, ...rest } = modelInferenceOverrides.value
    modelInferenceOverrides.value = rest
    refreshModelInferenceOverrides(name)
    try {
      await flushPersistState()
    } catch (error) {
      modelInferenceOverrides.value = previousOverrides
      refreshModelInferenceOverrides(name)
      throw error
    }
  }

  function getModelInferenceOverrides(name: string) {
    return modelInferenceOverrides.value[name]
  }

  function getModelPreference(name: string) {
    return modelPreferences.value[name] || {}
  }

  function isModelFavorite(name: string) {
    return Boolean(modelPreferences.value[name]?.favorite)
  }

  function getModelNote(name: string) {
    return modelPreferences.value[name]?.note || ''
  }

  function getModelUseCount(name: string) {
    return modelPreferences.value[name]?.useCount || 0
  }

  function setModelPreference(name: string, patch: ModelPreference) {
    const previous = modelPreferences.value[name] || {}
    // Keep the note verbatim (including newlines) so the controlled textarea
    // does not fight the caret; only use a trimmed copy to decide emptiness.
    const note = typeof patch.note === 'string' ? patch.note : previous.note
    const next: ModelPreference = {
      ...previous,
      ...patch,
      note,
      useCount: typeof patch.useCount === 'number' && Number.isFinite(patch.useCount)
        ? Math.max(0, Math.trunc(patch.useCount))
        : previous.useCount,
      lastUsedAt: typeof patch.lastUsedAt === 'number' && Number.isFinite(patch.lastUsedAt)
        ? Math.max(0, patch.lastUsedAt)
        : previous.lastUsedAt,
      updatedAt: Date.now(),
    }
    if (!next.favorite && !(next.note || '').trim() && !next.useCount && !next.lastUsedAt) {
      const { [name]: _, ...rest } = modelPreferences.value
      modelPreferences.value = rest
    } else {
      modelPreferences.value = {
        ...modelPreferences.value,
        [name]: next,
      }
    }
    queuePersist()
  }

  function setModelFavorite(name: string, favorite: boolean) {
    setModelPreference(name, { favorite })
  }

  function toggleModelFavorite(name: string) {
    setModelFavorite(name, !isModelFavorite(name))
  }

  function setModelNote(name: string, note: string) {
    setModelPreference(name, { note })
  }

  function recordModelUse(name: string) {
    const key = String(name || '').trim()
    if (!key) return
    setModelPreference(key, {
      useCount: getModelUseCount(key) + 1,
      lastUsedAt: Date.now(),
    })
  }

  function isDeleteTaskTerminal(task?: DeleteTask | null) {
    return task?.status === 'done' || task?.status === 'error' || task?.status === 'cancelled'
  }

  function clearDeleteTaskIndex(taskId?: string | null) {
    if (!taskId || !deleteTaskIndex.value[taskId]) return
    const { [taskId]: _, ...rest } = deleteTaskIndex.value
    deleteTaskIndex.value = rest
  }

  function upsertModel(modelInfo: ModelEntry) {
    const normalizedModel = normalizeModelEntryWithOverrides(modelInfo)
    const index = models.value.findIndex((item) => item.name === modelInfo.name)
    if (index >= 0) models.value[index] = normalizedModel
    else models.value.push(normalizedModel)
    if (selectedModel.value === modelInfo.name) selectedInfo.value = normalizedModel
    persistModelCache()
  }

  async function loadModels() {
    const settings = useSettingsStore()
    isLoading.value = true
    error.value = null
    try {
      const result = await invoke<ModelsPayload>('list_models', {
        payload: {
          category: null,
          supportedOnly: false,
          includeLocalState: true,
          includeCustom: true,
          modelDir: settings.modelDir || null,
        },
      })
      models.value = result.models.map((model) => normalizeModelEntryWithOverrides(model))
      categories.value = result.categories
      categoriesCn.value = result.categoriesCn
      modelDir.value = result.modelDir
      debugStatus.value = result.debugStatus || null
      modelsLoaded.value = true
      const nextSelected = models.value.find((model) => model.name === selectedModel.value) || null
      if (nextSelected) selectedInfo.value = nextSelected
      else if (selectedInfo.value?.name === selectedModel.value) selectedInfo.value = null
      persistModelCache()
      const firstDownloaded = models.value.find((model) => model.supported && model.downloaded)
      if (!models.value.some((model) => model.name === selectedModel.value && model.downloaded)) {
        selectedModel.value = firstDownloaded?.name || ''
      }
    } catch (err) {
      error.value = err instanceof Error ? err.message : String(err)
      throw err
    } finally {
      isLoading.value = false
    }
  }

  function selectModel(modelOrName: string | ModelEntry) {
    const settings = useSettingsStore()
    const name = typeof modelOrName === 'string' ? modelOrName : modelOrName.name
    selectedModel.value = name

    const listEntry = typeof modelOrName === 'string'
      ? models.value.find((item) => item.name === name) || null
      : modelOrName
    if (listEntry) selectedInfo.value = listEntry

    const hasResolvedDefaults = Boolean(listEntry?.defaultInferenceParamsResolved && listEntry.defaultInferenceParams)
    const shouldRefreshBaseDefaults = Boolean(modelInferenceOverrides.value[name] && !modelInferenceBaseKnown.value[name])
    if (listEntry && hasResolvedDefaults && !shouldRefreshBaseDefaults && String(listEntry.configInstruments || '').trim()) {
      detailLoading.value = false
      return Promise.resolve(listEntry)
    }

    detailLoading.value = true
    return invoke<ModelEntry>('get_model_info', {
      payload: {
        model: name,
        modelDir: settings.modelDir || null,
      },
    }).then((info) => {
      const normalizedInfo = normalizeModelEntryWithOverrides(info)
      if (selectedModel.value === name) selectedInfo.value = normalizedInfo
      return normalizedInfo
    }).catch((err) => {
      error.value = err instanceof Error ? err.message : String(err)
      throw err
    }).finally(() => {
      if (selectedModel.value === name) detailLoading.value = false
    })
  }

  function handleWorkerEvent(event: WorkerEvent) {
    const taskId = event?.taskId as string | undefined
    const payload = event.payload || {}

    if (taskId && taskId === customImportState.value.taskId) {
      if (event.type === 'custom_model_import_progress') {
        customImportState.value = {
          ...customImportState.value,
          stage: payload.stage || customImportState.value.stage,
          progress: typeof payload.progress === 'number' ? payload.progress : customImportState.value.progress,
          message: payload.message || payload.file || customImportState.value.message,
        }
      } else if (event.type === 'custom_model_import_finished') {
        customImportState.value = { ...customImportState.value, status: 'success', stage: 'done', progress: 100, message: '' }
        // The new model only becomes visible once the list is refetched.
        void loadModels()
      } else if (event.type === 'error') {
        customImportState.value = {
          ...customImportState.value,
          status: 'error',
          message: payload.message || 'Import failed',
        }
      } else if (event.type === 'task_cancelled') {
        customImportState.value = { ...customImportState.value, status: 'cancelled' }
      }
      return
    }

    if (taskId?.startsWith('download_')) {
      const modelName = payload.model || downloadTaskIndex.value[taskId] || Object.values(downloadTasks.value).find((task) => task.taskId === taskId)?.model
      if (!modelName) return
      const previous = downloadTasks.value[modelName] || {
        taskId,
        model: modelName,
        status: 'downloading',
        progress: 0,
        message: '',
        completedFiles: 0,
        totalFiles: 1,
        updatedAt: Date.now(),
        logs: [],
        seen: true,
      }
      if (downloadTasks.value[modelName] && downloadTasks.value[modelName]!.taskId !== taskId) return
      if (previous.status === 'cancelled' || previous.status === 'paused') {
        if (event.type !== 'task_cancelled') return
      }
      const next: DownloadTask = { ...previous, taskId, updatedAt: Date.now(), logs: previous.logs || [] }
      if (event.type === 'download_started') {
        next.status = 'downloading'
        next.progress = payload.progress ?? 0
        next.message = 'Started'
        next.totalFiles = payload.totalFiles || next.totalFiles
        downloadStates.value = { ...downloadStates.value, [modelName]: 'downloading' }
      } else if (event.type === 'download_stage') {
        next.status = 'downloading'
        next.progress = payload.progress ?? Math.max(next.progress, 5)
        next.message = payload.message || payload.stage || 'Downloading'
      } else if (event.type === 'download_file') {
        next.status = 'downloading'
        next.progress = payload.progress ?? next.progress
        next.completedFiles = payload.completedFiles || next.completedFiles
        next.totalFiles = payload.totalFiles || next.totalFiles
        next.message = payload.status || 'Downloading'
      } else if (event.type === 'download_progress') {
        next.status = 'downloading'
        next.progress = payload.progress ?? next.progress
        next.completedFiles = payload.completedFiles || next.completedFiles
        next.totalFiles = payload.totalFiles || next.totalFiles
        next.message = payload.completedFiles ? 'Downloading' : (next.message || 'Downloading')
        // Aggregate figures cover the whole model; the per-file ones would jump backwards each
        // time a new file starts.
        next.downloadedBytes = payload.aggregateDownloadedBytes ?? next.downloadedBytes
        next.totalBytes = payload.aggregateTotalBytes ?? next.totalBytes
        next.speedBytesPerSecond = payload.speedBytesPerSecond ?? next.speedBytesPerSecond
      } else if (event.type === 'download_done') {
        next.status = 'done'
        next.progress = 100
        next.speedBytesPerSecond = 0
        next.message = 'Done'
        next.completedFiles = payload.downloaded?.length + payload.skipped?.length || next.completedFiles
        next.totalFiles = Math.max(next.totalFiles, next.completedFiles || 1)
        if (payload.modelInfo) upsertModel(payload.modelInfo)
        if (payload.modelDir) modelDir.value = payload.modelDir
        downloadStates.value = { ...downloadStates.value, [modelName]: 'done' }
        // 模型已成功落盘后，卡片改由 model.downloaded 渲染“已下载”状态，
        // 此处清理常驻的 done 任务，避免内存与持久化冗余。
        // 若 modelInfo 缺失导致仍未标记下载，则保留任务以维持“下载/重试”入口。
        const markedDownloaded = models.value.find((item) => item.name === modelName)?.downloaded
        if (markedDownloaded) {
          if (downloadTasks.value[modelName]) {
            const { [modelName]: _removed, ...restTasks } = downloadTasks.value
            downloadTasks.value = restTasks
          }
          const { [taskId]: _removedIndex, ...restIndex } = downloadTaskIndex.value
          downloadTaskIndex.value = restIndex
          return
        }
      } else if (event.type === 'task_cancelled') {
        next.status = 'cancelled'
        next.message = 'Cancelled'
        downloadStates.value = { ...downloadStates.value, [modelName]: 'idle' }
      } else if (event.type === 'error') {
        if (previous.status === 'cancelled' || previous.status === 'paused') {
          downloadTasks.value = { ...downloadTasks.value, [modelName]: next }
          return
        }
        next.status = 'error'
        next.message = payload.message || 'Failed'
        next.errorMessage = payload.message || 'Failed'
        next.seen = false
        downloadStates.value = { ...downloadStates.value, [modelName]: 'error' }
        downloadErrors.value = { ...downloadErrors.value, [modelName]: next.message }
        if (payload.message) {
          next.logs = appendLog(next.logs, {
            ts: Date.now(),
            level: 'error',
            message: payload.message,
          })
        }
      } else if (event.type === 'download_log') {
        const level = (payload.level === 'warn' || payload.level === 'error' ? payload.level : 'info') as DownloadLogLevel
        const message = String(payload.message || '')
        next.logs = appendLog(next.logs, {
          ts: Date.now(),
          level,
          message,
          fileIndex: payload.fileIndex,
          totalFiles: payload.totalFiles,
          stage: payload.stage,
        })
        if (level === 'info' && message) {
          next.message = message.length > 80 ? message.slice(0, 77) + '...' : message
        }
      }
      downloadTasks.value = { ...downloadTasks.value, [modelName]: next }
      return
    }

    if (taskId?.startsWith('delete_')) {
      const modelName = payload.model || deleteTaskIndex.value[taskId] || Object.values(deleteTasks.value).find((task) => task.taskId === taskId)?.model
      if (!modelName) return
      const previous = deleteTasks.value[modelName] || {
        taskId,
        model: modelName,
        status: 'deleting' as const,
        progress: 0,
        message: '',
        completedFiles: 0,
        totalFiles: 1,
        updatedAt: Date.now(),
      }
      const next: DeleteTask = { ...previous, taskId, updatedAt: Date.now() }
      if (event.type === 'model_delete_started') {
        next.status = 'deleting'
        next.progress = payload.progress ?? 0
        next.message = payload.message || 'Deleting model files'
        next.completedFiles = payload.completedFiles ?? 0
        next.totalFiles = payload.totalFiles || next.totalFiles
      } else if (event.type === 'model_delete_progress') {
        next.status = 'deleting'
        next.progress = payload.progress ?? next.progress
        next.message = payload.message || 'Deleting model files'
        next.completedFiles = payload.completedFiles ?? next.completedFiles
        next.totalFiles = payload.totalFiles || next.totalFiles
      } else if (event.type === 'model_delete_done') {
        next.status = 'done'
        next.progress = 100
        next.message = payload.message || 'Deleting model files'
        next.completedFiles = payload.completedFiles ?? next.completedFiles
        next.totalFiles = payload.totalFiles || next.totalFiles
        next.resultModelInfo = payload.modelInfo || null
        if (payload.modelInfo) upsertModel(payload.modelInfo)
      } else if (event.type === 'model_delete_failed') {
        next.status = 'error'
        next.message = payload.message || 'Delete failed'
        next.resultModelInfo = payload.modelInfo || null
        if (payload.modelInfo) upsertModel(payload.modelInfo)
      } else if (event.type === 'error') {
        next.status = 'error'
        next.message = payload.message || 'Delete failed'
      }
      deleteTasks.value = { ...deleteTasks.value, [modelName]: next }
      return
    }

    if (taskId?.startsWith('cleanup_residual_')) {
      if (event.type === 'model_residual_cleanup_started') {
        residualCleanupState.value = {
          taskId,
          active: true,
          status: 'deleting',
          progress: payload.progress ?? 0,
          message: payload.message || 'Cleaning residual files',
          completedFiles: payload.completedFiles ?? 0,
          totalFiles: payload.totalFiles ?? 0,
          updatedAt: Date.now(),
        }
      } else if (event.type === 'model_residual_cleanup_progress') {
        residualCleanupState.value = {
          ...residualCleanupState.value,
          taskId,
          active: true,
          status: 'deleting',
          progress: payload.progress ?? residualCleanupState.value.progress,
          message: payload.message || residualCleanupState.value.message,
          completedFiles: payload.completedFiles ?? residualCleanupState.value.completedFiles,
          totalFiles: payload.totalFiles ?? residualCleanupState.value.totalFiles,
          updatedAt: Date.now(),
        }
      } else if (event.type === 'model_residual_cleanup_done') {
        residualCleanupState.value = {
          ...residualCleanupState.value,
          taskId,
          active: false,
          status: 'done',
          progress: 100,
          message: payload.message || residualCleanupState.value.message,
          completedFiles: payload.completedFiles ?? residualCleanupState.value.completedFiles,
          totalFiles: payload.totalFiles ?? residualCleanupState.value.totalFiles,
          updatedAt: Date.now(),
          notified: false,
        }
        const summary = payload.modelStorageSummary
        if (summary?.models) {
          modelStorageSummary.value = summary
          modelStorageSummaryLoadedAt.value = Date.now()
        }
      } else if (event.type === 'model_residual_cleanup_failed') {
        residualCleanupState.value = {
          ...residualCleanupState.value,
          taskId,
          active: false,
          status: 'error',
          message: payload.message || 'Cleanup failed',
          updatedAt: Date.now(),
          notified: false,
        }
        const summary = payload.modelStorageSummary
        if (summary?.models) {
          modelStorageSummary.value = summary
          modelStorageSummaryLoadedAt.value = Date.now()
        }
      } else if (event.type === 'error') {
        residualCleanupState.value = {
          ...residualCleanupState.value,
          taskId,
          active: false,
          status: 'error',
          message: payload.message || 'Cleanup failed',
          updatedAt: Date.now(),
          notified: false,
        }
      }
    }
  }

  async function downloadModel(name: string, force = false): Promise<boolean> {
    const app = useAppStore()
    const settings = useSettingsStore()
    const existingTask = downloadTasks.value[name]
    if (existingTask && ['preparing', 'downloading'].includes(existingTask.status)) return false
    const taskId = `download_${crypto.randomUUID()}`
    downloadStates.value = { ...downloadStates.value, [name]: 'preparing' }
    downloadErrors.value = { ...downloadErrors.value, [name]: '' }
    downloadTasks.value = {
      ...downloadTasks.value,
      [name]: {
        taskId,
        model: name,
        status: 'preparing',
        progress: 0,
        // Left empty on purpose: the store has no translator, and every display path already
        // derives a localised label from the status. A literal here leaked English into the UI.
        message: '',
        completedFiles: 0,
        totalFiles: 1,
        updatedAt: Date.now(),
        logs: [],
        seen: true,
      },
    }
    downloadTaskIndex.value = { ...downloadTaskIndex.value, [taskId]: name }
    try {
      if (app.runtimeInstallStatus === 'installing') {
        await app.waitForRuntimeInstall()
      }
      const pendingTask = downloadTasks.value[name]
      if (!pendingTask || pendingTask.taskId !== taskId || pendingTask.status !== 'preparing') return false
      await app.checkRuntimeInfo()
      if (!app.runtimeInfo?.ready) {
        throw new Error('请先完成并激活 Python 运行环境后再下载模型')
      }
      const currentTask = downloadTasks.value[name]
      if (!currentTask || currentTask.taskId !== taskId || currentTask.status !== 'preparing') return false
      await invoke<{ taskId: string; started: boolean }>('start_model_download', {
        payload: {
          taskId,
          model: name,
          modelDir: settings.modelDir || null,
          source: settings.downloadSource,
          downloadMethod: settings.downloadMethod,
          endpoint: null,
          force,
        },
      })
      const startedTask = downloadTasks.value[name]
      if (!startedTask || startedTask.taskId !== taskId) {
        await invoke<boolean>('cancel_task', { taskId }).catch(() => false)
        return false
      }
      if (startedTask.status === 'cancelled' || startedTask.status === 'paused') {
        await invoke<boolean>('cancel_task', { taskId }).catch(() => false)
        return false
      }
      if (startedTask.status === 'preparing') {
        downloadStates.value = { ...downloadStates.value, [name]: 'downloading' }
        downloadTasks.value = {
          ...downloadTasks.value,
          [name]: { ...startedTask, status: 'downloading', updatedAt: Date.now() },
        }
      }
    } catch (err) {
      const currentTask = downloadTasks.value[name]
      if (!currentTask || currentTask.taskId !== taskId || ['paused', 'cancelled'].includes(currentTask.status)) return false
      // The command may have spawned the worker even if its IPC response was lost. A best-effort
      // cancellation prevents that worker from continuing after the UI has reported a failure.
      await invoke<boolean>('cancel_task', { taskId }).catch(() => false)
      const message = err instanceof Error ? err.message : String(err)
      downloadStates.value = { ...downloadStates.value, [name]: 'error' }
      downloadErrors.value = { ...downloadErrors.value, [name]: message }
      downloadTasks.value = {
        ...downloadTasks.value,
        [name]: {
          ...currentTask,
          status: 'error',
          message,
          errorMessage: message,
          seen: false,
          logs: appendLog(currentTask.logs || [], { ts: Date.now(), level: 'error', message }),
          updatedAt: Date.now(),
        },
      }
      throw err
    }
    return true
  }

  async function cancelDownload(name: string, pause = false) {
    const task = downloadTasks.value[name]
    if (!task || !['preparing', 'downloading'].includes(task.status)) return false
    if (task.status === 'preparing') {
      downloadTasks.value = {
        ...downloadTasks.value,
        [name]: { ...task, status: pause ? 'paused' : 'cancelled', message: pause ? 'Paused' : 'Cancelled', updatedAt: Date.now() },
      }
      downloadStates.value = { ...downloadStates.value, [name]: 'idle' }
      await invoke<boolean>('cancel_task', { taskId: task.taskId }).catch(() => false)
      return true
    }
    const cancelled = await invoke<boolean>('cancel_task', { taskId: task.taskId })
    if (cancelled) {
      downloadTasks.value = {
        ...downloadTasks.value,
        [name]: { ...task, status: pause ? 'paused' : 'cancelled', message: pause ? 'Paused' : 'Cancelled', updatedAt: Date.now() },
      }
      downloadStates.value = { ...downloadStates.value, [name]: 'idle' }
    }
    return cancelled
  }

  async function deleteModel(name: string, source: 'single' | 'batch' = 'single') {
    const existingTask = deleteTasks.value[name]
    if (existingTask && !isDeleteTaskTerminal(existingTask)) {
      throw new Error('Model deletion already in progress')
    }
    const settings = useSettingsStore()
    const taskId = `delete_${Date.now()}`
    deleteTasks.value = {
      ...deleteTasks.value,
      [name]: {
        taskId,
        model: name,
        status: 'deleting',
        progress: 0,
        message: 'Deleting model files',
        completedFiles: 0,
        totalFiles: 1,
        updatedAt: Date.now(),
        source,
        resultModelInfo: null,
      },
    }
    deleteTaskIndex.value = { ...deleteTaskIndex.value, [taskId]: name }
    try {
      await invoke<{ taskId: string; started: boolean }>('start_model_delete', {
        payload: {
          taskId,
          model: name,
          modelDir: settings.modelDir || null,
        },
      })
      if (source === 'single') {
        // Batch deletion already awaits each task; a single one used to fire and forget, so a
        // terminal event that never arrived — the window reloading mid-delete, an event emitted
        // before the listener was attached — left the card reading 'deleting' with no way back.
        void watchSingleDeleteTask(name, taskId)
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      const previous = deleteTasks.value[name]
      if (previous) {
        deleteTasks.value = {
          ...deleteTasks.value,
          [name]: { ...previous, status: 'error', message, updatedAt: Date.now(), resultModelInfo: null },
        }
      }
    }
  }

  function finalizeDeletedModel(name: string, modelInfo?: ModelEntry | null) {
    if (modelInfo) {
      upsertModel(modelInfo)
    } else {
      const idx = models.value.findIndex((m) => m.name === name)
      if (idx >= 0) {
        models.value[idx] = { ...models.value[idx], downloaded: false, missingPaths: [models.value[idx].modelPath] }
      }
      if (selectedInfo.value?.name === name) {
        selectedInfo.value = { ...selectedInfo.value, downloaded: false }
      }
      persistModelCache()
    }
    if (downloadTasks.value[name]) {
      const { [name]: _, ...rest } = downloadTasks.value
      downloadTasks.value = rest
    }
    const idxTask = deleteTasks.value[name]
    if (idxTask) clearDeleteTaskIndex(idxTask.taskId)
  }

  /**
   * Backstop for a delete whose completion never arrives.
   *
   * The worker itself is reliable and the Rust layer reports an unexpected exit, but an event is
   * only delivered to whatever listener exists at that moment. Rather than leave the entry stuck,
   * fail it so the card returns to a state the user can act on.
   */
  async function watchSingleDeleteTask(name: string, taskId: string) {
    try {
      const task = await waitForDeleteTask(name, taskId)
      finalizeDeletedModel(name, task.resultModelInfo ?? null)
      clearDeleteTask(name)
    } catch (err) {
      const current = deleteTasks.value[name]
      if (!current || current.taskId !== taskId || isDeleteTaskTerminal(current)) return
      deleteTasks.value = {
        ...deleteTasks.value,
        [name]: {
          ...current,
          status: 'error',
          message: err instanceof Error ? err.message : String(err),
          updatedAt: Date.now(),
        },
      }
    }
  }

  async function waitForDeleteTask(name: string, taskId: string) {
    return new Promise<DeleteTask>((resolve, reject) => {
      let timer: ReturnType<typeof setTimeout> | null = null
      const stop = watch(
        () => deleteTasks.value[name],
        (task) => {
          if (!task) return
          if (task.taskId !== taskId) return
          if (task.status === 'done') {
            if (timer) clearTimeout(timer)
            stop()
            resolve(task)
          } else if (task.status === 'error') {
            if (timer) clearTimeout(timer)
            stop()
            reject(new Error(task.message || 'Delete failed'))
          }
        },
        { deep: true, immediate: true },
      )
      timer = setTimeout(() => {
        stop()
        reject(new Error('Delete task timed out'))
      }, DELETE_TASK_TIMEOUT_MS)
    })
  }

  async function deleteToolModel(target: ModelStorageDeleteTarget) {
    const settings = useSettingsStore()
    const result = await invoke<{ modelStorageSummary?: ModelStorageSummary }>('delete_tool_model', {
      payload: {
        id: target.id,
        tool: target.tool,
        modelDir: settings.modelDir || null,
      },
    })
    if (result.modelStorageSummary) {
      modelStorageSummary.value = result.modelStorageSummary
      modelStorageSummaryLoadedAt.value = Date.now()
    }
  }

  async function deleteStorageModels(targets: ModelStorageDeleteTarget[]) {
    batchDeleteState.value = {
      active: true,
      totalModels: targets.length,
      completedModels: 0,
      currentModel: '',
      failedModels: [],
    }
    for (const target of targets) {
      batchDeleteState.value = {
        ...batchDeleteState.value,
        currentModel: target.name,
      }
      try {
        if (target.kind === 'tool') {
          await deleteToolModel(target)
        } else {
          await deleteModel(target.id, 'batch')
          const taskId = deleteTasks.value[target.id]?.taskId
          if (!taskId) throw new Error('Delete task was not created')
          const task = await waitForDeleteTask(target.id, taskId)
          finalizeDeletedModel(target.id, task.resultModelInfo ?? null)
          clearDeleteTask(target.id)
        }
      } catch {
        batchDeleteState.value = {
          ...batchDeleteState.value,
          failedModels: [...batchDeleteState.value.failedModels, target.name],
        }
        if (target.kind === 'catalog') clearDeleteTask(target.id)
      } finally {
        batchDeleteState.value = {
          ...batchDeleteState.value,
          completedModels: batchDeleteState.value.completedModels + 1,
        }
      }
    }
    batchDeleteState.value = {
      ...batchDeleteState.value,
      active: false,
      currentModel: '',
    }
    await loadModelStorageSummary({ force: true })
  }

  async function deleteModels(names: string[]) {
    return deleteStorageModels(names.map((name) => ({ kind: 'catalog', id: name, name })))
  }

  async function loadModelStorageSummary(options?: { force?: boolean }) {
    const settings = useSettingsStore()
    const requestedModelDir = String(settings.modelDir || '').trim()
    const cachedModelDir = String(modelStorageSummary.value?.modelDir || '').trim()
    const hasFreshCache = Boolean(
      modelStorageSummary.value
      && cachedModelDir
      && cachedModelDir === requestedModelDir
      && Date.now() - modelStorageSummaryLoadedAt.value < STORAGE_SUMMARY_CACHE_TTL_MS,
    )
    if (!options?.force && hasFreshCache) return modelStorageSummary.value
    storageLoading.value = true
    try {
      const result = await invoke<ModelStorageSummary>('get_model_storage_summary', {
        payload: { modelDir: settings.modelDir || null },
      })
      modelStorageSummary.value = result
      modelStorageSummaryLoadedAt.value = Date.now()
      return result
    } finally {
      storageLoading.value = false
    }
  }

  async function cleanupModelResidualFiles() {
    const settings = useSettingsStore()
    const taskId = `cleanup_residual_${Date.now()}`
    residualCleanupState.value = {
      taskId,
      active: true,
      status: 'deleting',
      progress: 0,
      message: 'Cleaning residual files',
      completedFiles: 0,
      totalFiles: 0,
      updatedAt: Date.now(),
      notified: false,
    }
    try {
      await invoke<any>('start_cleanup_model_residual_files', {
        payload: { taskId, modelDir: settings.modelDir || null },
      })
    } catch (err) {
      residualCleanupState.value = {
        ...residualCleanupState.value,
        active: false,
        status: 'error',
        message: err instanceof Error ? err.message : String(err),
        updatedAt: Date.now(),
        notified: false,
      }
    }
  }

  function clearDownloadError(name: string) {
    if (downloadErrors.value[name]) {
      const { [name]: _, ...rest } = downloadErrors.value
      downloadErrors.value = rest
    }
  }

  function markDownloadTaskSeen(name: string) {
    const task = downloadTasks.value[name]
    if (task && !task.seen) {
      downloadTasks.value = {
        ...downloadTasks.value,
        [name]: { ...task, seen: true },
      }
    }
  }

  function clearDownloadTask(name: string) {
    const task = downloadTasks.value[name]
    if (task) {
      const { [name]: _, ...rest } = downloadTasks.value
      downloadTasks.value = rest
      if (downloadTaskIndex.value[task.taskId]) {
        const { [task.taskId]: _idx, ...restIdx } = downloadTaskIndex.value
        downloadTaskIndex.value = restIdx
      }
    }
    clearDownloadError(name)
  }

  function clearDeleteTask(name: string) {
    const task = deleteTasks.value[name]
    if (task) {
      clearDeleteTaskIndex(task.taskId)
      const { [name]: _, ...rest } = deleteTasks.value
      deleteTasks.value = rest
    }
  }

  function resetResidualCleanupState() {
    residualCleanupState.value = {
      taskId: '',
      active: false,
      status: 'idle',
      progress: 0,
      message: '',
      completedFiles: 0,
      totalFiles: 0,
      updatedAt: 0,
      notified: false,
    }
  }

  /** Inspect a weights file to suggest an architecture before the user has to pick one. */
  async function inspectCustomModel(modelPath: string, configPath?: string | null) {
    return invoke<CustomModelInspection>('inspect_custom_model', {
      payload: { modelPath, configPath: configPath || null },
    })
  }

  /**
   * Start an import. Backgrounded: copying multi-GB weights and verifying them by really
   * loading the model both take long enough to need progress and cancellation.
   */
  async function importCustomModel(request: CustomModelImportRequest) {
    const settings = useSettingsStore()
    const taskId = `custom_model_import_${crypto.randomUUID()}`
    customImportState.value = {
      taskId,
      name: request.name,
      status: 'importing',
      stage: 'starting',
      progress: 0,
      message: '',
    }
    try {
      // The configured model directory travels with the request: copies must land beside the
      // catalog models, and the worker's PYMSS_MODEL_DIR only ever holds the default location.
      await invoke('start_custom_model_import', {
        payload: { ...request, taskId, modelDir: settings.modelDir || null },
      })
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      customImportState.value = { ...customImportState.value, status: 'error', message }
      throw err
    }
    return taskId
  }

  /**
   * Stop a running import.
   *
   * Necessary rather than optional: the wizard blocks every other way out while importing, and
   * verifying a large model can take a long time, so without this the user would be stuck.
   * Killing the worker leaves no half-written weights behind — a copy writes to a temporary file,
   * and the next import of the same name clears the directory first.
   */
  async function cancelCustomModelImport() {
    const taskId = customImportState.value.taskId
    if (!taskId || customImportState.value.status !== 'importing') return false
    return invoke<boolean>('cancel_task', { taskId })
  }

  function resetCustomImportState() {
    customImportState.value = { taskId: '', name: '', status: 'idle', stage: '', progress: 0, message: '' }
  }

  async function removeCustomModel(name: string, deleteFiles = false) {
    const result = await invoke<CustomModelRemoval>('unregister_custom_model', {
      payload: { name, deleteFiles },
    })
    await loadModels()
    return result
  }

  async function relinkCustomModel(name: string, modelPath: string, configPath?: string | null) {
    const result = await invoke<{ name: string }>('relink_custom_model', {
      payload: { name, modelPath, configPath: configPath || null },
    })
    await loadModels()
    return result
  }

  // Model downloads/imports/deletions use background workers but are not part of
  // the separation task list. Stop any active worker before the main window closes.
  registerWindowCloseGuard(async () => {
    if (!isTauriRuntime()) return
    const taskIds = new Set<string>()
    Object.values(downloadTasks.value).forEach((task) => {
      if (['preparing', 'downloading'].includes(task.status)) taskIds.add(task.taskId)
    })
    Object.values(deleteTasks.value).forEach((task) => {
      if (task.status === 'deleting') taskIds.add(task.taskId)
    })
    if (residualCleanupState.value.active && residualCleanupState.value.taskId) {
      taskIds.add(residualCleanupState.value.taskId)
    }
    if (customImportState.value.status === 'importing' && customImportState.value.taskId) {
      taskIds.add(customImportState.value.taskId)
    }
    await Promise.all([...taskIds].map((taskId) =>
      invoke<boolean>('cancel_task', { taskId }).catch(() => false),
    ))
  }, 70)

  return {
    initialized,
    models,
    categories,
    categoriesCn,
    modelDir,
    debugStatus,
    selectedModel,
    selectedInfo,
    isLoading,
    modelsLoaded,
    detailLoading,
    error,
    search,
    supportedOnly,
    category,
    modelSource,
    modelViewMode,
    modelPageSize,
    customModelCount,
    debugModelCount,
    downloadStates,
    downloadErrors,
    downloadTasks,
    modelInferenceOverrides,
    modelPreferences,
    deleteTasks,
    modelStorageSummary,
    storageLoading,
    batchDeleteState,
    residualCleanupState,
    customImportState,
    filteredModels,
    downloadedModels,
    initialize,
    loadModels,
    selectModel,
    setModelInferenceOverrides,
    resetModelInferenceOverrides,
    getModelInferenceOverrides,
    getModelBaseInferenceDefaults,
    hasKnownModelInferenceBase,
    getModelPreference,
    isModelFavorite,
    getModelNote,
    getModelUseCount,
    setModelFavorite,
    toggleModelFavorite,
    setModelNote,
    recordModelUse,
    deleteModel,
    downloadModel,
    cancelDownload,
    deleteModels,
    deleteStorageModels,
    loadModelStorageSummary,
    cleanupModelResidualFiles,
    clearDeleteTask,
    clearDownloadTask,
    markDownloadTaskSeen,
    resetResidualCleanupState,
    inspectCustomModel,
    importCustomModel,
    cancelCustomModelImport,
    resetCustomImportState,
    removeCustomModel,
    relinkCustomModel,
    handleWorkerEvent,
  }
})
