<script setup lang="ts">
import { computed, nextTick, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { useDialog, useMessage, type DropdownOption } from 'naive-ui'
import { convertFileSrc } from '@tauri-apps/api/core'
import { getCurrentWebview } from '@tauri-apps/api/webview'
import type { UnlistenFn } from '@tauri-apps/api/event'
import {
  CubeOutline,
  GitNetworkOutline,
  CheckmarkCircle,
  PlayOutline,
  MusicalNotesOutline,
  SearchOutline,
  FolderOutline,
  CloudUploadOutline,
  CloseOutline,
  SettingsOutline,
  OpenOutline,
  PauseOutline,
  TerminalOutline,
  TimeOutline,
  ReorderFourOutline,
  ChevronUpOutline,
  ChevronDownOutline,
  GridOutline,
  ListOutline,
} from '@vicons/ionicons5'
import { useTaskStore, type ModelListSortMode, type OutputLayout, type SeparationTask, type StemOutput } from '@/stores/task'
import { resolveJobStatus } from '@/features/tasks/lifecycle'
import { useWorkflowStore, type WorkflowEntry } from '@/stores/workflow'
import { WORKFLOW_FORMAT_VERSION } from '@/workflows/formats'
import {
  getWorkflowDefinitionIssue,
  resolveWorkflowRuntimeDefaults,
} from '@/workflows/runtimeDefinition'
import { useSettingsStore } from '@/stores/settings'
import { useModelStore, type ModelDefaultInferenceParams, type ModelEntry } from '@/stores/model'
import { useAppStore } from '@/stores/app'
import { buildModelCategoryOptionsFromModels, getModelCategoryLabel } from '@/utils/modelCategory'
import { matchesModelQuery } from '@/utils/modelSearch'
import { sortStemOutputsByOrder } from '@/utils/stemOrder'
import { retainAvailableModelNames } from '@/utils/modelSource'
import AppBrandMark from '@/components/AppBrandMark.vue'

const { t, locale } = useI18n()
const message = useMessage()
const dialog = useDialog()
const router = useRouter()
const route = useRoute()
const task = useTaskStore()
const model = useModelStore()
const workflow = useWorkflowStore()
const settings = useSettingsStore()
const app = useAppStore()

const {
  inputFiles,
  separateRunMode: runMode,
  ensembleEnabled,
  ensembleModels,
  ensembleStem,
  ensembleModelStems,
  ensembleType,
  ensembleWeights,
  separateTemporaryOutputDir: temporaryOutputDir,
  separateOutputLayout: outputLayout,
  separateOutputNamingTemplate: outputNamingTemplate,
  separateCustomStemOrder: customStemOrder,
  useTta,
  batch_size,
  overlap_size,
  num_overlap,
  chunk_size,
  standardize,
  normalize,
  window_size,
  aggression,
  enable_post_process,
  post_process_threshold,
  high_end_process,
  selectedStems,
  modelListViewMode,
  modelListSortMode,
} = storeToRefs(task)
const { selectedModel, downloadedModels, models: modelEntries, isLoading, modelsLoaded, detailLoading, modelPreferences } = storeToRefs(model)
const { workflows, selectedWorkflow, selectedWorkflowId } = storeToRefs(workflow)

const isDragging = ref(false)
const showSettingsDrawer = ref(false)
const showEnsembleModal = ref(false)
const showNamingModal = ref(false)
const showLogModal = ref(false)
const modelSearch = ref('')
const modelCategoryFilter = ref('')
const modelPage = ref(1)
const modelPageSize = ref(12)
const workflowSearch = ref('')
const contextMenuX = ref(0)
const contextMenuY = ref(0)
const contextMenuVisible = ref(false)
const contextMenuKind = ref<'model' | 'workflow' | null>(null)
const contextModel = ref<ModelEntry | null>(null)
const contextWorkflow = ref<WorkflowEntry | null>(null)
const showModelNoteEditor = ref(false)
const noteEditorModel = ref<ModelEntry | null>(null)
const noteDraft = ref('')
const showWorkflowMetaEditor = ref(false)
const workflowMetaEditor = ref<WorkflowEntry | null>(null)
const workflowNameDraft = ref('')
const workflowNoteDraft = ref('')
const workflowMetaSaving = ref(false)
if (route.query.mode === 'workflow') runMode.value = 'workflow'
const focusedSeparationJobId = ref<string | null>(null)
const cancellingTaskId = ref<string | null>(null)
const audioElements = new Map<string, HTMLAudioElement>()
const playingOutputPath = ref('')
const outputPlayback = ref<Record<string, { currentTime: number; duration: number }>>({})
const draggedInputIndex = ref<number | null>(null)
const inputDragPointerId = ref<number | null>(null)
const draggedStemIndex = ref<number | null>(null)
const stemDragPointerId = ref<number | null>(null)
const namingTemplateInputRef = ref<any>(null)
const STEM_ORDER_ROW_HEIGHT = 43
const WINDOWS_RESERVED_FILENAMES = new Set([
  'CON',
  'PRN',
  'AUX',
  'NUL',
  ...Array.from({ length: 9 }, (_, index) => `COM${index + 1}`),
  ...Array.from({ length: 9 }, (_, index) => `LPT${index + 1}`),
])
const MAX_FILENAME_PART_BYTES = 200
let unlistenDragDrop: UnlistenFn | null = null

const formatOptions = [
  { label: 'WAV', value: 'wav' },
  { label: 'FLAC', value: 'flac' },
  { label: 'MP3', value: 'mp3' },
  { label: 'M4A', value: 'm4a' },
]

function getFileKindLabel(path: string) {
  const ext = path.split('.').pop()?.toLowerCase() || ''
  if (['mp4', 'mkv', 'mov', 'avi', 'webm', 'flv'].includes(ext)) return t('separate.videoFile')
  return t('separate.audioFile')
}

const wavBitDepthOptions = computed(() => [
  { label: t('audio.pcm16'), value: 'PCM_16' },
  { label: t('audio.pcm24'), value: 'PCM_24' },
  { label: t('audio.float'), value: 'FLOAT' },
])
const flacBitDepthOptions = computed(() => [
  { label: t('audio.pcm16'), value: 'PCM_16' },
  { label: t('audio.pcm24'), value: 'PCM_24' },
])
const bitRateOptions = computed(() => [
  { label: t('audio.bitrate128'), value: '128k' },
  { label: t('audio.bitrate192'), value: '192k' },
  { label: t('audio.bitrate256'), value: '256k' },
  { label: t('audio.bitrate320'), value: '320k' },
  { label: t('audio.bitrate512'), value: '512k' },
])
const m4aCodecOptions = computed(() => [
  { label: t('audio.codecAac'), value: 'aac' },
])
const selectedModelName = computed(() => String(selectedModel.value || ''))
const ensembleTypeOptions = [
  'avg_wave', 'median_wave', 'min_wave', 'max_wave',
  'avg_fft', 'median_fft', 'min_fft', 'max_fft',
].map(value => ({ label: value, value }))
const runModeOptions = computed(() => [
  { label: t('separate.runModeModel'), value: 'model' },
  { label: t('separate.runModeWorkflow'), value: 'workflow' },
])
const modelSortOptions = computed(() => [
  { label: t('separate.modelSortUsage'), value: 'usage' },
  { label: t('separate.modelSortRecent'), value: 'recent' },
  { label: t('separate.modelSortFavorite'), value: 'favorite' },
  { label: t('separate.modelSortNameAsc'), value: 'name-asc' },
  { label: t('separate.modelSortNameDesc'), value: 'name-desc' },
] as Array<{ label: string; value: ModelListSortMode }>)
const saveAsFolder = computed({
  get: () => outputLayout.value === 'folders',
  set: (value: boolean) => {
    outputLayout.value = value ? 'folders' : 'flat'
  },
})
const effectiveOutputLayout = computed<OutputLayout>(() => outputLayout.value)
function compareModelName(a: string, b: string) {
  return a.localeCompare(b, locale.value === 'zh-CN' ? 'zh-CN' : 'en')
}

const listedDownloadedModels = computed(() => {
  return [...downloadedModels.value].sort((a, b) => {
    const prefA = modelPreferences.value[a.name] || {}
    const prefB = modelPreferences.value[b.name] || {}
    const favoriteDelta = Number(Boolean(prefB.favorite)) - Number(Boolean(prefA.favorite))
    const useDelta = Number(prefB.useCount || 0) - Number(prefA.useCount || 0)
    const recentDelta = Number(prefB.lastUsedAt || 0) - Number(prefA.lastUsedAt || 0)
    if (modelListSortMode.value === 'favorite') return favoriteDelta || recentDelta || compareModelName(a.name, b.name)
    if (modelListSortMode.value === 'recent') return recentDelta || useDelta || favoriteDelta || compareModelName(a.name, b.name)
    if (modelListSortMode.value === 'name-desc') return compareModelName(b.name, a.name)
    if (modelListSortMode.value === 'name-asc') return compareModelName(a.name, b.name)
    return useDelta || recentDelta || favoriteDelta || compareModelName(a.name, b.name)
  })
})
const selectedModelListItem = computed(() => listedDownloadedModels.value.find(item => item.name === selectedModelName.value) || null)
const modelDownloaded = computed(() => Boolean(selectedModelListItem.value))
const currentModelInfo = computed(() => {
  if (model.selectedInfo?.name === selectedModelName.value) return model.selectedInfo
  return selectedModelListItem.value
})
const currentModelDefaults = computed(() => currentModelInfo.value?.defaultInferenceParams || {})
const currentModelDefaultsResolved = computed(() => Boolean(currentModelInfo.value?.defaultInferenceParamsResolved))
const currentModelType = computed(() => String(currentModelInfo.value?.modelType || '').trim().toLowerCase())
const isVrModel = computed(() => currentModelType.value === 'vr')
const isApolloModel = computed(() => currentModelType.value === 'apollo')
const showStandardizeField = computed(() => Boolean(currentModelInfo.value) && !isVrModel.value)
const showNormalizeField = computed(() => Boolean(currentModelInfo.value))
const hasVisibleAdvancedFields = computed(() => (
  Object.keys(currentModelDefaults.value).some(key => !['standardize', 'normalize'].includes(key))
))
const shouldPrefetchAdvancedParams = computed(() => (
  Boolean(currentModelInfo.value?.downloaded)
  && (!currentModelDefaultsResolved.value || !String(currentModelInfo.value?.configInstruments || '').trim())
))
const advancedParamsLoading = computed(() => shouldPrefetchAdvancedParams.value && detailLoading.value)
function hasInferenceField(key: string) {
  if (key === 'standardize' || key === 'normalize') return false
  if (key === 'num_overlap' && isApolloModel.value) return false
  return Object.prototype.hasOwnProperty.call(currentModelDefaults.value, key)
}
function parseModelInstruments(value?: unknown) {
  const seen = new Set<string>()
  const rawItems = Array.isArray(value)
    ? value
    : (() => {
        const text = String(value || '').trim()
        if (!text) return []
        if (text.startsWith('[')) {
          try {
            const parsed = JSON.parse(text)
            if (Array.isArray(parsed)) return parsed
          } catch {
            // Fall through to delimiter parsing for Python-style list strings.
          }
        }
        return text.split(/[,，;；/|\n]+/)
      })()
  return rawItems
    .map(item => String(item || '').trim().replace(/^[\s"'[\](){}]+|[\s"'[\](){}]+$/g, ''))
    .filter((item) => {
      if (!item) return false
      const key = item.toLowerCase()
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
}
const availableStemNames = computed(() => parseModelInstruments(currentModelInfo.value?.configInstruments))
const ensembleModelEntries = computed(() => ensembleModels.value
  .map(name => listedDownloadedModels.value.find(item => item.name === name))
  .filter((item): item is (typeof listedDownloadedModels.value)[number] => Boolean(item)))
const ensembleStemOptionsByModel = computed(() => Object.fromEntries(
  ensembleModelEntries.value.map(item => [
    item.name,
    parseModelInstruments(item.configInstruments).map(stem => ({ label: stem, value: stem })),
  ]),
))
const ensembleReady = computed(() => ensembleEnabled.value
  && ensembleModels.value.length >= 2
  && ensembleModelEntries.value.length === ensembleModels.value.length
  && Boolean(ensembleStem.value.trim())
  && ensembleModels.value.every((name) => {
    const selectedStem = String(ensembleModelStems.value[name] || '').trim()
    const options = ensembleStemOptionsByModel.value[name] || []
    return Boolean(selectedStem) && options.some(option => option.value.toLowerCase() === selectedStem.toLowerCase())
  }))
const selectedStemSummary = computed(() => {
  if (!selectedStems.value.length) return t('separate.allStems')
  return selectedStems.value.join(', ')
})
const checkedOutputStems = computed<string[]>({
  get() {
    if (!selectedStems.value.length) return [...availableStemNames.value]
    return selectedStems.value
  },
  set(value) {
    const allowed = new Set(availableStemNames.value)
    const next = value.filter(stem => allowed.has(stem))
    selectedStems.value = next.length === availableStemNames.value.length ? [] : next
  },
})
const selectedOutputStemCount = computed(() => checkedOutputStems.value.length)
const selectedStemDetail = computed(() => {
  if (selectedStems.value.length > 6) {
    return t('separate.selectedStemCount', {
      count: selectedOutputStemCount.value,
      total: availableStemNames.value.length,
    })
  }
  return selectedStemSummary.value
})

const namingTokens = computed(() => [
  { value: '%index%', label: t('separate.namingTokenIndex') },
  { value: '%input_number%', label: t('separate.namingTokenInputNumber') },
  { value: '%filename%', label: t('separate.namingTokenFilename') },
  { value: '%stem%', label: t('separate.namingTokenStem') },
  { value: '%model%', label: t('separate.namingTokenModel') },
  { value: '%yyyyMMdd%', label: t('separate.namingTokenDate') },
  { value: '%hhmmss%', label: t('separate.namingTokenTime') },
  { value: '%ddmmss%', label: t('separate.namingTokenLegacyTime') },
])

function normalizeStemOrder(order: string[], stems: string[]) {
  const byKey = new Map(stems.map(stem => [stem.toLowerCase(), stem]))
  const used = new Set<string>()
  const next: string[] = []
  order.forEach((stem) => {
    const key = stem.toLowerCase()
    const resolved = byKey.get(key)
    if (!resolved || used.has(key)) return
    used.add(key)
    next.push(resolved)
  })
  stems.forEach((stem) => {
    const key = stem.toLowerCase()
    if (!used.has(key)) next.push(stem)
  })
  return next
}

const orderedOutputStems = computed(() => runMode.value === 'model' && ensembleEnabled.value
  ? [ensembleStem.value.trim()].filter(Boolean)
  : normalizeStemOrder(customStemOrder.value, checkedOutputStems.value))
const outputNamingConfig = computed(() => ({
  enabled: Boolean(outputNamingTemplate.value.trim()),
  template: outputNamingTemplate.value.trim() || '%index%_%filename%_%stem%',
  stemOrder: orderedOutputStems.value,
}))
const outputNamingSummary = computed(() => outputNamingConfig.value.enabled
  ? outputNamingConfig.value.template
  : t('separate.namingDefaultSummary'))
const usesIndexToken = computed(() => outputNamingTemplate.value.includes('%index%'))
function workflowDefinitionError(definition: Record<string, unknown>): string {
  const issue = getWorkflowDefinitionIssue(definition)
  if (issue === 'steps-required') return t('workflows.stepsRequired')
  if (issue === 'no-save-outputs') return t('workflows.workflowNoSaveOutputs')
  if (issue === 'invalid-definition') return t('workflows.workflowFormatInvalid')
  if (issue === 'invalid-format') return t('workflows.workflowFormatInvalid')
  return ''
}

const workflowIssueMap = computed(() => new Map(
  workflows.value.map(item => [item.id, workflowDefinitionError(item.definition)]),
))
const runnableWorkflows = computed(() => workflows.value.filter(item => !workflowIssueMap.value.get(item.id)))

function workflowIssue(item: WorkflowEntry): string {
  return workflowIssueMap.value.get(item.id) || ''
}

const contextMenuOptions = computed<DropdownOption[]>(() => {
  if (contextMenuKind.value === 'model' && contextModel.value) {
    return [{ key: 'edit-model-note', label: t('models.editNote') }]
  }
  if (contextMenuKind.value === 'workflow' && contextWorkflow.value) {
    return [{ key: 'edit-workflow-meta', label: t('workflows.editMeta') }]
  }
  return []
})

function selectRunnableWorkflow(item: WorkflowEntry) {
  if (workflowIssue(item)) return
  workflow.selectWorkflow(item.id)
}

function openSeparateContextMenu(
  event: MouseEvent,
  kind: 'model' | 'workflow',
  target: ModelEntry | WorkflowEntry,
) {
  contextMenuKind.value = kind
  contextModel.value = kind === 'model' ? target as ModelEntry : null
  contextWorkflow.value = kind === 'workflow' ? target as WorkflowEntry : null
  contextMenuVisible.value = false
  contextMenuX.value = event.clientX
  contextMenuY.value = event.clientY
  window.requestAnimationFrame(() => {
    contextMenuVisible.value = true
  })
}

function closeSeparateContextMenu() {
  contextMenuVisible.value = false
  contextMenuKind.value = null
  contextModel.value = null
  contextWorkflow.value = null
}

function openModelNoteEditor(item: ModelEntry) {
  noteEditorModel.value = item
  noteDraft.value = modelNote(item.name)
  showModelNoteEditor.value = true
}

function closeModelNoteEditor() {
  showModelNoteEditor.value = false
  noteEditorModel.value = null
}

function handleModelNoteEditorUpdate(value: boolean) {
  showModelNoteEditor.value = value
  if (!value) noteEditorModel.value = null
}

function saveModelNote() {
  const item = noteEditorModel.value
  if (!item) return
  model.setModelNote(item.name, noteDraft.value)
  closeModelNoteEditor()
  message.success(t('models.noteSaved'))
}

function openWorkflowMetaEditor(item: WorkflowEntry) {
  workflowMetaEditor.value = item
  workflowNameDraft.value = item.name
  workflowNoteDraft.value = item.description
  showWorkflowMetaEditor.value = true
}

function closeWorkflowMetaEditor() {
  showWorkflowMetaEditor.value = false
  workflowMetaEditor.value = null
}

function handleWorkflowMetaEditorUpdate(value: boolean) {
  showWorkflowMetaEditor.value = value
  if (!value) workflowMetaEditor.value = null
}

async function saveWorkflowMeta() {
  const current = workflowMetaEditor.value
  if (!current || workflowMetaSaving.value) return
  const trimmedName = workflowNameDraft.value.trim()
  if (!trimmedName) {
    message.error(t('workflows.nameRequired'))
    return
  }
  const trimmedNote = workflowNoteDraft.value.trim()
  if (trimmedName === current.name && trimmedNote === current.description) {
    closeWorkflowMetaEditor()
    return
  }

  const selectedIdBeforeSave = selectedWorkflowId.value
  workflowMetaSaving.value = true
  try {
    await workflow.saveWorkflow({
      id: current.id,
      name: trimmedName,
      description: trimmedNote,
      definition: current.definition,
      format: current.format,
      formatVersion: current.formatVersion,
      expectedUpdatedAt: current.updatedAt,
    })
    // Saving a non-selected workflow should not change the workflow used by the current run.
    if (selectedWorkflowId.value === current.id && selectedIdBeforeSave !== current.id) {
      workflow.selectWorkflow(selectedIdBeforeSave)
    }
    closeWorkflowMetaEditor()
    message.success(t('workflows.saved'))
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    workflowMetaSaving.value = false
  }
}

function handleSeparateContextMenuSelect(key: string | number) {
  const item = contextMenuKind.value === 'model' ? contextModel.value : contextWorkflow.value
  closeSeparateContextMenu()
  if (!item) return
  if (key === 'edit-model-note' && 'name' in item) {
    openModelNoteEditor(item as ModelEntry)
    return
  }
  if (key === 'edit-workflow-meta' && 'id' in item) {
    openWorkflowMetaEditor(item as WorkflowEntry)
  }
}

function workflowValidationError() {
  if (!selectedWorkflow.value) return ''
  return workflowDefinitionError(selectedWorkflow.value.definition)
}
const workflowStructureInvalid = computed(() => runMode.value === 'workflow' && Boolean(workflowValidationError()))
const startStatusText = computed(() => {
  if (runMode.value === 'workflow' && workflows.value.length && !runnableWorkflows.value.length) {
    return t('separate.startHintNoRunnableWorkflow')
  }
  if (runMode.value === 'workflow' && !selectedWorkflow.value) return t('separate.startHintNoWorkflow')
  const validationError = workflowValidationError()
  if (validationError) return validationError
  if (outputDirectoryError.value) return outputDirectoryError.value
  if (!inputFiles.value.length) return t('separate.outputDirectorySummary')
  if (runMode.value === 'model' && ensembleEnabled.value && !ensembleReady.value) return t('separate.ensembleNotReady')
  if (runMode.value === 'model' && !ensembleEnabled.value && !modelDownloaded.value) return t('separate.startHintModelMissing')
  return t('separate.outputDirectorySummary')
})
const modelCategoryOptions = computed(() => [
  { label: t('common.all'), value: '' },
  ...buildModelCategoryOptionsFromModels(listedDownloadedModels.value, locale.value),
])
const filteredDownloadedModels = computed(() => {
  const query = modelSearch.value.trim().toLowerCase()
  const selectedCategory = modelCategoryFilter.value.trim().toLowerCase()
  return listedDownloadedModels.value.filter((item) => {
    // Notes are searchable here too: picking a model to run is exactly when "the one that was
    // good for vocals" is how someone remembers it.
    const matchesQuery = matchesModelQuery(item, query, modelNote(item.name))
    const matchesCategory = !selectedCategory
      || item.category.toLowerCase() === selectedCategory
      || item.primaryCategory.toLowerCase() === selectedCategory
      || item.secondaryCategory.toLowerCase() === selectedCategory
    return matchesQuery && matchesCategory
  })
})
const pagedDownloadedModels = computed(() => {
  const start = (modelPage.value - 1) * modelPageSize.value
  return filteredDownloadedModels.value.slice(start, start + modelPageSize.value)
})
const modelPageSizeOptions = [8, 12, 24]
const filteredWorkflows = computed(() => {
  const query = workflowSearch.value.trim().toLowerCase()
  return [...workflows.value]
    .filter((item) => {
      if (!query) return true
      return item.name.toLowerCase().includes(query) || item.description.toLowerCase().includes(query)
    })
    .sort((a, b) => b.updatedAt - a.updatedAt)
})

const normalizedOutputDir = computed(() => (temporaryOutputDir.value || settings.outputDir || 'results').trim() || 'results')
const outputDirectoryPlatform = computed(() => {
  const reported = String(app.envInfo?.platform || '').trim().toLowerCase()
  if (reported === 'win32' || reported === 'windows') return 'windows' as const
  if (reported === 'darwin' || reported === 'macos' || reported === 'mac') return 'macos' as const
  if (reported === 'linux') return 'linux' as const
  const browserPlatform = String(typeof navigator !== 'undefined' ? navigator.platform : '').toLowerCase()
  if (browserPlatform.includes('win')) return 'windows' as const
  if (browserPlatform.includes('mac')) return 'macos' as const
  return 'linux' as const
})
const outputDirectoryError = computed(() => validateOutputDirectory(temporaryOutputDir.value, outputDirectoryPlatform.value))
const outputPreview = computed(() => {
  const base = normalizedOutputDir.value.replace(/[\\/]$/, '')
  const separator = base.includes('\\') ? '\\' : '/'
  const previewName = outputNamingPreviewFiles.value[0] || t('separate.outputFilePreview')
  if (effectiveOutputLayout.value === 'flat') {
    return `${base}${separator}${previewName}`
  }
  return `${base}${separator}${t('separate.resultFolderPreview')}${separator}${previewName}`
})
const effectiveFormat = computed(() => {
  if (runMode.value === 'workflow' && selectedWorkflow.value) {
    return resolveWorkflowRuntimeDefaults(selectedWorkflow.value.definition, {
      device: 'auto',
      outputFormat: settings.defaultFormat || 'wav',
      modelDir: settings.modelDir || null,
    }).outputFormat
  }
  return String(settings.defaultFormat || 'wav').trim().toLowerCase() || 'wav'
})
const formatLabel = computed(() => effectiveFormat.value.toUpperCase())
const outputNamingPreviewFiles = computed(() => {
  const stems = orderedOutputStems.value.length ? orderedOutputStems.value : ['vocals']
  return stems.slice(0, 8).map((stem, index) => formatOutputNamingPreviewFile(stem, index))
})
const outputNamingPreviewParts = computed(() => {
  const stems = orderedOutputStems.value.length ? orderedOutputStems.value : ['vocals']
  return stems.slice(0, 8).map((stem, index) => ({
    key: `${stem}-${index}`,
    parts: formatOutputNamingPreviewParts(stem, index),
  }))
})
const outputSummaryPath = computed(() => runMode.value === 'workflow' ? normalizedOutputDir.value : outputPreview.value)
const canStart = computed(() => (
  !workflowStructureInvalid.value
  && !outputDirectoryError.value
  && (runMode.value === 'workflow'
    ? Boolean(selectedWorkflow.value) && inputFiles.value.length > 0
    : ensembleEnabled.value ? ensembleReady.value : (modelDownloaded.value && inputFiles.value.length > 0))
))
const newestRunningJob = computed(() => {
  return [...task.allJobs]
    .filter(job => job.tasks.some(item => !['done', 'failed', 'cancelled'].includes(item.status)))
    .sort((a, b) => b.createdAt - a.createdAt)[0] || null
})
const focusedJob = computed(() => task.getJobById(focusedSeparationJobId.value))
const currentJob = computed(() => focusedJob.value || newestRunningJob.value)
const focusedBatchTasks = computed(() => currentJob.value?.tasks || [])
const activeFocusedBatchTask = computed(() => {
  return [...focusedBatchTasks.value]
    .filter(item => !['done', 'failed', 'cancelled'].includes(item.status))
    .sort((a, b) => {
      const aQueued = a.status === 'queued' ? 1 : 0
      const bQueued = b.status === 'queued' ? 1 : 0
      if (aQueued !== bQueued) return aQueued - bQueued
      return a.createdAt - b.createdAt
    })[0] || null
})
const currentTask = computed(() => {
  if (focusedBatchTasks.value.length) return activeFocusedBatchTask.value || focusedBatchTasks.value[0] || null
  return null
})
const currentBatchTasks = computed(() => focusedBatchTasks.value.length ? focusedBatchTasks.value : currentTask.value ? [currentTask.value] : [])
const currentBatchTotal = computed(() => currentBatchTasks.value.length)
const currentBatchDoneCount = computed(() => currentBatchTasks.value.filter(item => item.status === 'done').length)
const currentBatchFailedCount = computed(() => currentBatchTasks.value.filter(item => item.status === 'failed').length)
const currentBatchCancelledCount = computed(() => currentBatchTasks.value.filter(item => item.status === 'cancelled').length)
const currentBatchFinishedCount = computed(() => currentBatchDoneCount.value + currentBatchFailedCount.value + currentBatchCancelledCount.value)
const currentBatchIsMulti = computed(() => currentBatchTotal.value > 1)
const taskPanelState = computed<'ready' | 'running' | 'done' | 'failed' | 'cancelled'>(() => {
  const items = currentBatchTasks.value
  if (!items.length) return 'ready'
  const status = resolveJobStatus(items)
  return ['done', 'failed', 'cancelled'].includes(status)
    ? status as 'done' | 'failed' | 'cancelled'
    : 'running'
})
const isConfigCompact = computed(() => taskPanelState.value !== 'ready')
const isTerminalState = computed(() => ['done', 'failed', 'cancelled'].includes(taskPanelState.value))
const isRunModeLocked = computed(() => taskPanelState.value === 'running')
const inputCompactLine = computed(() => {
  if (currentBatchIsMulti.value && currentBatchTotal.value) return t('separate.batchInputCompact', { count: currentBatchTotal.value })
  if (currentTask.value) return getFileName(currentTask.value.input)
  if (!inputFiles.value.length) return t('separate.noInputSelected')
  const first = getFileName(inputFiles.value[0])
  if (inputFiles.value.length === 1) return first
  return t('separate.inputCompactMultiple', { count: inputFiles.value.length, name: first })
})
const modelCompactLine = computed(() => {
  if (currentTask.value?.model) return currentTask.value.model
  if (runMode.value === 'workflow') return selectedWorkflow.value?.name || t('separate.noWorkflowSelected')
  const name = selectedModelName.value || t('separate.noModelSelected')
  const category = currentModelInfo.value ? categoryLabel(currentModelInfo.value) : ''
  return category ? `${name} · ${category}` : name
})
const currentTaskFileName = computed(() => currentTask.value ? getFileName(currentTask.value.input) : '')
const currentTaskOutputPath = computed(() => currentJob.value?.output || currentTask.value?.output || normalizedOutputDir.value)
const currentTaskOutputSummary = computed(() => shortenMiddle(currentTaskOutputPath.value, 72))
const currentTaskDuration = computed(() => currentTask.value ? taskDuration(currentTask.value) : '')

function configuredStemOrder(item: SeparationTask) {
  if (item.runConfig?.outputNaming?.stemOrder?.length) return item.runConfig.outputNaming.stemOrder
  if (item.runConfig?.runMode === 'workflow') return []
  const modelEntry = modelEntries.value.find(modelItem => modelItem.name === item.model)
  const configured = parseModelInstruments(modelEntry?.configInstruments)
  const selected = item.runConfig?.selectedStems || []
  if (!configured.length) return selected
  if (!selected.length) return configured

  const selectedKeys = new Set(selected.map(stem => stem.toLowerCase()))
  const configuredKeys = new Set(configured.map(stem => stem.toLowerCase()))
  return [
    ...configured.filter(stem => selectedKeys.has(stem.toLowerCase())),
    ...selected.filter(stem => !configuredKeys.has(stem.toLowerCase())),
  ]
}

const playableOutputGroups = computed(() => currentBatchTasks.value
  .filter(item => item.status === 'done')
  .map(item => ({
    taskId: item.id,
    input: item.input,
    outputs: sortStemOutputsByOrder(
      item.outputs.filter(output => Boolean(output.path)),
      configuredStemOrder(item),
    ),
  }))
  .filter(group => group.outputs.length > 0))
const playableOutputs = computed(() => playableOutputGroups.value.flatMap(group => group.outputs))
const currentBatchProgress = computed(() => {
  const items = currentBatchTasks.value
  if (!items.length) return 0
  const total = items.reduce((sum, item) => {
    if (item.status === 'done' || item.status === 'failed' || item.status === 'cancelled') return sum + 100
    if (item.status === 'queued') return sum
    return sum + Math.max(0, Math.min(99, Number(item.progress || 0)))
  }, 0)
  return Math.round(total / items.length)
})
const currentBatchActiveIndex = computed(() => {
  if (!currentBatchTotal.value) return 0
  if (taskPanelState.value !== 'running') return currentBatchFinishedCount.value
  return Math.min(currentBatchTotal.value, currentBatchFinishedCount.value + 1)
})
const currentBatchTitle = computed(() => {
  if (!currentBatchIsMulti.value) return t('separate.taskRunningTitle')
  if (taskPanelState.value === 'running') {
    return t('separate.batchRunningTitle', { current: currentBatchActiveIndex.value, total: currentBatchTotal.value })
  }
  if (taskPanelState.value === 'done') return t('separate.batchDoneTitle', { count: currentBatchDoneCount.value })
  return statusLabel(taskPanelState.value)
})
const currentBatchLine = computed(() => {
  if (!currentBatchIsMulti.value) return currentTaskFileName.value
  if (taskPanelState.value === 'running' && currentTask.value) {
    return t('separate.batchCurrentInput', { name: getFileName(currentTask.value.input) })
  }
  return t('separate.batchFinishedSummary', {
    done: currentBatchDoneCount.value,
    failed: currentBatchFailedCount.value,
    cancelled: currentBatchCancelledCount.value,
    total: currentBatchTotal.value,
  })
})
const currentBatchOutputSummary = computed(() => {
  if (!currentBatchIsMulti.value) return currentTaskOutputSummary.value
  return t('separate.batchResultSummary', { count: currentBatchDoneCount.value, total: currentBatchTotal.value })
})

function formatPlaybackTime(value: number) {
  if (!Number.isFinite(value) || value <= 0) return '00:00'
  const total = Math.floor(value)
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

function getOutputPlayback(path: string) {
  return outputPlayback.value[path] || { currentTime: 0, duration: 0 }
}

function setOutputPlayback(path: string, patch: Partial<{ currentTime: number; duration: number }>) {
  const previous = getOutputPlayback(path)
  outputPlayback.value = {
    ...outputPlayback.value,
    [path]: { ...previous, ...patch },
  }
}

function getAudio(path: string) {
  const cached = audioElements.get(path)
  if (cached) return cached

  const audio = new Audio(convertFileSrc(path))
  audio.preload = 'metadata'
  audio.addEventListener('loadedmetadata', () => {
    setOutputPlayback(path, { duration: audio.duration || 0 })
  })
  audio.addEventListener('timeupdate', () => {
    setOutputPlayback(path, { currentTime: audio.currentTime || 0, duration: audio.duration || 0 })
  })
  audio.addEventListener('ended', () => {
    if (playingOutputPath.value === path) playingOutputPath.value = ''
    setOutputPlayback(path, { currentTime: 0, duration: audio.duration || 0 })
  })
  audio.addEventListener('error', () => {
    if (playingOutputPath.value === path) playingOutputPath.value = ''
  })
  audioElements.set(path, audio)
  return audio
}

async function toggleOutputPlayback(output: StemOutput) {
  if (!output.path) return
  const audio = getAudio(output.path)
  if (playingOutputPath.value === output.path && !audio.paused) {
    audio.pause()
    playingOutputPath.value = ''
    return
  }

  audioElements.forEach((item, path) => {
    if (path !== output.path) item.pause()
  })
  try {
    await audio.play()
    playingOutputPath.value = output.path
  } catch (error) {
    message.error(error instanceof Error ? error.message : t('separate.previewPlayFailed'))
  }
}

function seekOutput(path: string, value: number) {
  const audio = getAudio(path)
  const next = Number(value || 0)
  audio.currentTime = next
  setOutputPlayback(path, { currentTime: next, duration: audio.duration || getOutputPlayback(path).duration })
}

function stopAllPreviewAudio() {
  audioElements.forEach((audio) => {
    audio.pause()
    audio.removeAttribute('src')
    audio.load()
  })
  audioElements.clear()
  playingOutputPath.value = ''
  outputPlayback.value = {}
}

function getFileName(path: string) {
  return path.split(/[/\\]/).filter(Boolean).pop() || path
}

function validateOutputDirectory(value: string, platform: 'windows' | 'macos' | 'linux') {
  const path = String(value || '').trim()
  if (!path) return ''
  if (/[\u0000-\u001f]/u.test(path)) {
    return t('separate.outputDirectoryInvalidCharacters')
  }

  if (platform !== 'windows') {
    if (path.includes('\0')) return t('separate.outputDirectoryInvalidCharacters')
    return ''
  }

  if (/[<>"|?*]/u.test(path)) return t('separate.outputDirectoryInvalidCharacters')
  const normalized = path.replace(/\//g, '\\')
  if (/^[A-Za-z]:[^\\]/u.test(normalized)) return t('separate.outputDirectoryIncomplete')
  const withoutRoot = normalized
    .replace(/^[A-Za-z]:\\/u, '')
    .replace(/^\\\\[^\\]+\\[^\\]+\\?/u, '')
    .replace(/^\\+/u, '')
  const segments = withoutRoot.split(/\\+/u).filter(Boolean)
  if (segments.some(segment => segment.includes(':'))) return t('separate.outputDirectoryInvalidCharacters')
  if (segments.some(segment => /[ .]$/u.test(segment))) return t('separate.outputDirectoryTrailingCharacter')
  if (segments.some(segment => WINDOWS_RESERVED_FILENAMES.has(segment.split('.', 1)[0].toUpperCase()))) {
    return t('separate.outputDirectoryReservedName')
  }
  if (/^[A-Za-z]:$/u.test(path) || /^\\\\[^\\]+$/u.test(path)) return t('separate.outputDirectoryIncomplete')
  return ''
}

function stripFileExtension(path: string) {
  return getFileName(path).replace(/\.[^/.\\]+$/, '')
}

function padNumber(value: number) {
  return String(value).padStart(2, '0')
}

function formatDateToken(date = new Date()) {
  return `${date.getFullYear()}${padNumber(date.getMonth() + 1)}${padNumber(date.getDate())}`
}

function formatTimeToken(date = new Date()) {
  return `${padNumber(date.getHours())}${padNumber(date.getMinutes())}${padNumber(date.getSeconds())}`
}

function formatLegacyTimeToken(date = new Date()) {
  return `${padNumber(date.getDate())}${padNumber(date.getMinutes())}${padNumber(date.getSeconds())}`
}

function truncateUtf8(value: string, maxBytes: number) {
  let bytes = 0
  let result = ''
  for (const char of value) {
    const size = new TextEncoder().encode(char).length
    if (bytes + size > maxBytes) break
    bytes += size
    result += char
  }
  return result.replace(/[ ._]+$/u, '')
}

function normalizeFilenameFragment(value: string) {
  return String(value || '')
    .replace(/[<>:"/\\|?*\x00-\x1f]+/g, '_')
    .replace(/\s+/g, ' ')
    .replace(/\s*_\s*/g, '_')
}

function safeFilenamePart(value: string) {
  let text = normalizeFilenameFragment(value).replace(/^[ ._]+|[ ._]+$/g, '')
  text = truncateUtf8(text, MAX_FILENAME_PART_BYTES) || 'output'
  const reservedKey = text.split('.', 1)[0].toUpperCase()
  if (WINDOWS_RESERVED_FILENAMES.has(reservedKey) || text === '.' || text === '..') text = `${text}_`
  return text
}

function formatOutputNamingPreviewFile(stem: string, stemIndex: number) {
  return formatOutputNamingPreviewParts(stem, stemIndex).map(part => part.text).join('')
}

function formatOutputNamingPreviewParts(stem: string, stemIndex: number) {
  const inputPath = inputFiles.value[0] || t('separate.resultFolderPreview')
  const modelName = runMode.value === 'workflow'
    ? selectedWorkflow.value?.name || t('separate.workflow')
    : ensembleEnabled.value ? 'Ensemble' : selectedModelName.value || t('separate.model')
  const now = new Date()
  const values: Record<string, string> = {
    '%index%': padNumber(stemIndex + 1),
    '%input_number%': '01',
    '%filename%': stripFileExtension(inputPath),
    '%stem%': stem,
    '%model%': modelName,
    '%yyyyMMdd%': formatDateToken(now),
    '%hhmmss%': formatTimeToken(now),
    '%ddmmss%': formatLegacyTimeToken(now),
  }
  const template = outputNamingConfig.value.enabled ? outputNamingConfig.value.template : '%filename%_%stem%'
  const parts: Array<{ text: string; token: string }> = []
  const tokenPattern = /%(?:index|input_number|filename|stem|model|yyyyMMdd|hhmmss|ddmmss)%/g
  let cursor = 0
  for (const match of template.matchAll(tokenPattern)) {
    const token = match[0]
    const index = match.index ?? 0
    if (index > cursor) parts.push({ text: template.slice(cursor, index), token: 'literal' })
    parts.push({ text: values[token] || token, token })
    cursor = index + token.length
  }
  if (cursor < template.length) parts.push({ text: template.slice(cursor), token: 'literal' })
  const namedParts = parts.length
    ? parts.map(part => ({ ...part, text: normalizeFilenameFragment(part.text) }))
    : [{ text: 'output', token: 'literal' }]
  const firstPart = namedParts[0]
  const lastPart = namedParts[namedParts.length - 1]
  firstPart.text = firstPart.text.replace(/^[ ._]+/g, '')
  lastPart.text = lastPart.text.replace(/[ ._]+$/g, '')
  const fullName = namedParts.map(part => part.text).join('')
  if (!fullName) namedParts.splice(0, namedParts.length, { text: 'output', token: 'literal' })
  else {
    const safeName = safeFilenamePart(fullName)
    if (safeName.startsWith(fullName)) lastPart.text += safeName.slice(fullName.length)
  }
  return [
    ...namedParts.filter(part => part.text),
    { text: `.${effectiveFormat.value || 'wav'}`, token: 'extension' },
  ]
}

function moveItem<T>(items: T[], fromIndex: number, toIndex: number) {
  if (fromIndex === toIndex || fromIndex < 0 || toIndex < 0 || fromIndex >= items.length || toIndex >= items.length) return items
  const next = [...items]
  const [item] = next.splice(fromIndex, 1)
  next.splice(toIndex, 0, item)
  return next
}

function moveStem(fromIndex: number, toIndex: number) {
  customStemOrder.value = moveItem(orderedOutputStems.value, fromIndex, toIndex)
}

function onInputPointerDown(event: PointerEvent, index: number) {
  if (isRunModeLocked.value) return
  const target = event.target as HTMLElement | null
  if (target?.closest('.file-chip__remove')) return
  draggedInputIndex.value = index
  inputDragPointerId.value = event.pointerId
  ;(event.currentTarget as HTMLElement | null)?.setPointerCapture?.(event.pointerId)
}

function onInputPointerMove(event: PointerEvent) {
  if (inputDragPointerId.value !== event.pointerId || draggedInputIndex.value === null) return
  const list = (event.currentTarget as HTMLElement).parentElement
  const rows = Array.from(list?.querySelectorAll<HTMLElement>('.file-chip') || [])
  if (!rows.length) return
  const targetIndex = rows.findIndex(row => event.clientY < row.getBoundingClientRect().top + row.getBoundingClientRect().height / 2)
  const nextIndex = targetIndex === -1 ? rows.length - 1 : targetIndex
  if (nextIndex === draggedInputIndex.value) return
  task.moveInputFile(draggedInputIndex.value, nextIndex)
  draggedInputIndex.value = nextIndex
}

function finishInputPointerDrag(event?: PointerEvent) {
  if (event && inputDragPointerId.value === event.pointerId) {
    ;(event.currentTarget as HTMLElement | null)?.releasePointerCapture?.(event.pointerId)
  }
  inputDragPointerId.value = null
  draggedInputIndex.value = null
}

function onStemPointerDown(event: PointerEvent, index: number) {
  const target = event.target as HTMLElement | null
  if (target?.closest('.stem-order-row__actions')) return
  draggedStemIndex.value = index
  stemDragPointerId.value = event.pointerId
  ;(event.currentTarget as HTMLElement | null)?.setPointerCapture?.(event.pointerId)
}

function onStemPointerMove(event: PointerEvent) {
  if (stemDragPointerId.value !== event.pointerId || draggedStemIndex.value === null) return
  const list = (event.currentTarget as HTMLElement).parentElement
  const listTop = list?.getBoundingClientRect().top ?? (event.currentTarget as HTMLElement).getBoundingClientRect().top
  const targetIndex = Math.max(0, Math.min(
    orderedOutputStems.value.length - 1,
    Math.trunc((event.clientY - listTop) / STEM_ORDER_ROW_HEIGHT),
  ))
  if (targetIndex === draggedStemIndex.value) return
  moveStem(draggedStemIndex.value, targetIndex)
  draggedStemIndex.value = targetIndex
}

function finishStemPointerDrag(event?: PointerEvent) {
  if (event && stemDragPointerId.value === event.pointerId) {
    ;(event.currentTarget as HTMLElement | null)?.releasePointerCapture?.(event.pointerId)
  }
  stemDragPointerId.value = null
  draggedStemIndex.value = null
}

async function insertNamingToken(token: string) {
  const input = namingTemplateInputRef.value?.inputElRef as HTMLInputElement | undefined
  const current = outputNamingTemplate.value
  const withSeparators = (start: number, end: number) => {
    const before = current.slice(0, start)
    const after = current.slice(end)
    const prefix = before && !before.endsWith('_') ? '_' : ''
    const suffix = after && !after.startsWith('_') ? '_' : ''
    return {
      text: `${before}${prefix}${token}${suffix}${after}`,
      cursor: before.length + prefix.length + token.length,
    }
  }
  if (!input) {
    const next = withSeparators(current.length, current.length)
    outputNamingTemplate.value = next.text
    return
  }
  const start = input.selectionStart ?? current.length
  const end = input.selectionEnd ?? start
  const next = withSeparators(start, end)
  outputNamingTemplate.value = next.text
  await nextTick()
  input.focus()
  input.setSelectionRange(next.cursor, next.cursor)
}

function resetOutputNaming() {
  outputNamingTemplate.value = '%index%_%filename%_%stem%'
  customStemOrder.value = []
}

function categoryLabel(item: { categoryCn?: string; category?: string; primaryCategoryCn?: string; primaryCategory?: string } | null | undefined) {
  return getModelCategoryLabel(item, locale.value, t('common.notSet'))
}

function modelTargetLabel(item: {
  targetStem?: string
  configTargetInstrument?: string
} | null | undefined) {
  return item?.targetStem || item?.configTargetInstrument || t('common.notSet')
}

function modelArchitectureLabel(item: {
  architecture?: string
  modelType?: string | null
} | null | undefined) {
  return item?.architecture || item?.modelType || t('common.notSet')
}

/** The user's own note for a model, kept beside the catalog entry rather than in it. */
function modelNote(name: string) {
  return modelPreferences.value[name]?.note || ''
}

function modelUseCount(name: string) {
  return model.getModelUseCount(name)
}

function modelMetaLine(item: {
  targetStem?: string
  configTargetInstrument?: string
  architecture?: string
  modelType?: string | null
}) {
  return `${modelTargetLabel(item)} · ${modelArchitectureLabel(item)}`
}

function shortenMiddle(text: string, maxLength = 48) {
  if (text.length <= maxLength) return text
  const keep = Math.max(8, Math.floor((maxLength - 3) / 2))
  return `${text.slice(0, keep)}...${text.slice(-keep)}`
}

function statusLabel(status: string) {
  const labels: Record<string, string> = {
    queued: t('tasks.statusQueued'),
    preparing: t('tasks.statusPreparing'),
    validating_input: t('tasks.statusValidatingInput'),
    downloading_model: t('tasks.statusCheckingModel'),
    ensuring_model: t('tasks.statusCheckingModel'),
    loading_model: t('tasks.statusLoadingModel'),
    separating: t('tasks.statusSeparating'),
    writing_output: t('tasks.statusWritingOutput'),
    done: t('tasks.statusDone'),
    failed: t('tasks.statusFailed'),
    cancelled: t('tasks.statusCancelled'),
  }
  return labels[status] || status
}

function statusType(status: string) {
  switch (status) {
    case 'done': return 'success' as const
    case 'failed': return 'error' as const
    case 'cancelled': return 'warning' as const
    default: return 'info' as const
  }
}

function normalizeProgressMessage(value?: string) {
  const message = (value || '').trim()
  const key = message.toLowerCase()
  const mapped: Record<string, string> = {
    'task started': t('tasks.progressPreparingTask'),
    'validating input': t('tasks.progressValidatingInput'),
    'checking model files': t('tasks.progressCheckingModel'),
    'loading model': t('tasks.progressLoadingModel'),
    'separating audio': t('tasks.progressSeparatingHint'),
    'processing audio chunks': t('tasks.progressProcessingChunks'),
    'processing vr batches': t('tasks.progressProcessingVrBatches'),
    'collecting outputs': t('tasks.progressCollectingOutputs'),
  }
  return mapped[key] || message
}

function progressStatus(status: string) {
  switch (status) {
    case 'done': return 'success' as const
    case 'failed': return 'error' as const
    case 'cancelled': return 'warning' as const
    default: return 'info' as const
  }
}

function progressTitle(item: SeparationTask) {
  if (item.status === 'separating') return t('tasks.progressTitleSeparating')
  return statusLabel(item.status)
}

function formatProgressTime(seconds: number) {
  const total = Math.max(0, Math.round(seconds))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const rest = total % 60
  return `${padNumber(hours)}:${padNumber(minutes)}:${padNumber(rest)}`
}

function progressDetail(item: SeparationTask) {
  if (
    item.status === 'separating'
    && typeof item.progressCurrent === 'number'
    && typeof item.progressTotal === 'number'
    && item.progressTotal > 0
  ) {
    return `${formatProgressTime(item.progressCurrent)} / ${formatProgressTime(item.progressTotal)}`
  }
  return ''
}

function taskSubMessage(item: SeparationTask) {
  if (item.error) return item.error
  return normalizeProgressMessage(item.progressDetail || item.message)
}

function taskDuration(item: SeparationTask) {
  const seconds = Math.max(0, Math.round((item.updatedAt - item.createdAt) / 1000))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return `${minutes}m ${rest}s`
}

function handleSelectModel(item: (typeof listedDownloadedModels.value)[number]) {
  if (ensembleEnabled.value) {
    const next = ensembleModels.value.includes(item.name)
      ? ensembleModels.value.filter(name => name !== item.name)
      : [...ensembleModels.value, item.name]
    ensembleModels.value = next
    return
  }
  model.selectModel(item).catch(() => {})
}

function prefetchSelectedModelAdvancedParams() {
  if (runMode.value !== 'model') return
  if (!shouldPrefetchAdvancedParams.value || detailLoading.value || isLoading.value || !selectedModelName.value) return
  model.selectModel(selectedModelName.value).catch(() => {})
}

watch(selectedModelName, (name, previousName) => {
  if (previousName && previousName !== name) task.saveCurrentModelState(previousName)
})

watch(ensembleModels, (models) => {
  const next = { ...ensembleWeights.value }
  const nextStems = { ...ensembleModelStems.value }
  models.forEach(name => {
    if (!Number.isFinite(Number(next[name])) || Number(next[name]) < 0) next[name] = 1
    const options = ensembleStemOptionsByModel.value[name] || []
    if (!options.length) delete nextStems[name]
    else {
      const savedStem = String(nextStems[name] || '').trim()
      const savedOption = options.find(option => option.value.toLowerCase() === savedStem.toLowerCase())
      const legacyStem = ensembleStem.value
      const legacyOption = options.find(option => option.value.toLowerCase() === legacyStem.toLowerCase())
      nextStems[name] = savedOption?.value || legacyOption?.value || options[0].value
    }
  })
  Object.keys(next).forEach(name => {
    if (!models.includes(name)) delete next[name]
  })
  Object.keys(nextStems).forEach(name => {
    if (!models.includes(name)) delete nextStems[name]
  })
  ensembleWeights.value = next
  ensembleModelStems.value = nextStems
  if (!ensembleStem.value.trim()) {
    const firstStem = models.map(name => nextStems[name]).find(Boolean)
    if (firstStem) ensembleStem.value = firstStem
  }
}, { deep: true, immediate: true })

watch(
  [downloadedModels, modelsLoaded],
  ([availableModels, loaded]) => {
    if (!loaded) return
    const validModels = retainAvailableModelNames(
      ensembleModels.value,
      availableModels.map(item => item.name),
    )
    if (validModels.length !== ensembleModels.value.length) ensembleModels.value = validModels
  },
  { deep: true, immediate: true },
)

watch(ensembleStemOptionsByModel, () => {
  const models = ensembleModels.value
  if (!models.length) return
  const nextStems = { ...ensembleModelStems.value }
  let changed = false
  models.forEach((name) => {
    const options = ensembleStemOptionsByModel.value[name] || []
    if (!options.length) {
      if (name in nextStems) {
        delete nextStems[name]
        changed = true
      }
      return
    }
    const savedStem = String(nextStems[name] || '').trim()
    const savedOption = options.find(option => option.value.toLowerCase() === savedStem.toLowerCase())
    if (!savedOption || savedOption.value !== nextStems[name]) {
      const legacyStem = ensembleStem.value
      const legacyOption = options.find(option => option.value.toLowerCase() === legacyStem.toLowerCase())
      nextStems[name] = savedOption?.value || legacyOption?.value || options[0].value
      changed = true
    }
  })
  if (changed) ensembleModelStems.value = nextStems
}, { deep: true, immediate: true })

watch([modelSearch, modelCategoryFilter, modelPageSize], () => {
  modelPage.value = 1
})

watch(() => filteredDownloadedModels.value.length, (count) => {
  const pageCount = Math.max(1, Math.ceil(count / modelPageSize.value))
  if (modelPage.value > pageCount) modelPage.value = pageCount
})

watch(
  availableStemNames,
  (stems) => {
    if (!selectedStems.value.length) return
    const allowed = new Set(stems)
    selectedStems.value = selectedStems.value.filter(stem => allowed.has(stem))
  },
  { immediate: true },
)

watch(
  checkedOutputStems,
  (stems) => {
    customStemOrder.value = normalizeStemOrder(customStemOrder.value, stems)
  },
  { immediate: true },
)

watch(
  [selectedModelName, currentModelInfo],
  ([name, info], previous) => {
    if (!info || !name || info.name !== name) return
    task.applySelectedModelDefaults(
      model.getModelBaseInferenceDefaults(info.name) || info.defaultInferenceParams,
      info.modelType,
      task.getSavedModelState(info.name),
      model.getModelInferenceOverrides(info.name),
      {
        force: Boolean(
          (previous?.[0] && previous[0] !== name)
          || (previous?.[1]?.name && previous[1].name !== info.name),
        ),
      },
    )
  },
  { immediate: true },
)

watch(
  shouldPrefetchAdvancedParams,
  (shouldPrefetch) => {
    if (!shouldPrefetch) return
    prefetchSelectedModelAdvancedParams()
  },
  { immediate: true },
)

watch(
  [listedDownloadedModels, selectedModel, isLoading],
  ([list, current, loading]) => {
    if (loading) return
    if (!list.length) return
    const valid = current && list.some((item) => item.name === current)
    if (!valid) {
      selectedModel.value = list[0].name
      model.selectModel(list[0]).catch(() => {})
    }
  },
  { immediate: true },
)
watch(
  [workflows, selectedWorkflowId],
  ([list, current]) => {
    if (!list.length) return
    const selected = list.find(item => item.id === current)
    if (selected && !workflowIssueMap.value.get(selected.id)) return
    workflow.selectWorkflow(list.find(item => !workflowIssueMap.value.get(item.id))?.id || '')
  },
  { immediate: true },
)

onMounted(async () => {
  if (import.meta.env.DEV && route.query.preview === 'results') {
    const previewJob = [...task.resultJobs].sort((a, b) => b.updatedAt - a.updatedAt)[0]
    if (previewJob) focusedSeparationJobId.value = previewJob.id
  }
  if (!app.envInfo && !app.envLoading) {
    app.checkEnvInBackground().catch(() => {})
  }
  try {
    unlistenDragDrop = await getCurrentWebview().onDragDropEvent(async (event) => {
      const type = event.payload.type
      if (type === 'over' || type === 'enter') {
        isDragging.value = true
      } else if (type === 'drop') {
        isDragging.value = false
        const paths = (event.payload as { paths?: string[] }).paths || []
        const added = await task.addPaths(paths)
        if (added > 0) message.success(t('separate.addedFiles', { count: added }))
        else message.warning(t('separate.noAudioAdded'))
      } else {
        isDragging.value = false
      }
    })
  } catch {
    // 非 Tauri 环境静默降级
  }
})

onBeforeUnmount(() => {
  if (unlistenDragDrop) unlistenDragDrop()
  finishInputPointerDrag()
  finishStemPointerDrag()
  stopAllPreviewAudio()
})

async function handlePickFiles() {
  const before = inputFiles.value.length
  await task.pickFiles()
  const added = inputFiles.value.length - before
  if (added > 0) message.success(t('separate.addedFiles', { count: added }))
}

async function handlePickFolder() {
  const count = await task.pickInputFolder()
  if (count > 0) message.success(t('separate.folderScanned', { count }))
  else message.warning(t('separate.folderEmpty'))
}

async function pickTemporaryOutputDir() {
  const folder = await settings.pickFolder()
  if (folder) temporaryOutputDir.value = folder
}

function positiveInferenceNumber(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null
}

function nonNegativeInferenceNumber(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

function buildEnsembleInferenceParams(modelType?: string | null) {
  const normalizedModelType = String(modelType || '').trim().toLowerCase()
  const vrModel = normalizedModelType === 'vr'
  const apolloModel = normalizedModelType === 'apollo'
  const params: Record<string, unknown> = {
    standardize: standardize.value,
    normalize: normalize.value,
  }
  const batchSizeValue = positiveInferenceNumber(batch_size.value)
  if (batchSizeValue !== null) params.batch_size = batchSizeValue

  if (vrModel) {
    const windowSizeValue = positiveInferenceNumber(window_size.value)
    const aggressionValue = nonNegativeInferenceNumber(aggression.value)
    const postProcessThresholdValue = nonNegativeInferenceNumber(post_process_threshold.value)
    if (windowSizeValue !== null) params.window_size = windowSizeValue
    if (aggressionValue !== null) params.aggression = aggressionValue
    params.enable_post_process = enable_post_process.value
    if (postProcessThresholdValue !== null) params.post_process_threshold = postProcessThresholdValue
    params.high_end_process = high_end_process.value
    return params
  }

  const overlapSizeValue = positiveInferenceNumber(overlap_size.value)
  const numOverlapValue = apolloModel ? null : positiveInferenceNumber(num_overlap.value)
  const chunkSizeValue = positiveInferenceNumber(chunk_size.value)
  if (overlapSizeValue !== null) params.overlap_size = overlapSizeValue
  if (numOverlapValue !== null) params.num_overlap = numOverlapValue
  if (chunkSizeValue !== null) params.chunk_size = chunkSizeValue
  return params
}

function buildEnsembleWorkflow(): WorkflowEntry {
  const outputStem = ensembleStem.value.trim()
  const runtimeDevice = settings.getRuntimeDeviceConfig(app.envInfo)
  const device = runtimeDevice.device || 'auto'
  const fmt = effectiveFormat.value || 'wav'
  const models = ensembleModels.value
  const nodes: any[] = []
  const links: any[] = []
  let nodeId = 1
  let linkId = 1
  const nextId = () => nodeId++
  const link = (srcNode: number, srcSlot: number, dstNode: number, dstSlot: number, type: string) => {
    const id = linkId++
    links.push([id, srcNode, srcSlot, dstNode, dstSlot, type])
    return id
  }

  // 1. load_audio: outputs AUDIO(0), STRING(1)
  const loadId = nextId()
  nodes.push({ id: loadId, type: 'pymss_load_audio', pos: [72, 210], size: [220, 80], flags: {}, order: nodes.length, mode: 0, properties: { audio: 'input.wav' }, widgets_values: ['input.wav'], inputs: [{ name: 'audio', type: 'COMBO', widget: { name: 'audio' }, link: null }], outputs: [{ name: 'audio', type: 'AUDIO', links: [] }, { name: 'audio_name', type: 'STRING', links: [] }] })

  // 2. per model: mss_params + mss_separate
  const separateIds: number[] = []
  const separateStemOutSlots: number[] = [] // the AUDIO slot for the chosen stem
  models.forEach((modelName, index) => {
    const entry = ensembleModelEntries.value.find(item => item.name === modelName) || null
    const stem = String(ensembleModelStems.value[modelName] || '').trim() || 'vocals'
    const paramsId = nextId()
    nodes.push({ id: paramsId, type: 'pymss_mss_params', pos: [300, 100 + index * 160], size: [220, 120], flags: {}, order: nodes.length, mode: 0, properties: {}, widgets_values: [1, 'Default', 'Default', false, false, false], inputs: [], outputs: [{ name: 'mss_params', type: 'PYMSS_MSS_PARAMS', links: [] }] })
    const sepId = nextId()
    // stem outputs: pairs of (stem Audio, stem String); find the Audio slot index
    const audioSlot = index * 2 // we connect load.audio(0) -> sep.audio(0), params(0) -> sep.params(1)
    const sepOutputs: any[] = [
      { name: `${stem} (Audio)`, type: 'AUDIO', links: [] },
      { name: `${stem} (String)`, type: 'STRING', links: [] },
    ]
    nodes.push({ id: sepId, type: 'mss_separate', pos: [560, 100 + index * 160], size: [240, 100], flags: {}, order: nodes.length, mode: 0, properties: {}, widgets_values: [modelName, device, true, settings.downloadSource, String(runtimeDevice.deviceIds.join(',') || '0'), false], inputs: [{ name: 'audio', type: 'AUDIO', link: null }, { name: 'params', type: 'PYMSS_MSS_PARAMS', shape: 7, link: null }], outputs: sepOutputs })
    separateIds.push(sepId)
    separateStemOutSlots.push(index) // stem Audio is output slot 0 of each sep (only one stem)
    // wire load.audio -> sep.audio, params -> sep.params
    const la = link(loadId, 0, sepId, 0, 'AUDIO')
    nodes[nodes.length - 1].inputs[0].link = la
    const lp = link(paramsId, 0, sepId, 1, 'PYMSS_MSS_PARAMS')
    nodes[nodes.length - 1].inputs[1].link = lp
    nodes[nodes.length - 1].outputs[0].links = []
    void audioSlot
  })

  // 3. audio_ensemble: inputs audio_1..audio_N (slot 0..N-1), output audio(0)
  const ensId = nextId()
  const ensInputs: any[] = []
  models.forEach((_, index) => ensInputs.push({ name: `audio_${index + 1}`, type: 'AUDIO', link: null }))
  nodes.push({ id: ensId, type: 'pymss_audio_ensemble', pos: [900, 210], size: [220, 120], flags: {}, order: nodes.length, mode: 0, properties: {}, widgets_values: [models.length, ensembleType.value, ...models.map(m => { const w = Number(ensembleWeights.value[m]); return Number.isFinite(w) ? w : 1 })], inputs: ensInputs, outputs: [{ name: 'audio', type: 'AUDIO', links: [] }] })
  // wire each sep stem Audio -> ensemble input slot index
  models.forEach((_, index) => {
    const lid = link(separateIds[index], 0, ensId, index, 'AUDIO')
    nodes[nodes.length - 1].inputs[index].link = lid
    // record outgoing link on sep output[0]
    const sepNode = nodes.find((n) => n.id === separateIds[index])
    if (sepNode) sepNode.outputs[0].links = [lid]
  })

  // 4. save_audio: input audio(0), filename(1)
  const saveId = nextId()
  nodes.push({ id: saveId, type: 'pymss_save_audio', pos: [1180, 210], size: [240, 120], flags: {}, order: nodes.length, mode: 0, properties: {}, widgets_values: [fmt, 'Default', '44100', 'FLOAT', 'PCM_24', '320k'], inputs: [{ name: 'audio', type: 'AUDIO', link: null }, { name: 'filename', type: 'STRING', shape: 7, link: null }], outputs: [] })
  const lsave = link(ensId, 0, saveId, 0, 'AUDIO')
  nodes[nodes.length - 1].inputs[0].link = lsave
  nodes[nodes.length - 1].outputs = []
  nodes.find((n) => n.id === ensId)!.outputs[0].links = [lsave]

  return {
    id: 'temporary-ensemble',
    name: t('separate.ensembleWorkflowName'),
    description: t('separate.ensembleWorkflowDescription'),
    format: 'graph',
    formatVersion: WORKFLOW_FORMAT_VERSION,
    createdAt: Date.now(),
    updatedAt: Date.now(),
    definition: {
      last_node_id: nodeId - 1,
      last_link_id: linkId - 1,
      nodes,
      links,
      version: 1,
      extra: { appDefaults: { device, output_format: fmt }, studioEnsemble: { outputStem } },
    } as Record<string, unknown>,
  }
}

async function start() {
  if (outputDirectoryError.value) {
    message.warning(outputDirectoryError.value)
    return
  }
  if (runMode.value === 'model' && ensembleEnabled.value && !ensembleReady.value) {
    message.warning(t('separate.ensembleNotReady'))
    return
  }
  if (runMode.value === 'workflow' && !selectedWorkflow.value) {
    message.warning(t('separate.startHintNoWorkflow'))
    return
  }
  const validationError = workflowValidationError()
  if (validationError) {
    message.warning(validationError)
    return
  }
  if (!inputFiles.value.length) {
    message.warning(t('separate.startHintNoInput'))
    return
  }
  if (runMode.value === 'model' && !ensembleEnabled.value && !modelDownloaded.value) {
    message.warning(t('separate.startHintModelMissing'))
    return
  }
  try {
    const result = runMode.value === 'workflow' && selectedWorkflow.value
      ? await task.startWorkflowInference(selectedWorkflow.value, { outputDir: normalizedOutputDir.value, outputLayout: effectiveOutputLayout.value })
      : ensembleEnabled.value
        ? await task.startWorkflowInference(buildEnsembleWorkflow(), { outputDir: normalizedOutputDir.value, outputLayout: effectiveOutputLayout.value, outputNaming: outputNamingConfig.value })
        : await task.startSeparation({ outputDir: normalizedOutputDir.value, outputLayout: effectiveOutputLayout.value, outputNaming: outputNamingConfig.value })
    focusedSeparationJobId.value = result?.jobId || newestRunningJob.value?.id || focusedSeparationJobId.value
    if (runMode.value === 'model' && ensembleEnabled.value && result) {
      ensembleModels.value.forEach((name) => model.recordModelUse(name))
    }
    if (settings.clearInputAfterSubmit) task.clearInputFiles()
    if (result && result.failed > 0) {
      message.warning(t('separate.batchPartial', { succeeded: result.succeeded, failed: result.failed }))
    } else {
      message.success(t('separate.batchStarted', { count: result?.succeeded ?? 1 }))
    }
  } catch (err) {
    message.error(err instanceof Error ? err.message : t('toast.taskFailed'))
  }
}

function resetForNextSeparation() {
  stopAllPreviewAudio()
  showLogModal.value = false
  focusedSeparationJobId.value = null
}

function goToResults() {
  router.push('/results')
}

function openCurrentLogs() {
  if (!currentTask.value) return
  showLogModal.value = true
}

function handleCancelCurrentTask() {
  const targets = currentBatchTasks.value.filter(item => !['done', 'failed', 'cancelled'].includes(item.status))
  if (!targets.length) return
  dialog.warning({
    title: t('tasks.cancelConfirmTitle'),
    content: t('tasks.cancelConfirmContent'),
    positiveText: t('tasks.cancelAction'),
    negativeText: t('common.cancel'),
    positiveButtonProps: { type: 'error' },
    negativeButtonProps: { secondary: true },
    onPositiveClick: async () => {
      if (cancellingTaskId.value) return
      cancellingTaskId.value = targets.length > 1 ? 'batch' : targets[0].id
      try {
        const results = await Promise.all(targets.map(item => task.cancelTask(item.id)))
        if (results.some(Boolean)) message.success(t('tasks.cancelSuccess'))
      } catch (error) {
        message.error(error instanceof Error ? error.message : String(error))
      } finally {
        cancellingTaskId.value = null
      }
    },
  })
}

async function retryCurrentTask() {
  const item = currentTask.value
  if (!item) return
  dialog.warning({
    title: t('tasks.confirmRetryTitle'),
    content: t('tasks.confirmRetry'),
    positiveText: t('common.retry'),
    negativeText: t('common.cancel'),
    negativeButtonProps: { secondary: true },
    onPositiveClick: async () => {
      stopAllPreviewAudio()
      try {
        const next = await task.retryTask(item.id)
        focusedSeparationJobId.value = next?.jobId || next?.id || focusedSeparationJobId.value
        message.success(t('toast.taskRetried'))
      } catch (error) {
        message.error(error instanceof Error ? error.message : String(error))
      }
    },
  })
}
</script>

<template>
  <div class="page separate-page">
    <header class="console-topbar">
      <div class="console-topbar__brand">
        <AppBrandMark :size="30" variant="compact" shadow />
        <div class="console-topbar__title">
          <h1>Pymss-Studio</h1>
          <span>{{ t('separate.subtitle') }}</span>
        </div>
      </div>
      <div class="console-topbar__controls">
        <n-radio-group
          v-model:value="runMode"
          class="mode-switch"
          :disabled="isRunModeLocked"
        >
          <n-radio-button
            v-for="option in runModeOptions"
            :key="option.value"
            :value="option.value"
          >
            {{ option.label }}
          </n-radio-button>
        </n-radio-group>
        <n-button text class="console-topbar__manage" @click="router.push(runMode === 'workflow' ? '/workflows' : '/models')">
          {{ runMode === 'workflow' ? t('separate.manageWorkflowsInline') : t('separate.manageModelsInline') }}
        </n-button>
      </div>
    </header>

    <div class="console" :class="[`console--${taskPanelState}`, { 'console--busy': isConfigCompact }]">
      <aside class="console__rail">
        <section class="rail-card rail-card--input">
          <div class="rail-card__head">
            <span class="rail-card__index">01</span>
            <div class="rail-card__label">
              <strong>{{ t('separate.input') }}</strong>
              <small>{{ t('separate.candidateCount', { count: inputFiles.length }) }}</small>
            </div>
            <div class="rail-card__actions rail-card__actions--input">
              <label class="input-retention-toggle input-retention-toggle--head">
                <span>{{ t('separate.clearInputAfterSubmit') }}</span>
                <n-switch v-model:value="settings.clearInputAfterSubmit" size="small" :disabled="isRunModeLocked" />
              </label>
              <n-button
                v-if="inputFiles.length"
                text
                size="tiny"
                class="rail-card__clear"
                :disabled="isRunModeLocked"
                @click="task.clearInputFiles()"
              >
                {{ t('separate.clearAll') }}
              </n-button>
            </div>
          </div>
          <div class="rail-card__body">
            <div class="picker-buttons">
              <button type="button" class="picker-btn" :disabled="isRunModeLocked" @click="handlePickFiles">
                <n-icon :component="MusicalNotesOutline" />
                <span>{{ t('separate.chooseFiles') }}</span>
              </button>
              <button type="button" class="picker-btn" :disabled="isRunModeLocked" @click="handlePickFolder">
                <n-icon :component="FolderOutline" />
                <span>{{ t('separate.chooseFolder') }}</span>
              </button>
            </div>

            <div
              class="dropzone"
              :class="{ 'dropzone--dragging': isDragging, 'dropzone--filled': inputFiles.length, 'dropzone--clickable': !inputFiles.length && !isRunModeLocked }"
              @click="(!inputFiles.length && !isRunModeLocked) ? handlePickFiles() : undefined"
            >
              <div v-if="inputFiles.length" class="file-list">
                <div
                  v-for="(path, index) in inputFiles"
                  :key="path"
                  class="file-chip"
                  :class="{ 'file-chip--dragging': draggedInputIndex === index }"
                  @pointerdown="onInputPointerDown($event, index)"
                  @pointermove="onInputPointerMove"
                  @pointerup="finishInputPointerDrag"
                  @pointercancel="finishInputPointerDrag"
                >
                  <span class="file-chip__handle" :title="t('separate.dragToReorder')"><n-icon :component="ReorderFourOutline" /></span>
                  <span class="file-chip__glyph"><n-icon :component="MusicalNotesOutline" /></span>
                  <div class="file-chip__main">
                    <strong :title="getFileName(path)">{{ getFileName(path) }}</strong>
                    <div class="file-chip__sub">
                      <span class="file-chip__kind">{{ getFileKindLabel(path) }}</span>
                      <code :title="path">{{ shortenMiddle(path, 60) }}</code>
                    </div>
                  </div>
                  <n-button quaternary circle size="tiny" class="file-chip__remove" :title="t('separate.remove')" :disabled="isRunModeLocked" @click="task.removeInputFile(path)">
                    <template #icon><n-icon :component="CloseOutline" /></template>
                  </n-button>
                </div>
              </div>
              <div v-else class="dropzone__empty">
                <div class="dropzone__glyph"><n-icon :component="CloudUploadOutline" /></div>
                <strong>{{ isDragging ? t('separate.dropHere') : t('separate.candidateEmpty') }}</strong>
              </div>
            </div>
          </div>
        </section>

        <section class="rail-card rail-card--output">
          <div class="rail-card__head">
            <span class="rail-card__index">02</span>
            <div class="rail-card__label">
              <strong>{{ t('separate.output') }}</strong>
              <small>{{ t('separate.outputSummaryHint') }}</small>
            </div>
            <n-button text size="tiny" class="rail-card__more" :disabled="isRunModeLocked" @click="showSettingsDrawer = true">
              <template #icon><n-icon :component="SettingsOutline" /></template>
              {{ t('separate.configParams') }}
            </n-button>
          </div>
          <div class="rail-card__body rail-card__body--output">
            <label class="ofield">
              <span class="ofield__label">{{ t('separate.temporaryOutputDir') }}</span>
              <div class="dir-input">
                <n-input v-model:value="temporaryOutputDir" size="small" :status="outputDirectoryError ? 'error' : undefined" :placeholder="settings.outputDir || t('separate.outputDefault')" :disabled="isRunModeLocked" clearable />
                <n-button secondary size="small" class="dir-input__browse" :title="t('separate.chooseOutput')" :disabled="isRunModeLocked" @click="pickTemporaryOutputDir">
                  <template #icon><n-icon :component="FolderOutline" /></template>
                </n-button>
              </div>
              <small v-if="outputDirectoryError" class="output-directory-error">{{ outputDirectoryError }}</small>
            </label>

            <div class="ofield-row">
              <label class="ofield">
                <span class="ofield__label">{{ runMode === 'workflow' ? t('separate.currentFormat') : t('settings.defaultFormat') }}</span>
                <n-select v-if="runMode === 'model'" v-model:value="settings.defaultFormat" size="small" :options="formatOptions" :disabled="isRunModeLocked" />
                <div v-else class="ofield__static">{{ formatLabel }}</div>
              </label>
              <div class="ofield">
                <span class="ofield__label">{{ t('separate.saveMode') }}</span>
                <div class="seg" :class="{ 'seg--locked': isRunModeLocked }">
                  <button
                    type="button"
                    class="seg__btn"
                    :class="{ 'seg__btn--active': !saveAsFolder }"
                    :disabled="isRunModeLocked"
                    @click="saveAsFolder = false"
                  >{{ t('separate.saveModeFlatName') }}</button>
                  <button
                    type="button"
                    class="seg__btn"
                    :class="{ 'seg__btn--active': saveAsFolder }"
                    :disabled="isRunModeLocked"
                    @click="saveAsFolder = true"
                  >{{ t('separate.saveModeFolderName') }}</button>
                </div>
              </div>
            </div>

            <div v-if="runMode === 'model'" class="ofield naming-field">
              <span class="ofield__label">{{ t('separate.namingRule') }}</span>
              <button type="button" class="naming-summary" :disabled="isRunModeLocked" @click="showNamingModal = true">
                <span>{{ outputNamingSummary }}</span>
                <small>{{ t('separate.namingRuleAction') }}</small>
              </button>
            </div>

            <div v-if="runMode === 'model'" class="ofield ofield--stems">
              <span class="ofield__label">{{ t('separate.outputStems') }}</span>
              <div v-if="ensembleEnabled" class="ofield__static">{{ ensembleStem || '—' }}</div>
              <div v-else-if="availableStemNames.length" class="stem-chips">
                <n-checkbox-group v-model:value="checkedOutputStems" :disabled="isRunModeLocked">
                  <n-checkbox
                    v-for="stem in availableStemNames"
                    :key="stem"
                    :value="stem"
                    :label="stem"
                  />
                </n-checkbox-group>
              </div>
              <div v-else class="ofield__static ofield__static--muted">{{ t('separate.allStems') }}</div>
            </div>
          </div>
        </section>
      </aside>

      <main class="console__stage">
        <transition name="stage-swap" mode="out-in">
          <section v-if="taskPanelState === 'running' && currentTask" key="running" class="stage-view stage-view--running">
            <div class="stage-hero">
              <div class="stage-hero__top">
                <span class="stage-badge stage-badge--running">
                  <span class="stage-badge__dot"></span>
                  {{ statusLabel(currentTask.status) }}
                </span>
                <n-button text size="small" :disabled="!currentTask.logs.length" @click="openCurrentLogs">
                  <template #icon><n-icon :component="TerminalOutline" /></template>
                  {{ t('tasks.logs') }}
                </n-button>
              </div>
              <div class="stage-hero__title">
                <h2>{{ currentBatchTitle }}</h2>
                <p>{{ currentBatchLine }}</p>
              </div>
              <div class="progress-ring-block">
                <div class="progress-ring" :style="{ '--pct': currentBatchProgress }">
                  <div class="progress-ring__center">
                    <strong>{{ currentBatchProgress }}</strong>
                    <span>%</span>
                  </div>
                </div>
                <div class="progress-ring__meta">
                  <div class="progress-meta-line">
                    <span class="progress-meta-line__label">
                      {{ currentBatchIsMulti ? t('separate.batchOverallProgress') : progressTitle(currentTask) }}
                    </span>
                    <span v-if="!currentBatchIsMulti && progressDetail(currentTask)" class="progress-meta-line__detail">{{ progressDetail(currentTask) }}</span>
                  </div>
                  <p v-if="taskSubMessage(currentTask)" class="progress-submessage">{{ taskSubMessage(currentTask) }}</p>
                  <div class="progress-facts">
                    <span><n-icon :component="TimeOutline" /> {{ currentTaskDuration }}</span>
                    <span :title="currentTaskOutputPath">{{ t('separate.outputTo') }} {{ currentTaskOutputSummary }}</span>
                  </div>
                </div>
              </div>
            </div>
            <div class="stage-actions">
              <n-button
                strong
                size="large"
                type="error"
                secondary
                class="stage-actions__primary"
                :loading="cancellingTaskId === currentTask.id || cancellingTaskId === 'batch'"
                :disabled="cancellingTaskId === currentTask.id || cancellingTaskId === 'batch'"
                @click="handleCancelCurrentTask"
              >
                {{ t('tasks.cancelAction') }}
              </n-button>
            </div>
          </section>

          <section v-else-if="isTerminalState && currentTask" key="terminal" :class="['stage-view', 'stage-view--terminal', `stage-view--${taskPanelState}`]">
            <div class="stage-hero stage-hero--result">
              <div class="stage-hero__top">
                <span class="stage-badge" :class="`stage-badge--${taskPanelState}`">
                  <n-icon :component="taskPanelState === 'done' ? CheckmarkCircle : (taskPanelState === 'failed' ? CloseOutline : PauseOutline)" />
                  {{ statusLabel(currentTask.status) }}
                </span>
                <n-button v-if="currentTask.logs.length" text size="small" @click="openCurrentLogs">
                  <template #icon><n-icon :component="TerminalOutline" /></template>
                  {{ t('tasks.logs') }}
                </n-button>
              </div>
              <div class="stage-hero__title">
                <h2>{{ currentBatchIsMulti ? currentBatchTitle : statusLabel(currentTask.status) }}</h2>
                <p>
                  {{ currentBatchLine }}
                  <template v-if="taskPanelState === 'done' && !currentBatchIsMulti"> · {{ currentTask.outputs.length }} {{ t('separate.previewStemUnit') }} · {{ currentTaskDuration }}</template>
                </p>
              </div>
              <div v-if="taskPanelState === 'done'" class="result-path" :title="currentTaskOutputPath">
                <n-icon :component="FolderOutline" />
                <code>{{ currentBatchOutputSummary }}</code>
              </div>
              <div v-else-if="taskSubMessage(currentTask)" class="result-note">{{ taskSubMessage(currentTask) }}</div>
              <section v-if="taskPanelState === 'done' && playableOutputs.length" class="result-preview-panel">
                <div class="result-preview-panel__head">
                  <strong>{{ t('separate.previewTitle') }}</strong>
                  <span>{{ playableOutputs.length }} {{ t('separate.previewStemUnit') }}</span>
                </div>
                <div class="preview-output-groups">
                  <div v-for="group in playableOutputGroups" :key="group.taskId" class="preview-output-group">
                    <div v-if="currentBatchIsMulti" class="preview-output-group__head">
                      <strong :title="group.input">{{ getFileName(group.input) }}</strong>
                      <span>{{ group.outputs.length }} {{ t('separate.previewStemUnit') }}</span>
                    </div>
                    <div class="preview-track-list">
                      <div v-for="output in group.outputs" :key="`${group.taskId}:${output.path}`" class="preview-track">
                        <div class="preview-track__title">
                          <strong :title="output.path">{{ getFileName(output.path) }}</strong>
                          <small :title="output.path">{{ output.stem }}</small>
                        </div>
                        <n-button circle secondary size="small" @click="toggleOutputPlayback(output)">
                          <template #icon>
                            <n-icon :component="playingOutputPath === output.path ? PauseOutline : PlayOutline" />
                          </template>
                        </n-button>
                        <n-slider
                          class="preview-track__slider"
                          :value="getOutputPlayback(output.path).currentTime"
                          :min="0"
                          :max="Math.max(getOutputPlayback(output.path).duration, 1)"
                          :step="0.1"
                          :tooltip="false"
                          @update:value="(value: number) => seekOutput(output.path, value)"
                        />
                        <span class="preview-track__time">
                          {{ formatPlaybackTime(getOutputPlayback(output.path).currentTime) }} / {{ formatPlaybackTime(getOutputPlayback(output.path).duration) }}
                        </span>
                      </div>
                    </div>
                  </div>
                </div>
              </section>
            </div>
            <div class="stage-actions">
              <n-button v-if="taskPanelState === 'done'" secondary size="large" @click="task.revealPath(currentTask.outputs[0]?.path || currentTask.output)">
                <template #icon><n-icon :component="OpenOutline" /></template>
                {{ t('separate.openOutput') }}
              </n-button>
              <n-button secondary size="large" @click="retryCurrentTask">
                <template #icon><n-icon :component="PlayOutline" /></template>
                {{ t('common.retry') }}
              </n-button>
              <n-button secondary size="large" @click="resetForNextSeparation">
                {{ t('separate.newSeparation') }}
              </n-button>
              <n-button v-if="taskPanelState === 'done'" type="primary" size="large" class="stage-actions__primary" @click="goToResults">
                {{ t('separate.viewResults') }}
              </n-button>
            </div>
          </section>

          <section v-else key="ready" class="stage-view stage-view--ready">
            <div class="stage-head">
              <div class="stage-head__label">
                <n-icon :component="runMode === 'workflow' ? GitNetworkOutline : CubeOutline" />
                <div>
                  <h2>{{ t('separate.runTarget') }}</h2>
                  <p>{{ runMode === 'workflow' ? t('separate.workflowPanelHint') : t('separate.modelPanelHint') }}</p>
                </div>
              </div>
              <div v-if="runMode === 'model'" class="stage-head__extra">
                <n-select
                  :value="ensembleEnabled ? 'ensemble' : 'single'"
                  size="small"
                  :options="[
                    { label: t('separate.singleModelMode'), value: 'single' },
                    { label: t('separate.ensembleMode'), value: 'ensemble' },
                  ]"
                  class="model-mode-select"
                  :aria-label="t('separate.modelModeLabel')"
                  @update:value="(value: string | number) => { ensembleEnabled = value === 'ensemble' }"
                />
              </div>
            </div>

            <transition name="stage-swap" mode="out-in">
              <div v-if="runMode === 'model'" key="model" class="target-pane" :class="{ 'target-pane--ensemble': ensembleEnabled }">
                <template v-if="downloadedModels.length">
                  <div class="target-toolbar">
                    <n-input
                      v-model:value="modelSearch"
                      clearable
                      :placeholder="t('separate.modelSearchPlaceholder')"
                    >
                      <template #prefix><n-icon :component="SearchOutline" /></template>
                    </n-input>
                    <n-select
                      class="target-toolbar__filter"
                      v-model:value="modelCategoryFilter"
                      :menu-props="{ class: 'model-picker__category-menu' }"
                      :options="modelCategoryOptions"
                    />
                    <n-select
                      v-model:value="modelListSortMode"
                      class="target-toolbar__sort"
                      :options="modelSortOptions"
                      :aria-label="t('separate.modelSortLabel')"
                    />
                    <div class="view-toggle target-toolbar__view" role="group" :aria-label="t('models.viewMode')">
                      <button
                        type="button"
                        class="view-toggle__button"
                        :class="{ 'view-toggle__button--active': modelListViewMode === 'list' }"
                        :aria-pressed="modelListViewMode === 'list'"
                        :title="t('models.viewList')"
                        @click="modelListViewMode = 'list'"
                      >
                        <n-icon :component="ListOutline" />
                      </button>
                      <button
                        type="button"
                        class="view-toggle__button"
                        :class="{ 'view-toggle__button--active': modelListViewMode === 'card' }"
                        :aria-pressed="modelListViewMode === 'card'"
                        :title="t('models.viewCard')"
                        @click="modelListViewMode = 'card'"
                      >
                        <n-icon :component="GridOutline" />
                      </button>
                    </div>
                  </div>
                  <div v-if="ensembleEnabled" class="ensemble-summary-bar">
                    <div class="ensemble-summary-bar__info">
                      <strong>{{ t('separate.ensembleMode') }}</strong>
                      <span>{{ ensembleModels.length ? t('separate.ensembleSelectedCount', { count: ensembleModels.length }) : t('separate.ensembleNeedModels') }}</span>
                    </div>
                    <n-button size="small" secondary :disabled="!ensembleModels.length" @click="showEnsembleModal = true">
                      <template #icon><n-icon :component="SettingsOutline" /></template>
                      {{ t('separate.ensembleConfigure') }}
                    </n-button>
                  </div>
                  <div
                    v-if="pagedDownloadedModels.length"
                    class="target-list target-list--models"
                    :class="`target-list--${modelListViewMode}`"
                    role="listbox"
                    :aria-label="t('separate.model')"
                  >
                    <button
                      v-for="item in pagedDownloadedModels"
                      :key="item.name"
                      type="button"
                      role="option"
                      :aria-selected="ensembleEnabled ? ensembleModels.includes(item.name) : selectedModelName === item.name"
                      class="target-row"
                      :class="{ 'target-row--active': ensembleEnabled ? ensembleModels.includes(item.name) : selectedModelName === item.name }"
                      @click="handleSelectModel(item)"
                      @contextmenu.stop.prevent="openSeparateContextMenu($event, 'model', item)"
                    >
                      <span class="target-row__radio"></span>
                      <span class="target-row__body">
                        <span class="target-row__name" :title="item.name">{{ item.name }}</span>
                        <span class="target-row__meta">
                          <span class="target-row__tag" :title="categoryLabel(item)">{{ categoryLabel(item) }}</span>
                          <span class="target-row__desc" :title="modelMetaLine(item)">{{ modelMetaLine(item) }}</span>
                          <span v-if="modelListViewMode === 'list'" class="target-row__usage">{{ t('models.useCountValue', { count: modelUseCount(item.name) }) }}</span>
                        </span>
                        <!-- Shown because it is searchable: matching on text the user cannot see
                             would make the result list look wrong. -->
                        <span
                          v-if="modelNote(item.name)"
                          class="target-row__note"
                          :title="modelNote(item.name)"
                        >{{ modelNote(item.name) }}</span>
                      </span>
                    </button>
                  </div>
                  <div v-else class="stage-empty">
                    <div class="stage-empty__glyph"><n-icon :component="SearchOutline" /></div>
                    <strong>{{ t('separate.modelSearchEmpty') }}</strong>
                  </div>
                  <div v-if="filteredDownloadedModels.length" class="model-pagination">
                    <span>{{ t('separate.modelPageSummary', { total: filteredDownloadedModels.length }) }}</span>
                    <n-pagination
                      v-model:page="modelPage"
                      v-model:page-size="modelPageSize"
                      :item-count="filteredDownloadedModels.length"
                      :page-sizes="modelPageSizeOptions"
                      show-size-picker
                      size="small"
                    />
                  </div>
                  <div v-if="selectedModelName && !modelDownloaded" class="stage-alert">
                    {{ t('separate.startHintModelMissing') }}
                  </div>
                </template>
                <div v-else class="stage-empty" :class="{ 'stage-empty--loading': isLoading }">
                  <div class="stage-empty__glyph">
                    <n-spin v-if="isLoading" size="medium" />
                    <n-icon v-else :component="CubeOutline" />
                  </div>
                  <strong>{{ isLoading ? t('separate.modelPanelLoadingTitle') : t('separate.modelPanelEmptyTitle') }}</strong>
                  <p>{{ isLoading ? t('separate.modelPanelLoadingDesc') : t('separate.modelPanelEmptyDesc') }}</p>
                  <n-button secondary :loading="isLoading" @click="model.loadModels()">
                    {{ t('separate.modelPanelPrimaryAction') }}
                  </n-button>
                </div>
              </div>

              <div v-else key="workflow" class="target-pane">
                <div class="target-toolbar target-toolbar--single">
                  <n-input
                    v-model:value="workflowSearch"
                    clearable
                    :placeholder="t('separate.workflowSearchPlaceholder')"
                  >
                    <template #prefix><n-icon :component="SearchOutline" /></template>
                  </n-input>
                </div>
                <div v-if="filteredWorkflows.length" class="target-list" role="listbox" :aria-label="t('separate.workflow')">
                  <button
                    v-for="item in filteredWorkflows"
                    :key="item.id"
                    type="button"
                    role="option"
                    :aria-selected="selectedWorkflowId === item.id"
                    :aria-disabled="Boolean(workflowIssue(item))"
                    :title="workflowIssue(item) || item.name"
                    class="target-row"
                    :class="{
                      'target-row--active': selectedWorkflowId === item.id,
                      'target-row--unavailable': Boolean(workflowIssue(item)),
                    }"
                    @click="selectRunnableWorkflow(item)"
                    @contextmenu.stop.prevent="openSeparateContextMenu($event, 'workflow', item)"
                  >
                    <span class="target-row__radio"></span>
                    <span class="target-row__body">
                      <span class="target-row__heading">
                        <span class="target-row__name" :title="item.name">{{ item.name }}</span>
                        <span v-if="workflowIssue(item)" class="target-row__unavailable">
                          {{ t('separate.workflowUnavailable') }}
                        </span>
                      </span>
                      <span class="target-row__meta">
                        <span class="target-row__tag">{{ t('separate.workflow') }}</span>
                        <span class="target-row__desc" :title="item.description">{{ item.description || t('separate.workflowNoDescription') }}</span>
                      </span>
                      <span v-if="workflowIssue(item)" class="target-row__issue">
                        {{ workflowIssue(item) }}
                      </span>
                    </span>
                  </button>
                </div>
                <div v-else class="stage-empty">
                  <div class="stage-empty__glyph"><n-icon :component="GitNetworkOutline" /></div>
                  <strong>{{ t('separate.workflowEmptyTitle') }}</strong>
                  <p>{{ t('separate.workflowEmptyDesc') }}</p>
                  <n-button secondary @click="router.push('/workflows')">
                    {{ t('separate.workflowCreateAction') }}
                  </n-button>
                </div>
              </div>
            </transition>

            <footer class="launch-bar" :class="`launch-bar--${canStart ? 'ready' : 'idle'}`">
              <div class="launch-bar__status">
                <span class="launch-bar__glyph"><n-icon :component="CheckmarkCircle" /></span>
                <div class="launch-bar__text">
                  <strong>{{ startStatusText }}</strong>
                  <span :title="outputSummaryPath">{{ outputSummaryPath }}</span>
                </div>
              </div>
              <div class="launch-bar__actions">
                <n-button quaternary class="launch-bar__reveal" :title="t('separate.openOutput')" @click="task.revealPath(currentTask?.outputs[0]?.path || normalizedOutputDir)">
                  <template #icon><n-icon :component="OpenOutline" /></template>
                  {{ t('separate.openOutput') }}
                </n-button>
                <n-button type="primary" size="large" class="launch-bar__go" :disabled="!canStart" @click="start">
                  <template #icon><n-icon :component="PlayOutline" /></template>
                  {{ t('separate.startTask') }}
                </n-button>
              </div>
            </footer>
          </section>
        </transition>
      </main>
    </div>

    <n-dropdown
      placement="bottom-start"
      trigger="manual"
      :x="contextMenuX"
      :y="contextMenuY"
      :options="contextMenuOptions"
      :show="contextMenuVisible"
      @clickoutside="closeSeparateContextMenu"
      @select="handleSeparateContextMenuSelect"
    />

    <n-modal
      :show="showModelNoteEditor"
      @update:show="handleModelNoteEditorUpdate"
    >
      <n-card
        class="separate-note-modal"
        :title="t('models.editNote')"
        :bordered="false"
        closable
        role="dialog"
        aria-modal="true"
        @close="closeModelNoteEditor"
      >
        <div v-if="noteEditorModel" class="separate-note-modal__body">
          <strong class="separate-note-modal__name">{{ noteEditorModel.name }}</strong>
          <n-input
            v-model:value="noteDraft"
            type="textarea"
            :autosize="{ minRows: 4, maxRows: 8 }"
            clearable
            :maxlength="240"
            show-count
            :placeholder="t('models.notePlaceholder')"
          />
        </div>
        <template #footer>
          <div class="separate-note-modal__footer">
            <n-button secondary @click="closeModelNoteEditor">{{ t('common.cancel') }}</n-button>
            <n-button type="primary" :disabled="!noteEditorModel" @click="saveModelNote">{{ t('common.save') }}</n-button>
          </div>
        </template>
      </n-card>
    </n-modal>

    <n-modal
      :show="showWorkflowMetaEditor"
      @update:show="handleWorkflowMetaEditorUpdate"
    >
      <n-card
        class="separate-workflow-meta-modal"
        :title="t('workflows.editMeta')"
        :bordered="false"
        closable
        role="dialog"
        aria-modal="true"
        @close="closeWorkflowMetaEditor"
      >
        <div v-if="workflowMetaEditor" class="separate-workflow-meta-modal__body">
          <label class="separate-workflow-meta-modal__field">
            <span>{{ t('workflows.name') }}</span>
            <n-input
              v-model:value="workflowNameDraft"
              :disabled="workflowMetaSaving"
              :placeholder="t('workflows.namePlaceholder')"
              maxlength="120"
              show-count
            />
          </label>
          <label class="separate-workflow-meta-modal__field">
            <span>{{ t('workflows.note') }}</span>
            <n-input
              v-model:value="workflowNoteDraft"
              type="textarea"
              :disabled="workflowMetaSaving"
              :autosize="{ minRows: 4, maxRows: 8 }"
              clearable
              :maxlength="240"
              show-count
              :placeholder="t('workflows.notePlaceholder')"
            />
          </label>
        </div>
        <template #footer>
          <div class="separate-workflow-meta-modal__footer">
            <n-button secondary :disabled="workflowMetaSaving" @click="closeWorkflowMetaEditor">
              {{ t('common.cancel') }}
            </n-button>
            <n-button type="primary" :loading="workflowMetaSaving" :disabled="!workflowMetaEditor" @click="saveWorkflowMeta">
              {{ t('common.save') }}
            </n-button>
          </div>
        </template>
      </n-card>
    </n-modal>

    <n-modal v-model:show="showEnsembleModal" :mask-closable="true">
      <n-card
        class="ensemble-modal"
        :title="t('separate.ensembleConfigureTitle')"
        :bordered="false"
        closable
        role="dialog"
        aria-modal="true"
        @close="showEnsembleModal = false"
      >
        <div class="ensemble-modal__intro">
          <span>{{ t('separate.ensembleHint') }}</span>
          <strong>{{ t('separate.ensembleSelectedCount', { count: ensembleModels.length }) }}</strong>
        </div>
        <div class="ensemble-config__fields">
          <label>
            <span>{{ t('separate.ensembleStem') }}</span>
            <n-input v-model:value="ensembleStem" :placeholder="t('separate.ensembleStemPlaceholder')" />
          </label>
          <label>
            <span>{{ t('separate.ensembleType') }}</span>
            <n-select v-model:value="ensembleType" :options="ensembleTypeOptions" />
          </label>
        </div>
        <n-divider title-placement="left">{{ t('separate.ensembleWeights') }}</n-divider>
        <div class="ensemble-modal__weights">
          <div v-for="name in ensembleModels" :key="name" class="ensemble-modal__weight">
            <span class="ensemble-modal__model-name" :title="name">{{ name }}</span>
            <n-select
              v-model:value="ensembleModelStems[name]"
              size="small"
              :options="ensembleStemOptionsByModel[name] || []"
              :placeholder="t('separate.ensembleModelStemPlaceholder')"
              :disabled="!(ensembleStemOptionsByModel[name] || []).length"
            />
            <n-slider v-model:value="ensembleWeights[name]" :min="0" :max="1" :step="0.05" />
            <n-input-number v-model:value="ensembleWeights[name]" class="ensemble-modal__weight-input" size="small" :min="0" :max="1" :step="0.05" />
          </div>
        </div>
        <template #footer>
          <n-button type="primary" @click="showEnsembleModal = false">{{ t('common.close') }}</n-button>
        </template>
      </n-card>
    </n-modal>

    <n-modal v-model:show="showSettingsDrawer">
      <n-card
        class="settings-modal"
        :title="t('separate.settingsDrawerTitle')"
        :bordered="false"
        closable
        role="dialog"
        aria-modal="true"
        @close="showSettingsDrawer = false"
      >
        <div class="settings-drawer__content">
          <div class="settings-group">
            <div class="settings-group__head">
              <strong>{{ t('separate.runOptionsTitle') }}</strong>
              <span>{{ t('separate.runOptionsHint') }}</span>
            </div>
            <div class="check-list">
              <n-checkbox v-model:checked="useTta">{{ t('separate.tta') }}</n-checkbox>
            </div>
          </div>

          <div class="settings-group">
            <div class="settings-group__head">
              <strong>{{ t('separate.audioQualityTitle') }} · {{ formatLabel }}</strong>
              <span>{{ t('separate.audioQualityEditable') }}</span>
            </div>
            <n-grid :cols="2" :x-gap="16" :y-gap="16" responsive="screen">
              <n-grid-item v-if="effectiveFormat === 'wav'">
                <div class="field-block">
                  <label>{{ t('audio.wavBitDepth') }}</label>
                  <n-select v-model:value="settings.wavBitDepth" :options="wavBitDepthOptions" />
                </div>
              </n-grid-item>
              <n-grid-item v-if="effectiveFormat === 'flac'">
                <div class="field-block">
                  <label>{{ t('audio.flacBitDepth') }}</label>
                  <n-select v-model:value="settings.flacBitDepth" :options="flacBitDepthOptions" />
                </div>
              </n-grid-item>
              <n-grid-item v-if="effectiveFormat === 'mp3'">
                <div class="field-block">
                  <label>{{ t('audio.mp3BitRate') }}</label>
                  <n-select v-model:value="settings.mp3BitRate" :options="bitRateOptions" />
                </div>
              </n-grid-item>
              <n-grid-item v-if="effectiveFormat === 'm4a'">
                <div class="field-block">
                  <label>{{ t('audio.m4aBitRate') }}</label>
                  <n-select v-model:value="settings.m4aBitRate" :options="bitRateOptions" />
                </div>
              </n-grid-item>
              <n-grid-item v-if="effectiveFormat === 'm4a'">
                <div class="field-block">
                  <label>{{ t('audio.m4aCodec') }}</label>
                  <n-select v-model:value="settings.m4aCodec" :options="m4aCodecOptions" />
                </div>
              </n-grid-item>
              <n-grid-item v-if="runMode === 'workflow' || showNormalizeField">
                <div class="field-block field-block--inline-check">
                  <n-checkbox v-model:checked="normalize">{{ t('inference.normalize') }}</n-checkbox>
                </div>
              </n-grid-item>
            </n-grid>
          </div>

          <div class="settings-group">
            <n-collapse :default-expanded-names="[]">
              <n-collapse-item :title="t('inference.advancedParams')" name="inference">
                <p class="advanced-hint">{{ t('separate.advancedPanelHint') }}</p>
                <div v-if="advancedParamsLoading" class="advanced-loading">
                  <n-spin size="small" />
                  <span>{{ t('separate.advancedPanelLoading') }}</span>
                </div>
                <n-grid v-if="runMode === 'model'" :cols="2" :x-gap="16" :y-gap="16" responsive="screen">
                  <n-grid-item v-if="hasInferenceField('batch_size')">
                    <div class="field-block">
                      <label>{{ t('inference.batchSize') }}</label>
                      <n-input-number v-model:value="batch_size" :min="0" :max="32" style="width:100%" @blur="task.restoreInferenceNumberFallback('batch_size')" />
                    </div>
                  </n-grid-item>
                  <n-grid-item v-if="hasInferenceField('overlap_size')">
                    <div class="field-block">
                      <label>{{ t('inference.overlapSize') }}</label>
                      <n-input-number v-model:value="overlap_size" :min="0" :max="1048576" style="width:100%" @blur="task.restoreInferenceNumberFallback('overlap_size')" />
                    </div>
                  </n-grid-item>
                  <n-grid-item v-if="hasInferenceField('num_overlap')">
                    <div class="field-block">
                      <label>{{ t('inference.numOverlap') }}</label>
                      <n-input-number v-model:value="num_overlap" :min="0" :max="128" style="width:100%" @blur="task.restoreInferenceNumberFallback('num_overlap')" />
                    </div>
                  </n-grid-item>
                  <n-grid-item v-if="hasInferenceField('chunk_size')">
                    <div class="field-block">
                      <label>{{ t('inference.chunkSize') }}</label>
                      <n-input-number v-model:value="chunk_size" :min="0" :max="1048576" :step="1024" style="width:100%" @blur="task.restoreInferenceNumberFallback('chunk_size')" />
                    </div>
                  </n-grid-item>
                  <n-grid-item v-if="hasInferenceField('window_size')">
                    <div class="field-block">
                      <label>{{ t('inference.vrWindowSize') }}</label>
                      <n-input-number v-model:value="window_size" :min="0" :max="4096" style="width:100%" @blur="task.restoreInferenceNumberFallback('window_size')" />
                    </div>
                  </n-grid-item>
                  <n-grid-item v-if="hasInferenceField('aggression')">
                    <div class="field-block">
                      <label>{{ t('inference.vrAggression') }}</label>
                      <n-input-number v-model:value="aggression" :min="0" :max="100" style="width:100%" />
                    </div>
                  </n-grid-item>
                  <n-grid-item v-if="hasInferenceField('post_process_threshold')">
                    <div class="field-block">
                      <label>{{ t('inference.vrPostProcessThreshold') }}</label>
                      <n-input-number v-model:value="post_process_threshold" :min="0" :max="1" :step="0.05" style="width:100%" />
                    </div>
                  </n-grid-item>
                </n-grid>
                <div class="check-list check-list--spaced">
                  <n-checkbox v-if="runMode === 'workflow' || showStandardizeField" v-model:checked="standardize">{{ t('inference.standardize') }}</n-checkbox>
                  <n-checkbox v-if="runMode === 'model' && hasInferenceField('enable_post_process')" v-model:checked="enable_post_process">{{ t('inference.vrEnablePostProcess') }}</n-checkbox>
                  <n-checkbox v-if="runMode === 'model' && hasInferenceField('high_end_process')" v-model:checked="high_end_process">{{ t('inference.vrHighEndProcess') }}</n-checkbox>
                </div>
                <p v-if="runMode === 'model' && !advancedParamsLoading && !hasVisibleAdvancedFields" class="advanced-empty">
                  {{ t('separate.advancedPanelEmpty') }}
                </p>
              </n-collapse-item>
            </n-collapse>
          </div>
        </div>

        <template #footer>
          <div class="drawer-footer">
            <n-button type="primary" @click="showSettingsDrawer = false">{{ t('common.close') }}</n-button>
          </div>
        </template>
      </n-card>
    </n-modal>
    <n-modal v-model:show="showNamingModal" style="width:min(640px, 92vw)">
      <n-card
        class="naming-modal"
        :title="t('separate.namingModalTitle')"
        :bordered="false"
        closable
        role="dialog"
        aria-modal="true"
        @close="showNamingModal = false"
      >
        <div class="naming-modal__content">
          <div class="naming-section naming-section--main">
            <div class="naming-section__head">
              <strong>{{ t('separate.namingTemplate') }}</strong>
              <span>{{ t('separate.namingTemplateHint') }}</span>
            </div>
            <n-input
              ref="namingTemplateInputRef"
              v-model:value="outputNamingTemplate"
              placeholder="%index%_%filename%_%stem%"
            />
            <div class="naming-preview-inline">
              <span>{{ t('separate.namingPreview') }}</span>
              <div v-for="row in outputNamingPreviewParts" :key="row.key" class="naming-preview-file">
                <span
                  v-for="(part, partIndex) in row.parts"
                  :key="`${row.key}-${partIndex}`"
                  class="naming-preview-part"
                  :class="`naming-preview-part--${part.token.replace(/[%_]/g, '')}`"
                >{{ part.text }}</span>
              </div>
            </div>
          </div>

          <div class="naming-section">
            <div class="naming-section__head">
              <strong>{{ t('separate.namingVariables') }}</strong>
              <span>{{ t('separate.namingVariablesHint') }}</span>
            </div>
            <div class="token-grid">
              <button
                v-for="token in namingTokens"
                :key="token.value"
                type="button"
                class="token-chip"
                :title="token.value"
                @click="insertNamingToken(token.value)"
              >
                <span>{{ token.label }}</span>
                <code>{{ token.value }}</code>
              </button>
            </div>
          </div>

          <div v-if="usesIndexToken" class="naming-section">
            <div class="naming-section__head">
              <strong>{{ t('separate.namingNumberOrder') }}</strong>
              <span>{{ t('separate.namingNumberOrderHint') }}</span>
            </div>
            <div v-if="orderedOutputStems.length" class="stem-order-list">
              <div
                v-for="(stem, index) in orderedOutputStems"
                :key="stem"
                class="stem-order-row"
                :class="{ 'stem-order-row--dragging': draggedStemIndex === index }"
                @pointerdown="onStemPointerDown($event, index)"
                @pointermove="onStemPointerMove"
                @pointerup="finishStemPointerDrag"
                @pointercancel="finishStemPointerDrag"
              >
                <n-icon :component="ReorderFourOutline" />
                <span>{{ padNumber(index + 1) }}</span>
                <strong>{{ stem }}</strong>
                <div class="stem-order-row__actions">
                  <n-button quaternary circle size="tiny" :disabled="index === 0" :title="t('separate.moveUp')" @click="moveStem(index, index - 1)">
                    <template #icon><n-icon :component="ChevronUpOutline" /></template>
                  </n-button>
                  <n-button quaternary circle size="tiny" :disabled="index === orderedOutputStems.length - 1" :title="t('separate.moveDown')" @click="moveStem(index, index + 1)">
                    <template #icon><n-icon :component="ChevronDownOutline" /></template>
                  </n-button>
                </div>
              </div>
            </div>
            <n-empty v-else size="small" :description="t('separate.namingNoStems')" />
          </div>
          <div v-else class="naming-section naming-section--muted">
            {{ t('separate.namingNumberOrderDisabled') }}
          </div>
        </div>

        <template #footer>
          <div class="drawer-footer">
            <n-button secondary @click="resetOutputNaming">{{ t('separate.namingReset') }}</n-button>
            <n-button type="primary" @click="showNamingModal = false">{{ t('common.close') }}</n-button>
          </div>
        </template>
      </n-card>
    </n-modal>
    <n-modal v-model:show="showLogModal" style="width:min(900px, 92vw)">
      <n-card
        :title="currentTask ? `${currentTaskFileName} - ${t('tasks.logs')}` : t('tasks.logs')"
        :bordered="false"
        size="small"
        role="dialog"
        aria-modal="true"
      >
        <div v-if="currentTask?.logs.length" class="log-console">
          <div v-for="(line, index) in currentTask.logs" :key="`${index}-${line}`" class="log-line">
            <span class="log-line-number">{{ String(index + 1).padStart(3, '0') }}</span>
            <span class="log-line-text">{{ line }}</span>
          </div>
        </div>
        <div v-else class="log-empty">{{ t('tasks.noLogs') }}</div>
      </n-card>
    </n-modal>
  </div>
</template>


<style scoped>
/* ============ Workstation shell ============ */
.separate-page {
  max-width: var(--page-max-width);
  margin: 0 auto;
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.console-topbar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
}

.console-topbar__brand {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.console-topbar__brand :deep(.app-brand-mark) {
  transform-origin: center center;
}

.console-topbar__title {
  display: grid;
  gap: 1px;
  min-width: 0;
}

.console-topbar__title h1 {
  margin: 0;
  font-size: 19px;
  font-weight: 600;
  letter-spacing: -0.03em;
  line-height: 1.15;
}

.console-topbar__title span {
  font-size: 12px;
  color: var(--on-surface-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.console-topbar__controls {
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
}

.console-topbar__manage {
  color: color-mix(in srgb, var(--on-surface-muted) 84%, var(--on-surface));
  white-space: nowrap;
  font-size: 12px;
}

/* segmented mode switch */
.mode-switch {
  display: inline-flex;
  padding: 3px;
  border-radius: 12px;
  background: color-mix(in srgb, var(--surface-2) 60%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 90%, transparent);
}

.mode-switch :deep(.n-radio-button) {
  height: 30px;
  line-height: 30px;
  padding: 0 16px;
  border: 0;
  border-radius: 9px;
  background: transparent;
  font-size: 12.5px;
  color: var(--on-surface-muted);
  transition: color 160ms ease, background 200ms ease, box-shadow 200ms ease;
}

.mode-switch :deep(.n-radio-group__splitor) { display: none; }
.mode-switch :deep(.n-radio-button__state-border) { display: none; }

.mode-switch :deep(.n-radio-button--checked) {
  color: var(--on-surface);
  background: color-mix(in srgb, var(--surface) 70%, var(--surface-1));
  box-shadow:
    0 1px 2px rgba(0,0,0,0.28),
    inset 0 0 0 1px color-mix(in srgb, var(--primary-border) 40%, transparent);
}

/* ============ Console grid: rail + stage ============ */
.console {
  flex: 1 1 auto;
  min-height: 0;
  display: grid;
  /* The rail grows with the window instead of staying at 480px: on a wide screen a fixed rail
     leaves the input panel cramped next to a stage several times its width. The cap keeps it from
     taking over once the stage has all the room it needs. */
  grid-template-columns: minmax(420px, 0.42fr) minmax(0, 1fr);
  gap: 16px;
}

.console__rail {
  min-height: 0;
  display: grid;
  grid-template-rows: minmax(0, 1fr) auto;
  gap: 12px;
}

.console__stage {
  min-height: 0;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

/* ============ Rail cards ============ */
.rail-card {
  min-height: 0;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  gap: 14px;
  padding: 17px 18px;
  border-radius: 18px;
  background: color-mix(in srgb, var(--surface-1) 78%, transparent);
  box-shadow:
    inset 0 0 0 1px color-mix(in srgb, var(--outline) 80%, transparent),
    inset 0 1px 0 rgba(255,255,255,0.03);
}

.rail-card--output {
  grid-template-rows: auto auto;
}

.rail-card__head {
  display: flex;
  align-items: center;
  gap: 11px;
  min-width: 0;
}

.rail-card__index {
  flex: 0 0 auto;
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border-radius: 9px;
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--primary-strong);
  background: var(--primary-soft);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary-border) 40%, transparent);
}

.rail-card__label {
  min-width: 0;
  flex: 1;
  display: grid;
  gap: 1px;
}

.rail-card__label strong {
  font-size: 13.5px;
  font-weight: 600;
  letter-spacing: -0.01em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rail-card__label small {
  font-size: 11px;
  color: var(--on-surface-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rail-card__actions {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.rail-card__actions--input {
  flex: 0 0 auto;
  align-self: center;
  margin-left: auto;
  justify-content: flex-end;
}

.rail-card__clear {
  color: var(--danger);
  font-size: 11px;
}

.rail-card__more {
  color: var(--on-surface-muted);
  font-size: 11px;
}

.rail-card__body {
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* pick buttons */
.picker-buttons {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.picker-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  height: 36px;
  border: 0;
  border-radius: 11px;
  background: color-mix(in srgb, var(--surface-2) 62%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 88%, transparent);
  color: var(--on-surface);
  font-size: 12.5px;
  font-family: inherit;
  cursor: pointer;
  transition: background 150ms ease, box-shadow 150ms ease, transform 120ms ease;
}

.picker-btn .n-icon { font-size: 15px; color: var(--primary-strong); }
.picker-btn:hover:not(:disabled) {
  background: color-mix(in srgb, var(--primary-soft) 26%, var(--surface-2));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary-border) 52%, transparent);
}
.picker-btn:active:not(:disabled) { transform: translateY(1px); }
.picker-btn:disabled { opacity: 0.5; cursor: not-allowed; }

.input-retention-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--on-surface-muted);
  font-size: 12px;
}

.input-retention-toggle--head {
  flex: 0 0 auto;
  justify-content: flex-end;
  min-width: 0;
  gap: 6px;
  font-size: 11px;
  white-space: nowrap;
}

.input-retention-toggle--head span {
  min-width: 0;
  line-height: 1.25;
  text-align: right;
}

/* dropzone / file list */
.dropzone {
  flex: 1 1 auto;
  min-height: 0;
  border-radius: 13px;
  border: 1px dashed color-mix(in srgb, var(--outline) 130%, transparent);
  background: color-mix(in srgb, var(--surface) 40%, transparent);
  transition: border-color 150ms ease, background 150ms ease, box-shadow 150ms ease;
  overflow: hidden;
}

.dropzone--filled {
  border-style: solid;
  border-color: color-mix(in srgb, var(--outline) 90%, transparent);
}

.dropzone--dragging {
  border-color: var(--primary);
  background: color-mix(in srgb, var(--primary-soft) 18%, var(--surface-1));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary-border) 60%, transparent);
}

.dropzone--clickable { cursor: pointer; }
.dropzone--clickable:hover {
  border-color: color-mix(in srgb, var(--primary) 70%, var(--outline));
  background: color-mix(in srgb, var(--primary-soft) 12%, var(--surface));
}
.dropzone--clickable:hover .dropzone__glyph {
  color: var(--primary);
  background: color-mix(in srgb, var(--primary-soft) 46%, var(--surface-2));
}

.file-list {
  height: 100%;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  scrollbar-width: thin;
  scrollbar-color: color-mix(in srgb, var(--outline) 120%, transparent) transparent;
}
.file-list::-webkit-scrollbar { width: 6px; }
.file-list::-webkit-scrollbar-thumb { border-radius: 999px; background: color-mix(in srgb, var(--outline) 130%, transparent); }
.file-list::-webkit-scrollbar-track { background: transparent; }

.file-chip {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 7px 8px 7px 9px;
  border-radius: 10px;
  background: color-mix(in srgb, var(--surface-2) 50%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 76%, transparent);
  cursor: grab;
  user-select: none;
  touch-action: none;
}

.file-chip--dragging {
  opacity: 0.55;
}

.file-chip__handle {
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  width: 16px;
  color: var(--on-surface-muted);
}

.file-chip:active { cursor: grabbing; }

.file-chip__glyph {
  flex: 0 0 auto;
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border-radius: 8px;
  font-size: 14px;
  color: var(--primary-strong);
  background: var(--primary-soft);
}

.file-chip__main {
  min-width: 0;
  flex: 1;
  display: grid;
  gap: 3px;
}

.file-chip__main strong {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12.5px;
  font-weight: 600;
}

.file-chip__sub {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 7px;
}

.file-chip__sub code {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 10.5px;
  color: var(--on-surface-muted);
}

.file-chip__kind {
  flex: 0 0 auto;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 10px;
  color: color-mix(in srgb, var(--primary-strong) 76%, var(--on-surface-muted));
  background: color-mix(in srgb, var(--primary-soft) 30%, var(--surface-2));
}

.file-chip__remove { flex: 0 0 auto; color: var(--on-surface-muted); }

.dropzone__empty {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 18px;
  text-align: center;
}

.dropzone__glyph {
  width: 44px;
  height: 44px;
  display: grid;
  place-items: center;
  border-radius: 13px;
  font-size: 21px;
  color: var(--primary-strong);
  background: color-mix(in srgb, var(--primary-soft) 34%, var(--surface-2));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary-border) 30%, transparent);
  margin-bottom: 2px;
}

.dropzone__empty strong { font-size: 13px; color: color-mix(in srgb, var(--on-surface) 72%, var(--on-surface-muted)); font-weight: 500; max-width: 240px; }

/* output fields */
.rail-card__body--output { gap: 14px; }

.ofield { display: grid; gap: 7px; min-width: 0; }

.ofield__label {
  font-size: 11px;
  color: var(--on-surface-muted);
  font-weight: 500;
}

.ofield__static {
  min-height: 30px;
  display: flex;
  align-items: center;
  padding: 0 11px;
  border-radius: 9px;
  font-size: 12.5px;
  font-weight: 600;
  background: color-mix(in srgb, var(--surface-2) 56%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 80%, transparent);
}
.ofield__static--muted { font-weight: 400; color: var(--on-surface-muted); }

.ofield-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 10px;
  align-items: start;
}

.dir-input {
  display: flex;
  gap: 8px;
}
.dir-input .n-input { flex: 1 1 auto; min-width: 0; }
.dir-input__browse { flex: 0 0 auto; }

.naming-field { margin-top: -2px; }

.naming-summary {
  width: 100%;
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-height: 32px;
  border: 0;
  border-radius: 9px;
  padding: 6px 10px;
  background: color-mix(in srgb, var(--surface-2) 48%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 82%, transparent);
  color: var(--on-surface);
  font-family: inherit;
  cursor: pointer;
}

.naming-summary:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.naming-summary span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}

.naming-summary small {
  flex: 0 0 auto;
  color: var(--primary-strong);
  font-size: 11px;
}

/* save-mode segmented control */
.seg {
  display: inline-flex;
  width: 100%;
  padding: 2px;
  border-radius: 9px;
  background: color-mix(in srgb, var(--surface-2) 46%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 82%, transparent);
}
.seg--locked { opacity: 0.55; pointer-events: none; }

.seg__btn {
  flex: 1 1 0;
  min-width: 0;
  height: 28px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--on-surface-muted);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
  transition: color 140ms ease, background 180ms ease, box-shadow 180ms ease;
}
.seg__btn--active {
  color: var(--on-surface);
  background: color-mix(in srgb, var(--surface) 72%, var(--surface-1));
  box-shadow:
    0 1px 2px rgba(0,0,0,0.24),
    inset 0 0 0 1px color-mix(in srgb, var(--primary-border) 38%, transparent);
}

.ofield--stems .stem-chips {
  max-height: 108px;
  overflow-y: auto;
  padding: 2px 2px 2px 0;
  overscroll-behavior: contain;
}

.stem-chips :deep(.n-checkbox-group) {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}

.stem-chips :deep(.n-checkbox) {
  min-height: 28px;
  align-items: center;
  padding: 4px 10px 4px 8px;
  border-radius: 9px;
  background: color-mix(in srgb, var(--surface-2) 52%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 78%, transparent);
  font-size: 12px;
  transition: background 140ms ease, box-shadow 140ms ease;
}
.stem-chips :deep(.n-checkbox:hover) {
  background: color-mix(in srgb, var(--primary-soft) 22%, var(--surface-2));
}
.stem-chips :deep(.n-checkbox--checked) {
  background: color-mix(in srgb, var(--primary-soft) 34%, var(--surface-2));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary-border) 60%, transparent);
}

/* ============ Launch bar (stage bottom) ============ */
.launch-bar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 12px 14px;
  border-radius: 15px;
  background:
    linear-gradient(180deg, color-mix(in srgb, var(--primary-soft) 12%, transparent), transparent),
    color-mix(in srgb, var(--surface-2) 34%, transparent);
  box-shadow:
    inset 0 0 0 1px color-mix(in srgb, var(--outline) 84%, transparent),
    inset 0 1px 0 rgba(255,255,255,0.04);
}
.launch-bar--ready {
  box-shadow:
    inset 0 0 0 1px color-mix(in srgb, var(--success) 30%, transparent),
    inset 0 1px 0 rgba(255,255,255,0.04);
}

.launch-bar__status {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 11px;
}

.launch-bar__glyph {
  flex: 0 0 auto;
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  border-radius: 10px;
  font-size: 19px;
  color: var(--success);
  background: color-mix(in srgb, var(--success) 14%, var(--surface-2));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--success) 32%, transparent);
}
.launch-bar--idle .launch-bar__glyph {
  color: var(--on-surface-muted);
  background: color-mix(in srgb, var(--surface-2) 60%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 80%, transparent);
}

.launch-bar__text {
  min-width: 0;
  display: grid;
  gap: 1px;
}

.launch-bar__text strong {
  font-size: 13px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.launch-bar--idle .launch-bar__text strong { color: var(--on-surface-muted); }

.launch-bar__text span {
  font-size: 11px;
  color: var(--on-surface-muted);
  overflow-wrap: anywhere;
  white-space: normal;
  font-variant-numeric: tabular-nums;
}

.launch-bar__actions {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
}

.launch-bar__reveal { color: var(--on-surface-muted); }

.launch-bar__go {
  min-width: 156px;
  font-weight: 600;
  box-shadow:
    inset 0 1px 0 rgba(255,255,255,0.2),
    0 12px 28px color-mix(in srgb, var(--primary-glow) 42%, transparent);
}

/* ============ Stage ============ */
.stage-view {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 18px;
  border-radius: 20px;
  background:
    radial-gradient(120% 90% at 50% -10%, color-mix(in srgb, var(--primary-glow) 10%, transparent), transparent 60%),
    color-mix(in srgb, var(--surface-1) 74%, transparent);
  box-shadow:
    inset 0 0 0 1px color-mix(in srgb, var(--outline) 76%, transparent),
    inset 0 1px 0 rgba(255,255,255,0.03);
}

.stage-view--running {
  box-shadow:
    inset 0 0 0 1px color-mix(in srgb, var(--primary-border) 48%, transparent),
    0 0 0 1px color-mix(in srgb, var(--primary-glow) 16%, transparent);
}
.stage-view--done { box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--success) 40%, transparent); }
.stage-view--failed { box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--danger) 40%, transparent); }
.stage-view--cancelled { box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--warning) 40%, transparent); }

