<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useI18n } from 'vue-i18n'
import { useMessage, useDialog, type DropdownOption } from 'naive-ui'
import { invoke } from '@tauri-apps/api/core'
import {
  SearchOutline,
  DownloadOutline,
  CheckmarkCircleOutline,
  CloudDownloadOutline,
  RefreshOutline,
  CubeOutline,
  TrashOutline,
  FolderOpenOutline,
  ServerOutline,
  Star,
  StarOutline,
  AddCircleOutline,
  EllipsisHorizontalOutline,
  LinkOutline,
  GridOutline,
  ListOutline,
} from '@vicons/ionicons5'
import {
  useModelStore,
  type ModelDefaultInferenceParams,
  type ModelEntry,
  type ModelStorageDeleteTarget,
  type ToolModelStorageItem,
} from '@/stores/model'
import { useSettingsStore } from '@/stores/settings'
import { useTaskStore } from '@/stores/task'
import { useAppStore } from '@/stores/app'
import { formatBytes, formatSpeedMBps } from '@/utils/format'
import { buildModelCategoryOptionsFromPairs, getModelCategoryLabel } from '@/utils/modelCategory'
import { MODEL_LIBRARY_PAGE_SIZES } from '@/utils/pagination'
import { inferenceChunkSizeIssue, resolveInferenceChunkStep, resolveInferenceSampleStep } from '@/features/inference/sampleStep'
import ModelProgressBlock from '@/components/ModelProgressBlock.vue'
import DownloadDetailModal from '@/components/DownloadDetailModal.vue'
import CustomModelImportDialog from '@/components/CustomModelImportDialog.vue'
import AlignedInferenceInputNumber from '@/components/AlignedInferenceInputNumber.vue'

const { t, locale } = useI18n()
const message = useMessage()
const dialog = useDialog()
const modelStore = useModelStore()
const settings = useSettingsStore()
const taskStore = useTaskStore()
const app = useAppStore()
const {
  filteredModels,
  selectedInfo,
  selectedModel,
  search,
  category,
  categories,
  categoriesCn,
  isLoading,
  detailLoading,
  modelDir,
  debugStatus,
  modelSource,
  modelViewMode,
  modelPageSize: pageSize,
  customModelCount,
  debugModelCount,
  downloadTasks,
  deleteTasks,
  modelStorageSummary,
  storageLoading,
  modelPreferences,
  batchDeleteState,
  residualCleanupState,
} = storeToRefs(modelStore)

const showDetail = ref(false)
const showCustomImport = ref(false)
const showDownloadDetail = ref(false)
const downloadDetailModel = ref<string>('')
const downloadedOnly = ref(false)
const page = ref(1)
const pageSizeOptions = [...MODEL_LIBRARY_PAGE_SIZES]
type ModelSort = 'default' | 'favorite' | 'name-asc' | 'name-desc' | 'size-desc' | 'size-asc' | 'category' | 'type' | 'downloaded'
const modelSort = ref<ModelSort>('default')
const showStorage = ref(false)
const storageSearch = ref('')
const storageSort = ref<'size-desc' | 'size-asc' | 'name-asc' | 'name-desc'>('size-desc')
const storageDownloadedOnly = ref(true)
const selectedStorageModels = ref<string[]>([])
const inferenceDraft = ref<Required<Pick<ModelDefaultInferenceParams, 'batch_size' | 'overlap_size' | 'chunk_size'>>>({
  batch_size: 1,
  overlap_size: 0,
  chunk_size: 0,
})
const contextMenuX = ref(0)
const contextMenuY = ref(0)
const contextMenuVisible = ref(false)
const contextModel = ref<ModelEntry | null>(null)
const showNoteEditor = ref(false)
const noteEditorModel = ref<ModelEntry | null>(null)
const noteDraft = ref('')
const showInferenceEditor = ref(false)
const inferenceEditorModel = ref<ModelEntry | null>(null)
const inferenceEditorMeta = computed(() => modelStore.models.find(item => item.name === inferenceEditorModel.value?.name)?.inferenceParamMeta
  || inferenceEditorModel.value?.inferenceParamMeta)
const inferenceEditorSampleStep = computed(() => inferenceEditorMeta.value?.recommendedSampleStep)
const inferenceEditorOverlapStep = computed(() => resolveInferenceSampleStep(inferenceEditorMeta.value, 1))
const inferenceEditorChunkStep = computed(() => resolveInferenceChunkStep(inferenceEditorMeta.value, 1024))
const inferenceEditorChunkConstraint = computed(() => inferenceEditorMeta.value?.chunkSizeConstraint)
const inferenceEditorChunkIssue = computed(() => inferenceChunkSizeIssue(inferenceDraft.value.chunk_size, inferenceEditorMeta.value))

function updateInferenceDraftSize(key: 'overlap_size' | 'chunk_size', value: number | null) {
  inferenceDraft.value[key] = value as number
}

const categoryOptions = computed(() => {
  return buildModelCategoryOptionsFromPairs(categories.value, categoriesCn.value, locale.value, t('common.all'))
})

// The imported count is shown on the option itself: it is the quickest answer to "did my import
// land?", and it keeps the option from looking broken when there is nothing to show yet.
const modelSourceOptions = computed(() => [
  { label: t('models.sourceAll'), value: 'all' },
  { label: t('models.sourceCatalog'), value: 'catalog' },
  { label: t('models.sourceDebug', { count: debugModelCount.value }), value: 'debug' },
  { label: t('models.sourceUser', { count: customModelCount.value }), value: 'user' },
])

const debugStatusText = computed(() => {
  const status = debugStatus.value
  if (!status?.active) return ''
  if (status.addedCount || status.changedCount || status.removedCount) return t('models.debugCatalogActive')
  return ''
})

const modelSortOptions = computed(() => [
  { label: t('models.sortDefault'), value: 'default' },
  { label: t('models.sortFavorite'), value: 'favorite' },
  { label: t('models.sortNameAsc'), value: 'name-asc' },
  { label: t('models.sortNameDesc'), value: 'name-desc' },
  { label: t('models.sortSizeDesc'), value: 'size-desc' },
  { label: t('models.sortSizeAsc'), value: 'size-asc' },
  { label: t('models.sortCategory'), value: 'category' },
  { label: t('models.sortType'), value: 'type' },
  { label: t('models.sortDownloaded'), value: 'downloaded' },
])

/** Whether anything is narrowing the list — decides "no matches" vs "nothing loaded yet". */
const hasActiveFilter = computed(() =>
  Boolean(search.value || category.value || downloadedOnly.value || modelSource.value !== 'all'))

const collator = computed(() => new Intl.Collator(locale.value === 'zh-CN' ? 'zh-CN' : 'en', { numeric: true, sensitivity: 'base' }))

function compareModelName(a: ModelEntry, b: ModelEntry) {
  return collator.value.compare(a.name, b.name)
}

function isFavoriteModel(model: ModelEntry | null | undefined) {
  return Boolean(model?.name && modelPreferences.value[model.name]?.favorite)
}

function modelNote(modelOrName: ModelEntry | string | null | undefined) {
  const name = typeof modelOrName === 'string' ? modelOrName : modelOrName?.name
  return name ? modelPreferences.value[name]?.note || '' : ''
}

function modelUseCount(modelOrName: ModelEntry | string | null | undefined) {
  const name = typeof modelOrName === 'string' ? modelOrName : modelOrName?.name
  return name ? modelStore.getModelUseCount(name) : 0
}

function toggleFavorite(model: ModelEntry, event?: MouseEvent) {
  event?.stopPropagation()
  modelStore.toggleModelFavorite(model.name)
}

const sortedModels = computed(() => {
  const list = downloadedOnly.value
    ? filteredModels.value.filter((m) => m.downloaded)
    : filteredModels.value
  const sort = modelSort.value
  // 收藏置顶仅在默认/收藏排序下生效，其余排序纯按维度排列
  const pinFavorite = sort === 'default' || sort === 'favorite'
  // 预计算排序键，避免比较器内重复调用 isFavoriteModel/categoryLabel（O(n log n) 放大）
  const decorated = list.map((model) => ({
    model,
    favorite: isFavoriteModel(model),
    categoryKey: sort === 'category' ? categoryLabel(model) : '',
    typeKey: sort === 'type' ? String(model.modelType || '') : '',
  }))
  decorated.sort((a, b) => {
    if (pinFavorite) {
      const favoriteDelta = Number(b.favorite) - Number(a.favorite)
      if (favoriteDelta) return favoriteDelta
    }
    switch (sort) {
      case 'name-desc':
        return compareModelName(b.model, a.model)
      case 'size-desc':
        return b.model.sizeBytes - a.model.sizeBytes || compareModelName(a.model, b.model)
      case 'size-asc':
        return a.model.sizeBytes - b.model.sizeBytes || compareModelName(a.model, b.model)
      case 'category':
        return collator.value.compare(a.categoryKey, b.categoryKey) || compareModelName(a.model, b.model)
      case 'type':
        return collator.value.compare(a.typeKey, b.typeKey) || compareModelName(a.model, b.model)
      case 'downloaded':
        if (a.model.downloaded !== b.model.downloaded) return a.model.downloaded ? -1 : 1
        return compareModelName(a.model, b.model)
      case 'favorite':
      case 'name-asc':
        return compareModelName(a.model, b.model)
      default:
        if (a.model.downloaded !== b.model.downloaded) return a.model.downloaded ? -1 : 1
        return compareModelName(a.model, b.model)
    }
  })
  return decorated.map((d) => d.model)
})
const pagedModels = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return sortedModels.value.slice(start, start + pageSize.value)
})

type StorageListItem = ModelStorageDeleteTarget & {
  selectionKey: string
  downloaded: boolean
  sizeBytes: number
  expectedSizeBytes: number
  fileCount: number
  role?: ToolModelStorageItem['role']
}

