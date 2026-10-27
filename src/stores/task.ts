import { createLogger } from '@/utils/logger'
import { defineStore } from 'pinia'
import { computed, nextTick, ref, watch } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import i18n from '@/i18n'
import { hasBackendBridge as isTauriRuntime, loadAppStore, saveAppStore } from '@/utils/appStore'
import { useSettingsStore } from '@/stores/settings'
import { useModelStore, type ModelDefaultInferenceParams } from '@/stores/model'
import { useAppStore } from '@/stores/app'
import type { WorkflowEntry } from '@/stores/workflow'
import { appendWorkflowNodeLogs, type WorkflowNodeLogs } from '@/workflows/nodeLogs'
import { normalizeLogRecord, workerLogRecord, localLogRecord, type LogEntry } from '@/utils/logRecord'
import { detectWorkflowFormat, WORKFLOW_FORMAT_VERSION } from '@/workflows/formats'
import {
  getWorkflowDefinitionIssue,
  prepareWorkflowDefinitionForRun,
  resolveWorkflowRuntimeDefaults,
} from '@/workflows/runtimeDefinition'
import {
  isInterruptedTaskStatus,
  isTerminalTaskStatus,
  resolveJobStatus,
  selectQueuedJobGroups,
  sortJobTasks,
  taskJobId,
  TERMINAL_TASK_STATUSES,
  type TaskStatus,
} from '@/features/tasks/lifecycle'
import { normalizeStemOutputs, stemFromOutputPath, type StemOutput } from '@/utils/stemOutputs'
import { normalizeConcurrentSeparations } from '@/features/tasks/concurrency'
import { isMainTaskOwner } from '@/utils/taskOwner'
import { getSeparateWorkflowInputIssue } from '@/utils/workflowInputs'
import { cloneWorkflow, prepareWorkbenchRun, workflowInputNodes, type WorkflowRuntimeBindings, type WorkflowWorkbenchSnapshot, type WorkflowNodeState } from '@/workflows/workbench'
import { SEPARATE_MODEL_PAGE_SIZES, normalizePageSize } from '@/utils/pagination'

const studioLog = createLogger('ui.stores.task')

export type { TaskStatus } from '@/features/tasks/lifecycle'
export type { StemOutput } from '@/utils/stemOutputs'

export type OutputLayout = 'folders' | 'flat'
export type ModelListSortMode = 'usage' | 'recent' | 'favorite' | 'name-asc' | 'name-desc'

export type OutputNamingConfig = {
  enabled: boolean
  template: string
  stemOrder: string[]
}

export type SeparationRunConfig = {
  runMode?: 'model' | 'workflow'
  modelDir: string | null
  downloadSource: string
  downloadMethod: string
  workflowId?: string
  workflowName?: string
  workflowDefinition?: Record<string, unknown>
  workbench?: WorkflowWorkbenchSnapshot
  modelType?: string | null
  outputLayout: OutputLayout
  outputNaming?: OutputNamingConfig
  device: string
  deviceIds: number[]
  outputFormat: string
  selectedStems: string[]
  useTta: boolean
  debug: boolean
  audioParams: Record<string, string | number>
  inferenceParamsVersion?: number
  inferenceParams: Record<string, unknown>
}


type ScanMediaPathsResult = {
  files: string[]
  warnings: string[]
}

export type SeparationTask = {
  id: string
  jobId?: string
  resultId?: string
  batchId?: string
  batchIndex?: number
  model: string
  input: string
  jobOutput?: string
  outputPrefix?: string
  output: string
  outputDirectories?: string[]
  status: TaskStatus
  message: string
  createdAt: number
  updatedAt: number
  startedAt?: number
  finishedAt?: number
  durationMs?: number
  progress: number
  stageLabel: string
  progressCurrent?: number
  progressTotal?: number
  progressDetail?: string
  files: string[]
  outputs: StemOutput[]
  /** Plugin graph leaf values (ASR text, measurements, MIDI paths) by node. */
  nodeValues?: { nodeId: number; nodeType: string; slot: number; value: string }[]
  logs: LogEntry[]
  nodeLogs?: WorkflowNodeLogs
  error?: string
  runConfig?: SeparationRunConfig
  nodeStates?: Record<string, WorkflowNodeState>
  nodeExecutionOrder?: string[]
  currentNodeId?: string
  taskHidden?: boolean
  resultHidden?: boolean
}

export type SeparationJob = {
  id: string
  output: string
  tasks: SeparationTask[]
  primary: SeparationTask
  model: string
  inputCount: number
  outputCount: number
  createdAt: number
  updatedAt: number
  startedAt?: number
  finishedAt?: number
  durationMs?: number
  status: TaskStatus
  progress: number
}

type PersistedTaskState = {
  tasks?: Partial<SeparationTask>[]
}

type PersistedSeparateModelState = {
  batch_size?: number | null
  overlap_size?: number | null
  num_overlap?: number | null
  chunk_size?: number | null
  standardize?: boolean
  normalize?: boolean
  window_size?: number | null
  aggression?: number | null
  enable_post_process?: boolean
  post_process_threshold?: number | null
  high_end_process?: boolean
  selectedStems?: string[]
  selectedStemsCleared?: boolean
}

type PersistedSeparateState = {
  version?: number
  runMode?: 'model' | 'workflow'
  ensembleEnabled?: boolean
  ensembleModels?: string[]
  ensembleStem?: string
  ensembleModelStems?: Record<string, string>
  ensembleType?: string
  ensembleWeights?: Record<string, number>
  outputLayout?: OutputLayout
  outputNamingTemplate?: string
  customStemOrder?: string[]
  modelListViewMode?: 'card' | 'list'
  modelListSortMode?: ModelListSortMode
  modelListPageSize?: number
  useTta?: boolean
  inferenceParamsByModel?: Record<string, PersistedSeparateModelState>
}

type ModelInferenceUiDefaults = {
  [K in keyof ModelDefaultInferenceParams]: ModelDefaultInferenceParams[K] | null
}

const AUDIO_EXTENSIONS = ['wav', 'mp3', 'flac', 'm4a', 'aac', 'ogg', 'opus']
const VIDEO_EXTENSIONS = ['mp4', 'mkv', 'mov', 'avi', 'webm', 'flv']
const CURRENT_INFERENCE_PARAMS_VERSION = 3
const TERMINAL_STATUSES: TaskStatus[] = [...TERMINAL_TASK_STATUSES]
const NORMAL_LOG_LIMIT = 300
const DEVELOPER_LOG_LIMIT = 1200

const STAGE_META: Record<TaskStatus, { progress: number; label: string }> = {
  queued: { progress: 2, label: 'Queued' },
  preparing: { progress: 8, label: 'Preparing' },
  validating_input: { progress: 12, label: 'Validating input' },
  downloading_model: { progress: 22, label: 'Preparing model' },
  ensuring_model: { progress: 22, label: 'Preparing model' },
  loading_model: { progress: 35, label: 'Loading model' },
  separating: { progress: 0, label: 'Separating' },
  writing_output: { progress: 92, label: 'Collecting outputs' },
  done: { progress: 100, label: 'Done' },
  failed: { progress: 100, label: 'Failed' },
  cancelled: { progress: 100, label: 'Cancelled' },
}

function isSupportedInputPath(path: string) {
  const ext = path.split('.').pop()?.toLowerCase() || ''
  return AUDIO_EXTENSIONS.includes(ext) || VIDEO_EXTENSIONS.includes(ext)
}

function normalizeStatus(status: unknown): TaskStatus {
  if (typeof status !== 'string') return 'queued'
  if (status in STAGE_META) return status as TaskStatus
  return 'preparing'
}

function normalizeOutputLayout(value: unknown): OutputLayout {
  return value === 'flat' ? 'flat' : 'folders'
}

function normalizePersistedModelState(value: unknown): PersistedSeparateModelState {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  const raw = value as Record<string, unknown>
  const numberFields = ['batch_size', 'overlap_size', 'num_overlap', 'chunk_size', 'window_size', 'aggression', 'post_process_threshold']
  const next: PersistedSeparateModelState = {}
  numberFields.forEach((key) => {
    const item = raw[key]
    if (item === null) (next as Record<string, unknown>)[key] = null
    else if (typeof item === 'number' && Number.isFinite(item)) (next as Record<string, unknown>)[key] = item
  })
  ;['standardize', 'normalize', 'enable_post_process', 'high_end_process'].forEach((key) => {
    if (typeof raw[key] === 'boolean') (next as Record<string, unknown>)[key] = raw[key]
  })
  if (Array.isArray(raw.selectedStems)) {
    next.selectedStems = raw.selectedStems.map(item => String(item || '').trim()).filter(Boolean)
  }
  if (typeof raw.selectedStemsCleared === 'boolean') {
    next.selectedStemsCleared = raw.selectedStemsCleared
  }
  return next
}

function normalizeOutputNaming(value: unknown): OutputNamingConfig | undefined {
  if (!value || typeof value !== 'object') return undefined
  const raw = value as Partial<OutputNamingConfig>
  const template = typeof raw.template === 'string' ? raw.template.trim() : ''
  const stemOrder = Array.isArray(raw.stemOrder)
    ? raw.stemOrder.map(item => String(item || '').trim()).filter(Boolean)
    : []
  return {
    enabled: raw.enabled === true,
    template,
    stemOrder,
  }
}

function normalizeOutputPath(value?: string | null) {
  return value && value.trim() ? value : 'outputs'
}

function resolveOutputDirectory(value?: string | null, fallback?: string | null, windows = false) {
  const directory = value && value.trim() ? value : fallback && fallback.trim() ? fallback : 'outputs'
  return windows || !directory.startsWith('/') ? directory.trim() : directory
}

function normalizeTimestamp(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : undefined
}

function normalizeDurationMs(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.max(0, Math.round(value)) : undefined
}

function joinOutputPath(base: string, child: string, windows: boolean) {
  const separator = windows && base.includes('\\') ? '\\' : '/'
  const trailingSeparator = windows ? /[\\/]$/ : /[/]$/
  return `${base.replace(trailingSeparator, '')}${separator}${child}`
}