.stage-swap-enter-active { transition: opacity 260ms cubic-bezier(0.22,1,0.36,1), transform 300ms cubic-bezier(0.22,1,0.36,1); }
.stage-swap-leave-active { transition: opacity 160ms ease, transform 160ms ease; }
.stage-swap-enter-from { opacity: 0; transform: translateY(10px) scale(0.99); }
.stage-swap-leave-to { opacity: 0; transform: translateY(-6px) scale(0.995); }

/* stage: ready target selection */
.stage-head {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.stage-head__label {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.stage-head__label > .n-icon {
  flex: 0 0 auto;
  width: 38px;
  height: 38px;
  display: grid;
  place-items: center;
  border-radius: 12px;
  font-size: 19px;
  color: var(--primary-strong);
  background: color-mix(in srgb, var(--primary-soft) 40%, var(--surface-2));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary-border) 34%, transparent);
}

.stage-head__label h2 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  letter-spacing: -0.02em;
}

.stage-head__label p {
  margin: 2px 0 0;
  font-size: 12px;
  color: var(--on-surface-muted);
  line-height: 1.4;
}

.target-pane {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.stage-head__extra { flex: 0 0 auto; }
.model-mode-select { width: 156px; }
.model-mode-select :deep(.n-base-selection-label) { white-space: nowrap; }

.view-toggle {
  display: inline-flex;
  flex-shrink: 0;
  padding: 2px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--surface-2) 60%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 55%, transparent);
}