const allStorageModels = computed<StorageListItem[]>(() => {
  const catalogModels: StorageListItem[] = (modelStorageSummary.value?.models || []).map(item => ({
    kind: 'catalog',
    id: item.name,
    name: item.name,
    selectionKey: `catalog:${item.name}`,
    downloaded: item.downloaded,
    sizeBytes: item.sizeBytes,
    expectedSizeBytes: item.expectedSizeBytes,
    fileCount: item.files.filter(file => file.exists).length,
  }))
  const toolModels: StorageListItem[] = (modelStorageSummary.value?.toolModels || []).map(item => ({
    kind: 'tool',
    id: item.id,
    name: item.name,
    tool: item.tool,
    role: item.role,
    selectionKey: `tool:${item.tool}:${item.id}`,
    downloaded: true,
    sizeBytes: item.sizeBytes,
    expectedSizeBytes: item.sizeBytes,
    fileCount: item.fileCount,
  }))
  return [...catalogModels, ...toolModels]
})

const storageModels = computed<StorageListItem[]>(() => {
  const q = storageSearch.value.trim().toLowerCase()
  return [...allStorageModels.value]
    .filter((item) => (!storageDownloadedOnly.value || item.downloaded)
      && (!q || item.name.toLowerCase().includes(q) || item.tool?.includes(q)))
    .sort((a, b) => {
      if (storageSort.value === 'size-desc') return b.sizeBytes - a.sizeBytes
      if (storageSort.value === 'size-asc') return a.sizeBytes - b.sizeBytes
      if (storageSort.value === 'name-desc') return collator.value.compare(b.name, a.name)
      return collator.value.compare(a.name, b.name)
    })
})

const allStorageModelsByKey = computed(() => new Map(allStorageModels.value.map(item => [item.selectionKey, item])))

const selectedStorageBytes = computed(() => selectedStorageModels.value
  .map(key => allStorageModelsByKey.value.get(key))
  .filter((item): item is NonNullable<typeof item> => Boolean(item))
  .reduce((sum, item) => sum + item.sizeBytes, 0))

const hasDeletingSelectedStorageModels = computed(() => selectedStorageModels.value.some((key) => {
  const item = allStorageModelsByKey.value.get(key)
  return item?.kind === 'catalog' && isDeletingModel(item.id)
}))

const batchDeleteProgress = computed(() => {
  if (!batchDeleteState.value.totalModels) return 0
  const currentTask = batchDeleteState.value.currentModel ? deleteTasks.value[batchDeleteState.value.currentModel] : null
  const currentFraction = currentTask && currentTask.totalFiles > 0
    ? currentTask.completedFiles / currentTask.totalFiles
    : 0
  return Math.max(0, Math.min(100, Math.round(((batchDeleteState.value.completedModels + currentFraction) / batchDeleteState.value.totalModels) * 100)))
})

function setStorageSelected(name: string, checked: boolean) {
  selectedStorageModels.value = checked
    ? Array.from(new Set([...selectedStorageModels.value, name]))
    : selectedStorageModels.value.filter((item) => item !== name)
}

watch([search, category, modelSource, downloadedOnly, pageSize, modelSort], () => {
  page.value = 1
})

watch(sortedModels, (list) => {
  const maxPage = Math.max(1, Math.ceil(list.length / pageSize.value))
  if (page.value > maxPage) page.value = maxPage
})

function categoryLabel(model: ModelEntry) {
  return getModelCategoryLabel(model, locale.value, '—')
}

function targetStemLabel(model: ModelEntry | null | undefined) {
  const direct = String(model?.targetStem || '').trim()
  if (direct) return direct
  const configTarget = String(model?.configTargetInstrument || '').trim()
  if (configTarget) return configTarget
  return String(model?.configInstruments || '')
    .split('|')
    .map((item) => item.trim())
    .filter(Boolean)
    .join(' / ')
}

function normalizedTagValue(value: string | null | undefined) {
  return String(value || '').trim().toLowerCase()
}

function showModelTypeTag(model: ModelEntry) {
  if (!model.modelType) return false
  return normalizedTagValue(model.modelType) !== normalizedTagValue(model.architecture)
}

function hasRequiredCompanionFiles(model: ModelEntry) {
  return Boolean(model.configPath || (model.auxiliaryPaths && model.auxiliaryPaths.length > 0))
}

function requiredModelFileCount(model: ModelEntry) {
  return 1 + (model.configPath ? 1 : 0) + (model.auxiliaryPaths?.length || 0)
}

function isPartiallyAvailable(model: ModelEntry) {
  if (model.downloaded || !hasRequiredCompanionFiles(model) || !Array.isArray(model.missingPaths)) return false
  const missingCount = model.missingPaths.length
  const totalCount = requiredModelFileCount(model)
  return missingCount > 0 && missingCount < totalCount
}

function modelStatusLabel(model: ModelEntry) {
  if (!model.supported) return t('models.unsupported')
  if (model.downloaded) return t('models.downloaded')
  if (isPartiallyAvailable(model)) return t('models.partial')
  return t('models.notDownloaded')
}

function modelStatusType(model: ModelEntry) {
  if (!model.supported) return 'warning'
  if (model.downloaded) return 'success'
  if (isPartiallyAvailable(model)) return 'info'
  return 'default'
}

function downloadStatusMessage(modelName: string) {
  const task = downloadTasks.value[modelName]
  if (!task) return ''
  if (task.status === 'preparing') return t('models.downloadPreparing')
  if (task.status === 'interrupted') return t('models.downloadInterrupted')
  return task.message || task.status
}

function cardDownloadMessage(modelName: string) {
  const task = downloadTasks.value[modelName]
  if (!task) return ''
  switch (task.status) {
    case 'preparing':
      return t('models.cardDownloadPreparing')
    case 'downloading':
      return task.totalFiles > 1
        ? t('models.cardDownloadingFiles', { completed: task.completedFiles, total: task.totalFiles })
        : t('models.cardDownloading')
    case 'error':
      return t('models.cardDownloadError')
    case 'paused':
      return t('models.cardDownloadPaused')
    case 'cancelled':
      return t('models.cardDownloadCancelled')
    case 'interrupted':
      return t('models.cardDownloadInterrupted')
    case 'done':
      return t('models.downloaded')
    default:
      return task.status
  }
}

function downloadProgressColor(status: string) {
  if (status === 'error') return 'var(--danger)'
  if (status === 'interrupted') return 'var(--warning)'
  return 'var(--primary)'
}

function deleteStatusMessage(modelName: string) {
  const task = deleteTasks.value[modelName]
  if (!task) return ''
  return task.message || t('models.deleting')
}

function isDeletingModel(modelName: string) {
  return deleteTasks.value[modelName]?.status === 'deleting'
}

/**
 * Whether the card is showing a progress block rather than an action button.
 *
 * The footer then needs the full width of the card: a progress bar squeezed into the narrow
 * action column is unreadable, and its status line has nowhere to go.
 */
/**
 * The percentage to draw along the card's bottom edge, or null when nothing is running.
 *
 * Rendering progress as the card border rather than as a block inside it costs no height, which
 * is what stops a downloading card from towering over its neighbours and leaving the grid ragged.
 */
/** Current rate, or '' when the download is not moving (paused, queued, finished). */
function downloadSpeed(modelName: string) {
  const task = downloadTasks.value[modelName]
  if (!task || task.status !== 'downloading') return ''
  return formatSpeedMBps(task.speedBytesPerSecond)
}

/**
 * The size cell. While downloading it becomes "done / total", which is the same number the
 * percentage refers to and answers "how much is left" without a second row.
 */
function sizeLabel(model: ModelEntry) {
  const task = downloadTasks.value[model.name]
  if (task && task.status === 'downloading' && task.downloadedBytes && task.totalBytes) {
    return `${formatBytes(task.downloadedBytes)} / ${formatBytes(task.totalBytes)}`
  }
  return formatBytes(model.sizeBytes)
}

function cardProgressPercent(model: ModelEntry): number | null {
  if (isDeletingModel(model.name)) return deleteTasks.value[model.name]?.progress || 0
  const task = downloadTasks.value[model.name]
  if (task && task.status !== 'done') return task.progress || 0
  return null
}

function cardProgressState(model: ModelEntry) {
  if (isDeletingModel(model.name)) return 'downloading'
  return downloadTasks.value[model.name]?.status || 'downloading'
}

function isModelBusy(model: ModelEntry) {
  if (isDeletingModel(model.name)) return true
  const task = downloadTasks.value[model.name]
  return Boolean(task && task.status !== 'done')
}

const modelMenuOptions = computed<DropdownOption[]>(() => {
  const model = contextModel.value
  if (!model) return []

  const options: DropdownOption[] = [
    { key: 'detail', label: t('models.viewDetail') },
    { key: 'note', label: t('models.editNote') },
    { key: 'inference', label: t('models.editInferenceDefaults') },
    {
      key: 'open-directory',
      label: t('models.openModelDir'),
      disabled: !model.downloaded,
    },
    {
      key: 'favorite',
      label: isFavoriteModel(model) ? t('models.unfavorite') : t('models.favorite'),
    },
  ]
  const task = downloadTasks.value[model.name]

  if (isCustomModel(model)) {
    options.push(
      { type: 'divider', key: 'model-actions-divider' },
      { key: 'relink', label: t('models.customRelink') },
      { key: 'remove-custom', label: t('models.customRemoveConfirm') },
    )
  } else if (task && task.status !== 'done') {
    options.push(
      { type: 'divider', key: 'model-actions-divider' },
      { key: 'download-detail', label: t('models.downloadDetail') },
    )
    if (['preparing', 'downloading'].includes(task.status)) {
      options.push({ key: 'cancel-download', label: t('common.cancel') })
    } else if (task.status !== 'preparing' && task.status !== 'downloading') {
      options.push({
        key: 'resume-download',
        label: task.status === 'interrupted' ? t('models.continueDownload') : t('common.resume'),
      })
      if (['paused', 'cancelled', 'error', 'interrupted'].includes(task.status)) {
        options.push({ key: 'delete', label: t('models.delete') })
      }
    }
  } else if (model.downloaded) {
    options.push(
      { type: 'divider', key: 'model-actions-divider' },
      { key: 'delete', label: t('models.delete') },
    )
  } else if (model.supported) {
    options.push(
      { type: 'divider', key: 'model-actions-divider' },
      { key: 'download', label: t('common.download') },
    )
  }

  return options
})