function createRunId(prefix: string) {
  return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`
}

function inputStemSegment(input: string, fallback: string, windows: boolean) {
  return input
    .split(windows ? /[/\\]/ : /[/]/)
    .filter(Boolean)
    .pop()
    ?.replace(windows ? /\.[^/.\\]+$/ : /\.[^/.]+$/, '') || fallback
}

function outputsFromFiles(outputDir: string, files: string[], outputFormat = 'wav', outputPrefix?: string, inputPath?: string, windows = /^[A-Za-z]:|^\\\\/.test(outputDir)): StemOutput[] {
  if (!files?.length) return []
  return files.flatMap((file) => {
    const rawPath = String(file || '')
    const parts = rawPath.split(windows ? /[/\\]/ : /[/]/)
    if (!rawPath || parts.some(part => part === '.' || part === '..')) return []
    const leaf = parts.at(-1) || ''
    const extension = leaf.match(windows ? /\.[^/.\\]+$/ : /\.[^/.]+$/)?.[0] || ''
    const rawStemName = extension ? leaf.slice(0, -extension.length) : leaf
    const outputFileName = outputPrefix ? `${outputPrefix}_${rawStemName}` : rawStemName
    const isAbsolutePath = windows ? /^(?:[A-Za-z]:[\\/]|[\\/]{2})/.test(rawPath) : rawPath.startsWith('/')
    const relativeParent = rawPath.slice(0, rawPath.length - leaf.length)
    return [{
      stem: stemFromOutputPath(leaf, inputPath, windows),
      path: isAbsolutePath ? rawPath : joinOutputPath(outputDir, `${relativeParent}${outputFileName}${extension || `.${outputFormat}`}`, windows),
    }]
  })
}

function cleanupPath(path: string, windows: boolean) {
  let normalized = windows ? path.replace(/\\/g, '/') : path
  if (windows) {
    normalized = normalized.replace(/^\/\/\?\/UNC\//i, '//').replace(/^\/\/\?\/([A-Za-z]:\/)/, '$1')
  }
  return normalized.replace(/\/+$/, '') || (normalized.startsWith('/') ? '/' : '')
}

function parentDir(path: string, windows: boolean) {
  const normalized = cleanupPath(path, windows)
  const index = normalized.lastIndexOf('/')
  return index > 0 ? normalized.slice(0, index) : ''
}

function pathKey(path: string, windows: boolean) {
  const normalized = cleanupPath(path, windows)
  const drive = windows ? normalized.match(/^[A-Za-z]:/)?.[0] || '' : ''
  let prefix = ''
  if (drive) prefix = `${drive}/`
  else if (normalized.startsWith('/')) prefix = windows && normalized.startsWith('//') ? '//' : '/'
  const segments: string[] = []
  const rootDepth = prefix === '//' ? 2 : 0
  for (const part of (drive ? normalized.slice(2) : normalized).split('/')) {
    if (!part || part === '.') continue
    if (part === '..') {
      if (segments.length > rootDepth && segments.at(-1) !== '..') segments.pop()
      else if (!prefix) segments.push(part)
    } else segments.push(part)
  }
  const key = prefix + segments.join('/')
  return windows ? key.toLowerCase() : key
}

function samePath(a: string | undefined, b: string | undefined, windows: boolean) {
  return Boolean(a && b && pathKey(a, windows) === pathKey(b, windows))
}

function cleanupDirChain(path: string, stopAt: string | undefined, windows: boolean) {
  const dirs: string[] = []
  const absolute = windows ? /^(?:[A-Za-z]:[\\/]|[\\/]{2}[^\\/]+[\\/][^\\/]+)/ : /^\//
  if (!stopAt || !absolute.test(path) || !absolute.test(stopAt)) return dirs
  const stop = cleanupPath(stopAt, windows)
  let current = cleanupPath(path, windows)
  if ([stop, current].some(value => value.split('/').some(part => part === '.' || part === '..'))) return dirs
  const stopKey = pathKey(stop, windows)
  const prefix = stopKey.endsWith('/') ? stopKey : `${stopKey}/`
  while (current && !samePath(current, stop, windows)) {
    if (!pathKey(current, windows).startsWith(prefix)) return []
    dirs.push(current)
    current = parentDir(current, windows)
  }
  return dirs
}

function clamp(value: number, minimum: number, maximum: number) {
  return Math.max(minimum, Math.min(maximum, value))
}

function getLogLimit(developerMode: boolean) {
  return developerMode ? DEVELOPER_LOG_LIMIT : NORMAL_LOG_LIMIT
}

function markTaskStarted(task: SeparationTask, at = Date.now()) {
  if (!task.startedAt) task.startedAt = at
}

function markTaskFinished(task: SeparationTask, at = Date.now()) {
  task.finishedAt = at
  const startedAt = task.startedAt || task.createdAt
  task.durationMs = Math.max(0, at - startedAt)
}

function resolveStageProgress(status: TaskStatus, current?: number, total?: number) {
  const meta = STAGE_META[status]
  if (status !== 'separating') return meta.progress
  if (
    typeof current !== 'number'
    || typeof total !== 'number'
    || !Number.isFinite(current)
    || !Number.isFinite(total)
    || total <= 0
  ) {
    return meta.progress
  }
  return Math.round(clamp(current / total, 0, 1) * 100)
}

function normalizeTask(task: Partial<SeparationTask>, windows = /^[A-Za-z]:|^\\\\/.test(task.output || task.jobOutput || '')): SeparationTask {
  let status = normalizeStatus(task.status)
  let interrupted = false
  if (isInterruptedTaskStatus(status)) {
    status = 'failed'
    interrupted = true
  }
  const normalizedRunConfig = normalizeRunConfig(task.runConfig)
  const normalizedOutput = normalizeOutputPath(task.output)
  const normalizedFiles = task.files || []
  const workerOutputs = normalizeStemOutputs(task.outputs, task.input, windows)
  const normalizedOutputs = workerOutputs.length
    ? workerOutputs
    : outputsFromFiles(normalizedOutput, normalizedFiles, normalizedRunConfig?.outputFormat || 'wav', task.outputPrefix, task.input, windows)
  const meta = STAGE_META[status]
  const startedAt = normalizeTimestamp(task.startedAt)
  const finishedAt = normalizeTimestamp(task.finishedAt)
  const persistedDurationMs = normalizeDurationMs(task.durationMs)
  const durationMs = persistedDurationMs ?? (startedAt && finishedAt ? Math.max(0, finishedAt - startedAt) : undefined)
  const progress = typeof task.progress === 'number'
    ? Math.max(meta.progress, Math.min(100, task.progress))
    : meta.progress
  return {
    id: task.id || `task_${Date.now()}`,
    jobId: typeof task.jobId === 'string' && task.jobId.trim() ? task.jobId : undefined,
    resultId: typeof task.resultId === 'string' && task.resultId.trim() ? task.resultId : undefined,
    batchId: typeof task.batchId === 'string' && task.batchId.trim() ? task.batchId : undefined,
    ...(typeof task.batchIndex === 'number' && Number.isSafeInteger(task.batchIndex) && task.batchIndex >= 0 ? { batchIndex: task.batchIndex } : {}),
    model: task.model || '',
    input: task.input || '',
    jobOutput: typeof task.jobOutput === 'string' && task.jobOutput.trim() ? normalizeOutputPath(task.jobOutput) : undefined,
    outputPrefix: typeof task.outputPrefix === 'string' && task.outputPrefix.trim() ? task.outputPrefix : undefined,
    output: normalizedOutput,
    ...(Array.isArray(task.outputDirectories) ? { outputDirectories: task.outputDirectories.filter(path => typeof path === 'string' && path.trim()) } : {}),
    status,
    message: interrupted ? '上次运行未完成' : (task.message || meta.label),
    createdAt: task.createdAt || Date.now(),
    updatedAt: task.updatedAt || task.createdAt || Date.now(),
    startedAt,
    finishedAt,
    durationMs,
    progress: TERMINAL_STATUSES.includes(status) ? 100 : progress,
    stageLabel: interrupted ? '已中断' : (task.stageLabel || meta.label),
    progressCurrent: typeof task.progressCurrent === 'number' ? task.progressCurrent : undefined,
    progressTotal: typeof task.progressTotal === 'number' ? task.progressTotal : undefined,
    progressDetail: task.progressDetail || undefined,
    files: normalizedFiles,
    outputs: normalizedOutputs,
    logs: interrupted
      ? [...(task.logs || []), `${new Date().toLocaleTimeString()} 应用关闭或重启，任务已标记为中断。`].slice(-300)
      : (task.logs || []),
    error: interrupted ? '应用关闭或重启导致任务中断' : task.error,
    runConfig: normalizedRunConfig,
    nodeStates: task.nodeStates,
    nodeExecutionOrder: Array.isArray(task.nodeExecutionOrder)
      ? [...new Set(task.nodeExecutionOrder.filter(id => typeof id === 'string' && id.trim()))] : undefined,
    nodeLogs: task.nodeLogs,
    currentNodeId: interrupted ? undefined : task.currentNodeId,
    taskHidden: task.taskHidden === true,
    resultHidden: task.resultHidden === true,
  }
}

function normalizeRunConfig(runConfig?: SeparationRunConfig): SeparationRunConfig | undefined {
  if (!runConfig) return undefined
  const next: SeparationRunConfig = {
    ...runConfig,
    downloadMethod: runConfig.downloadMethod || 'aria2c',
    outputLayout: normalizeOutputLayout(runConfig.outputLayout),
    outputNaming: normalizeOutputNaming(runConfig.outputNaming),
    selectedStems: Array.isArray(runConfig.selectedStems)
      ? runConfig.selectedStems.map(item => String(item || '').trim()).filter(Boolean)
      : [],
    audioParams: { ...(runConfig.audioParams || {}) },
    inferenceParams: { ...(runConfig.inferenceParams || {}) },
  }
  const version = next.inferenceParamsVersion || 0
  if (version >= CURRENT_INFERENCE_PARAMS_VERSION) {
    next.inferenceParamsVersion = CURRENT_INFERENCE_PARAMS_VERSION
    return next
  }

  const params = { ...(next.inferenceParams || {}) }
  const hasStandardize = Object.prototype.hasOwnProperty.call(params, 'standardize')
  const hasNormalize = Object.prototype.hasOwnProperty.call(params, 'normalize')

  if (!hasStandardize && !hasNormalize) {
    // Legacy desktop only omitted `normalize` when the old input-standardize
    // checkbox was enabled. Preserve that behavior on retry/history restore.
    params.standardize = true
    params.normalize = false
  } else if (!hasStandardize && hasNormalize) {
    // Legacy desktop used `normalize` to mean input standardization.
    params.standardize = Boolean(params.normalize)
    params.normalize = false
  } else if (hasStandardize && !hasNormalize) {
    // First compatibility pass already switched to `standardize`, but output
    // normalize had not been made explicit yet.
    params.normalize = false
  }

  next.inferenceParams = params
  next.inferenceParamsVersion = CURRENT_INFERENCE_PARAMS_VERSION
  return next
}

function isVrModelType(modelType?: string | null) {
  return String(modelType || '').trim().toLowerCase() === 'vr'
}

function isApolloModelType(modelType?: string | null) {
  return String(modelType || '').trim().toLowerCase() === 'apollo'
}

type InferenceNumberField =
  | 'batch_size'
  | 'overlap_size'
  | 'num_overlap'
  | 'chunk_size'
  | 'window_size'
  | 'aggression'
  | 'post_process_threshold'

const ZERO_AS_UNSET_FIELDS = new Set<InferenceNumberField>([
  'batch_size',
  'overlap_size',
  'num_overlap',
  'chunk_size',
  'window_size',
])

function normalizeEditableNumberValue(
  current: number | null | undefined,
  options: { zeroMeansUnset: boolean },
) {
  const { zeroMeansUnset } = options
  if (typeof current !== 'number' || !Number.isFinite(current)) return null
  if (zeroMeansUnset && current <= 0) return null
  return current
}

function resolveNumberOverride(
  defaults: ModelDefaultInferenceParams | undefined,
  key: keyof ModelDefaultInferenceParams,
  current: number | null | undefined,
  options?: { zeroMeansUnset?: boolean },
) {
  const zeroMeansUnset = options?.zeroMeansUnset ?? false
  const normalizedCurrent = normalizeEditableNumberValue(current, { zeroMeansUnset })
  if (normalizedCurrent === null) return null
  const defaultValue = defaults?.[key]
  if (typeof defaultValue === 'number') return normalizedCurrent === defaultValue ? null : normalizedCurrent
  return normalizedCurrent
}

function resolveBooleanOverride(defaults: ModelDefaultInferenceParams | undefined, key: keyof ModelDefaultInferenceParams, current: boolean) {
  const defaultValue = defaults?.[key]
  if (typeof defaultValue === 'boolean') return current === defaultValue ? null : current
  return current ? true : null
}

function preferCurrentOverride<T>(
  currentOverride: T | null,
  modelOverrides: ModelDefaultInferenceParams,
  key: keyof ModelDefaultInferenceParams,
  useModelOverrideFallback: boolean,
) {
  if (currentOverride !== null) return currentOverride
  if (!useModelOverrideFallback) return null
  return Object.prototype.hasOwnProperty.call(modelOverrides, key) ? modelOverrides[key] as T : null
}

function applyModelDefaultsToUi(
  defaults: ModelDefaultInferenceParams | undefined,
  modelType?: string | null,
) {
  const vrModel = isVrModelType(modelType)
  const apolloModel = isApolloModelType(modelType)
  return {
    batch_size: defaults?.batch_size ?? (vrModel ? 2 : 1),
    overlap_size: vrModel ? 0 : (defaults?.overlap_size ?? 0),
    num_overlap: (vrModel || apolloModel) ? 0 : (defaults?.num_overlap ?? 0),
    chunk_size: vrModel ? 0 : (defaults?.chunk_size ?? 0),
    standardize: false,
    normalize: false,
    window_size: vrModel ? (defaults?.window_size ?? 512) : 0,
    aggression: vrModel ? (defaults?.aggression ?? 5) : 0,
    enable_post_process: vrModel ? (defaults?.enable_post_process ?? false) : false,
    post_process_threshold: vrModel ? (defaults?.post_process_threshold ?? 0.2) : 0,
    high_end_process: vrModel ? (defaults?.high_end_process ?? false) : false,
  }
}

function resolveTaskErrorMessage(code: unknown, message: unknown, input?: string) {
  const detail = typeof message === 'string' ? message : ''
  const path = typeof input === 'string' ? input : ''
  if (code === 'INPUT_AUDIO_STREAM_MISSING') {
    return i18n.global.t('separate.inputAudioStreamMissing', { path })
  }
  if (code === 'INPUT_MEDIA_UNSUPPORTED') {
    return i18n.global.t('separate.inputMediaUnsupported', { path })
  }
  return detail || i18n.global.t('toast.taskFailed')
}

export const useTaskStore = defineStore('task', () => {
  const initialized = ref(false)
  const tasks = ref<SeparationTask[]>([])
  const activeTaskId = ref<string | null>(null)
  // Session-only queue visibility. Persisted task/result history remains the
  // source for Results and recovery, while this set intentionally starts empty
  // on every application launch.
  const sessionTaskIds = ref<Set<string>>(new Set())
  const focusedResultTaskId = ref<string | null>(null)
  const focusedTaskId = ref<string | null>(null)
  const inputFiles = ref<string[]>([])
  const separateRunMode = ref<'model' | 'workflow'>('model')
  const ensembleEnabled = ref(false)
  const ensembleModels = ref<string[]>([])
  const ensembleStem = ref('')
  const ensembleModelStems = ref<Record<string, string>>({})
  const ensembleType = ref('avg_wave')
  const ensembleWeights = ref<Record<string, number>>({})
  // Retain directory overrides across route changes, but inherit Settings after restarting.
  const separateTemporaryOutputDir = ref('')
  const separateOutputDir = computed(() => resolveOutputDirectory(separateTemporaryOutputDir.value, useSettingsStore().outputDir, windowsPaths.value))
  const windowsPaths = computed(() => {
    const app = useAppStore()
    const platform = app.envInfo?.platform || app.runtimeInfo?.platform
      || (typeof navigator !== 'undefined' ? navigator.platform : '')
    return /^win/i.test(platform)
  })
  const separateOutputLayout = ref<OutputLayout>('flat')
  const separateOutputNamingTemplate = ref('%index%_%filename%_%stem%')
  const separateCustomStemOrder = ref<string[]>([])
  const modelListViewMode = ref<'card' | 'list'>('card')
  const modelListSortMode = ref<ModelListSortMode>('usage')
  const modelListPageSize = ref(12)
  const useTta = ref(false)
  const batch_size = ref<number | null>(1)
  const overlap_size = ref<number | null>(0)
  const num_overlap = ref<number | null>(0)
  const chunk_size = ref<number | null>(0)
  const standardize = ref(false)
  const normalize = ref(false)
  const selectedStems = ref<string[]>([])
  const window_size = ref<number | null>(0)
  const aggression = ref<number | null>(0)
  const enable_post_process = ref(false)
  const post_process_threshold = ref<number | null>(0)
  const high_end_process = ref(false)
  const selectedModelDefaults = ref<ModelDefaultInferenceParams>({})
  const selectedModelUiDefaults = ref<ModelInferenceUiDefaults>({})
  const selectedModelOverrides = ref<ModelDefaultInferenceParams>({})
  const selectedModelType = ref<string | null>(null)
  const selectedStemsCleared = ref(false)
  const inferenceParamsDirty = ref(false)
  const persistedSeparateModelState = ref<Record<string, PersistedSeparateModelState>>({})

  const inputPath = computed(() => inputFiles.value[0] || '')
  const activeTask = computed(() => tasks.value.find((task) => task.id === activeTaskId.value) || null)
  const completedTasks = computed(() => tasks.value.filter((task) => task.status === 'done'))
  const runningTasks = computed(() => tasks.value.filter((task) => !TERMINAL_STATUSES.includes(task.status)))
  const activeWorkerTasks = computed(() => tasks.value.filter((task) => !TERMINAL_STATUSES.includes(task.status) && task.status !== 'queued'))
  // 任务页可见任务：排除已在任务页隐藏的记录（结果页隐藏不影响任务页）
  const taskBoardTasks = computed(() => tasks.value.filter((task) => !task.taskHidden))
  // 结果页可见结果：仅含有输出且未在结果页隐藏的任务（任务页隐藏不影响结果页）
  const resultTasks = computed(() => completedTasks.value.filter((task) => !task.resultHidden && (task.outputs.length || task.files.length)))
  const queuedTasks = computed(() => tasks.value.filter((task) => task.status === 'queued'))
  const failedTasks = computed(() => tasks.value.filter((task) => task.status === 'failed'))
  const allJobs = computed(() => buildJobs(tasks.value))
  const resultJobs = computed(() => buildJobs(resultTasks.value))
  const sessionJobs = computed(() => buildJobs(tasks.value.filter(task => sessionTaskIds.value.has(task.id))))

  let saveTimer: ReturnType<typeof setTimeout> | null = null
  let progressPersistTimer: ReturnType<typeof setTimeout> | null = null
  let separateStateSaveTimer: ReturnType<typeof setTimeout> | null = null
  let applyingModelDefaults = false

  function separateStateSnapshot(): PersistedSeparateState {
    return {
      version: 1,
      runMode: separateRunMode.value,
      ensembleEnabled: ensembleEnabled.value,
      ensembleModels: [...ensembleModels.value],
      ensembleStem: ensembleStem.value,
      ensembleModelStems: { ...ensembleModelStems.value },
      ensembleType: ensembleType.value,
      ensembleWeights: { ...ensembleWeights.value },
      outputLayout: separateOutputLayout.value,
      outputNamingTemplate: separateOutputNamingTemplate.value,
      customStemOrder: [...separateCustomStemOrder.value],
      modelListViewMode: modelListViewMode.value,
      modelListSortMode: modelListSortMode.value,
      modelListPageSize: modelListPageSize.value,
      useTta: useTta.value,
      inferenceParamsByModel: JSON.parse(JSON.stringify(persistedSeparateModelState.value)),
    }
  }

  function persistSeparateState() {
    if (!initialized.value) return
    void saveAppStore('separate-state', separateStateSnapshot()).catch((error) => {
      studioLog.warn('Failed to persist separate settings', error)
    })
  }

  function queueSeparateStatePersist() {
    if (!initialized.value) return
    if (separateStateSaveTimer) clearTimeout(separateStateSaveTimer)
    separateStateSaveTimer = setTimeout(() => {
      separateStateSaveTimer = null
      persistSeparateState()
    }, 120)
  }

  function saveCurrentModelState(modelName: string) {
    const name = String(modelName || '').trim()
    if (!name) return
    const current = persistedSeparateModelState.value[name] || {}
    persistedSeparateModelState.value[name] = {
      ...current,
      selectedStems: [...selectedStems.value],
      selectedStemsCleared: selectedStemsCleared.value,
    }
    queueSeparateStatePersist()
  }

  function getSavedModelState(modelName: string) {
    return persistedSeparateModelState.value[String(modelName || '').trim()]
  }

  function buildJobs(sourceTasks: SeparationTask[]): SeparationJob[] {
    const groups = new Map<string, SeparationTask[]>()
    sourceTasks.forEach((task) => {
      const id = taskJobId(task)
      groups.set(id, [...(groups.get(id) || []), task])
    })
    return [...groups.entries()].map(([id, items]) => {
      const sorted = sortJobTasks(items)
      const primary = sorted[0]
      const output = primary.jobOutput || primary.output
      const progressTotal = sorted.reduce((sum, item) => {
        if (TERMINAL_STATUSES.includes(item.status)) return sum + 100
        if (item.status === 'queued') return sum
        return sum + Math.max(0, Math.min(99, Number(item.progress || 0)))
      }, 0)
      const status = resolveJobStatus(sorted)
      const startedValues = sorted.map(item => item.startedAt || item.createdAt).filter((value): value is number => typeof value === 'number')
      const finishedValues = sorted.map(item => item.finishedAt || item.updatedAt).filter((value): value is number => typeof value === 'number')
      const startedAt = startedValues.length ? Math.min(...startedValues) : undefined
      const finishedAt = TERMINAL_STATUSES.includes(status) && finishedValues.length ? Math.max(...finishedValues) : undefined
      return {
        id,
        output,
        tasks: sorted,
        primary,
        model: primary.model,
        inputCount: sorted.length,
        outputCount: sorted.reduce((sum, item) => sum + item.outputs.length, 0),
        createdAt: Math.min(...sorted.map(item => item.createdAt)),
        updatedAt: Math.max(...sorted.map(item => item.updatedAt)),
        startedAt,
        finishedAt,
        durationMs: startedAt && finishedAt ? Math.max(0, finishedAt - startedAt) : undefined,
        status,
        progress: Math.round(progressTotal / sorted.length),
      }
    })
  }

  function getJobById(id: string | null | undefined) {
    if (!id) return null
    return allJobs.value.find((job) => job.id === id) || null
  }

  function rememberSessionTasks(items: readonly Pick<SeparationTask, 'id'>[]) {
    if (!items.length) return
    const next = new Set(sessionTaskIds.value)
    items.forEach(item => next.add(item.id))
    sessionTaskIds.value = next
  }

  function removeSessionJob(jobId: string) {
    const next = new Set(sessionTaskIds.value)
    let removed = 0
    tasks.value.forEach((task) => {
      if (taskJobId(task) === jobId && next.delete(task.id)) removed += 1
    })
    if (removed) sessionTaskIds.value = next
    return removed
  }

  function clearFinishedSessionJobs(jobIds?: readonly string[]) {
    const allowedIds = jobIds ? new Set(jobIds) : null
    const finishedJobIds = new Set(sessionJobs.value
      .filter(job => isTerminalTaskStatus(job.status) && (!allowedIds || allowedIds.has(job.id)))
      .map(job => job.id))
    if (!finishedJobIds.size) return 0
    const next = new Set(sessionTaskIds.value)
    tasks.value.forEach((task) => {
      if (finishedJobIds.has(taskJobId(task))) next.delete(task.id)
    })
    sessionTaskIds.value = next
    return finishedJobIds.size
  }

  async function persistTasks() {
    if (!initialized.value) return
    await saveAppStore('task-history', {
      tasks: tasks.value.map((task) => ({ ...task, logs: task.logs.slice(-120) })),
    } satisfies PersistedTaskState)
  }

  function queuePersist() {
    if (!initialized.value) return
    if (saveTimer) clearTimeout(saveTimer)
    saveTimer = setTimeout(() => {
      saveTimer = null
      void persistTasks().catch(error => studioLog.warn('Failed to persist task history', error))
    }, 120)
  }

  function queueProgressPersist() {
    if (!initialized.value) return
    if (progressPersistTimer) return
    progressPersistTimer = setTimeout(() => {
      progressPersistTimer = null
      void persistTasks().catch(error => studioLog.warn('Failed to persist task history', error))
    }, 800)
  }

  async function initialize() {
    if (initialized.value) return
    const [stored, separateStored] = await Promise.all([
      loadAppStore<PersistedTaskState>('task-history'),
      loadAppStore<PersistedSeparateState>('separate-state'),
    ])
    // Older versions retained result-page deletions as hidden task records.
    tasks.value = (stored?.tasks || []).filter(task => !task.resultHidden).map((task) => normalizeTask(task, windowsPaths.value))
    activeTaskId.value = tasks.value[0]?.id || null
    separateRunMode.value = 'model'
    ensembleEnabled.value = separateStored?.ensembleEnabled === true
    ensembleModels.value = Array.isArray(separateStored?.ensembleModels)
      ? separateStored.ensembleModels.map(item => String(item || '').trim()).filter(Boolean)
      : []
    ensembleStem.value = String(separateStored?.ensembleStem || '')
    ensembleModelStems.value = Object.fromEntries(
      Object.entries(separateStored?.ensembleModelStems || {}).flatMap(([name, value]) => {
        const stem = String(value || '').trim()
        return stem ? [[name, stem]] : []
      }),
    )
    ensembleType.value = String(separateStored?.ensembleType || 'avg_wave')
    ensembleWeights.value = Object.fromEntries(
      Object.entries(separateStored?.ensembleWeights || {}).flatMap(([name, value]) => {
        const weight = Number(value)
        return Number.isFinite(weight) && weight >= 0 ? [[name, weight]] : []
      }),
    )
    separateOutputLayout.value = normalizeOutputLayout(separateStored?.outputLayout)
    separateOutputNamingTemplate.value = String(separateStored?.outputNamingTemplate || '%index%_%filename%_%stem%')
    separateCustomStemOrder.value = Array.isArray(separateStored?.customStemOrder)
      ? separateStored.customStemOrder.map(item => String(item || '').trim()).filter(Boolean)
      : []
    modelListViewMode.value = separateStored?.modelListViewMode === 'list' ? 'list' : 'card'
    modelListSortMode.value = ['recent', 'favorite', 'name-asc', 'name-desc'].includes(String(separateStored?.modelListSortMode))
      ? separateStored?.modelListSortMode as ModelListSortMode
      : 'usage'
    modelListPageSize.value = normalizePageSize(separateStored?.modelListPageSize, SEPARATE_MODEL_PAGE_SIZES, 12)
    useTta.value = separateStored?.useTta === true
    persistedSeparateModelState.value = Object.fromEntries(
      Object.entries(separateStored?.inferenceParamsByModel || {})
        .map(([name, value]) => {
          const normalized = normalizePersistedModelState(value)
          const state: PersistedSeparateModelState = {}
          if (Array.isArray(normalized.selectedStems)) state.selectedStems = [...normalized.selectedStems]
          if (typeof normalized.selectedStemsCleared === 'boolean') state.selectedStemsCleared = normalized.selectedStemsCleared
          return [name, state]
        }),
    )
    initialized.value = true
    if (stored?.tasks?.some(task => task.resultHidden)) queuePersist()
  }

  watch(
    [separateRunMode, ensembleEnabled, ensembleModels, ensembleStem, ensembleModelStems, ensembleType, ensembleWeights,
      separateOutputLayout, separateOutputNamingTemplate, separateCustomStemOrder, modelListViewMode, modelListSortMode, modelListPageSize, useTta],
    () => {
      if (applyingModelDefaults) return
      queueSeparateStatePersist()
    },
    { deep: true },
  )

  watch(
    [batch_size, overlap_size, num_overlap, chunk_size, standardize, normalize, window_size, aggression, enable_post_process,
      post_process_threshold, high_end_process],
    () => {
      if (applyingModelDefaults) return
      inferenceParamsDirty.value = true
      const modelStore = useModelStore()
      if (modelStore.selectedModel) saveCurrentModelState(modelStore.selectedModel)
    },
  )

  watch(
    [selectedStems, selectedStemsCleared],
    () => {
      if (applyingModelDefaults) return
      const modelStore = useModelStore()
      if (modelStore.selectedModel) saveCurrentModelState(modelStore.selectedModel)
      else queueSeparateStatePersist()
    },
    { deep: true },
  )

  watch(() => useSettingsStore().maxConcurrentSeparations, () => {
    scheduleQueue()
  })

  function touch(task: SeparationTask) {
    task.updatedAt = Date.now()
  }

  function applySelectedModelDefaults(
    baseDefaults: ModelDefaultInferenceParams | undefined,
    modelType?: string | null,
    saved?: PersistedSeparateModelState,
    modelOverrides?: ModelDefaultInferenceParams,
    options: { force?: boolean } = {},
  ) {
    selectedModelDefaults.value = baseDefaults ? { ...baseDefaults } : {}
    selectedModelOverrides.value = modelOverrides ? { ...modelOverrides } : {}
    selectedModelType.value = modelType ?? null
    const next = applyModelDefaultsToUi(baseDefaults, modelType)
    const modelOverrideValue = <K extends keyof ModelDefaultInferenceParams>(key: K) => (
      modelOverrides && Object.prototype.hasOwnProperty.call(modelOverrides, key) ? modelOverrides[key] : undefined
    )
    const modelDefaultValue = <K extends keyof ModelDefaultInferenceParams>(key: K) => (
      baseDefaults && Object.prototype.hasOwnProperty.call(baseDefaults, key) ? baseDefaults[key] : undefined
    )
    const savedValue = <K extends keyof ModelDefaultInferenceParams & keyof PersistedSeparateModelState>(key: K, fallback: PersistedSeparateModelState[K]) => (
      saved && Object.prototype.hasOwnProperty.call(saved, key) ? saved[key] : fallback
    )
    const modelOrSavedValue = <K extends keyof ModelDefaultInferenceParams & keyof PersistedSeparateModelState>(key: K) => {
      const override = modelOverrideValue(key)
      if (override !== undefined) return override
      const modelDefault = modelDefaultValue(key)
      if (modelDefault !== undefined) return modelDefault
      return savedValue(key, next[key])
    }
    const valueOrDefault = <T>(value: T | undefined, fallback: T): T => value === undefined ? fallback : value
    const resolvedDefaults = {
      batch_size: valueOrDefault(modelOrSavedValue('batch_size'), 1),
      overlap_size: valueOrDefault(modelOrSavedValue('overlap_size'), 0),
      num_overlap: valueOrDefault(modelOrSavedValue('num_overlap'), 0),
      chunk_size: valueOrDefault(modelOrSavedValue('chunk_size'), 0),
      standardize: valueOrDefault(modelOrSavedValue('standardize'), false),
      normalize: valueOrDefault(modelOrSavedValue('normalize'), false),
      window_size: valueOrDefault(modelOrSavedValue('window_size'), 0),
      aggression: valueOrDefault(modelOrSavedValue('aggression'), 0),
      enable_post_process: valueOrDefault(modelOrSavedValue('enable_post_process'), false),
      post_process_threshold: valueOrDefault(modelOrSavedValue('post_process_threshold'), 0),
      high_end_process: valueOrDefault(modelOrSavedValue('high_end_process'), false),
    }
    selectedModelUiDefaults.value = { ...resolvedDefaults }
    if (inferenceParamsDirty.value && !options.force) return

    applyingModelDefaults = true
    batch_size.value = resolvedDefaults.batch_size
    overlap_size.value = resolvedDefaults.overlap_size
    num_overlap.value = resolvedDefaults.num_overlap
    chunk_size.value = resolvedDefaults.chunk_size
    standardize.value = resolvedDefaults.standardize
    normalize.value = resolvedDefaults.normalize
    window_size.value = resolvedDefaults.window_size
    aggression.value = resolvedDefaults.aggression
    enable_post_process.value = resolvedDefaults.enable_post_process
    post_process_threshold.value = resolvedDefaults.post_process_threshold
    high_end_process.value = resolvedDefaults.high_end_process
    selectedStems.value = Array.isArray(saved?.selectedStems) ? [...saved.selectedStems] : []
    selectedStemsCleared.value = saved?.selectedStemsCleared === true
    inferenceParamsDirty.value = false
    void nextTick(() => {
      applyingModelDefaults = false
    })
  }

  function getCurrentUiInferenceDefaults() {
    const defaults = Object.fromEntries(
      Object.entries(selectedModelUiDefaults.value).map(([key, value]) => [key, value === null ? undefined : value]),
    ) as ModelDefaultInferenceParams
    return applyModelDefaultsToUi(defaults, selectedModelType.value)
  }

  function restoreInferenceNumberFallback(field: InferenceNumberField) {
    if (!ZERO_AS_UNSET_FIELDS.has(field)) return
    const defaults = getCurrentUiInferenceDefaults()
    const nextValue = defaults[field]
    if (typeof nextValue !== 'number' || !Number.isFinite(nextValue)) return
    switch (field) {
      case 'batch_size':
        if (normalizeEditableNumberValue(batch_size.value, { zeroMeansUnset: true }) === null) batch_size.value = nextValue
        break
      case 'overlap_size':
        if (normalizeEditableNumberValue(overlap_size.value, { zeroMeansUnset: true }) === null) overlap_size.value = nextValue
        break
      case 'num_overlap':
        if (normalizeEditableNumberValue(num_overlap.value, { zeroMeansUnset: true }) === null) num_overlap.value = nextValue
        break
      case 'chunk_size':
        if (normalizeEditableNumberValue(chunk_size.value, { zeroMeansUnset: true }) === null) chunk_size.value = nextValue
        break
      case 'window_size':
        if (normalizeEditableNumberValue(window_size.value, { zeroMeansUnset: true }) === null) window_size.value = nextValue
        break
      case 'aggression':
      case 'post_process_threshold':
        break
    }
  }

  function normalizeInferenceInputsBeforeSubmit() {
    restoreInferenceNumberFallback('batch_size')
    restoreInferenceNumberFallback('overlap_size')
    restoreInferenceNumberFallback('num_overlap')
    restoreInferenceNumberFallback('chunk_size')
    restoreInferenceNumberFallback('window_size')
  }

  function appendTaskLogs(task: SeparationTask, lines: LogEntry | LogEntry[]) {
    const settings = useSettingsStore()
    const values = Array.isArray(lines) ? lines : [lines]
    const normalized = values
      .flatMap<LogEntry>((line) => typeof line === 'string' ? line.split(/\r?\n/).map(value => value.trimEnd()).filter(Boolean) : [line])
      .filter(line => typeof line === 'string' || line.legacy || !task.logs.some(previous => typeof previous !== 'string' && previous.id === line.id))
    if (!normalized.length) return
    task.logs.push(...normalized)
    task.logs = task.logs.slice(-getLogLimit(settings.developerMode))
    touch(task)
    queuePersist()
  }

  function maxConcurrentSeparations() {
    const settings = useSettingsStore()
    return normalizeConcurrentSeparations(settings.maxConcurrentSeparations)
  }

  function buildRunConfig(inferenceParams: Record<string, unknown>, modelType?: string | null, outputLayout: OutputLayout = 'folders', outputNaming?: OutputNamingConfig): SeparationRunConfig {
    const settings = useSettingsStore()
    const app = useAppStore()
    const runtimeDevice = settings.getRuntimeDeviceConfig(app.envInfo)
    return {
      runMode: 'model',
      modelDir: settings.modelDir || null,
      downloadSource: settings.downloadSource,
      downloadMethod: settings.downloadMethod,
      modelType: modelType ?? null,
      outputLayout,
      outputNaming: normalizeOutputNaming(outputNaming),
      device: runtimeDevice.device,
      deviceIds: runtimeDevice.deviceIds,
      outputFormat: settings.defaultFormat,
      selectedStems: [...selectedStems.value],
      useTta: useTta.value,
      debug: settings.developerMode,
      audioParams: settings.getAudioParams(),
      inferenceParamsVersion: CURRENT_INFERENCE_PARAMS_VERSION,
      inferenceParams,
    }
  }

  function workflowModelInferenceParams(definition: Record<string, unknown>) {
    if (detectWorkflowFormat(definition) !== 'simple' || !Array.isArray(definition.steps)) {
      return {} as Record<string, Record<string, unknown>>
    }
    const modelStore = useModelStore()
    const result: Record<string, Record<string, unknown>> = {}
    for (const value of definition.steps) {
      if (!value || typeof value !== 'object' || Array.isArray(value)) continue
      const step = value as Record<string, unknown>
      const modelName = String(step.model || '').trim()
      if (!modelName || result[modelName]) continue
      const entry = modelStore.models.find(item => (
        item.name.localeCompare(modelName, undefined, { sensitivity: 'accent' }) === 0
        || item.aliases?.some(alias => alias.localeCompare(modelName, undefined, { sensitivity: 'accent' }) === 0)
      ))
      const overrides = modelStore.getModelInferenceOverrides(entry?.name || modelName)
      if (!overrides) continue
      const stepModelType = typeof step.model_type === 'string' ? step.model_type.trim() : ''
      const modelType = String(stepModelType || entry?.modelType || '').trim().toLowerCase()
      const allowed = new Set<keyof ModelDefaultInferenceParams>(modelType === 'vr'
        ? ['batch_size', 'window_size', 'aggression', 'enable_post_process', 'post_process_threshold', 'high_end_process', 'normalize']
        : ['batch_size', 'overlap_size', 'chunk_size', 'standardize', 'normalize'])
      const compatible = Object.fromEntries(
        Object.entries(overrides).filter(([key, value]) => {
          if (!allowed.has(key as keyof ModelDefaultInferenceParams)) return false
          if (['batch_size', 'overlap_size', 'chunk_size', 'window_size'].includes(key)) {
            return typeof value === 'number' && Number.isFinite(value) && value > 0
          }
          return true
        }),
      )
      if (Object.keys(compatible).length) result[modelName] = compatible
    }
    return result
  }

  function buildWorkflowRunConfig(workflow: WorkflowEntry, outputLayout: OutputLayout = 'folders', outputNaming?: OutputNamingConfig): SeparationRunConfig {
    const settings = useSettingsStore()
    const app = useAppStore()
    const runtimeDevice = settings.getRuntimeDeviceConfig(app.envInfo)
    const defaults = resolveWorkflowRuntimeDefaults(workflow.definition, {
      device: runtimeDevice.device,
      outputFormat: settings.defaultFormat || 'wav',
      modelDir: settings.modelDir || null,
    })
    return {
      runMode: 'workflow',
      modelDir: defaults.modelDir,
      downloadSource: settings.downloadSource,
      downloadMethod: settings.downloadMethod,
      workflowId: workflow.id,
      workflowName: workflow.name,
      workflowDefinition: prepareWorkflowDefinitionForRun(
        workflow.definition,
        {
          device: defaults.device,
          outputFormat: defaults.outputFormat,
          modelInferenceParams: workflowModelInferenceParams(workflow.definition),
        },
      ),
      outputLayout,
      outputNaming: normalizeOutputNaming(outputNaming),
      device: defaults.device,
      deviceIds: defaults.device === runtimeDevice.device || defaults.device === 'auto' ? runtimeDevice.deviceIds : [0],
      outputFormat: defaults.outputFormat,
      selectedStems: [],
      useTta: useTta.value,
      debug: settings.developerMode,
      audioParams: settings.getAudioParams(),
      inferenceParamsVersion: CURRENT_INFERENCE_PARAMS_VERSION,
      inferenceParams: {},
    }
  }

  function resolveTaskOutputPath(jobOutput: string, input: string, resultId: string, outputLayout: OutputLayout) {
    return outputLayout === 'flat'
      ? jobOutput
      : joinOutputPath(jobOutput, inputStemSegment(input, resultId, windowsPaths.value), windowsPaths.value)
  }

  function createQueuedTask(input: string, model: string, inferenceParams: Record<string, unknown>, modelType?: string | null, jobId?: string, jobOutput?: string, outputLayout: OutputLayout = 'folders', outputNaming?: OutputNamingConfig, batchIndex?: number) {
    const settings = useSettingsStore()
    const id = createRunId('sep')
    const resultId = createRunId('result')
    const resolvedJobOutput = jobOutput || resolveOutputDirectory(settings.outputDir, undefined, windowsPaths.value)
    const task: SeparationTask = {
      id,
      jobId,
      resultId,
      ...(typeof batchIndex === 'number' && Number.isSafeInteger(batchIndex) && batchIndex >= 0 ? { batchIndex } : {}),
      model,
      input,
      jobOutput: resolvedJobOutput,
      output: resolveTaskOutputPath(resolvedJobOutput, input, resultId, outputLayout),
      status: 'queued',
      message: 'Queued',
      createdAt: Date.now(),
      updatedAt: Date.now(),
      progress: STAGE_META.queued.progress,
      stageLabel: STAGE_META.queued.label,
      files: [],
      outputs: [],
      logs: [localLogRecord('Queued', 'task.queued', 'info', { taskId: id })],
      runConfig: buildRunConfig(inferenceParams, modelType, outputLayout, outputNaming),
    }
    tasks.value.unshift(task)
    rememberSessionTasks([task])
    activeTaskId.value = id
    queuePersist()
    return task
  }

  function createQueuedWorkflowTask(input: string, workflow: WorkflowEntry, jobId?: string, jobOutput?: string, outputLayout: OutputLayout = 'folders', outputNaming?: OutputNamingConfig, batchIndex?: number) {
    const settings = useSettingsStore()
    const id = createRunId('wf')
    const resultId = createRunId('result')
    const resolvedJobOutput = jobOutput || resolveOutputDirectory(settings.outputDir, undefined, windowsPaths.value)
    const task: SeparationTask = {
      id,
      jobId,
      resultId,
      ...(typeof batchIndex === 'number' && Number.isSafeInteger(batchIndex) && batchIndex >= 0 ? { batchIndex } : {}),
      model: workflow.name,
      input,
      jobOutput: resolvedJobOutput,
      output: resolveTaskOutputPath(resolvedJobOutput, input, resultId, outputLayout),
      status: 'queued',
      message: 'Queued',
      createdAt: Date.now(),
      updatedAt: Date.now(),
      progress: STAGE_META.queued.progress,
      stageLabel: STAGE_META.queued.label,
      files: [],
      outputs: [],
      logs: [localLogRecord(`Queued workflow: ${workflow.name}`, 'task.queued', 'info', { taskId: id })],
      runConfig: buildWorkflowRunConfig(workflow, outputLayout, outputNaming),
    }
    tasks.value.unshift(task)
    rememberSessionTasks([task])
    activeTaskId.value = id
    queuePersist()
    return task
  }

  async function startQueuedTask(taskId: string) {
    const task = tasks.value.find((item) => item.id === taskId)
    if (!task || task.status !== 'queued') return false
    rememberSessionTasks([task])
    const settings = useSettingsStore()
    const config = task.runConfig || buildRunConfig({})
    setTaskStatus(task.id, 'preparing', 'Preparing task')
    try {
      if (config.runMode === 'workflow') {
        validateWorkflowRunConfig(config)
        await invoke<{ taskId: string; started: boolean }>('start_workflow_inference', {
          payload: {
            taskId: task.id,
            workflowName: config.workflowName || task.model,
            workflow: config.workflowDefinition || {},
            input: task.input || null,
            ...(config.workbench && !config.workbench.nativeGraph ? { workbench: true, inputs: config.workbench.inputs, workbenchOutputs: config.workbench.outputs } : {}),
            output: config.outputLayout === 'folders' ? (task.jobOutput || task.output) : task.output,
            modelDir: config.modelDir ?? (settings.modelDir || null),
            source: config.downloadSource || settings.downloadSource,
            downloadMethod: config.downloadMethod || settings.downloadMethod,
            device: config.device,
            deviceIds: config.deviceIds,
            outputFormat: config.outputFormat,
            outputLayout: config.outputLayout,
            outputNaming: config.outputNaming,
            useTta: config.useTta,
            debug: config.debug,
            audioParams: config.audioParams,
          },
        })
        return true
      }
      await invoke<{ taskId: string; started: boolean }>('start_separation', {
        payload: {
          taskId: task.id,
          model: task.model,
          input: task.input,
          output: config.outputLayout === 'folders' ? (task.jobOutput || task.output) : task.output,
          modelDir: config.modelDir ?? (settings.modelDir || null),
          download: true,
          source: config.downloadSource || settings.downloadSource,
          downloadMethod: config.downloadMethod || settings.downloadMethod,
          endpoint: null,
          device: config.device,
          deviceIds: config.deviceIds,
          outputFormat: config.outputFormat,
          outputLayout: config.outputLayout,
          outputNaming: config.outputNaming,
          selectedStems: config.selectedStems || [],
          useTta: config.useTta,
          debug: config.debug,
          audioParams: config.audioParams,
          inferenceParamsVersion: config.inferenceParamsVersion ?? CURRENT_INFERENCE_PARAMS_VERSION,
          inferenceParams: config.inferenceParams || {},
        },
      })
      return true
    } catch (err) {
      task.status = 'failed'
      task.error = err instanceof Error ? err.message : String(err)
      task.message = task.error
      task.stageLabel = STAGE_META.failed.label
      task.progress = 100
      appendTaskLogs(task, `error: ${task.error}`)
      markTaskFinished(task)
      queuePersist()
      scheduleQueue()
      return false
    }
  }

  async function startBatchWorker(batchTasks: SeparationTask[]) {
    if (!batchTasks.length || batchTasks.some(task => task.status !== 'queued')) return false
    rememberSessionTasks(batchTasks)
    const settings = useSettingsStore()
    const primary = batchTasks[0]
    const config = primary.runConfig || buildRunConfig({})
    batchTasks.forEach((task) => {
      setTaskStatus(task.id, 'preparing', 'Preparing batch task')
    })
    try {
      await invoke<{ taskId: string; started: boolean }>('start_separation', {
        payload: {
          taskId: primary.id,
          model: primary.model,
          output: primary.jobOutput || primary.output,
          tasks: batchTasks.map((task, index) => ({
            taskId: task.id,
            input: task.input,
            output: task.output,
            inputIndex: index + 1,
          })),
          modelDir: config.modelDir ?? (settings.modelDir || null),
          download: true,
          source: config.downloadSource || settings.downloadSource,
          downloadMethod: config.downloadMethod || settings.downloadMethod,
          endpoint: null,
          device: config.device,
          deviceIds: config.deviceIds,
          outputFormat: config.outputFormat,
          outputLayout: config.outputLayout,
          outputNaming: config.outputNaming,
          selectedStems: config.selectedStems || [],
          useTta: config.useTta,
          debug: config.debug,
          audioParams: config.audioParams,
          inferenceParamsVersion: config.inferenceParamsVersion ?? CURRENT_INFERENCE_PARAMS_VERSION,
          inferenceParams: config.inferenceParams || {},
        },
      })
      return true
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      batchTasks.forEach((task) => {
        task.status = 'failed'
        task.error = message
        task.message = message
        task.stageLabel = STAGE_META.failed.label
        task.progress = 100
        appendTaskLogs(task, `error: ${message}`)
        markTaskFinished(task)
      })
      queuePersist()
      scheduleQueue()
      return false
    }
  }

  async function startWorkflowBatchWorker(batchTasks: SeparationTask[]) {
    if (!batchTasks.length || batchTasks.some(task => task.status !== 'queued')) return false
    rememberSessionTasks(batchTasks)
    const settings = useSettingsStore()
    const primary = batchTasks[0]
    const config = primary.runConfig || buildRunConfig({})
    batchTasks.forEach((task) => {
      setTaskStatus(task.id, 'preparing', 'Preparing workflow batch task')
    })
    try {
      for (const task of batchTasks) validateWorkflowRunConfig(task.runConfig || config)
      await invoke<{ taskId: string; started: boolean }>('start_workflow_inference', {
        payload: {
          taskId: primary.id,
          workflowName: config.workflowName || primary.model,
          workflow: config.workflowDefinition || {},
          output: primary.jobOutput || primary.output,
          ...(config.workbench && !config.workbench.nativeGraph ? { workbench: true } : {}),
          tasks: batchTasks.map((task, index) => ({
            taskId: task.id,
            input: task.input,
            output: task.output,
            inputIndex: index + 1,
            ...(task.runConfig?.workbench && !task.runConfig.workbench.nativeGraph ? { inputs: task.runConfig.workbench.inputs, workbenchOutputs: task.runConfig.workbench.outputs } : {}),
          })),
          modelDir: config.modelDir ?? (settings.modelDir || null),
          source: config.downloadSource || settings.downloadSource,
          downloadMethod: config.downloadMethod || settings.downloadMethod,
          device: config.device,
          deviceIds: config.deviceIds,
          outputFormat: config.outputFormat,
          outputLayout: config.outputLayout,
          outputNaming: config.outputNaming,
          useTta: config.useTta,
          debug: config.debug,
          audioParams: config.audioParams,
        },
      })
      return true
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      batchTasks.forEach((task) => {
        task.status = 'failed'
        task.error = message
        task.message = message
        task.stageLabel = STAGE_META.failed.label
        task.progress = 100
        appendTaskLogs(task, `error: ${message}`)
        markTaskFinished(task)
      })
      queuePersist()
      scheduleQueue()
      return false
    }
  }

  let schedulingQueue = false
  let queueScheduleRequested = false

  function scheduleQueue() {
    // Browser-only previews cannot start a Rust worker. Keep restored queue items
    // intact until the desktop runtime is available instead of failing them on boot.
    if (!isTauriRuntime()) return
    if (!isMainTaskOwner()) return
    if (schedulingQueue) {
      queueScheduleRequested = true
      return
    }
    schedulingQueue = true
    try {
      do {
        queueScheduleRequested = false
        const nextJobs = selectQueuedJobGroups(tasks.value, maxConcurrentSeparations())
        nextJobs.forEach((jobTasks) => {
          if (jobTasks.length === 1) {
            void startQueuedTask(jobTasks[0].id)
            return
          }
          if (jobTasks[0].runConfig?.runMode === 'workflow') {
            void startWorkflowBatchWorker(jobTasks)
          } else {
            void startBatchWorker(jobTasks)
          }
        })
        // Synchronous validation failures request another pass after the current selection is consumed.
      } while (queueScheduleRequested)
    } finally {
      schedulingQueue = false
    }
  }

  function setTaskStatus(id: string, status: TaskStatus, message?: string, progress?: number) {
    const settings = useSettingsStore()
    const task = tasks.value.find((item) => item.id === id)
    if (!task) return
    if (TERMINAL_STATUSES.includes(task.status) && !TERMINAL_STATUSES.includes(status)) return
    const meta = STAGE_META[status]
    if (status !== 'queued') markTaskStarted(task)
    task.status = status
    task.stageLabel = meta.label
    task.message = message || meta.label
    if (status !== 'separating') {
      task.progressCurrent = undefined
      task.progressTotal = undefined
      task.progressDetail = undefined
    }
    const nextProgress = progress ?? meta.progress
    const normalizedProgress = Math.min(99, Math.max(0, nextProgress))
    task.progress = status === 'done' || status === 'failed' || status === 'cancelled'
      ? 100
      : status === 'separating'
        ? normalizedProgress
        : Math.max(task.progress || 0, normalizedProgress)
    task.logs.push(localLogRecord(task.message, `task.${status}`, status === 'failed' ? 'error' : 'info', { taskId: task.id }))
    task.logs = task.logs.slice(-getLogLimit(settings.developerMode))
    touch(task)
    if (TERMINAL_STATUSES.includes(status)) markTaskFinished(task, task.updatedAt)
    queuePersist()
  }

  function handleWorkerEvent(event: any) {
    const taskId = event?.taskId
    if (!taskId) return
    const task = tasks.value.find((item) => item.id === taskId)
    if (!task) return
    if (event.type === 'task_started') {
      setTaskStatus(taskId, 'preparing', 'Task started', 6)
    } else if (event.type === 'task_stage') {
      const stage = normalizeStatus(event.payload?.stage)
      setTaskStatus(taskId, stage, event.payload?.message || STAGE_META[stage].label, event.payload?.progress)
    } else if (event.type === 'workflow_execution') {
      // Native graph execution supplies explicit lifecycle events. Do not infer
      // node completion from a progress fraction: output collection still runs
      // after execution_completed, and task_done remains the task terminal event.
      if (isTerminalTaskStatus(task.status) || task.runConfig?.runMode !== 'workflow') return
      const payload = event.payload || {}
      const executionEvent = typeof payload.event === 'string' ? payload.event : ''
      const knownExecutionEvent = [
        'execution_started', 'execution_completed', 'execution_failed', 'execution_cancelled',
        'node_started', 'node_progress', 'node_completed', 'node_failed', 'node_cancelled', 'node_skipped',
      ].includes(executionEvent)
      if (payload.version !== 1 || !knownExecutionEvent) return
      const nodeEvent = executionEvent.startsWith('node_')
      const nodeId = typeof payload.nodeId === 'string' && payload.nodeId.trim() ? payload.nodeId : undefined
      const message = typeof payload.message === 'string' ? payload.message : ''
      const fraction = typeof payload.overallFraction === 'number' && Number.isFinite(payload.overallFraction)
        ? clamp(payload.overallFraction, 0, 1) : undefined
      task.status = 'separating'
      markTaskStarted(task)
      task.stageLabel = STAGE_META.separating.label
      task.message = message || STAGE_META.separating.label
      if (fraction !== undefined) task.progress = Math.max(task.progress || 0, Math.min(90, 35 + fraction * 55))
      const done = Number(payload.done)
      const total = Number(payload.total)
      if (payload.unit === 'seconds' && Number.isFinite(done) && Number.isFinite(total)) {
        task.progressCurrent = done
        task.progressTotal = total
        task.progressDetail = message || undefined
      } else {
        task.progressCurrent = undefined
        task.progressTotal = undefined
        task.progressDetail = undefined
      }
      if (nodeId && nodeEvent) {
        task.nodeStates ||= {}
        const previous = task.nodeStates[nodeId]
        if (!previous) (task.nodeExecutionOrder ||= []).push(nodeId)
        const nodeFraction = typeof payload.nodeProgress === 'number' && Number.isFinite(payload.nodeProgress)
          ? clamp(payload.nodeProgress, 0, 1) : undefined
        const nodeStatus = executionEvent === 'node_completed' ? 'done'
          : executionEvent === 'node_failed' ? 'failed'
            : executionEvent === 'node_cancelled' ? 'cancelled'
              : executionEvent === 'node_skipped' ? 'skipped'
                : 'running'
        const progress = nodeStatus === 'done' ? 100
          : nodeFraction === undefined ? (previous?.progress || 0) : nodeFraction * 100
        task.nodeStates[nodeId] = { status: nodeStatus, progress, ...(message ? { message } : {}) }
        if (nodeStatus === 'running' || nodeStatus === 'failed') task.currentNodeId = nodeId
        else if (task.currentNodeId === nodeId) task.currentNodeId = undefined
        if (message && ['node_started', 'node_completed', 'node_failed', 'node_cancelled', 'node_skipped'].includes(executionEvent)) {
          const prefix = executionEvent === 'node_completed' ? 'Node completed: '
            : executionEvent === 'node_failed' ? 'Node failed: '
              : executionEvent === 'node_cancelled' ? 'Node cancelled: '
                : executionEvent === 'node_skipped' ? 'Node skipped: ' : 'Node started: '
          appendWorkflowNodeLogs(task, nodeId, localLogRecord(`${prefix}${message}`, executionEvent, nodeStatus === 'failed' ? 'error' : 'info', { taskId: task.id, nodeId }))
        }
      }
      touch(task)
      queueProgressPersist()
    } else if (event.type === 'task_progress') {
      // Terminal states are monotonic. A worker may flush a progress line after
      // cancellation or failure has already been observed; never resurrect it.
      if (isTerminalTaskStatus(task.status)) return
      const stage = normalizeStatus(event.payload?.stage)
      const done = Number(event.payload?.done)
      const total = Number(event.payload?.total)
      const detail = typeof event.payload?.message === 'string' ? event.payload.message : undefined
      const nodeId = typeof event.payload?.nodeId === 'string' ? event.payload.nodeId : undefined
      const nodeProgress = event.payload?.nodeProgress
      if (nodeId && typeof nodeProgress === 'number' && Number.isFinite(nodeProgress)) {
        const fraction = clamp(nodeProgress, 0, 1)
        task.nodeStates ||= {}
        const previous = task.nodeStates[nodeId]
        if (!previous) (task.nodeExecutionOrder ||= []).push(nodeId)
        const complete = fraction >= 1 && event.payload?.audioIndex == null && event.payload?.unit == null
        const status = complete ? 'done' : 'running'
        if (detail && (!previous || previous.status !== status || previous.message !== detail)) {
          const transition = status === 'done' && previous?.status !== 'done' ? 'Node completed: ' : !previous ? 'Node started: ' : ''
          appendWorkflowNodeLogs(task, nodeId, localLogRecord(`${transition}${detail}`, `node.${status}`, 'info', { taskId: task.id, nodeId }))
        }
        task.nodeStates[nodeId] = { status, progress: fraction * 100, message: detail }
        task.currentNodeId = complete ? undefined : nodeId
      }
      task.status = stage
      if (stage !== 'queued') markTaskStarted(task)
      task.stageLabel = STAGE_META[stage].label
      task.message = detail || STAGE_META[stage].label
      task.progressCurrent = Number.isFinite(done) ? done : undefined
      task.progressTotal = Number.isFinite(total) ? total : undefined
      task.progressDetail = detail
      const explicitProgress = event.payload?.progress
      const resolvedProgress = typeof explicitProgress === 'number' && Number.isFinite(explicitProgress)
        ? clamp(explicitProgress, 0, 99)
        : Math.min(99, resolveStageProgress(stage, task.progressCurrent, task.progressTotal))
      task.progress = stage === 'separating'
        ? resolvedProgress
        : Math.max(task.progress || 0, resolvedProgress)
      touch(task)
      queueProgressPersist()
    } else if (event.type === 'task_log' || event.type === 'worker_log') {
      const record = workerLogRecord(event)
      if ((record.level === 'debug' || record.level === 'trace') && !useSettingsStore().developerMode) return
      appendTaskLogs(task, record)
      appendWorkflowNodeLogs(task, record.nodeId, record)
    } else if (event.type === 'error') {
      if (isTerminalTaskStatus(task.status)) return
      if (task.runConfig?.workbench && Array.isArray(event.payload?.outputs)) {
        task.outputs = normalizeStemOutputs(event.payload.outputs, task.input, windowsPaths.value)
        task.files = task.outputs.map(output => output.path)
      }
      if (typeof event.payload?.nodeId === 'string' && task.nodeStates?.[event.payload.nodeId]) {
        task.nodeStates[event.payload.nodeId].status = 'failed'
      }
      if (task.currentNodeId && task.nodeStates?.[task.currentNodeId]) {
        task.nodeStates[task.currentNodeId].status = 'failed'
      }
      const codeValue = event.payload?.code
      const code = codeValue ? `[${codeValue}] ` : ''
      const message = resolveTaskErrorMessage(codeValue, event.payload?.message, task.input)
      const detail = event.payload?.detail || ''
      const errorRecord = event.payload?.logRecord ? workerLogRecord(event) : normalizeLogRecord({
        schemaVersion: 1, id: `${task.id}-error-${task.logs.length}`, timestamp: event.timestamp || new Date().toISOString(),
        source: 'worker', level: 'error', logger: 'worker', event: 'task.failed', message: `${code}${message}`,
        detail: String(detail), taskId: task.id, nodeId: event.payload?.nodeId || task.currentNodeId, context: {},
      })
      appendWorkflowNodeLogs(task, typeof event.payload?.nodeId === 'string' ? event.payload.nodeId : task.currentNodeId,
        errorRecord)
      const recoverable = Boolean(event.payload?.recoverable)
      task.error = message
      task.message = message
      task.progressCurrent = undefined
      task.progressTotal = undefined
      task.progressDetail = undefined
      if (recoverable) {
        task.stageLabel = task.stageLabel || STAGE_META.failed.label
        task.progress = Math.min(99, Math.max(task.progress || 0, 1))
      } else {
        task.status = 'failed'
        task.stageLabel = STAGE_META.failed.label
        task.progress = 100
      }
      if (!recoverable) markTaskFinished(task)
      appendTaskLogs(task, errorRecord)
    } else if (event.type === 'task_done') {
      if (isTerminalTaskStatus(task.status)) return
      task.status = 'done'
      markTaskStarted(task)
      task.message = 'Done'
      task.stageLabel = STAGE_META.done.label
      task.progress = 100
      task.progressCurrent = undefined
      task.progressTotal = undefined
      task.progressDetail = undefined
      task.files = event.payload?.files || []
      if (Array.isArray(event.payload?.nodeValues)) task.nodeValues = event.payload.nodeValues
      if (event.payload?.outputDir) {
        task.output = event.payload.outputDir
      }
      if (task.runConfig?.workbench && Array.isArray(event.payload?.outputDirectories)) {
        task.outputDirectories = event.payload.outputDirectories.filter((path: unknown): path is string => typeof path === 'string' && Boolean(path.trim()))
        if (task.outputDirectories?.length) task.output = task.outputDirectories[0]
      }
      if (typeof event.payload?.outputRoot === 'string' && event.payload.outputRoot) {
        task.jobOutput = event.payload.outputRoot
      } else if (task.runConfig?.outputLayout === 'flat' && event.payload?.outputDir) {
        task.jobOutput = event.payload.outputDir
      }
      const normalizedOutputs = normalizeStemOutputs(event.payload?.outputs, task.input, windowsPaths.value)
      task.outputs = normalizedOutputs.length
        ? normalizedOutputs
        : outputsFromFiles(task.output, task.files, event.payload?.outputFormat || 'wav', task.outputPrefix, task.input, windowsPaths.value)
      task.error = undefined
      touch(task)
      markTaskFinished(task, task.updatedAt)
      queuePersist()
    } else if (event.type === 'task_cancelled') {
      if (isTerminalTaskStatus(task.status)) return
      if (task.currentNodeId && task.nodeStates?.[task.currentNodeId]) task.nodeStates[task.currentNodeId].status = 'cancelled'
      appendWorkflowNodeLogs(task, task.currentNodeId, localLogRecord(event.payload?.message || 'Cancelled', 'task.cancelled', 'info', { taskId: task.id, nodeId: task.currentNodeId }))
      task.status = 'cancelled'
      markTaskStarted(task)
      task.message = event.payload?.message || 'Cancelled'
      task.stageLabel = STAGE_META.cancelled.label
      task.progress = 100
      task.progressCurrent = undefined
      task.progressTotal = undefined
      task.progressDetail = undefined
      touch(task)
      markTaskFinished(task, task.updatedAt)
      queuePersist()
    }
    if (TERMINAL_STATUSES.includes(task.status)) scheduleQueue()
  }

  function addInputFiles(paths: string[]) {
    const valid = paths.filter((p) => p?.trim() && isSupportedInputPath(p))
    if (!valid.length) return 0
    const existing = new Set(inputFiles.value)
    const additions = valid.filter((p) => !existing.has(p))
    if (additions.length) inputFiles.value = [...inputFiles.value, ...additions]
    return additions.length
  }

  function removeInputFile(path: string) {
    inputFiles.value = inputFiles.value.filter((p) => p !== path)
  }

  function clearInputFiles() {
    inputFiles.value = []
  }

  function moveInputFile(fromIndex: number, toIndex: number) {
    const from = Math.trunc(Number(fromIndex))
    const to = Math.trunc(Number(toIndex))
    if (from === to || from < 0 || to < 0 || from >= inputFiles.value.length || to >= inputFiles.value.length) return false
    const next = [...inputFiles.value]
    const [item] = next.splice(from, 1)
    next.splice(to, 0, item)
    inputFiles.value = next
    return true
  }

  async function pickFiles() {
    const files = await invoke<string[]>('pick_media_files')
    addInputFiles(files || [])
    return files?.length || 0
  }

  async function scanAndAddPaths(paths: string[]) {
    if (!paths?.length) return { added: 0, warnings: [] as string[] }
    const result = await invoke<ScanMediaPathsResult>('scan_media_paths', { paths })
    return { added: addInputFiles(result.files || []), warnings: result.warnings || [] }
  }

  async function pickInputFolder() {
    const folder = await invoke<string | null>('pick_input_folder')
    if (!folder) return 0
    const result = await scanAndAddPaths([folder])
    return result.added
  }

  async function addPaths(paths: string[]) {
    const result = await scanAndAddPaths(paths)
    return result.added
  }

  async function revealPath(path: string) {
    await invoke('reveal_path', { path })
  }

  async function trashPaths(paths: string[]) {
    const valid = paths.filter((p) => p?.trim())
    if (!valid.length) return { trashed: [] as string[], failed: [] as string[] }
    return invoke<{ trashed: string[]; failed: string[] }>('move_paths_to_trash', { paths: valid, emptyDirs: [] })
  }

  async function trashResultPaths(paths: string[], emptyDirs: string[] = []) {
    const valid = paths.filter((p) => p?.trim())
    const validEmptyDirs = emptyDirs.filter((p) => p?.trim())
    if (!valid.length && !validEmptyDirs.length) return { trashed: [] as string[], failed: [] as string[] }
    return invoke<{ trashed: string[]; failed: string[] }>('move_paths_to_trash', { paths: valid, emptyDirs: validEmptyDirs })
  }

  // 删除结果时始终仅回收当前结果对应的输出文件，避免误删共享目录、
  // 任务目录中的附加文件或后续派生产物。
  function resultTrashTargets(task: SeparationTask): string[] {
    const seen = new Set<string>()
    return task.outputs
      .map((output) => output.path)
      .filter((path): path is string => Boolean(path))
      .filter((path) => {
        if (seen.has(path)) return false
        seen.add(path)
        return true
      })
  }

  function resultTrashTargetsForTasks(items: SeparationTask[]): { paths: string[]; retained: number } {
    const removing = getRemovalTasks(items.map(item => item.id))
    const removingIds = new Set(removing.map(item => item.id))
    const windows = windowsPaths.value
    const protectedPaths = new Set<string>()
    const protectedDirectories = new Set<string>()
    for (const item of tasks.value) {
      if (item.input) protectedPaths.add(pathKey(item.input, windows))
      for (const input of Object.values(item.runConfig?.workbench?.inputs || {})) {
        if (input) protectedPaths.add(pathKey(input, windows))
      }
      const definition = item.runConfig?.workflowDefinition || item.runConfig?.workbench?.sourceDefinition
      if (definition) {
        for (const node of workflowInputNodes(definition)) {
          if (node.embeddedPath) protectedPaths.add(pathKey(node.embeddedPath, windows))
        }
        if (!removingIds.has(item.id)) {
          for (const node of Array.isArray(definition.nodes) ? definition.nodes : []) {
            if (node?.type === 'pymss_load_audio_batch' && typeof node.widgets_values?.[0] === 'string') {
              const directory = pathKey(node.widgets_values[0], windows)
              if (directory) protectedDirectories.add(directory.endsWith('/') ? directory : `${directory}/`)
            }
          }
        }
      }
      if (!removingIds.has(item.id)) {
        for (const output of item.outputs) protectedPaths.add(pathKey(output.path, windows))
      }
    }
    const seen = new Set<string>()
    let retained = 0
    const directories = [...protectedDirectories]
    const paths = removing.flatMap(resultTrashTargets).filter(path => {
      const key = pathKey(path, windows)
      if (seen.has(key)) return false
      seen.add(key)
      if (protectedPaths.has(key) || directories.some(directory => key.startsWith(directory))) {
        retained += 1
        return false
      }
      return true
    })
    return { paths, retained }
  }

  function resultTrashEmptyDirs(task: SeparationTask): string[] {
    // Node-bound directories can have unrelated roots; only recycle explicit output files.
    if (task.runConfig?.workbench) return []
    if (task.runConfig?.outputLayout !== 'folders') return []
    const jobOutput = task.jobOutput
    const output = task.output
    const windows = windowsPaths.value
    if (output && !samePath(output, jobOutput, windows)) return cleanupDirChain(output, jobOutput, windows)

    const parents = task.outputs
      .map((item) => parentDir(item.path || '', windows))
      .filter(Boolean)
    const dirs = parents.flatMap(parent => cleanupDirChain(parent, jobOutput, windows))
    const seen = new Set<string>()
    return dirs.filter((dir) => {
      const key = pathKey(dir, windows)
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
  }

  function primaryRevealPath(task: SeparationTask) {
    return task.runConfig?.workbench ? task.outputs[0]?.path || task.output : task.output
  }

  function getTaskById(id: string) {
    return tasks.value.find((task) => task.id === id) || null
  }

  function focusResultTask(id: string | null) {
    focusedResultTaskId.value = id
  }

  function focusTask(id: string | null) {
    focusedTaskId.value = id
  }

  // 任务是否还会出现在结果页（done + 有输出 + 未被结果页隐藏）。
  // 不会出现在结果页的任务，其“结果维度”视为已不存在。
  function hasResultPresence(task: SeparationTask) {
    return task.status === 'done'
      && !task.resultHidden
      && (task.outputs.length > 0 || task.files.length > 0)
  }

  // 当一个任务在任务页与结果页都不再可见时，从底层数组彻底回收，避免无限堆积。
  function reclaimHiddenTasks() {
    tasks.value = tasks.value.filter((task) => !task.taskHidden || hasResultPresence(task))
    const retainedIds = new Set(tasks.value.map(task => task.id))
    const nextSessionIds = new Set([...sessionTaskIds.value].filter(id => retainedIds.has(id)))
    if (nextSessionIds.size !== sessionTaskIds.value.size) sessionTaskIds.value = nextSessionIds
  }

  function removeTask(id: string) {
    const target = tasks.value.find((task) => task.id === id)
    if (!target) return
    // 任务页删除仅隐藏日志记录，不影响结果页与磁盘文件
    target.taskHidden = true
    if (activeTaskId.value === id) activeTaskId.value = taskBoardTasks.value[0]?.id || null
    if (focusedTaskId.value === id) focusedTaskId.value = null
    reclaimHiddenTasks()
    queuePersist()
  }

  function removeTasks(ids: string[]) {
    const removeSet = new Set(ids)
    if (!removeSet.size) return 0
    let removed = 0
    tasks.value.forEach((task) => {
      if (removeSet.has(task.id) && !task.taskHidden) {
        task.taskHidden = true
        removed += 1
      }
    })
    if (!removed) return 0
    if (activeTaskId.value && removeSet.has(activeTaskId.value)) {
      activeTaskId.value = taskBoardTasks.value[0]?.id || null
    }
    if (focusedTaskId.value && removeSet.has(focusedTaskId.value)) {
      focusedTaskId.value = null
    }
    reclaimHiddenTasks()
    queuePersist()
    return removed
  }

  function getRemovalTasks(ids: string[]) {
    const selected = new Set(ids)
    const jobs = new Set(tasks.value.filter(task => selected.has(task.id)).map(taskJobId))
    const related = tasks.value.filter(task => jobs.has(taskJobId(task)))
    if (related.some(task => !isTerminalTaskStatus(task.status))) {
      throw new Error(i18n.global.t('results.removeRunningBatch'))
    }
    return related
  }

  // Result deletion owns the complete run record, including failed siblings.
  // Editor projects and optional file recycling are handled before this call.
  function removeResult(id: string) {
    return removeResults([id])
  }

  async function removeResults(ids: string[]) {
    const related = getRemovalTasks(ids)
    if (!related.length) return 0
    const removedIds = new Set(related.map(task => task.id))
    const previous = [...tasks.value]
    tasks.value = tasks.value.filter(task => !removedIds.has(task.id))
    try {
      await persistTasks()
    } catch (error) {
      // Preserve concurrent progress and newly queued work when restoring records.
      const current = new Map(tasks.value.map(task => [task.id, task]))
      const previousIds = new Set(previous.map(task => task.id))
      tasks.value = [
        ...tasks.value.filter(task => !previousIds.has(task.id)),
        ...previous.flatMap(task => current.has(task.id) ? [current.get(task.id)!] : removedIds.has(task.id) ? [task] : []),
      ]
      queuePersist()
      throw error
    }
    sessionTaskIds.value = new Set([...sessionTaskIds.value].filter(id => !removedIds.has(id)))
    if (activeTaskId.value && removedIds.has(activeTaskId.value)) activeTaskId.value = taskBoardTasks.value[0]?.id || null
    if (focusedTaskId.value && removedIds.has(focusedTaskId.value)) focusedTaskId.value = null
    if (focusedResultTaskId.value && removedIds.has(focusedResultTaskId.value)) focusedResultTaskId.value = null
    return related.length
  }

  async function removeWorkflowRun(workflowId: string, jobId: string) {
    const job = getJobById(jobId)
    if (!job) return
    if (!job.tasks.every(task => task.runConfig?.workbench && task.runConfig.workflowId === workflowId)) {
      throw new Error(i18n.global.t('workbench.runNotFound'))
    }
    if (job.tasks.some(hasResultPresence)) throw new Error(i18n.global.t('workbench.removeRunHasResults'))
    await removeResults(job.tasks.map(task => task.id))
  }

  async function removeSeparationRuns(jobIds: string[]) {
    const selected = new Set(jobIds)
    const related = tasks.value.filter(task => selected.has(taskJobId(task)))
    if (related.some(task => task.runConfig?.workbench)) throw new Error(i18n.global.t('workbench.runNotFound'))
    if (related.some(hasResultPresence)) throw new Error(i18n.global.t('workbench.removeRunHasResults'))
    return removeResults(related.map(task => task.id))
  }

  function clearHistory() {
    // 仅清空任务页的历史记录（标记 taskHidden），保留结果页数据
    tasks.value.forEach((task) => {
      if (TERMINAL_STATUSES.includes(task.status)) task.taskHidden = true
    })
    reclaimHiddenTasks()
    queuePersist()
  }

  // Empty selections must not turn into a request to remove all results.
  function clearResults(ids?: string[]) {
    return removeResults(ids ?? resultTasks.value.map(task => task.id))
  }

  async function cancelAllTasks() {
    const currentRunning = [...runningTasks.value]
    if (!currentRunning.length) return { total: 0, cancelled: 0 }
    let cancelled = 0
    for (const item of currentRunning) {
      // 串行取消，避免同时调用底层取消造成竞态
      // eslint-disable-next-line no-await-in-loop
      const ok = await cancelTask(item.id)
      if (ok) cancelled += 1
    }
    return { total: currentRunning.length, cancelled }
  }

  async function cancelTask(id: string) {
    const task = tasks.value.find((item) => item.id === id)
    if (!task) return false
    if (task.status === 'queued') {
      task.status = 'cancelled'
      task.message = 'Cancelled'
      task.stageLabel = STAGE_META.cancelled.label
      task.progress = 100
      markTaskFinished(task)
      appendTaskLogs(task, 'Cancelled before execution')
      queuePersist()
      scheduleQueue()
      return true
    }
    const cancelled = await invoke<boolean>('cancel_task', { taskId: id })
    if (cancelled) {
      if (task.currentNodeId && task.nodeStates?.[task.currentNodeId]) task.nodeStates[task.currentNodeId].status = 'cancelled'
      appendWorkflowNodeLogs(task, task.currentNodeId, '[info] Cancelled')
      task.status = 'cancelled'
      task.message = 'Cancelled'
      task.stageLabel = STAGE_META.cancelled.label
      task.progress = 100
      touch(task)
      markTaskFinished(task, task.updatedAt)
      queuePersist()
      scheduleQueue()
    }
    return cancelled
  }

  async function cancelWorkbenchJob(jobId: string) {
    const targets = tasks.value.filter(task => (task.jobId || task.id) === jobId && !isTerminalTaskStatus(task.status))
    // Remove the entire queued remainder before any cancellation can resume scheduling.
    for (const target of targets.filter(task => task.status === 'queued')) setTaskStatus(target.id, 'cancelled', 'Cancelled')
    for (const target of targets) {
      if (!isTerminalTaskStatus(target.status)) await cancelTask(target.id)
    }
    scheduleQueue()
  }

  function buildInferenceParams(modelType?: string | null): Record<string, unknown> {
    const inferenceParams: Record<string, unknown> = {}
    const defaults = selectedModelDefaults.value
    const modelOverrides = selectedModelOverrides.value
    const useModelOverrideFallback = !inferenceParamsDirty.value
    const vrModel = isVrModelType(modelType)
    const apolloModel = isApolloModelType(modelType)

    const batchSizeOverride = preferCurrentOverride(
      resolveNumberOverride(defaults, 'batch_size', batch_size.value, { zeroMeansUnset: true }),
      modelOverrides,
      'batch_size',
      useModelOverrideFallback,
    )
    if (batchSizeOverride !== null) inferenceParams.batch_size = batchSizeOverride

    if (vrModel) {
      const windowSizeOverride = preferCurrentOverride(
        resolveNumberOverride(defaults, 'window_size', window_size.value, { zeroMeansUnset: true }),
        modelOverrides,
        'window_size',
        useModelOverrideFallback,
      )
      const aggressionOverride = preferCurrentOverride(
        resolveNumberOverride(defaults, 'aggression', aggression.value),
        modelOverrides,
        'aggression',
        useModelOverrideFallback,
      )
      const enablePostProcessOverride = preferCurrentOverride(
        resolveBooleanOverride(defaults, 'enable_post_process', enable_post_process.value),
        modelOverrides,
        'enable_post_process',
        useModelOverrideFallback,
      )
      const postProcessThresholdOverride = preferCurrentOverride(
        resolveNumberOverride(defaults, 'post_process_threshold', post_process_threshold.value),
        modelOverrides,
        'post_process_threshold',
        useModelOverrideFallback,
      )
      const highEndProcessOverride = preferCurrentOverride(
        resolveBooleanOverride(defaults, 'high_end_process', high_end_process.value),
        modelOverrides,
        'high_end_process',
        useModelOverrideFallback,
      )
      const normalizeOverride = preferCurrentOverride(
        resolveBooleanOverride(defaults, 'normalize', normalize.value),
        modelOverrides,
        'normalize',
        useModelOverrideFallback,
      )

      if (windowSizeOverride !== null) inferenceParams.window_size = windowSizeOverride
      if (aggressionOverride !== null) inferenceParams.aggression = aggressionOverride
      if (enablePostProcessOverride !== null) inferenceParams.enable_post_process = enablePostProcessOverride
      if (postProcessThresholdOverride !== null) inferenceParams.post_process_threshold = postProcessThresholdOverride
      if (highEndProcessOverride !== null) inferenceParams.high_end_process = highEndProcessOverride
      if (normalizeOverride !== null) inferenceParams.normalize = normalizeOverride
      return inferenceParams
    }

    const overlapSizeOverride = preferCurrentOverride(
      resolveNumberOverride(defaults, 'overlap_size', overlap_size.value, { zeroMeansUnset: true }),
      modelOverrides,
      'overlap_size',
      useModelOverrideFallback,
    )
    const numOverlapOverride = apolloModel
      ? null
      : preferCurrentOverride(
          resolveNumberOverride(defaults, 'num_overlap', num_overlap.value, { zeroMeansUnset: true }),
          modelOverrides,
          'num_overlap',
          useModelOverrideFallback,
        )
    const chunkSizeOverride = preferCurrentOverride(
      resolveNumberOverride(defaults, 'chunk_size', chunk_size.value, { zeroMeansUnset: true }),
      modelOverrides,
      'chunk_size',
      useModelOverrideFallback,
    )
    const standardizeOverride = preferCurrentOverride(
      resolveBooleanOverride(defaults, 'standardize', standardize.value),
      modelOverrides,
      'standardize',
      useModelOverrideFallback,
    )
    const normalizeOverride = preferCurrentOverride(
      resolveBooleanOverride(defaults, 'normalize', normalize.value),
      modelOverrides,
      'normalize',
      useModelOverrideFallback,
    )

    if (overlapSizeOverride !== null) inferenceParams.overlap_size = overlapSizeOverride
    if (numOverlapOverride !== null) inferenceParams.num_overlap = numOverlapOverride
    if (chunkSizeOverride !== null) inferenceParams.chunk_size = chunkSizeOverride
    if (standardizeOverride !== null) inferenceParams.standardize = standardizeOverride
    if (normalizeOverride !== null) inferenceParams.normalize = normalizeOverride
    return inferenceParams
  }

  function modelInferenceParamsFromState(modelName: string, modelType?: string | null): Record<string, unknown> {
    const modelStore = useModelStore()
    if (modelStore.selectedModel === modelName) return buildInferenceParams(modelType)
    const entry = modelStore.models.find(item => item.name === modelName) || null
    const defaults = modelStore.hasKnownModelInferenceBase(modelName)
      ? (modelStore.getModelBaseInferenceDefaults(modelName) || {})
      : (entry?.defaultInferenceParams || {})
    const overrides = modelStore.getModelInferenceOverrides(modelName) || {}
    const saved = getSavedModelState(modelName) || {}
    const source = { ...saved, ...overrides }
    const params: Record<string, unknown> = {}
    const vrModel = isVrModelType(modelType)
    const apolloModel = isApolloModelType(modelType)

    const numberParam = (key: keyof ModelDefaultInferenceParams, options?: { zeroMeansUnset?: boolean }) => {
      const value = source[key]
      if (typeof value !== 'number' || !Number.isFinite(value)) return
      if (options?.zeroMeansUnset && value <= 0) return
      if (defaults[key] === value) return
      params[key] = value
    }
    const booleanParam = (key: keyof ModelDefaultInferenceParams) => {
      const value = source[key]
      if (typeof value !== 'boolean') return
      if (defaults[key] === value) return
      if (!value && defaults[key] === undefined) return
      params[key] = value
    }

    numberParam('batch_size', { zeroMeansUnset: true })
    if (vrModel) {
      numberParam('window_size', { zeroMeansUnset: true })
      numberParam('aggression')
      numberParam('post_process_threshold')
      booleanParam('enable_post_process')
      booleanParam('high_end_process')
      booleanParam('normalize')
      return params
    }

    numberParam('overlap_size', { zeroMeansUnset: true })
    if (!apolloModel) numberParam('num_overlap', { zeroMeansUnset: true })
    numberParam('chunk_size', { zeroMeansUnset: true })
    booleanParam('standardize')
    booleanParam('normalize')
    return params
  }

  function submitOne(input: string, model: string, inferenceParams: Record<string, unknown>, modelType?: string | null, jobId?: string, jobOutput?: string, outputLayout: OutputLayout = 'folders', outputNaming?: OutputNamingConfig, batchIndex?: number) {
    return createQueuedTask(input, model, inferenceParams, modelType, jobId, jobOutput, outputLayout, outputNaming, batchIndex)
  }

  async function startSeparation(options: { outputDir?: string; outputLayout?: OutputLayout; outputNaming?: OutputNamingConfig } = {}) {
    const modelStore = useModelStore()
    if (!inputFiles.value.length) {
      throw new Error('Input file is required')
    }
    if (!modelStore.selectedModel.trim()) {
      throw new Error('Model is required')
    }
    const model = modelStore.selectedModel
    const selectedEntry = modelStore.selectedInfo?.name === model
      ? modelStore.selectedInfo
      : modelStore.models.find((item) => item.name === model) || null
    const modelType = selectedEntry?.modelType || null
    normalizeInferenceInputsBeforeSubmit()
    const inferenceParams = buildInferenceParams(modelType)
    const targets = [...inputFiles.value]
    const jobId = createRunId('job')
    const outputLayout = normalizeOutputLayout(options.outputLayout)
    const jobOutput = resolveOutputDirectory(options.outputDir, separateOutputDir.value, windowsPaths.value)
    const outputNaming = normalizeOutputNaming(options.outputNaming)
    const createdTasks = targets.map((input, batchIndex) => submitOne(
      input,
      model,
      inferenceParams,
      modelType,
      jobId,
      jobOutput,
      outputLayout,
      outputNaming,
      batchIndex,
    ))
    modelStore.recordModelUse(model)
    scheduleQueue()
    return { succeeded: targets.length, failed: 0, total: targets.length, jobId, tasks: createdTasks }
  }

  function workflowDefinitionValidationError(definition: Record<string, unknown>): string {
    const issue = getWorkflowDefinitionIssue(definition)
    if (issue === 'steps-required') return i18n.global.t('workflows.stepsRequired')
    if (issue === 'no-save-outputs') return i18n.global.t('workflows.workflowNoSaveOutputs')
    if (issue === 'invalid-definition') return i18n.global.t('workflows.workflowFormatInvalid')
    if (issue === 'invalid-format') return i18n.global.t('workflows.workflowFormatInvalid')
    return ''
  }

  function validateSeparateWorkflowInput(definition: Record<string, unknown>) {
    const issue = getSeparateWorkflowInputIssue(definition)
    if (issue) throw new Error(i18n.global.t(
      issue.code === 'multiple-inputs' ? 'separate.workflowMultipleInputs' : 'separate.workflowNoInput',
      { count: issue.count },
    ))
  }

  function validateWorkflowRunConfig(config: SeparationRunConfig) {
    const definition = config.workflowDefinition || {}
    const validationError = workflowDefinitionValidationError(definition)
    if (validationError) throw new Error(validationError)
    if (!config.workbench) validateSeparateWorkflowInput(definition)
  }

  async function startWorkflowInference(workflow: WorkflowEntry, options: { outputDir?: string; outputLayout?: OutputLayout; outputNaming?: OutputNamingConfig } = {}) {
    if (!workflow?.id) {
      throw new Error('Workflow is required')
    }
    const validationError = workflowDefinitionValidationError(workflow.definition)
    if (validationError) throw new Error(validationError)
    validateSeparateWorkflowInput(workflow.definition)
    const targets = [...inputFiles.value]
    if (!targets.length) {
      throw new Error(i18n.global.t('separate.startHintNoInput'))
    }
    const jobId = createRunId('job')
    const outputLayout = normalizeOutputLayout(options.outputLayout)
    const jobOutput = resolveOutputDirectory(options.outputDir, separateOutputDir.value, windowsPaths.value)
    const outputNaming = normalizeOutputNaming(options.outputNaming)
    const createdTasks = targets.map((input, batchIndex) => createQueuedWorkflowTask(
      input,
      workflow,
      jobId,
      jobOutput,
      outputLayout,
      outputNaming,
      batchIndex,
    ))
    scheduleQueue()
    return { succeeded: targets.length, failed: 0, total: targets.length, jobId, tasks: createdTasks }
  }
  async function startWorkbenchInference(workflow: WorkflowEntry, bindings: WorkflowRuntimeBindings) {
    if (!isMainTaskOwner()) throw new Error('workbench.mainWindowOnly')
    const validationError = workflowDefinitionValidationError(workflow.definition)
    if (validationError) throw new Error(validationError)
    const nativeGraph = detectWorkflowFormat(workflow.definition) === 'graph'
    const directory = resolveOutputDirectory(bindings.outputDirectory, useSettingsStore().outputDir, windowsPaths.value)
    const prepared = prepareWorkbenchRun(workflow.definition, bindings, directory, windowsPaths.value)
    const jobId = createRunId('job')
    const runtimeEntry = { ...workflow, definition: prepared.definition }
    const createdTasks = prepared.runs.map((run, batchIndex) => {
      const created = createQueuedWorkflowTask(run.input, runtimeEntry, jobId, directory, nativeGraph ? 'flat' : bindings.outputLayout, undefined, batchIndex)
      const config = tasks.value.find(task => task.id === created.id)!.runConfig!
      config.useTta = false
      if (nativeGraph) config.workflowDefinition = cloneWorkflow(workflow.definition)
      config.workbench = {
        version: 1, ...(nativeGraph ? { nativeGraph: true as const } : {}), sourceUpdatedAt: workflow.updatedAt,
        sourceDefinition: cloneWorkflow(workflow.definition), bindings: cloneWorkflow(bindings),
        inputs: cloneWorkflow(run.inputs), outputs: cloneWorkflow(run.outputs),
      }
      return created
    })
    queuePersist()
    scheduleQueue()
    return { jobId, tasks: createdTasks, total: createdTasks.length }
  }

  async function retryTask(taskId: string) {
    const existing = tasks.value.find((t) => t.id === taskId)
    if (!existing) return
    if (existing.runConfig?.workbench && existing.runConfig.workflowDefinition) {
      const config = cloneWorkflow(existing.runConfig)
      const snapshot = config.workbench!
      const created = createQueuedWorkflowTask(existing.input, {
        id: config.workflowId!, name: config.workflowName || existing.model, description: '',
        definition: snapshot.sourceDefinition, format: detectWorkflowFormat(snapshot.sourceDefinition),
        formatVersion: WORKFLOW_FORMAT_VERSION, createdAt: existing.createdAt, updatedAt: snapshot.sourceUpdatedAt,
      }, undefined, existing.jobOutput || existing.output, config.outputLayout, config.outputNaming)
      const queued = tasks.value.find(task => task.id === created.id)!
      queued.runConfig = config
      queued.output = existing.output
      scheduleQueue()
      return created
    }
    const outputLayout = normalizeOutputLayout(existing.runConfig?.outputLayout)
    const windows = windowsPaths.value
    const isAbsolute = (path: string) => windows ? /^(?:[A-Za-z]:[/\\]|[/\\]{2})/.test(path) : path.startsWith('/')
    let jobOutput = existing.jobOutput
    if (!jobOutput || !isAbsolute(jobOutput)) {
      const output = existing.output
      if (!isAbsolute(output)) throw new Error(i18n.global.t('separate.retryOutputDirectoryUnknown'))
      if (outputLayout === 'folders') {
        const folderName = output.split(windows ? /[/\\]/ : /[/]/).filter(Boolean).pop()
        const inputName = inputStemSegment(existing.input, existing.resultId || existing.id, windows)
        const matchesInput = windows ? folderName?.toLowerCase() === inputName.toLowerCase() : folderName === inputName
        if (!matchesInput) throw new Error(i18n.global.t('separate.retryOutputDirectoryUnknown'))
        jobOutput = output.replace(windows ? /[^/\\]+[/\\]*$/ : /[^/]+[/]*$/, '')
        if (jobOutput.length > 1 && !(windows && /^[A-Za-z]:[/\\]$/.test(jobOutput))) {
          jobOutput = jobOutput.replace(windows ? /[/\\]$/ : /[/]$/, '')
        }
      } else {
        jobOutput = output
      }
    }
    if (existing.runConfig?.runMode === 'workflow' && existing.runConfig.workflowId && existing.runConfig.workflowName && existing.runConfig.workflowDefinition) {
      validateSeparateWorkflowInput(existing.runConfig.workflowDefinition)
      const task = createQueuedWorkflowTask(existing.input, {
        id: existing.runConfig.workflowId,
        name: existing.runConfig.workflowName,
        description: '',
        definition: existing.runConfig.workflowDefinition,
        format: detectWorkflowFormat(existing.runConfig.workflowDefinition),
        formatVersion: WORKFLOW_FORMAT_VERSION,
        createdAt: existing.createdAt,
        updatedAt: existing.updatedAt,
      }, undefined, jobOutput, outputLayout, existing.runConfig.outputNaming)
      scheduleQueue()
      return task
    }
    const modelStore = useModelStore()
    modelStore.selectedModel = existing.model
    const retryParams = existing.runConfig?.inferenceParams || {}
    const persistedModelType = existing.runConfig?.modelType ?? null
    const modelEntry = modelStore.models.find((item) => item.name === existing.model) || null
    const modelType = modelEntry?.modelType || persistedModelType || null
    const task = createQueuedTask(existing.input, existing.model, retryParams, modelType, undefined, jobOutput, outputLayout, existing.runConfig?.outputNaming)
    scheduleQueue()
    return task
  }

  return {
    cancelWorkbenchJob,
    startWorkbenchInference,
    initialized,
    tasks,
    activeTaskId,
    activeTask,
    completedTasks,
    runningTasks,
    activeWorkerTasks,
    taskBoardTasks,
    resultTasks,
    allJobs,
    resultJobs,
    sessionJobs,
    queuedTasks,
    failedTasks,
    focusedResultTaskId,
    focusedTaskId,
    inputFiles,
    inputPath,
    separateRunMode,
    ensembleEnabled,
    ensembleModels,
    ensembleStem,
    ensembleModelStems,
    ensembleType,
    ensembleWeights,
    separateTemporaryOutputDir,
    separateOutputDir,
    separateOutputLayout,
    separateOutputNamingTemplate,
    separateCustomStemOrder,
    modelListViewMode,
    modelListSortMode,
    modelListPageSize,
    useTta,
    batch_size,
    overlap_size,
    num_overlap,
    chunk_size,
    standardize,
    normalize,
    selectedStems,
    selectedStemsCleared,
    window_size,
    aggression,
    enable_post_process,
    post_process_threshold,
    high_end_process,
    restoreInferenceNumberFallback,
    initialize,
    handleWorkerEvent,
    pickFiles,
    pickInputFolder,
    addPaths,
    addInputFiles,
    moveInputFile,
    removeInputFile,
    clearInputFiles,
    revealPath,
    trashPaths,
    trashResultPaths,
    resultTrashEmptyDirs,
    resultTrashTargets,
    resultTrashTargetsForTasks,
    primaryRevealPath,
    getTaskById,
    getJobById,
    taskJobId,
    focusResultTask,
    focusTask,
    removeTask,
    removeTasks,
    removeSessionJob,
    clearFinishedSessionJobs,
    removeResult,
    removeResults,
    getRemovalTasks,
    removeWorkflowRun,
    removeSeparationRuns,
    clearResults,
    clearHistory,
    cancelTask,
    cancelAllTasks,
    startSeparation,
    startWorkflowInference,
    retryTask,
    scheduleQueue,
    applySelectedModelDefaults,
    saveCurrentModelState,
    getSavedModelState,
    normalizeInferenceInputsBeforeSubmit,
  }
})