.view-toggle__button {
  display: grid;
  place-items: center;
  width: 28px;
  height: 24px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--on-surface-muted);
  cursor: pointer;
  font-size: 14px;
  transition: background 150ms ease, color 150ms ease;
}

.view-toggle__button:hover { color: var(--on-surface); }

.view-toggle__button:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
}

.view-toggle__button--active {
  background: var(--surface-1);
  color: var(--primary);
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.12);
}

.model-mode-control {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, auto));
  gap: 2px;
  padding: 3px;
  border: 1px solid var(--outline);
  border-radius: 8px;
  background: color-mix(in srgb, var(--surface-2) 72%, transparent);
}
.model-mode-control__tab {
  min-width: 86px;
  min-width: 0;
  padding: 6px 10px;
  border: 1px solid transparent;
  border-radius: 6px;
  color: var(--on-surface-muted);
  background: transparent;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  cursor: pointer;
  transition: background 180ms ease, border-color 180ms ease, color 180ms ease, transform 180ms ease;
}
.model-mode-control__tab:hover { color: var(--on-surface); background: color-mix(in srgb, var(--surface-1) 72%, transparent); }
.model-mode-control__tab:active { transform: translateY(1px); }
.model-mode-control__tab:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.model-mode-control__tab--active {
  color: var(--on-surface);
  border-color: color-mix(in srgb, var(--primary) 42%, var(--outline));
  background: var(--surface-1);
  box-shadow: 0 1px 4px color-mix(in srgb, var(--primary) 12%, transparent);
}