function inferenceValue(model: ModelEntry | null | undefined, key: keyof typeof inferenceDraft.value, fallback: number) {
  const value = model?.defaultInferenceParams?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function resolveInferenceDraft(model: ModelEntry | null | undefined) {
  return {
    batch_size: inferenceValue(model, 'batch_size', 1),
    overlap_size: inferenceValue(model, 'overlap_size', 0),
    chunk_size: inferenceValue(model, 'chunk_size', 0),
  }
}

function syncInferenceDraft(model: ModelEntry | null | undefined) {
  inferenceDraft.value = resolveInferenceDraft(model)
}

function hasInferenceOverride(model: ModelEntry | null | undefined) {
  return Boolean(model?.name && modelStore.getModelInferenceOverrides(model.name))
}

function hasInferenceDraftChanges(model: ModelEntry | null | undefined) {
  const defaults = resolveInferenceDraft(model)
  return inferenceDraft.value.batch_size !== defaults.batch_size
    || inferenceDraft.value.overlap_size !== defaults.overlap_size
    || inferenceDraft.value.chunk_size !== defaults.chunk_size
}

function canResetInferenceDefaults(model: ModelEntry | null | undefined) {
  return hasInferenceOverride(model) || hasInferenceDraftChanges(model)
}

async function saveInferenceDefaults() {
  const model = inferenceEditorModel.value
  if (!model) return
  if (inferenceEditorChunkIssue.value) {
    message.warning(t('inference.invalidChunkSize', inferenceEditorChunkIssue.value))
    return
  }
  try {
    await modelStore.setModelInferenceOverrides(model.name, inferenceDraft.value)
    if (selectedModel.value === model.name) {
      const selected = modelStore.selectedInfo?.name === model.name ? modelStore.selectedInfo : model
      taskStore.applySelectedModelDefaults(
        modelStore.getModelBaseInferenceDefaults(model.name) || selected.defaultInferenceParams,
        selected.modelType,
        taskStore.getSavedModelState(model.name),
        modelStore.getModelInferenceOverrides(model.name),
        { force: true },
      )
    }
    closeInferenceEditor()
    message.success(t('models.inferenceDefaultsSaved'))
  } catch (error) {
    console.error('Failed to persist model inference defaults', error)
    message.error(t('models.inferenceDefaultsSaveFailed'))
  }
}

function storageModelTypeLabel(item: StorageListItem) {
  if (item.kind === 'catalog') return item.downloaded ? t('models.downloaded') : t('models.notDownloaded')
  if (item.role === 'vad') return t('models.storageAsrVadModel')
  if (item.role === 'punctuation') return t('models.storageAsrPunctuationModel')
  return t('models.storageAsrRecognitionModel')
}

async function resetInferenceDefaults() {
  const model = inferenceEditorModel.value
  if (!model) return
  try {
    if (hasInferenceOverride(model)) {
      await modelStore.resetModelInferenceOverrides(model.name)
    }
    const latest = modelStore.models.find((item) => item.name === model.name) || model
    syncInferenceDraft(latest)
    if (selectedModel.value === model.name) {
      const selected = modelStore.selectedInfo?.name === model.name ? modelStore.selectedInfo : latest
      taskStore.applySelectedModelDefaults(
        modelStore.getModelBaseInferenceDefaults(model.name) || selected.defaultInferenceParams,
        selected.modelType,
        taskStore.getSavedModelState(model.name),
        modelStore.getModelInferenceOverrides(model.name),
        { force: true },
      )
    }
    message.success(t('models.inferenceDefaultsReset'))
  } catch (error) {
    console.error('Failed to restore model inference defaults', error)
    message.error(t('models.inferenceDefaultsResetFailed'))
  }
}

async function loadModels() {
  try {
    await modelStore.loadModels()
  } catch {
    message.error(modelStore.error || t('models.empty'))
  }
}

function selectModel(model: ModelEntry) {
  showDetail.value = true
  void modelStore.selectModel(model).catch((err) => {
    message.error(err instanceof Error ? err.message : String(err))
  })
}

function openModelContextMenu(event: MouseEvent, model: ModelEntry) {
  contextModel.value = model
  contextMenuVisible.value = false
  contextMenuX.value = event.clientX
  contextMenuY.value = event.clientY
  window.requestAnimationFrame(() => {
    contextMenuVisible.value = true
  })
}

function closeModelContextMenu() {
  contextMenuVisible.value = false
  contextModel.value = null
}

function openModelNoteEditor(model: ModelEntry) {
  noteEditorModel.value = model
  noteDraft.value = modelNote(model)
  showNoteEditor.value = true
}

function closeModelNoteEditor() {
  showNoteEditor.value = false
  noteEditorModel.value = null
}

function handleNoteEditorUpdate(value: boolean) {
  showNoteEditor.value = value
  if (!value) noteEditorModel.value = null
}

function saveModelNote() {
  const model = noteEditorModel.value
  if (!model) return
  modelStore.setModelNote(model.name, noteDraft.value)
  closeModelNoteEditor()
  message.success(t('models.noteSaved'))
}

function openInferenceEditor(model: ModelEntry) {
  inferenceEditorModel.value = model
  syncInferenceDraft(model)
  showInferenceEditor.value = true
}

function closeInferenceEditor() {
  showInferenceEditor.value = false
  inferenceEditorModel.value = null
}

function handleInferenceEditorUpdate(value: boolean) {
  showInferenceEditor.value = value
  if (!value) inferenceEditorModel.value = null
}

async function revealModelDirectory(model: ModelEntry) {
  if (!model.downloaded || !model.modelPath) return
  try {
    await taskStore.revealPath(model.modelPath)
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
}

function handleModelContextMenuSelect(key: string | number) {
  const model = contextModel.value
  closeModelContextMenu()
  if (!model) return

  if (key === 'detail') {
    selectModel(model)
    return
  }
  if (key === 'note') {
    openModelNoteEditor(model)
    return
  }
  if (key === 'inference') {
    openInferenceEditor(model)
    return
  }
  if (key === 'open-directory') {
    void revealModelDirectory(model)
    return
  }
  if (key === 'favorite') {
    toggleFavorite(model)
    return
  }
  if (key === 'download') {
    void downloadModel(model)
    return
  }
  if (key === 'download-detail') {
    openDownloadDetail(model.name)
    return
  }
  if (key === 'cancel-download') {
    void cancelDownloadModel(model)
    return
  }
  if (key === 'resume-download') {
    void downloadModel(model)
    return
  }
  if (key === 'relink') {
    void relinkCustomModel(model)
    return
  }
  if (key === 'remove-custom') {
    confirmRemoveCustomModel(model)
    return
  }
  if (key === 'delete') {
    confirmDeleteModel(model)
  }
}

function handleDetailDrawerUpdate(value: boolean) {
  showDetail.value = value
}

function handleDetailModalAfterLeave() {
  if (!showDetail.value) selectedInfo.value = null
}

async function downloadModel(model: ModelEntry, event?: MouseEvent) {
  event?.stopPropagation()
  try {
    const started = await modelStore.downloadModel(model.name)
    if (started) message.success(t('models.downloadStarted'))
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
}

async function cancelDownloadModel(model: ModelEntry, event?: MouseEvent) {
  event?.stopPropagation()
  await modelStore.cancelDownload(model.name)
}

function openDownloadDetail(modelName: string) {
  downloadDetailModel.value = modelName
  showDownloadDetail.value = true
  modelStore.markDownloadTaskSeen(modelName)
}

function closeDownloadDetail() {
  showDownloadDetail.value = false
  if (downloadDetailModel.value) {
    modelStore.markDownloadTaskSeen(downloadDetailModel.value)
  }
}

function onDownloadDetailCancel() {
  if (!downloadDetailModel.value) return
  void modelStore.cancelDownload(downloadDetailModel.value)
}

function onDownloadDetailResume() {
  const name = downloadDetailModel.value
  if (!name) return
  const model = modelStore.models.find((m) => m.name === name)
  if (!model) return
  void downloadModel(model)
}

function onDownloadDetailDelete() {
  const name = downloadDetailModel.value
  if (!name) return
  const model = modelStore.models.find((m) => m.name === name)
  if (!model) return
  confirmDeleteModel(model)
}

const downloadDetailTask = computed(() => {
  if (!downloadDetailModel.value) return null
  return downloadTasks.value[downloadDetailModel.value] || null
})

watch(downloadTasks, (value) => {
  Object.values(value).forEach((task) => {
    if (task.status === 'error' && !task.seen) {
      if (!showDownloadDetail.value) {
        openDownloadDetail(task.model)
      }
    }
  })
}, { deep: true })

function confirmDeleteModel(model: ModelEntry) {
  if (isDeletingModel(model.name)) return
  dialog.warning({
    title: t('models.deleteConfirmTitle'),
    content: t('models.deleteConfirmContent'),
    positiveText: t('common.confirm'),
    negativeText: t('common.cancel'),
    onPositiveClick: () => {
      modelStore.deleteModel(model.name).catch((err) => {
        message.error(err instanceof Error ? err.message : String(err))
      })
    },
  })
}


/** Imported models are local-only: pymss refuses to download them, so they need their own actions. */
function isCustomModel(model: ModelEntry | null | undefined) {
  return model?.source === 'user'
}

/** Whether the active runtime's pymss exposes the user-model registry at all (2.0.15+). */
const customModelsSupported = computed(() => app.envInfo?.customModelsSupported !== false)

/**
 * Show the freshly imported model instead of leaving it buried among 300+ catalog entries.
 * Other filters are cleared for the same reason — a stale search would hide it again.
 */
function onCustomModelImported() {
  modelSource.value = 'user'
  search.value = ''
  category.value = ''
  downloadedOnly.value = false
}

function confirmRemoveCustomModel(model: ModelEntry) {
  // State exactly what will happen rather than describing both cases: this deletes files, and a
  // vague warning is the wrong thing to show before a destructive action. Only copies the app
  // made are ever deleted — the worker enforces that too.
  const copied = model.importMode === 'copy'
  dialog.warning({
    title: t('models.customRemoveTitle'),
    content: copied
      ? t('models.customRemoveContentCopy', { name: model.name })
      : t('models.customRemoveContentReference', { name: model.name }),
    positiveText: t('models.customRemoveConfirm'),
    negativeText: t('common.cancel'),
    onPositiveClick: () => {
      modelStore.removeCustomModel(model.name, true)
        .then((result) => {
          message.success(result.deletedFiles
            ? t('models.customRemovedWithFiles', { name: model.name })
            : t('models.customRemoved', { name: model.name }))
        })
        .catch((err) => message.error(err instanceof Error ? err.message : String(err)))
    },
  })
}

async function relinkCustomModel(model: ModelEntry, event?: MouseEvent) {
  event?.stopPropagation()
  try {
    const picked = await invoke<string | null>('pick_model_weights_file', {
      title: t('models.customPickWeightsTitle', { name: model.name }),
    })
    if (!picked) return
    let configPicked: string | null = null
    if (model.configPath) {
      configPicked = await invoke<string | null>('pick_model_config_file', {
        title: t('models.customPickConfigTitle', { name: model.name }),
      })
      // Most architectures require a config, so relinking without one would fail deep inside
      // pymss with a confusing message. A cancelled picker means "abandon the relink".
      if (!configPicked) return
    }
    await modelStore.relinkCustomModel(model.name, picked, configPicked)
    message.success(t('models.customRelinked', { name: model.name }))
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
}

async function openStorageManager() {
  showStorage.value = true
  try {
    await modelStore.loadModelStorageSummary()
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
}

function openModelDir() {
  const path = modelStorageSummary.value?.modelDir || modelDir.value || settings.modelDir
  if (path) void taskStore.revealPath(path)
}

function confirmBatchDelete() {
  const targets = selectedStorageModels.value
    .map(key => allStorageModelsByKey.value.get(key))
    .filter((item): item is StorageListItem => Boolean(item))
    .filter(item => item.kind === 'tool' || !isDeletingModel(item.id))
    .map(({ kind, id, name, tool }) => ({ kind, id, name, tool }))
  if (!targets.length) return
  dialog.warning({
    title: t('models.storageBatchDeleteConfirmTitle'),
    content: t('models.storageBatchDeleteConfirmContent', {
      count: targets.length,
      size: formatBytes(selectedStorageBytes.value),
    }),
    positiveText: t('models.storageBatchDelete'),
    negativeText: t('common.cancel'),
    onPositiveClick: () => {
      modelStore.deleteStorageModels(targets).then(() => {
        const failed = new Set(modelStore.batchDeleteState.failedModels)
        selectedStorageModels.value = selectedStorageModels.value.filter((key) => {
          const item = allStorageModelsByKey.value.get(key)
          return Boolean(item && failed.has(item.name))
        })
        if (modelStore.batchDeleteState.failedModels.length) {
          message.warning(t('models.batchDeleteWithFailures', { count: modelStore.batchDeleteState.failedModels.length }))
        } else {
          message.success(t('models.batchDeleteSuccess'))
        }
      }).catch((err) => message.error(err instanceof Error ? err.message : String(err)))
    },
  })
}

function confirmCleanupResidual() {
  const summary = modelStorageSummary.value
  if (!summary?.residualFiles.length) return
  dialog.warning({
    title: t('models.storageCleanupConfirmTitle'),
    content: t('models.storageCleanupConfirmContent', {
      count: summary.residualFiles.length,
      size: formatBytes(summary.residualBytes),
    }),
    positiveText: t('models.storageCleanup'),
    negativeText: t('common.cancel'),
    onPositiveClick: () => {
      modelStore.cleanupModelResidualFiles().catch((err) => message.error(err instanceof Error ? err.message : String(err)))
    },
  })
}

watch(deleteTasks, (value) => {
  Object.values(value).forEach((task) => {
    if (task.status === 'done') {
      if (task.source !== 'batch') {
        message.success(t('models.deleteSuccess'))
        modelStore.clearDeleteTask(task.model)
      }
    } else if (task.status === 'error') {
      if (task.source !== 'batch') {
        message.error(`${t('models.deleteFailed')}: ${task.message}`)
        modelStore.clearDeleteTask(task.model)
      }
    }
  })
}, { deep: true })

watch(residualCleanupState, (state) => {
  if (state.status === 'done' && !state.active) {
    message.success(t('models.storageCleanupSuccess'))
    modelStore.resetResidualCleanupState()
  } else if (state.status === 'error' && !state.active && state.message) {
    message.error(`${t('models.cleanupFailed')}: ${state.message}`)
    modelStore.resetResidualCleanupState()
  }
}, { deep: true })

onMounted(() => {
  showDetail.value = false
  if (!modelStore.models.length) void loadModels()
})
</script>

<template>
  <div class="page page--models">
    <div class="page-header-compact">
      <div>
        <div class="models-title-row">
          <h1>{{ t('models.title') }}</h1>
          <n-tag size="small" :bordered="false" type="info" round>
            {{ t('models.count', { count: sortedModels.length }) }}
          </n-tag>
          <n-tag v-if="debugStatusText" size="small" :bordered="false" type="warning" round>
            {{ debugStatusText }}
          </n-tag>
        </div>
        <p>{{ t('models.subtitle') }}</p>
      </div>
      <div class="header-actions">
        <n-tooltip v-if="!customModelsSupported" trigger="hover">
          <template #trigger>
            <n-button secondary disabled>
              <template #icon><n-icon :component="AddCircleOutline" /></template>
              {{ t('models.customImport') }}
            </n-button>
          </template>
          {{ t('models.customUnsupported') }}
        </n-tooltip>
        <n-button v-else secondary @click="showCustomImport = true">
          <template #icon><n-icon :component="AddCircleOutline" /></template>
          {{ t('models.customImport') }}
        </n-button>
        <n-button secondary @click="openStorageManager">
          <template #icon><n-icon :component="ServerOutline" /></template>
          {{ t('models.storageManage') }}
        </n-button>
        <n-button secondary :loading="isLoading" @click="loadModels">
          <template #icon><n-icon :component="RefreshOutline" /></template>
          {{ t('models.load') }}
        </n-button>
      </div>
    </div>

    <!-- Filter Toolbar -->
    <div class="toolbar">
      <div class="toolbar-row">
        <n-input
          v-model:value="search"
          :placeholder="t('models.search')"
          clearable
          class="search-input"
        >
          <template #prefix><n-icon :component="SearchOutline" /></template>
        </n-input>
        <n-select
          v-model:value="category"
          :options="categoryOptions"
          size="small"
          class="category-select"
          :consistent-menu-width="false"
        />
        <n-select
          v-model:value="modelSource"
          :options="modelSourceOptions"
          size="small"
          class="source-select"
          :consistent-menu-width="false"
        />
        <n-select
          v-model:value="modelSort"
          :options="modelSortOptions"
          size="small"
          class="sort-select"
          :consistent-menu-width="false"
        />
        <div class="toolbar-actions">
          <n-switch v-model:value="downloadedOnly" size="small" />
          <span class="text-sm text-muted">{{ t('models.downloadedOnly') }}</span>
          <div class="view-toggle" role="group" :aria-label="t('models.viewMode')">
            <button
              type="button"
              class="view-toggle__button"
              :class="{ 'view-toggle__button--active': modelViewMode === 'list' }"
              :aria-pressed="modelViewMode === 'list'"
              :title="t('models.viewList')"
              @click="modelViewMode = 'list'"
            >
              <n-icon :component="ListOutline" />
            </button>
            <button
              type="button"
              class="view-toggle__button"
              :class="{ 'view-toggle__button--active': modelViewMode === 'card' }"
              :aria-pressed="modelViewMode === 'card'"
              :title="t('models.viewCard')"
              @click="modelViewMode = 'card'"
            >
              <n-icon :component="GridOutline" />
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Body: Model Grid + Detail Drawer -->
    <div class="models-body">
      <!-- Loading Skeleton -->
      <div v-if="isLoading && !modelStore.models.length" :class="['model-grid', `model-grid--${modelViewMode}`]">
        <div v-for="i in 6" :key="i" class="skel-card">
          <n-skeleton text style="width:70%;height:18px;margin-bottom:12px" />
          <n-skeleton text style="width:40%;height:14px;margin-bottom:16px" />
          <n-skeleton text style="width:100%;height:12px;margin-bottom:6px" />
          <n-skeleton text style="width:80%;height:12px" />
        </div>
      </div>

      <!-- Empty State -->
      <div v-else-if="!sortedModels.length" class="empty-state">
        <n-icon :component="CubeOutline" size="48" color="var(--on-surface-muted)" />
        <!-- Filtering to imported models with none imported is not a failed search: say what is
             missing and offer the action that fixes it, rather than "no results". -->
        <template v-if="modelSource === 'user' && !customModelCount">
          <p class="text-muted">{{ t('models.customEmpty') }}</p>
          <n-button v-if="customModelsSupported" secondary @click="showCustomImport = true">
            <template #icon><n-icon :component="AddCircleOutline" /></template>
            {{ t('models.customImport') }}
          </n-button>
        </template>
        <template v-else>
          <p class="text-muted">{{ hasActiveFilter ? t('models.searchEmpty') : t('models.empty') }}</p>
          <n-button v-if="!hasActiveFilter && !modelStore.models.length" secondary @click="loadModels">
            {{ t('models.load') }}
          </n-button>
        </template>
      </div>

      <!-- Model Grid -->
      <template v-else>
        <div :class="['model-grid', `model-grid--${modelViewMode}`]">
          <div
            v-for="model in pagedModels"
            :key="model.name"
            :class="['model-card', {
              'model-card--selected': selectedModel === model.name,
              'model-card--unsupported': !model.supported,
              'model-card--busy': isModelBusy(model)
            }]"
            tabindex="0"
            :aria-label="t('models.viewDetailAria', { name: model.name })"
            :aria-current="selectedModel === model.name ? 'true' : undefined"
            @click="selectModel(model)"
            @contextmenu.stop.prevent="openModelContextMenu($event, model)"
            @keydown.enter.self.prevent="selectModel(model)"
            @keydown.space.self.prevent="selectModel(model)"
          >
          <!-- Card Header -->
          <div class="mc-header">
            <div class="mc-title">
              <div class="mc-name-row">
                <span class="mc-name" :title="model.name">{{ model.name }}</span>
                <n-button
                  quaternary
                  circle
                  size="tiny"
                  :type="isFavoriteModel(model) ? 'warning' : 'default'"
                  :title="isFavoriteModel(model) ? t('models.unfavorite') : t('models.favorite')"
                  class="mc-favorite"
                  @click.stop="toggleFavorite(model, $event)"
                >
                  <template #icon>
                    <n-icon :component="isFavoriteModel(model) ? Star : StarOutline" />
                  </template>
                </n-button>
                <n-button
                  quaternary
                  circle
                  size="tiny"
                  class="mc-more"
                  :title="t('models.moreActions')"
                  :aria-label="t('models.moreActions')"
                  @click.stop="openModelContextMenu($event, model)"
                >
                  <template #icon><n-icon :component="EllipsisHorizontalOutline" /></template>
                </n-button>
              </div>
              <span v-if="modelNote(model)" class="mc-note" :title="modelNote(model)">{{ modelNote(model) }}</span>
            </div>
          </div>
          <!-- Meta Tags -->
          <div class="mc-tags">
            <n-tag v-if="model.architecture" :bordered="false" size="tiny" type="info" round>
              {{ model.architecture }}
            </n-tag>
            <n-tag v-if="showModelTypeTag(model)" :bordered="false" size="tiny" round>
              {{ model.modelType }}
            </n-tag>
          </div>

          <!-- Key info: values read fine without their labels shouting, so both sit on
               one wrapping line rather than as a label/value table. -->
          <div class="mc-meta">
            <div class="mc-meta-item">
              <span class="mc-label">{{ t('models.targetStem') }}</span>
              <span class="mc-value">{{ targetStemLabel(model) || '—' }}</span>
            </div>
            <div class="mc-meta-item">
              <span class="mc-label">{{ t('models.category') }}</span>
              <span class="mc-value">{{ categoryLabel(model) || '—' }}</span>
            </div>
            <div class="mc-meta-item mc-meta-item--usage">
              <span class="mc-label">{{ t('models.useCountLabel') }}</span>
              <span class="mc-value">{{ t('models.useCountValue', { count: modelUseCount(model) }) }}</span>
            </div>
          </div>

          <!-- Bottom bar: size on the left, whatever the model needs on the right. Keeping
               them in one element means a state change swaps its contents instead of
               re-choreographing the card grid. -->
          <!-- Action row: one line in every state, so a downloading card is the same height as
               its neighbours. The progress itself becomes the card's bottom edge. -->
          <div class="mc-bottom">
            <span class="mc-size">{{ sizeLabel(model) }}</span>

            <template v-if="isDeletingModel(model.name)">
              <span class="mc-state mc-state--downloading">
                <span class="mc-state__dot" />
                {{ deleteStatusMessage(model.name) || t('models.deleting') }}
              </span>
              <span class="mc-pct">{{ deleteTasks[model.name]?.progress || 0 }}%</span>
            </template>

            <!-- Imported models are local-only: a missing file means "relink", not "download". -->
            <template v-else-if="isCustomModel(model)">
              <span :class="['mc-state', model.downloaded ? 'mc-state--ok' : 'mc-state--warn']">
                <n-icon :component="model.downloaded ? CheckmarkCircleOutline : LinkOutline" />
                {{ model.downloaded ? t('models.customReady') : t('models.customFilesMissing') }}
               </span>
               <div class="mc-actions">
                 <n-button size="tiny" quaternary @click.stop="relinkCustomModel(model, $event)">
                   <template #icon><n-icon :component="LinkOutline" /></template>
                 </n-button>
               </div>
            </template>

            <template v-else-if="!model.downloaded && downloadTasks[model.name] && downloadTasks[model.name].status !== 'done'">
              <span :class="['mc-state', `mc-state--${downloadTasks[model.name].status}`]">
                <span class="mc-state__dot" />
                {{ cardDownloadMessage(model.name) }}
              </span>
              <span v-if="downloadSpeed(model.name)" class="mc-speed">{{ downloadSpeed(model.name) }}</span>
              <span class="mc-pct">{{ downloadTasks[model.name].progress }}%</span>
              <div class="mc-actions">
                <n-button size="tiny" quaternary @click.stop="openDownloadDetail(model.name)">
                  {{ t('models.downloadDetail') }}
                </n-button>
                <n-button
                  v-if="['preparing', 'downloading'].includes(downloadTasks[model.name].status)"
                  size="tiny"
                  quaternary
                  @click.stop="cancelDownloadModel(model, $event)"
                >
                  {{ t('common.cancel') }}
                </n-button>
                <n-button
                  v-else-if="downloadTasks[model.name].status !== 'preparing'"
                  size="tiny"
                  quaternary
                  type="primary"
                  @click.stop="downloadModel(model, $event)"
                >
                  {{ downloadTasks[model.name]?.status === 'interrupted' ? t('models.continueDownload') : t('common.resume') }}
                </n-button>
              </div>
            </template>

            <template v-else-if="model.downloaded">
              <span class="mc-state mc-state--ok">
                <n-icon :component="CheckmarkCircleOutline" />
                {{ t('models.downloaded') }}
              </span>
            </template>

            <n-button
              v-else-if="model.supported"
              class="mc-download-button"
              secondary
              size="tiny"
              :disabled="isModelBusy(model)"
              @click.stop="downloadModel(model, $event)"
            >
              <template #icon><n-icon :component="DownloadOutline" /></template>
              {{ t('common.download') }}
            </n-button>
          </div>

          <!-- Progress as the card's bottom edge: findable at a glance across the grid, and it
               costs no height, which is what made downloading cards tower over their neighbours. -->
          <div
            v-if="cardProgressPercent(model) !== null"
            class="mc-progressbar"
            :style="{ '--mc-progress': cardProgressPercent(model) + '%' }"
            :data-state="cardProgressState(model)"
          />
          </div>
        </div>
        <div class="pagination-row">
          <span class="text-sm text-muted">{{ t('models.pageSummary', { total: sortedModels.length }) }}</span>
          <n-pagination
            v-model:page="page"
            v-model:page-size="pageSize"
            :item-count="sortedModels.length"
            :page-sizes="pageSizeOptions"
            show-size-picker
          />
        </div>
      </template>
    </div>

    <n-dropdown
      placement="bottom-start"
      trigger="manual"
      :x="contextMenuX"
      :y="contextMenuY"
      :options="modelMenuOptions"
      :show="contextMenuVisible"
      @clickoutside="closeModelContextMenu"
      @select="handleModelContextMenuSelect"
    />

    <n-modal
      :show="showNoteEditor"
      @update:show="handleNoteEditorUpdate"
    >
      <n-card
        class="model-note-modal"
        :title="t('models.editNote')"
        :bordered="false"
        closable
        role="dialog"
        aria-modal="true"
        @close="closeModelNoteEditor"
      >
        <div v-if="noteEditorModel" class="model-note-modal__body">
          <strong class="model-note-modal__name">{{ noteEditorModel.name }}</strong>
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
          <div class="model-note-modal__footer">
            <n-button secondary @click="closeModelNoteEditor">{{ t('common.cancel') }}</n-button>
            <n-button type="primary" :disabled="!noteEditorModel" @click="saveModelNote">{{ t('common.save') }}</n-button>
          </div>
        </template>
      </n-card>
    </n-modal>

    <n-modal
      :show="showInferenceEditor"
      @update:show="handleInferenceEditorUpdate"
    >
      <n-card
        class="model-inference-modal"
        :title="t('models.editInferenceDefaults')"
        :bordered="false"
        closable
        role="dialog"
        aria-modal="true"
        @close="closeInferenceEditor"
      >
        <div v-if="inferenceEditorModel" class="model-inference-modal__body">
          <strong class="model-inference-modal__name">{{ inferenceEditorModel.name }}</strong>
          <p class="model-inference-modal__hint">{{ t('models.inferenceDefaultsHint') }}</p>
          <div class="inference-editor-grid">
            <label class="inference-editor-field">
              <span>{{ t('inference.batchSize') }}</span>
              <n-input-number v-model:value="inferenceDraft.batch_size" :min="1" :max="32" />
            </label>
            <label class="inference-editor-field">
              <span>{{ t('inference.overlapSize') }}</span>
              <AlignedInferenceInputNumber :value="inferenceDraft.overlap_size" :step="inferenceEditorOverlapStep" :alignment-step="inferenceEditorSampleStep" @update:value="updateInferenceDraftSize('overlap_size', $event)" />
            </label>
            <label class="inference-editor-field">
              <span>{{ t('inference.chunkSize') }}</span>
              <AlignedInferenceInputNumber :value="inferenceDraft.chunk_size" :step="inferenceEditorChunkStep" :alignment-step="inferenceEditorChunkConstraint?.step || inferenceEditorSampleStep" :alignment-offset="inferenceEditorChunkConstraint?.offset" :alignment-min="inferenceEditorChunkConstraint?.min" @update:value="updateInferenceDraftSize('chunk_size', $event)" />
            </label>
          </div>
          <n-alert v-if="inferenceEditorChunkIssue" type="warning" :show-icon="false" style="margin-top:12px">
            {{ t('inference.invalidChunkSize', inferenceEditorChunkIssue) }}
          </n-alert>
        </div>
        <template #footer>
          <div class="model-inference-modal__footer">
            <n-button
              secondary
              :disabled="!canResetInferenceDefaults(inferenceEditorModel)"
              @click="resetInferenceDefaults"
            >
              {{ t('models.inferenceDefaultsReset') }}
            </n-button>
            <span class="model-inference-modal__footer-spacer" />
            <n-button secondary @click="closeInferenceEditor">{{ t('common.cancel') }}</n-button>
            <n-button type="primary" :disabled="!inferenceEditorModel" @click="saveInferenceDefaults">
              {{ t('common.save') }}
            </n-button>
          </div>
        </template>
      </n-card>
    </n-modal>

    <!-- Detail Modal -->
    <n-modal :show="showDetail" @update:show="handleDetailDrawerUpdate" @after-leave="handleDetailModalAfterLeave">
      <n-card
        class="model-detail-modal"
        :title="t('models.detail')"
        :bordered="false"
        closable
        role="dialog"
        aria-modal="true"
        @close="handleDetailDrawerUpdate(false)"
      >
        <template v-if="selectedInfo">
          <div class="model-detail-modal__body">
            <div class="detail-content">
            <div class="detail-title-row">
              <strong class="detail-name">{{ selectedInfo.name }}</strong>
              <n-button
                secondary
                size="small"
                :type="isFavoriteModel(selectedInfo) ? 'warning' : 'default'"
                @click="toggleFavorite(selectedInfo, $event)"
              >
                <template #icon>
                  <n-icon :component="isFavoriteModel(selectedInfo) ? Star : StarOutline" />
                </template>
                {{ isFavoriteModel(selectedInfo) ? t('models.favorited') : t('models.favorite') }}
              </n-button>
            </div>

            <div class="detail-badges">
              <n-tag v-if="detailLoading" :bordered="false" size="small" type="info" round>
                {{ t('common.refreshing') }}
              </n-tag>
              <n-tag v-if="selectedInfo.architecture" :bordered="false" size="small" type="info" round>
                {{ selectedInfo.architecture }}
              </n-tag>
              <n-tag
                :bordered="false"
                size="small"
                :type="modelStatusType(selectedInfo)"
                round
              >
                {{ modelStatusLabel(selectedInfo) }}
              </n-tag>
            </div>

            <n-divider style="margin:16px 0" />

            <div class="detail-grid">
              <div class="detail-row">
                <span class="detail-label">{{ t('models.type') }}</span>
                <span class="detail-val">{{ selectedInfo.modelType || '—' }}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{{ t('models.targetStem') }}</span>
                <span class="detail-val">{{ targetStemLabel(selectedInfo) || '—' }}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{{ t('models.size') }}</span>
                <span class="detail-val">{{ formatBytes(selectedInfo.sizeBytes) }}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{{ t('models.category') }}</span>
                <span class="detail-val">{{ categoryLabel(selectedInfo) || '—' }}</span>
              </div>
              <div v-if="selectedInfo.aliases?.length" class="detail-row">
                <span class="detail-label">{{ t('models.aliases') }}</span>
                <span class="detail-val">{{ selectedInfo.aliases.join(', ') }}</span>
              </div>
            </div>

            <n-divider style="margin:16px 0" />

            <n-collapse class="detail-path-collapse" :default-expanded-names="[]">
              <n-collapse-item :title="t('models.path')" name="model-path">
                <div class="detail-path-block">
                  <code class="detail-path">{{ selectedInfo.modelPath }}</code>
                </div>
                <div v-if="isPartiallyAvailable(selectedInfo) && selectedInfo.missingPaths?.length" class="detail-partial-block">
                  <div class="detail-label" style="margin-bottom:6px">{{ t('models.partialHintTitle') }}</div>
                  <div class="text-muted">{{ t('models.partialHint') }}</div>
                  <ul class="detail-missing-list">
                    <li v-for="path in selectedInfo.missingPaths" :key="path">
                      <code class="detail-path">{{ path }}</code>
                    </li>
                  </ul>
                </div>
              </n-collapse-item>
            </n-collapse>

            </div>
          </div>
        </template>

        <template #footer>
          <div class="drawer-footer" v-if="selectedInfo">
            <!-- 删除中：进度块 -->
            <ModelProgressBlock
              v-if="isDeletingModel(selectedInfo.name)"
              status="downloading"
              :message="deleteStatusMessage(selectedInfo.name) || t('models.deleting')"
              :progress="deleteTasks[selectedInfo.name]?.progress || 0"
              :files-text="t('models.deleteProgress', { completed: deleteTasks[selectedInfo.name]?.completedFiles || 0, total: deleteTasks[selectedInfo.name]?.totalFiles || 0 })"
            />

            <!-- 下载中：进度块 + 取消 -->
            <ModelProgressBlock
              v-else-if="['preparing', 'downloading'].includes(downloadTasks[selectedInfo.name]?.status || '')"
              :status="downloadTasks[selectedInfo.name]!.status"
              :message="downloadStatusMessage(selectedInfo.name)"
              :progress="downloadTasks[selectedInfo.name]!.progress"
              :files-text="downloadTasks[selectedInfo.name]!.totalFiles > 1 ? t('models.fileProgress', { completed: downloadTasks[selectedInfo.name]!.completedFiles, total: downloadTasks[selectedInfo.name]!.totalFiles }) : ''"
              :color="downloadProgressColor(downloadTasks[selectedInfo.name]!.status)"
            >
              <template #actions>
                <n-button block secondary @click="cancelDownloadModel(selectedInfo!, $event)">
                  {{ t('common.cancel') }}
                </n-button>
              </template>
            </ModelProgressBlock>

            <!-- 暂停/取消/错误/中断：进度块 + 继续 + 删除 -->
            <template v-else-if="!selectedInfo.downloaded && downloadTasks[selectedInfo.name] && ['paused','cancelled','error','interrupted'].includes(downloadTasks[selectedInfo.name]!.status)">
              <ModelProgressBlock
                :status="downloadTasks[selectedInfo.name]!.status"
                :message="downloadStatusMessage(selectedInfo.name)"
                :progress="downloadTasks[selectedInfo.name]!.progress"
                :files-text="downloadTasks[selectedInfo.name]!.totalFiles > 1 ? t('models.fileProgress', { completed: downloadTasks[selectedInfo.name]!.completedFiles, total: downloadTasks[selectedInfo.name]!.totalFiles }) : ''"
                :color="downloadProgressColor(downloadTasks[selectedInfo.name]!.status)"
              />
              <div class="drawer-footer-actions">
                <n-button
                  block
                  type="primary"
                  :disabled="isModelBusy(selectedInfo)"
                  @click="downloadModel(selectedInfo!, $event)"
                >
                  {{ downloadTasks[selectedInfo.name]?.status === 'interrupted' ? t('models.continueDownload') : t('common.resume') }}
                </n-button>
                <n-button
                  block
                  type="error"
                  secondary
                  @click="confirmDeleteModel(selectedInfo!)"
                >
                  <template #icon><n-icon :component="TrashOutline" /></template>
                  {{ t('models.delete') }}
                </n-button>
              </div>
            </template>

            <!-- 已下载：状态 + 删除 -->
            <template v-else-if="selectedInfo.downloaded">
              <n-button block secondary disabled>
                <template #icon><n-icon :component="CheckmarkCircleOutline" /></template>
                {{ t('models.downloaded') }}
              </n-button>
              <n-button
                block
                type="error"
                secondary
                @click="confirmDeleteModel(selectedInfo!)"
              >
                <template #icon><n-icon :component="TrashOutline" /></template>
                {{ t('models.delete') }}
              </n-button>
            </template>

            <!-- 未下载且无任务：下载（含不支持则不显示） -->
            <n-button
              v-else-if="selectedInfo.supported && !selectedInfo.downloaded"
              block
              type="primary"
              @click="downloadModel(selectedInfo!, $event)"
            >
              <template #icon><n-icon :component="CloudDownloadOutline" /></template>
              {{ t('common.download') }}
            </n-button>
          </div>
        </template>
      </n-card>
    </n-modal>

    <n-drawer v-model:show="showStorage" :width="760" placement="right">
      <n-drawer-content :title="t('models.storageTitle')" closable>
        <div class="storage-panel">
          <div class="storage-summary">
            <div class="storage-stat">
              <span>{{ t('models.storageTotal') }}</span>
              <strong>{{ formatBytes(modelStorageSummary?.totalBytes || 0) }}</strong>
            </div>
            <div class="storage-stat">
              <span>{{ t('models.storageDownloadedCount') }}</span>
              <strong>{{ modelStorageSummary?.downloadedCount || 0 }}</strong>
            </div>
            <div class="storage-stat">
              <span>{{ t('models.storageResidual') }}</span>
              <strong>{{ formatBytes(modelStorageSummary?.residualBytes || 0) }}</strong>
            </div>
          </div>

          <div class="storage-dir">
            <span class="text-muted">{{ t('models.modelDir') }}:</span>
            <code>{{ modelStorageSummary?.modelDir || modelDir || settings.modelDir || '?' }}</code>
          </div>

          <div class="storage-toolbar">
            <n-input v-model:value="storageSearch" :placeholder="t('models.storageSearch')" clearable>
              <template #prefix><n-icon :component="SearchOutline" /></template>
            </n-input>
            <n-select
              v-model:value="storageSort"
              :options="[
                { label: t('models.storageSortSizeDesc'), value: 'size-desc' },
                { label: t('models.storageSortSizeAsc'), value: 'size-asc' },
                { label: t('models.storageSortNameAsc'), value: 'name-asc' },
                { label: t('models.storageSortNameDesc'), value: 'name-desc' },
              ]"
              style="width:160px"
            />
            <n-switch v-model:value="storageDownloadedOnly" size="small" />
            <span class="text-sm text-muted">{{ t('models.downloadedOnly') }}</span>
          </div>

          <div class="storage-actions">
            <n-button size="small" secondary :loading="storageLoading" @click="modelStore.loadModelStorageSummary({ force: true })">
              <template #icon><n-icon :component="RefreshOutline" /></template>
              {{ t('models.storageRefresh') }}
            </n-button>
            <n-button size="small" secondary @click="openModelDir">
              <template #icon><n-icon :component="FolderOpenOutline" /></template>
              {{ t('models.openModelDir') }}
            </n-button>
            <n-button
              size="small"
              type="error"
              secondary
              :disabled="!selectedStorageModels.length || hasDeletingSelectedStorageModels || batchDeleteState.active || residualCleanupState.active"
              @click="confirmBatchDelete"
            >
              {{ t('models.storageBatchDelete') }}
              <span v-if="selectedStorageModels.length">（{{ selectedStorageModels.length }} / {{ formatBytes(selectedStorageBytes) }}）</span>
            </n-button>
            <n-button
              size="small"
              type="warning"
              secondary
              :disabled="!(modelStorageSummary?.residualFiles.length) || batchDeleteState.active || residualCleanupState.active"
              @click="confirmCleanupResidual"
            >
              {{ t('models.storageCleanup') }}
            </n-button>
          </div>

          <div v-if="batchDeleteState.active" class="storage-progress-card">
            <strong>{{ t('models.batchDeleting', { count: batchDeleteState.totalModels }) }}</strong>
            <span class="text-muted">{{ batchDeleteState.completedModels }} / {{ batchDeleteState.totalModels }}</span>
            <div class="text-sm text-muted">{{ batchDeleteState.currentModel }}</div>
            <div v-if="batchDeleteState.currentModel && deleteTasks[batchDeleteState.currentModel]" class="mc-dl-files">
              {{ t('models.deleteProgress', { completed: deleteTasks[batchDeleteState.currentModel]?.completedFiles || 0, total: deleteTasks[batchDeleteState.currentModel]?.totalFiles || 0 }) }}
            </div>
            <n-progress
              :percentage="batchDeleteProgress"
              :show-indicator="false"
              :height="8"
              :border-radius="4"
              type="line"
              :rail-color="'var(--surface-3)'"
            />
          </div>

          <div v-if="residualCleanupState.active" class="storage-progress-card">
            <strong>{{ t('models.cleanupInProgress') }}</strong>
            <span class="text-muted">{{ t('models.deleteProgress', { completed: residualCleanupState.completedFiles, total: residualCleanupState.totalFiles }) }}</span>
            <n-progress
              :percentage="residualCleanupState.progress"
              :show-indicator="false"
              :height="8"
              :border-radius="4"
              type="line"
              :rail-color="'var(--surface-3)'"
            />
          </div>

          <div class="storage-list">
            <label
              v-for="item in storageModels"
              :key="item.selectionKey"
              class="storage-row"
            >
              <n-checkbox
                :checked="selectedStorageModels.includes(item.selectionKey)"
                :disabled="item.sizeBytes <= 0 || (item.kind === 'catalog' && isDeletingModel(item.id)) || batchDeleteState.active || residualCleanupState.active"
                @update:checked="(checked: boolean) => setStorageSelected(item.selectionKey, checked)"
              />
              <div class="storage-row__main">
                <strong>{{ item.name }}</strong>
                <span>{{ t('models.storageFileCount', { count: item.fileCount }) }}</span>
              </div>
              <n-tag size="small" :bordered="false" :type="item.kind === 'tool' ? 'info' : item.downloaded ? 'success' : 'default'">
                {{ storageModelTypeLabel(item) }}
              </n-tag>
              <strong class="storage-row__size">{{ formatBytes(item.sizeBytes || item.expectedSizeBytes) }}</strong>
            </label>
            <div v-if="!storageModels.length" class="empty-state storage-empty">
              <n-icon :component="CubeOutline" size="36" color="var(--on-surface-muted)" />
              <span class="text-muted">{{ t('models.searchEmpty') }}</span>
            </div>
          </div>
        </div>
      </n-drawer-content>
    </n-drawer>

    <DownloadDetailModal
      v-model:show="showDownloadDetail"
      :task="downloadDetailTask"
      :model-name="downloadDetailModel"
      @cancel="onDownloadDetailCancel"
      @resume="onDownloadDetailResume"
      @delete="onDownloadDetailDelete"
      @close="closeDownloadDetail"
    />

    <CustomModelImportDialog v-model:show="showCustomImport" @imported="onCustomModelImported" />
  </div>
</template>

<style scoped>
.page--models {
  max-width: var(--page-max-width);
}

.models-title-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.models-title-row h1 {
  margin: 0;
}

/* ===== Toolbar ===== */
.toolbar {
  margin-bottom: 14px;
  padding: 10px;
  border: 1px solid color-mix(in srgb, var(--outline) 58%, transparent);
  border-radius: 16px;
  background:
    linear-gradient(180deg, rgba(255,255,255,0.03), transparent 58%),
    color-mix(in srgb, var(--surface-1) 72%, transparent);
}

.toolbar-row {
  display: grid;
  /* One column per control: search | category | source | sort | actions. A missing column would
     push the last control into an implicit one and squeeze the switch label onto two lines. */
  grid-template-columns: minmax(180px, 0.92fr) 220px 160px 180px auto;
  align-items: center;
  gap: 10px;
}

.search-input {
  max-width: none;
  min-width: 180px;
}

.toolbar-actions {
  display: flex;
  align-items: center;
  gap: 7px;
  white-space: nowrap;
}

/* ===== Category Select ===== */
.category-select {
  width: 220px;
  flex-shrink: 0;
}

/* ===== View mode toggle ===== */
.view-toggle {
  display: inline-flex;
  padding: 2px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--surface-2) 60%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 55%, transparent);
  flex-shrink: 0;
}

.view-toggle__button {
  display: grid;
  place-items: center;
  width: 26px;
  height: 22px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--on-surface-muted);
  cursor: pointer;
  font-size: 14px;
  transition: background 150ms ease, color 150ms ease;
}

.view-toggle__button:hover {
  color: var(--on-surface);
}

.view-toggle__button--active {
  background: var(--surface-1);
  color: var(--primary);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.12);
}

.source-select {
  width: 150px;
  flex-shrink: 0;
}

.sort-select {
  width: 170px;
  flex-shrink: 0;
}

/* ===== Layout ===== */
.models-body {
  display: flex;
  flex-direction: column;
}

.model-grid {
  display: grid;
  align-content: start;
  gap: 10px;
}

/* One entry per row: the densest way to scan a few hundred models. */
.model-grid--list {
  grid-template-columns: minmax(0, 1fr);
}

/* Columns are added as the window allows rather than at fixed breakpoints. */
.model-grid--card {
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  /* Each card is as tall as its own content. Equal row heights line the bottom bars up, but one
     card downloading is much taller than the rest, and every neighbour then stretches to match
     it — a large void in each is a worse trade than a ragged bottom edge. */
  align-items: start;
  gap: 12px;
}

.pagination-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 16px;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 64px 0;
  color: var(--on-surface-muted);
}