.target-toolbar {
  flex: 0 0 auto;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 170px 170px auto;
  align-items: center;
  gap: 10px;
}
.target-toolbar--single { grid-template-columns: minmax(0, 1fr); }
.target-toolbar__filter,
.target-toolbar__sort { min-width: 0; }
.target-toolbar__view { justify-self: end; }

.ensemble-summary-bar {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
  padding: 7px 10px;
  border: 1px solid var(--outline);
  border-radius: 7px;
  background: var(--surface-2);
}
.ensemble-summary-bar__info { display: grid; gap: 2px; min-width: 0; }
.ensemble-summary-bar__info strong { font-size: 12px; }
.ensemble-summary-bar__info span,
.ensemble-weights > span { color: var(--on-surface-muted); font-size: 12px; }
.model-pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 38px;
  margin-top: 4px;
  padding: 10px 4px 2px;
  border-top: 1px solid color-mix(in srgb, var(--outline) 72%, transparent);
  color: var(--on-surface-muted);
  font-size: 11px;
}
.model-pagination :deep(.n-pagination) { flex: 0 0 auto; }
.ensemble-config__fields { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px; }
.ensemble-config__fields label { display: grid; gap: 5px; }
.ensemble-config__fields label > span { color: var(--on-surface-muted); font-size: 12px; }
.ensemble-weights { display: grid; gap: 5px; max-height: 78px; overflow-y: auto; padding-right: 4px; }
.ensemble-weight { display: grid; grid-template-columns: minmax(0, 1fr) 120px; align-items: center; gap: 10px; }
.separate-note-modal,
.separate-workflow-meta-modal {
  width: min(520px, calc(100vw - 48px));
  border-radius: 18px;
}
.separate-note-modal__body,
.separate-workflow-meta-modal__body {
  display: grid;
  gap: 12px;
  min-width: 0;
}
.separate-note-modal__name {
  overflow: hidden;
  color: var(--on-surface-muted);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.separate-workflow-meta-modal__field {
  display: grid;
  gap: 6px;
  min-width: 0;
}
.separate-workflow-meta-modal__field > span {
  color: var(--on-surface-muted);
  font-size: 12px;
}
.separate-note-modal__body :deep(.n-input),
.separate-workflow-meta-modal__field :deep(.n-input) {
  width: 100%;
  max-width: 100%;
  box-sizing: border-box;
}
.separate-note-modal__footer,
.separate-workflow-meta-modal__footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.ensemble-modal { width: min(720px, 92vw); }
.ensemble-modal__intro { display: flex; justify-content: space-between; gap: 16px; margin-bottom: 16px; color: var(--on-surface-muted); font-size: 12px; }
.ensemble-modal__intro strong { color: var(--primary-strong); white-space: nowrap; }
.ensemble-modal__weights { display: grid; gap: 12px; max-height: 220px; overflow-y: auto; padding-right: 5px; }
.ensemble-modal__weight { display: grid; grid-template-columns: minmax(130px, 0.9fr) minmax(120px, 0.8fr) minmax(120px, 1fr) 96px; align-items: center; gap: 10px; }
.ensemble-modal__weight-input { width: 96px; }
.ensemble-modal__model-name {
  min-width: 0;
  overflow: hidden;
  color: var(--on-surface);
  font-size: 12px;
  font-weight: 600;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.output-directory-error { display: block; margin-top: 5px; color: var(--danger); font-size: 11px; line-height: 1.35; }

.target-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  /* Wraps into columns once there is room. A single column of very wide rows wastes the space
     and makes each entry harder to scan, since the name and its tags drift far apart. */
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  align-content: start;
  gap: 6px;
  padding: 2px;
  padding-right: 6px;
  scrollbar-width: thin;
  scrollbar-color: color-mix(in srgb, var(--outline) 120%, transparent) transparent;
}
.target-list--list {
  grid-template-columns: minmax(0, 1fr);
  gap: 5px;
}
.target-list::-webkit-scrollbar { width: 7px; }
.target-list::-webkit-scrollbar-thumb { border-radius: 999px; background: color-mix(in srgb, var(--outline) 130%, transparent); }
.target-list::-webkit-scrollbar-track { background: transparent; }

.target-row {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  padding: 11px 14px;
  border-radius: 12px;
  border: 0;
  text-align: left;
  cursor: pointer;
  background: color-mix(in srgb, var(--surface-2) 38%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 78%, transparent);
  transition: background 150ms ease, box-shadow 150ms ease;
  font-family: inherit;
  color: inherit;
}

.target-row:hover {
  background: color-mix(in srgb, var(--surface-2) 62%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary-border) 40%, transparent);
}