/* ===== Model Card ===== */
.model-card {
  cursor: pointer;
  min-height: 0;
  min-width: 0;
  padding: 14px 16px;
  border-radius: 12px;
  border: 1px solid color-mix(in srgb, var(--outline) 58%, transparent);
  background:
    linear-gradient(180deg, rgba(255,255,255,0.018), transparent 54%),
    color-mix(in srgb, var(--surface-1) 64%, transparent);
  transition: box-shadow 0.15s ease, border-color 0.15s ease, background 0.15s ease;
  /* Layout comes from the card's own width, not the window's: in card mode the two are
     unrelated, since a 300px card can sit on a 2560px screen. */
  container-type: inline-size;
  position: relative;
  overflow: hidden;
}

/* ---- Card: a stack that ends in a bottom bar ---- */
.model-grid--card .model-card {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 10px;
  /* Room for the progress edge so it never sits under the action row. */
  padding-bottom: 15px;
}

.model-grid--card .mc-bottom {
  margin-top: auto;
  padding-top: 10px;
  border-top: 1px solid color-mix(in srgb, var(--outline) 38%, transparent);
}

/* ---- List: the original two-line row — the name over its details, with tags and actions
   riding on the right. Spreading the same content across one flex line left large gaps between
   the pieces and made long lists harder to scan. ---- */
.model-grid--list .model-card {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(112px, 160px) minmax(230px, 280px);
  grid-template-areas:
    "header tags bottom"
    "meta   tags bottom";
  align-items: center;
  gap: 8px 14px;
  padding: 14px 16px 17px;
}

.model-grid--list .mc-header { grid-area: header; min-width: 0; }
.model-grid--list .mc-meta { grid-area: meta; min-width: 0; }
.model-grid--list .mc-tags { grid-area: tags; }
.model-grid--list .mc-bottom { grid-area: bottom; justify-content: flex-end; gap: 12px; }

/* A narrow window cannot hold three columns; stack rather than crush them. */
@container (max-width: 620px) {
  .model-grid--list .model-card {
    grid-template-columns: minmax(0, 1fr);
    grid-template-areas:
      "header"
      "tags"
      "meta"
      "bottom";
  }

  .model-grid--list .mc-bottom {
    justify-content: space-between;
    padding-top: 10px;
    border-top: 1px solid color-mix(in srgb, var(--outline) 38%, transparent);
  }
}

/* A running download is the one thing on the page that changes by itself; a tinted edge makes
   it findable without scanning every card. */
.model-card--busy {
  border-color: color-mix(in srgb, var(--primary) 45%, transparent);
  background:
    linear-gradient(180deg, color-mix(in srgb, var(--primary) 5%, transparent), transparent 46%),
    color-mix(in srgb, var(--surface-1) 72%, transparent);
}