.target-row--active {
  background:
    linear-gradient(90deg, color-mix(in srgb, var(--primary-soft) 42%, transparent), color-mix(in srgb, var(--surface-2) 52%, transparent) 60%);
  box-shadow:
    inset 0 0 0 1.5px color-mix(in srgb, var(--primary) 54%, transparent),
    0 8px 22px color-mix(in srgb, var(--primary-glow) 18%, transparent);
}

.target-row--unavailable,
.target-row--unavailable:hover {
  cursor: not-allowed;
  opacity: 0.72;
  background: color-mix(in srgb, var(--surface-2) 34%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--danger) 24%, var(--outline));
}

.target-row__radio {
  flex: 0 0 auto;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  box-shadow: inset 0 0 0 1.5px color-mix(in srgb, var(--on-surface-muted) 60%, transparent);
  transition: box-shadow 150ms ease, background 150ms ease;
}
.target-row--active .target-row__radio {
  background: var(--primary);
  box-shadow:
    inset 0 0 0 1.5px var(--primary),
    inset 0 0 0 4px var(--surface-1);
}

.target-row__body {
  min-width: 0;
  flex: 1;
  display: grid;
  gap: 3px;
}

.target-row__heading {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.target-row__name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13.5px;
  font-weight: 600;
  letter-spacing: -0.01em;
}
.target-row--active .target-row__name { color: var(--primary-strong); }