/* ---- Action row: one line, identical height in every state ---- */
.mc-bottom {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  min-height: 26px;
}

.mc-size {
  font-size: 11px;
  color: var(--on-surface-muted);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  flex-shrink: 0;
}

/* Pushes whatever follows the size to the right edge. */
.mc-state,
.mc-download-button,
.mc-bottom > .n-button {
  margin-left: auto;
}

/* Once a state label is present it owns the middle, and the actions go right. */
.mc-state ~ .mc-pct,
.mc-state ~ .mc-actions,
.mc-state ~ .n-button {
  margin-left: 0;
}

.mc-state {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  font-weight: 500;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--on-surface-muted);
}

.mc-state__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
  background: currentColor;
}

.mc-state--downloading {
  color: var(--primary);
}

.mc-state--preparing {
  color: var(--primary);
}

.mc-state--preparing .mc-state__dot,
.mc-state--downloading .mc-state__dot {
  animation: mc-pulse 1.5s infinite;
}

.mc-state--ok {
  color: var(--success);
}

.mc-state--warn,
.mc-state--paused,
.mc-state--cancelled,
.mc-state--interrupted {
  color: var(--warning);
}

.mc-state--error {
  color: var(--danger);
}

@keyframes mc-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}

.mc-speed {
  font-size: 11px;
  color: var(--on-surface-muted);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  flex-shrink: 0;
}

.mc-pct {
  font-size: 12px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--on-surface);
  flex-shrink: 0;
}

.mc-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  flex-shrink: 0;
}

/* ---- Progress drawn as the card's bottom edge ---- */
.mc-progressbar {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 3px;
  border-radius: 0 0 12px 12px;
  overflow: hidden;
  background: color-mix(in srgb, var(--outline) 45%, transparent);
}

.mc-progressbar::after {
  content: '';
  position: absolute;
  inset: 0 auto 0 0;
  width: var(--mc-progress, 0%);
  background: var(--primary);
  transition: width 240ms ease;
}

.mc-progressbar[data-state='paused']::after,
.mc-progressbar[data-state='cancelled']::after,
.mc-progressbar[data-state='interrupted']::after {
  background: var(--warning);
}

.mc-progressbar[data-state='error']::after {
  background: var(--danger);
}

.model-card:hover {
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.045);
  border-color: color-mix(in srgb, var(--primary) 22%, var(--outline));
  background:
    linear-gradient(180deg, color-mix(in srgb, var(--primary-soft) 12%, transparent), transparent 64%),
    color-mix(in srgb, var(--surface-1) 78%, transparent);
}

.model-card:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--primary) 70%, transparent);
  outline-offset: 2px;
  border-color: color-mix(in srgb, var(--primary) 40%, var(--outline));
}

.model-card--selected {
  border-color: color-mix(in srgb, var(--outline-strong) 72%, transparent) !important;
  background: color-mix(in srgb, var(--surface-2) 58%, var(--surface-1));
}

.model-card--unsupported {
  opacity: 0.55;
}

/* Card header */
.mc-header {
  display: flex;
  align-items: flex-start;
  min-width: 0;
}

.mc-title {
  display: grid;
  gap: 3px;
  flex: 1;
  min-width: 0;
}

.mc-name-row {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  min-width: 0;
}

.mc-name {
  display: -webkit-box;
  font-size: 12.5px;
  font-weight: 600;
  flex: 0 1 auto;
  min-width: 0;
  max-width: 100%;
  line-height: 1.45;
  overflow: hidden;
  overflow-wrap: anywhere;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-clamp: 2;
}

.mc-note {
  display: -webkit-box;
  max-width: 100%;
  color: var(--on-surface-muted);
  font-size: 11px;
  line-height: 1.35;
  overflow: hidden;
  overflow-wrap: anywhere;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 1;
  line-clamp: 1;
}

.mc-favorite {
  flex: 0 0 auto;
  margin-top: -4px;
  opacity: 0.62;
}

.mc-more {
  flex: 0 0 auto;
  margin-top: -4px;
  opacity: 0.62;
}

.model-card:hover .mc-favorite,
.model-card--selected .mc-favorite,
.model-card:hover .mc-more,
.model-card--selected .mc-more {
  opacity: 1;
}

/* Tags row */
.mc-tags {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  justify-content: flex-start;
  gap: 5px;
  min-width: 0;
}

.mc-tags :deep(.n-tag) {
  max-width: 100%;
}

.mc-tags :deep(.n-tag__content) {
  overflow: hidden;
  text-overflow: ellipsis;
}

.mc-size {
  font-size: 11px;
  color: var(--on-surface-muted);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  flex-shrink: 0;
}

/* Meta info */
.mc-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  /* Wraps instead of stretching: pinning the value to the far edge left a wide gap between a
     short label and a short value, which is what made the card look hollow. */
  gap: 4px 14px;
  font-size: 11px;
  min-width: 0;
}

.mc-meta-item {
  display: flex;
  gap: 6px;
  min-width: 0;
}

.mc-label {
  color: var(--on-surface-muted);
  flex-shrink: 0;
}

.mc-value {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mc-download-button {
  min-width: 72px;
  --n-height: 30px;
  --n-padding: 0 10px;
}

.mc-download-button :deep(.n-button__content) {
  font-size: 12.5px;
  font-weight: 600;
}

.mc-download-button :deep(.n-icon) {
  font-size: 17px;
}

/* Download progress */
/* 下载/删除进度块样式已迁移至 ModelProgressBlock 组件；
   此处仅保留存储抽屉批量删除卡片仍引用的文件进度文本样式 */
.mc-dl-files {
  font-size: 11px;
  color: var(--on-surface-muted);
}

/* ===== Drawer Footer ===== */
.drawer-footer {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.model-note-modal {
  width: min(460px, calc(100vw - 48px));
  border-radius: 18px;
}

.model-note-modal__body {
  display: grid;
  gap: 12px;
  min-width: 0;
}

.model-note-modal__body :deep(.n-input) {
  width: 100%;
  max-width: 100%;
  box-sizing: border-box;
}

.model-note-modal__name {
  overflow: hidden;
  color: var(--on-surface-muted);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.model-note-modal__footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.model-inference-modal {
  width: min(560px, calc(100vw - 48px));
  border-radius: 18px;
}

.model-inference-modal__body {
  display: grid;
  gap: 10px;
  min-width: 0;
}

.model-inference-modal__name {
  overflow: hidden;
  color: var(--on-surface-muted);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.model-inference-modal__hint {
  margin: 0;
  color: var(--on-surface-muted);
  font-size: 12px;
  line-height: 1.5;
}

.inference-editor-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.inference-editor-field {
  display: grid;
  gap: 6px;
  min-width: 0;
}

.inference-editor-field > span {
  color: var(--on-surface-muted);
  font-size: 12px;
}

.inference-editor-field :deep(.n-input-number) {
  width: 100%;
}

.model-inference-modal__footer {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.model-inference-modal__footer-spacer {
  flex: 1 1 auto;
}


.model-detail-modal {
  width: min(600px, calc(100vw - 48px));
  height: min(620px, calc(100vh - 96px));
  max-height: calc(100vh - 96px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-radius: 20px;
}

.model-detail-modal :deep(.n-card-header) {
  flex: 0 0 auto;
  padding: 18px 22px 14px;
  border-bottom: 1px solid color-mix(in srgb, var(--outline) 72%, transparent);
}

.model-detail-modal :deep(.n-card-content),
.model-detail-modal :deep(.n-card__content) {
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
  padding: 0;
}

.model-detail-modal__body {
  height: 100%;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 16px 20px;
  box-sizing: border-box;
}

.model-detail-modal :deep(.n-card-footer),
.model-detail-modal :deep(.n-card__footer) {
  flex: 0 0 auto;
  padding: 12px 20px 16px;
  border-top: 1px solid color-mix(in srgb, var(--outline) 72%, transparent);
  background: color-mix(in srgb, var(--surface-1) 96%, transparent);
}

.model-detail-modal .drawer-footer {
  flex-direction: row;
  justify-content: flex-end;
  flex-wrap: wrap;
}

.model-detail-modal .drawer-footer :deep(.n-button) {
  width: auto;
  min-width: 112px;
}

/* 进度块在 modal footer 中占满整行，其后的操作按钮另起一行 */
.model-detail-modal .drawer-footer > .mc-dl {
  flex: 1 1 100%;
  width: 100%;
}

.drawer-footer-actions {
  display: flex;
  gap: 8px;
  flex: 1 1 100%;
  justify-content: flex-end;
  flex-wrap: wrap;
}

.model-detail-modal .drawer-footer-actions :deep(.n-button) {
  flex: 1 1 auto;
}

/* ===== Skeleton ===== */
.skel-card {
  padding: 14px;
  border-radius: 14px;
  border: 1px solid color-mix(in srgb, var(--outline) 58%, transparent);
  background: color-mix(in srgb, var(--surface-1) 72%, transparent);
}

/* ===== Detail Drawer ===== */
.detail-content {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.detail-title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.detail-name {
  font-size: 16px;
  font-weight: 600;
  word-break: break-word;
  line-height: 1.4;
}

.detail-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}

.detail-grid {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.detail-row {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  gap: 12px;
}

.detail-label {
  color: var(--on-surface-muted);
  flex-shrink: 0;
}

.detail-val {
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.detail-path {
  font-size: 11px;
  word-break: break-all;
  color: var(--on-surface-muted);
  background: var(--surface-2);
  padding: 8px 10px;
  border-radius: 8px;
  display: block;
}

.detail-path-collapse {
  margin-top: -2px;
}

.detail-path-collapse :deep(.n-collapse-item__header) {
  padding: 0;
  color: var(--on-surface-muted);
  font-size: 13px;
  font-weight: 600;
}

.detail-path-collapse :deep(.n-collapse-item__content-inner) {
  padding-top: 10px;
}

.detail-path-block,
.detail-partial-block {
  display: grid;
  gap: 8px;
}

.detail-partial-block {
  margin-top: 12px;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.storage-panel {
  display: grid;
  gap: 14px;
}

.storage-summary {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}

.storage-stat {
  padding: 14px;
  border: 1px solid var(--outline);
  border-radius: 12px;
  background: var(--surface-1);
}

.storage-stat span {
  display: block;
  font-size: 12px;
  color: var(--on-surface-muted);
}

.storage-stat strong {
  display: block;
  margin-top: 6px;
  font-size: 18px;
}

.storage-dir {
  display: flex;
  gap: 8px;
  align-items: center;
  min-width: 0;
  font-size: 12px;
}

.storage-dir code {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--on-surface-muted);
}

.storage-toolbar,
.storage-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.storage-toolbar .n-input {
  flex: 1;
  min-width: 220px;
}

.storage-list {
  display: grid;
  gap: 8px;
  max-height: 56vh;
  overflow: auto;
  padding-right: 4px;
}

.storage-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 12px;
  padding: 12px;
  border-radius: 12px;
  border: 1px solid var(--outline);
  background: var(--surface-1);
}

.storage-row__main {
  min-width: 0;
}

.storage-row__main strong,
.storage-row__main span {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.storage-row__main span {
  margin-top: 4px;
  font-size: 12px;
  color: var(--on-surface-muted);
}

.storage-row__size {
  font-size: 13px;
  white-space: nowrap;
}

.storage-empty {
  padding: 32px 0;
}

.storage-progress-card {
  display: grid;
  gap: 8px;
  padding: 12px;
  border-radius: 12px;
  border: 1px solid var(--outline);
  background: var(--surface-1);
}

/* ===== Responsive ===== */
@media (max-width: 900px) {
  .search-input {
    max-width: 100%;
  }

  /* Two controls per line instead of one: the search takes the first line by itself, the three
     selects and the actions share the next two. A 700px-wide window drops its toolbar from four
     stacked rows (~206px) to three (~140px) without any control losing width. */

  .toolbar-row {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  }

  .search-input {
    grid-column: 1 / -1;
  }

  /* The card's own stacking is handled by its container query, which is correct whatever the
     window is doing. */

  .category-select,
  .source-select,
  .sort-select {
    width: 100%;
  }

  .toolbar-actions {
    width: 100%;
    justify-content: space-between;
  }

  .mc-tags {
    justify-content: flex-start;
  }

  .mc-size {
    text-align: left;
  }

  .mc-meta {
    flex-wrap: wrap;
  }
}
@media (max-width: 720px) {
  .inference-editor-grid {
    grid-template-columns: 1fr;
  }

  .model-inference-modal__footer-spacer {
    display: none;
  }

  .model-detail-modal {
    width: calc(100vw - 28px);
    height: calc(100vh - 40px);
    max-height: calc(100vh - 40px);
  }

  .model-detail-modal .drawer-footer {
    flex-direction: column;
  }

  .model-detail-modal .drawer-footer :deep(.n-button) {
    width: 100%;
  }
}

</style>