.target-row__unavailable {
  flex: 0 0 auto;
  padding: 2px 7px;
  border-radius: 999px;
  color: var(--danger);
  background: color-mix(in srgb, var(--danger) 10%, var(--surface-2));
  font-size: 10px;
  font-weight: 650;
  line-height: 1.35;
}

.target-row__meta {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.target-row__tag {
  flex: 0 0 auto;
  max-width: 40%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 2px 9px;
  border-radius: 999px;
  font-size: 10px;
  color: color-mix(in srgb, var(--primary-strong) 74%, var(--on-surface-muted));
  background: color-mix(in srgb, var(--primary-soft) 26%, var(--surface-2));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary-border) 26%, transparent);
}

.target-row__desc {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  color: var(--on-surface-muted);
  line-height: 1.4;
}

.target-row__issue {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--danger);
  font-size: 10.5px;
  line-height: 1.4;
}

.target-row__usage {
  flex: 0 0 auto;
  font-size: 11px;
  color: var(--on-surface-muted);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

/* The user's own note. Distinguished from the generated meta line by the accent bar, so a row
   with a note reads as annotated rather than as having an extra field. */
.target-row__note {
  display: block;
  min-width: 0;
  margin-top: 3px;
  padding-left: 6px;
  border-left: 2px solid color-mix(in srgb, var(--primary) 55%, transparent);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  line-height: 1.4;
  color: color-mix(in srgb, var(--on-surface) 72%, transparent);
  text-align: left;
}

.target-list--list .target-row {
  min-height: 48px;
  padding: 8px 12px;
  border-radius: 8px;
}

.target-list--list .target-row__body {
  grid-template-columns: minmax(160px, 0.9fr) minmax(180px, 1.1fr);
  align-items: center;
  gap: 4px 14px;
}

.target-list--list .target-row__name { font-size: 13px; }

.target-list--list .target-row__meta { min-width: 0; }

.target-list--list .target-row__note {
  grid-column: 1 / -1;
  margin-top: 0;
}

.stage-empty {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  text-align: center;
  padding: 32px;
}

.stage-empty__glyph {
  width: 56px;
  height: 56px;
  display: grid;
  place-items: center;
  border-radius: 16px;
  font-size: 26px;
  color: var(--primary-strong);
  background: color-mix(in srgb, var(--primary-soft) 30%, var(--surface-2));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary-border) 26%, transparent);
  margin-bottom: 4px;
}

.stage-empty strong { font-size: 14px; }
.stage-empty p { margin: 0; font-size: 12px; color: var(--on-surface-muted); line-height: 1.5; max-width: 320px; }
.stage-empty .n-button { margin-top: 8px; }

.stage-alert {
  flex: 0 0 auto;
  padding: 11px 13px;
  border-radius: 12px;
  font-size: 12px;
  line-height: 1.5;
  color: color-mix(in srgb, var(--warning) 78%, var(--on-surface));
  background: color-mix(in srgb, var(--warning) 12%, var(--surface-2));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--warning) 34%, transparent);
}

/* stage: hero (running + terminal) */
.stage-hero {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.stage-hero__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.stage-badge {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 5px 12px 5px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: -0.01em;
}
.stage-badge .n-icon { font-size: 14px; }

.stage-badge--running {
  color: var(--primary-strong);
  background: var(--primary-soft);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary-border) 50%, transparent);
}
.stage-badge--done {
  color: color-mix(in srgb, var(--success) 84%, var(--on-surface));
  background: color-mix(in srgb, var(--success) 14%, var(--surface-2));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--success) 40%, transparent);
}
.stage-badge--failed {
  color: color-mix(in srgb, var(--danger) 84%, var(--on-surface));
  background: color-mix(in srgb, var(--danger) 14%, var(--surface-2));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--danger) 40%, transparent);
}
.stage-badge--cancelled {
  color: color-mix(in srgb, var(--warning) 84%, var(--on-surface));
  background: color-mix(in srgb, var(--warning) 14%, var(--surface-2));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--warning) 40%, transparent);
}

.stage-badge__dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--primary);
  box-shadow: 0 0 0 0 color-mix(in srgb, var(--primary) 60%, transparent);
  animation: pulse-dot 1.6s ease-out infinite;
}

@keyframes pulse-dot {
  0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--primary) 55%, transparent); }
  70% { box-shadow: 0 0 0 7px transparent; }
  100% { box-shadow: 0 0 0 0 transparent; }
}

.stage-hero__title h2 {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.03em;
  line-height: 1.15;
}

.stage-hero__title p {
  margin: 5px 0 0;
  font-size: 13px;
  color: var(--on-surface-muted);
  line-height: 1.45;
}

/* progress ring */
.progress-ring-block {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  align-items: center;
  gap: 28px;
  padding: 8px 0;
}

.progress-ring {
  --pct: 0;
  flex: 0 0 auto;
  width: 148px;
  height: 148px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background:
    radial-gradient(closest-side, var(--surface-1) 74%, transparent 75% 100%),
    conic-gradient(
      var(--primary) calc(var(--pct) * 1%),
      color-mix(in srgb, var(--outline) 130%, transparent) 0
    );
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 60%, transparent);
  transition: background 500ms cubic-bezier(0.22,1,0.36,1);
}

.progress-ring__center {
  display: flex;
  align-items: baseline;
  gap: 2px;
  color: var(--on-surface);
}
.progress-ring__center strong {
  font-size: 40px;
  font-weight: 600;
  letter-spacing: -0.04em;
  font-variant-numeric: tabular-nums;
}
.progress-ring__center span { font-size: 16px; color: var(--on-surface-muted); }

.progress-ring__meta {
  flex: 1 1 auto;
  min-width: 0;
  display: grid;
  gap: 10px;
  align-content: center;
}

.progress-meta-line {
  display: grid;
  gap: 2px;
}
.progress-meta-line__label { font-size: 13px; font-weight: 600; }
.progress-meta-line__detail { font-size: 12px; color: var(--on-surface-muted); }

.progress-submessage {
  margin: 0;
  font-size: 12px;
  color: var(--on-surface-muted);
  line-height: 1.5;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.progress-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
  padding-top: 4px;
  border-top: 1px solid color-mix(in srgb, var(--outline) 70%, transparent);
}
.progress-facts span {
  min-width: 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--on-surface-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.progress-facts .n-icon { flex: 0 0 auto; }

/* result path / note */
.stage-hero--result { justify-content: flex-start; gap: 14px; }

.result-path {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 12px 14px;
  border-radius: 13px;
  background: color-mix(in srgb, var(--surface-2) 40%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 80%, transparent);
}
.result-path .n-icon { flex: 0 0 auto; font-size: 16px; color: var(--primary-strong); }
.result-path code {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: color-mix(in srgb, var(--on-surface) 88%, var(--on-surface-muted));
}

.result-note {
  padding: 12px 14px;
  border-radius: 13px;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--on-surface-muted);
  background: color-mix(in srgb, var(--surface-2) 34%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 76%, transparent);
}

.result-preview-panel {
  flex: 0 1 auto;
  display: grid;
  gap: 8px;
  min-height: 0;
  max-height: 360px;
  padding: 12px 14px;
  overflow: hidden;
  border: 1px solid color-mix(in srgb, var(--outline) 80%, transparent);
  border-radius: 16px;
  background: color-mix(in srgb, var(--surface-2) 32%, transparent);
}

.result-preview-panel__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.result-preview-panel__head strong {
  font-size: 14px;
}

.result-preview-panel__head span,
.preview-track__title small,
.preview-track__time {
  color: var(--on-surface-muted);
  font-size: 11px;
}

.preview-output-groups {
  min-height: 0;
  display: grid;
  align-content: start;
  gap: 12px;
  overflow-y: auto;
  padding-right: 2px;
}

.preview-output-group {
  min-width: 0;
  display: grid;
  gap: 6px;
}

.preview-output-group__head {
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 4px;
}

.preview-output-group__head strong {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}

.preview-output-group__head span {
  flex: 0 0 auto;
  color: var(--on-surface-muted);
  font-size: 11px;
}

.preview-track-list {
  min-height: 0;
  display: grid;
  align-content: start;
  gap: 8px;
}

.preview-track {
  display: grid;
  grid-template-columns: minmax(160px, 0.9fr) auto minmax(180px, 1fr) auto;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid color-mix(in srgb, var(--outline) 78%, transparent);
  border-radius: 14px;
  background: color-mix(in srgb, var(--surface-2) 44%, transparent);
}

.preview-track__title {
  min-width: 0;
  display: grid;
  gap: 2px;
}

.preview-track__title strong,
.preview-track__title small {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.preview-track__title strong {
  font-size: 13px;
}

.preview-track__title small {
  width: fit-content;
  max-width: 100%;
  padding: 2px 6px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--primary-soft) 58%, transparent);
  color: var(--primary-strong);
  font-size: 10px;
  font-weight: 600;
}

.preview-track__slider {
  min-width: 0;
}

.preview-track__time {
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

/* stage action bar */
.stage-actions {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 10px;
  padding-top: 4px;
}
.stage-actions__primary { min-width: 150px; font-weight: 600; }
.stage-view--running .stage-actions { justify-content: flex-end; }


.log-console {
  max-height: min(62vh, 520px);
  overflow: auto;
  display: grid;
  gap: 2px;
  padding: 12px;
  border-radius: 12px;
  background: #0b1020;
  color: #c9d6ec;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.55;
  scrollbar-width: thin;
  scrollbar-color: color-mix(in srgb, #94a3b8 46%, transparent) transparent;
}

.log-console::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}

.log-console::-webkit-scrollbar-track {
  background: transparent;
}

.log-console::-webkit-scrollbar-thumb {
  border: 2px solid #0b1020;
  border-radius: 999px;
  background: color-mix(in srgb, #94a3b8 44%, transparent);
  background-clip: padding-box;
}

.log-console::-webkit-scrollbar-thumb:hover {
  background: color-mix(in srgb, #94a3b8 64%, transparent);
  background-clip: padding-box;
}

.log-console::-webkit-scrollbar-corner {
  background: transparent;
}

.log-line {
  display: grid;
  grid-template-columns: 38px minmax(0, 1fr);
  gap: 10px;
}

.log-line-number {
  color: #64748b;
  text-align: right;
  user-select: none;
}

.log-line-text {
  min-width: 0;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.log-empty {
  padding: 18px;
  color: var(--on-surface-muted);
  text-align: center;
}

.settings-drawer__content {
  display: grid;
  gap: 14px;
  min-height: 0;
  padding-top: 18px;
  padding-bottom: 8px;
}

.settings-modal {
  width: min(760px, calc(100vw - 48px));
  max-height: min(760px, calc(100vh - 64px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-radius: 20px;
}

.settings-modal :deep(.n-card-header) {
  flex: 0 0 auto;
  padding: 18px 22px 14px;
  border-bottom: 1px solid color-mix(in srgb, var(--outline) 86%, transparent);
  background: color-mix(in srgb, var(--surface-1) 96%, transparent);
}

.settings-modal :deep(.n-card-header__main) {
  font-size: 15px;
  font-weight: 600;
}

.settings-modal :deep(.n-card-content),
.settings-modal :deep(.n-card__content) {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 0 22px 20px;
}

.settings-modal :deep(.n-card-footer),
.settings-modal :deep(.n-card__footer) {
  flex: 0 0 auto;
  padding: 14px 22px 16px;
  border-top: 1px solid color-mix(in srgb, var(--outline) 86%, transparent);
  background: color-mix(in srgb, var(--surface-1) 96%, transparent);
}

.naming-modal {
  max-height: min(620px, calc(100vh - 72px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-radius: 16px;
}

.naming-modal :deep(.n-card-header) {
  padding: 16px 20px 10px;
}

.naming-modal :deep(.n-card-footer),
.naming-modal :deep(.n-card__footer) {
  padding: 10px 20px 14px;
}

.naming-modal :deep(.n-card-content),
.naming-modal :deep(.n-card__content) {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 8px 20px 12px;
}

.naming-modal__content {
  display: grid;
  gap: 10px;
}

.naming-section {
  display: grid;
  gap: 8px;
  padding: 11px 12px;
  border-radius: 12px;
  background: color-mix(in srgb, var(--surface-2) 56%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 82%, transparent);
}

.naming-section__head {
  display: grid;
  gap: 3px;
}

.naming-section--muted {
  color: var(--on-surface-muted);
  font-size: 12px;
  line-height: 1.45;
}

.naming-section strong,
.naming-section__head strong {
  font-size: 12.5px;
}

.naming-section span,
.naming-section__head span {
  color: var(--on-surface-muted);
  font-size: 11.5px;
  line-height: 1.35;
}

.token-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}

.token-chip {
  flex: 0 0 auto;
  display: inline-grid;
  grid-template-columns: auto auto;
  align-items: center;
  gap: 6px;
  border: 0;
  border-radius: 8px;
  padding: 5px 5px 5px 8px;
  background: color-mix(in srgb, var(--surface) 68%, var(--surface-1));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 78%, transparent);
  color: var(--on-surface);
  font-family: inherit;
  cursor: pointer;
  text-align: left;
}

.token-chip code {
  padding: 1px 5px;
  border-radius: 5px;
  font-family: inherit;
  font-size: 10px;
  font-weight: 600;
  color: color-mix(in srgb, var(--primary-strong) 82%, var(--on-surface-muted));
  background: color-mix(in srgb, var(--primary-soft) 34%, transparent);
}

.token-chip span {
  font-size: 12px;
  font-weight: 600;
  color: var(--on-surface);
}

.stem-order-list,
.naming-preview-list {
  display: grid;
  gap: 6px;
}

.naming-preview-inline {
  display: grid;
  gap: 6px;
}

.naming-preview-inline > span {
  color: var(--on-surface-muted);
  font-size: 11px;
}

.naming-preview-file {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 8px 10px;
  border-radius: 9px;
  background: color-mix(in srgb, var(--surface) 68%, var(--surface-1));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 78%, transparent);
  font-family: inherit;
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
  color: color-mix(in srgb, var(--on-surface) 86%, var(--on-surface-muted));
}

.naming-preview-part {
  border-radius: 5px;
  padding: 0 2px;
}

.naming-preview-part--literal,
.naming-preview-part--extension {
  color: color-mix(in srgb, var(--on-surface-muted) 88%, var(--on-surface));
}

.naming-preview-part--index,
.naming-preview-part--inputnumber {
  color: color-mix(in srgb, #2f6fed 78%, var(--on-surface));
  background: color-mix(in srgb, #2f6fed 10%, transparent);
}

.naming-preview-part--filename {
  color: color-mix(in srgb, #0f8a6b 78%, var(--on-surface));
  background: color-mix(in srgb, #0f8a6b 10%, transparent);
}

.naming-preview-part--stem {
  color: color-mix(in srgb, #9a5a00 78%, var(--on-surface));
  background: color-mix(in srgb, #d08400 12%, transparent);
}

.naming-preview-part--model {
  color: color-mix(in srgb, #7a4ed8 78%, var(--on-surface));
  background: color-mix(in srgb, #7a4ed8 10%, transparent);
}

.naming-preview-part--yyyyMMdd,
.naming-preview-part--hhmmss,
.naming-preview-part--ddmmss {
  color: color-mix(in srgb, #b54274 78%, var(--on-surface));
  background: color-mix(in srgb, #b54274 10%, transparent);
}

.stem-order-row {
  display: grid;
  grid-template-columns: auto 30px minmax(0, 1fr) auto;
  align-items: center;
  gap: 9px;
  padding: 7px 9px;
  border-radius: 9px;
  background: color-mix(in srgb, var(--surface) 68%, var(--surface-1));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 78%, transparent);
  cursor: grab;
  user-select: none;
  touch-action: none;
}

.stem-order-row--dragging { opacity: 0.55; }

.stem-order-row .n-icon { color: var(--on-surface-muted); }
.stem-order-row span { font-variant-numeric: tabular-nums; color: var(--primary-strong); }
.stem-order-row strong { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.stem-order-row__actions {
  display: inline-flex;
  align-items: center;
  gap: 2px;
}

.naming-preview-list code,
.naming-preview-inline code {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 8px 10px;
  border-radius: 9px;
  background: color-mix(in srgb, var(--surface) 68%, var(--surface-1));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 78%, transparent);
  font-family: inherit;
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
  color: color-mix(in srgb, var(--on-surface) 86%, var(--on-surface-muted));
}

.field-block {
  display: grid;
  gap: 6px;
}

.field-block label {
  font-size: 12px;
  color: var(--on-surface-muted);
}

.field-block--inline-check {
  min-height: 56px;
  align-content: end;
  padding-bottom: 7px;
}

.summary-static-value {
  min-height: 34px;
  display: flex;
  align-items: center;
  padding: 0 12px;
  border: 1px solid color-mix(in srgb, var(--outline) 46%, transparent);
  border-radius: 12px;
  background: color-mix(in srgb, var(--surface-2) 64%, transparent);
  color: var(--on-surface);
  font-size: 13px;
  font-weight: 700;
}

.field-block__hint {
  font-size: 11px;
  line-height: 1.45;
  color: var(--on-surface-muted);
}

.output-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 180px;
  gap: 12px;
}

.settings-group {
  display: grid;
  gap: 14px;
  padding: 16px;
  border: 1px solid var(--outline);
  border-radius: 16px;
  background: color-mix(in srgb, var(--surface-2) 62%, transparent);
}

.settings-group__head {
  display: grid;
  gap: 3px;
}

.settings-group__head strong {
  font-size: 13px;
}

.settings-group__head span {
  color: var(--on-surface-muted);
  font-size: 12px;
  line-height: 1.5;
}

.settings-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px;
  border: 1px solid var(--outline);
  border-radius: 12px;
  background: var(--surface-1);
}

.settings-row__copy {
  min-width: 0;
  display: grid;
  gap: 3px;
}

.settings-row__copy strong {
  font-size: 13px;
}

.settings-row__copy span {
  color: var(--on-surface-muted);
  font-size: 12px;
  line-height: 1.5;
}

.output-preview {
  display: grid;
  gap: 4px;
  padding: 12px 14px;
  border: 1px solid var(--outline);
  border-radius: 12px;
  background: var(--surface-1);
}

.output-preview span {
  color: var(--on-surface-muted);
  font-size: 12px;
}

.output-preview code {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}

.advanced-hint {
  margin: 0 0 12px;
  color: var(--on-surface-muted);
  font-size: 12px;
  line-height: 1.5;
}

.advanced-loading,
.advanced-empty {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 12px;
  color: var(--on-surface-muted);
  font-size: 12px;
  line-height: 1.5;
}

.advanced-empty {
  margin-top: 14px;
}

.check-list {
  display: flex;
  flex-wrap: wrap;
  gap: 14px 18px;
}

.check-list--spaced {
  margin-top: 14px;
}

.drawer-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

/* ============ Responsive ============ */
@media (max-width: 1180px) {
  .console {
    grid-template-columns: minmax(380px, 440px) minmax(0, 1fr);
  }
}

@media (max-width: 1000px) {
  .separate-page {
    height: auto;
  }
  .console {
    grid-template-columns: minmax(0, 1fr);
  }
  .console__rail {
    grid-template-rows: none;
    grid-auto-rows: auto;
  }
  .dropzone { min-height: 220px; }
  .stage-view { min-height: 440px; }
  .progress-ring-block { flex-direction: column; align-items: flex-start; gap: 18px; }
}

@media (max-width: 640px) {
  .stage-head { align-items: flex-start; flex-direction: column; }
  .stage-head__extra { width: 100%; }
  .model-mode-control { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .model-mode-control__tab { min-width: 0; }
  .console-topbar {
    flex-direction: column;
    align-items: stretch;
    gap: 12px;
  }
  .console-topbar__controls {
    justify-content: space-between;
  }
  .ofield-row {
    grid-template-columns: minmax(0, 1fr);
  }
  .naming-grid,
  .token-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .launch-bar {
    flex-direction: column;
    align-items: stretch;
    gap: 10px;
  }
  .launch-bar__actions { justify-content: flex-end; }
  .launch-bar__go { flex: 1 1 auto; }
  .picker-buttons {
    grid-template-columns: 1fr;
  }
  .target-toolbar {
    grid-template-columns: minmax(0, 1fr);
  }
  .target-toolbar__view { justify-self: start; }
  .target-list--list .target-row__body {
    grid-template-columns: minmax(0, 1fr);
    gap: 3px;
  }
  .model-mode-control {
    grid-template-columns: minmax(0, 1fr);
  }
  .ensemble-config__fields {
    grid-template-columns: minmax(0, 1fr);
  }
  .ensemble-summary-bar { grid-template-columns: minmax(0, 1fr); }
  .model-pagination { align-items: flex-start; flex-direction: column; }
  .ensemble-modal__weight { grid-template-columns: minmax(0, 1fr) 96px; }
  .ensemble-modal__weight .n-select { grid-column: 1 / -1; }
  .ensemble-modal__weight .n-slider { grid-column: 1 / -1; }
  .stage-actions { justify-content: stretch; }
  .stage-actions .n-button { flex: 1 1 160px; }
  .preview-track { grid-template-columns: minmax(0, 1fr) auto; }
  .preview-track__slider,
  .preview-track__time { grid-column: 1 / -1; }
  .progress-ring { width: 120px; height: 120px; }
  .progress-ring__center strong { font-size: 32px; }
  .settings-modal {
    width: calc(100vw - 28px);
    max-height: calc(100vh - 40px);
  }
  .settings-drawer__content,
  .settings-modal :deep(.n-card-content),
  .settings-modal :deep(.n-card__content) {
    padding-left: 16px;
    padding-right: 16px;
  }
  .settings-modal :deep(.n-card-header),
  .settings-modal :deep(.n-card-footer),
  .settings-modal :deep(.n-card__footer) {
    padding-left: 16px;
    padding-right: 16px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .stage-swap-enter-active,
  .stage-swap-leave-active {
    transition: none;
  }
  .stage-badge__dot { animation: none; }
  .progress-ring { transition: none; }
  .console-topbar__brand:hover :deep(.app-brand-mark) {
    animation: none;
  }
}
</style>
