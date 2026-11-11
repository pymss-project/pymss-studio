<script setup lang="ts">
import { createLogger } from '@/utils/logger'
import { isTutorialFrame } from '@/features/onboarding/demoContext'

import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
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
  CheckboxOutline,
  CloseOutline,
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
import { buildModelCategoryOptionsFromModels, getModelCategoryLabel, hasModelCategoryOption } from '@/utils/modelCategory'
import { compareModelDefaultOrder } from '@/utils/modelSort'
import { MODEL_LIBRARY_PAGE_SIZES } from '@/utils/pagination'
import { BATCH_DOWNLOAD_CONCURRENCY } from '@/utils/downloadQueue'
import {
  applyLibraryIntent,
  resolveParameters,
  type CustomParameterIntent,
  type NativeParameters,
  type ParameterResolutionMetadata,
} from '@/features/inference/parameters'
import ModelProgressBlock from '@/components/ModelProgressBlock.vue'
import ModelCategorySelect from '@/components/ModelCategorySelect.vue'
import DownloadDetailModal from '@/components/DownloadDetailModal.vue'
import CustomModelImportDialog from '@/components/CustomModelImportDialog.vue'
import InferenceParameterFields from '@/components/InferenceParameterFields.vue'
import { edgeScrollVelocity, segmentIntersectsRect, type SelectionPoint } from '@/utils/selection'

const studioLog = createLogger('ui.views.ModelsView')

const { t, locale } = useI18n()
const route = useRoute()
const router = useRouter()
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
  downloadQueue,
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
const storageList = ref<HTMLElement | null>(null)
const storageError = ref('')
const storageDrawerTop = typeof navigator !== 'undefined' && /Mac/i.test(navigator.platform) ? '0px' : '40px'
type StorageDrag = { pointerId: number; origin: SelectionPoint; previous: SelectionPoint; startKey: string;
  targetSelected: boolean; active: boolean; visited: Set<string>; scrollTop: number }
let storageDrag: StorageDrag | null = null
let storageSuppressClick = false
let storageListenersAttached = false
let storageScrollFrame: number | null = null
let storageScrollTime = 0
const contextMenuX = ref(0)
const contextMenuY = ref(0)
const contextMenuVisible = ref(false)
const contextModel = ref<ModelEntry | null>(null)
const showNoteEditor = ref(false)
const noteEditorModel = ref<ModelEntry | null>(null)
const noteDraft = ref('')
const showInferenceEditor = ref(false)
/** Name of the model whose library defaults are being edited; the entry itself is read live from the store. */
const inferenceEditorName = ref('')
/** Local copy of the saved native overrides. Restoring a field deletes from this copy only. */
const inferenceLibraryDraft = ref<Record<string, number | boolean>>({})
/** Fields the user explicitly edited in this dialog session (sparse; absence = keep library value). */
const inferenceEditorIntent = ref<CustomParameterIntent>({})
/** Library revision the draft was taken from; saving with a stale revision is rejected by the store. */
const inferenceEditorRevision = ref(0)
const inferenceEditorLoading = ref(false)
const inferenceEditorLoadError = ref('')
const inferenceEditorSaving = ref(false)
const inferenceEditorModel = computed(() => findModelEntry(inferenceEditorName.value))
const inferenceEditorMeta = computed(() => resolutionOf(inferenceEditorModel.value))
const inferenceEditorResolved = computed(() => resolveParameters(inferenceEditorMeta.value, inferenceLibraryDraft.value, 'custom', inferenceEditorIntent.value))
const inferenceEditorDirty = computed(() => Object.keys(inferenceEditorIntent.value).length > 0
  || !sameNativeProfile(inferenceLibraryDraft.value, modelStore.getModelInferenceOverrides(inferenceEditorName.value)))
const inferenceEditorBlocked = computed(() => !inferenceEditorMeta.value
  || !canEditModelParameters(inferenceEditorModel.value)
  || inferenceEditorResolved.value.issues.length > 0
  || intentProblems(inferenceEditorMeta.value, inferenceEditorResolved.value.parameters, inferenceEditorIntent.value).length > 0)

/* ---- Batch parameter editing ---- */
type BatchRowStatus = 'ok' | 'unchanged' | 'ineligible' | 'loading' | 'loadFailed' | 'metadataMissing' | 'invalid' | 'notApplicable'
type BatchChange = { key: string; before: number | boolean | undefined; after: number | boolean | undefined }
type BatchPreviewRow = {
  name: string
  status: BatchRowStatus
  fingerprint?: string
  profile?: NativeParameters
  changes: BatchChange[]
  fields: string[]
  sampleRate?: number
}
type BatchPreview = { kind: 'edit' | 'reset'; revision: number; intent: CustomParameterIntent; rows: BatchPreviewRow[] }
const BATCH_METADATA_CONCURRENCY = 4
const batchSelectMode = ref(false)
const batchSelection = ref<string[]>([])
const batchSelectionSet = computed(() => new Set(batchSelection.value))
const modelGrid = ref<HTMLElement | null>(null)
type BatchDragSelection = {
  pointerId: number
  startName: string
  origin: SelectionPoint
  previous: SelectionPoint
  targetSelected: boolean
  active: boolean
  visited: Set<string>
}
let batchDragSelection: BatchDragSelection | null = null
let suppressBatchClick = false
let batchListenersAttached = false
let modelPageActive = true
const showBatchEditor = ref(false)
const batchIntent = ref<CustomParameterIntent>({})
const batchReferenceName = ref('')
const batchLoadState = ref<Record<string, 'loading' | 'error'>>({})
const batchPreview = ref<BatchPreview | null>(null)
const batchExcluded = ref<string[]>([])
const batchStaleNames = ref<string[]>([])
const batchApplying = ref(false)
/** Bumped whenever the batch dialog is reopened, so late metadata responses cannot touch a newer session. */
let batchSession = 0

const categoryOptions = computed(() => {
  return buildModelCategoryOptionsFromModels(modelStore.models, locale.value, t('common.all'))
})
watch([categoryOptions, isLoading], () => {
  if (!isLoading.value && (modelStore.modelsLoaded || modelStore.models.length) && !hasModelCategoryOption(categoryOptions.value, category.value)) category.value = ''
}, { immediate: true })

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

function modelNameParts(name: string) {
  return name.split(/(?<=[_-])/u)
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
        return compareModelDefaultOrder(a.model, b.model, collator.value.compare)
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
const storageSelection = computed(() => new Set(selectedStorageModels.value))
const storageBusy = computed(() => storageLoading.value || batchDeleteState.value.active || residualCleanupState.value.active)
const selectableStorageModels = computed(() => storageModels.value.filter(item => canSelectStorageItem(item) || storageSelection.value.has(item.selectionKey)))
const storageVisibleKeys = computed(() => new Set(storageModels.value.map(item => item.selectionKey)))
const allStorageSelected = computed(() => selectableStorageModels.value.length > 0 && selectableStorageModels.value.every(item => storageSelection.value.has(item.selectionKey)))
const someStorageSelected = computed(() => !allStorageSelected.value && storageModels.value.some(item => storageSelection.value.has(item.selectionKey)))
const storageOutsideSelection = computed(() => selectedStorageModels.value.filter(key => !storageVisibleKeys.value.has(key)).length)

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

function canSelectStorageItem(item: StorageListItem) {
  return item.sizeBytes > 0 && !(item.kind === 'catalog' && isDeletingModel(item.id))
}
function formatStorageBytes(bytes: number) { return bytes > 0 ? formatBytes(bytes) : '0 B' }

function setStorageSelected(key: string, checked: boolean) {
  if (storageBusy.value) return
  const item = allStorageModelsByKey.value.get(key)
  if (checked && (!item || !canSelectStorageItem(item))) return
  if (storageSelection.value.has(key) === checked) return
  selectedStorageModels.value = checked ? [...selectedStorageModels.value, key] : selectedStorageModels.value.filter(item => item !== key)
}

function toggleStorageRow(item: StorageListItem) { setStorageSelected(item.selectionKey, !storageSelection.value.has(item.selectionKey)) }
function handleStorageRowClick(item: StorageListItem) {
  if (!window.getSelection()?.toString()) toggleStorageRow(item)
}
function selectStorageResults(checked: boolean) {
  finishStorageDrag()
  if (storageBusy.value) return
  const next = new Set(selectedStorageModels.value)
  for (const item of storageModels.value) {
    if (!checked) next.delete(item.selectionKey)
    else if (canSelectStorageItem(item)) next.add(item.selectionKey)
  }
  selectedStorageModels.value = [...next]
}
function clearStorageSelection() {
  finishStorageDrag()
  if (!storageBusy.value) selectedStorageModels.value = []
}
function consumeStorageClick(event: MouseEvent) {
  const suppress = storageSuppressClick && event.detail > 0
  storageSuppressClick = false
  if (suppress) { event.preventDefault(); event.stopPropagation() }
}
function startStorageDrag(event: PointerEvent, item: StorageListItem) {
  if (event.pointerType !== 'mouse' || event.button !== 0 || storageBusy.value) return
  if (!canSelectStorageItem(item) && !storageSelection.value.has(item.selectionKey)) return
  finishStorageDrag()
  storageSuppressClick = false
  const point = { x: event.clientX, y: event.clientY }
  storageDrag = { pointerId: event.pointerId, origin: point, previous: point, startKey: item.selectionKey,
    targetSelected: !storageSelection.value.has(item.selectionKey), active: false, visited: new Set(), scrollTop: storageList.value?.scrollTop || 0 }
}
function paintStorageSelection(point: SelectionPoint) {
  const state = storageDrag, list = storageList.value
  if (!state?.active || !list || storageBusy.value) return
  const previous = { x: state.previous.x, y: state.previous.y - (list.scrollTop - state.scrollTop) }
  const next = new Set(selectedStorageModels.value)
  let changed = false
  const apply = (key: string) => {
    if (state.visited.has(key)) return
    state.visited.add(key)
    const item = allStorageModelsByKey.value.get(key)
    if (!item || (state.targetSelected && !canSelectStorageItem(item)) || next.has(key) === state.targetSelected) return
    if (state.targetSelected) next.add(key)
    else next.delete(key)
    changed = true
  }
  apply(state.startKey)
  const viewport = list.getBoundingClientRect()
  for (const element of list.querySelectorAll<HTMLElement>('[data-storage-key]')) {
    const key = element.dataset.storageKey!
    if (state.visited.has(key)) continue
    const item = allStorageModelsByKey.value.get(key)
    if (!item || (state.targetSelected ? !canSelectStorageItem(item) : !next.has(key))) continue
    const bounds = element.getBoundingClientRect()
    const rect = { left: Math.max(bounds.left, viewport.left), top: Math.max(bounds.top, viewport.top),
      right: Math.min(bounds.right, viewport.right), bottom: Math.min(bounds.bottom, viewport.bottom) }
    if (rect.right > rect.left && rect.bottom > rect.top && segmentIntersectsRect(previous, point, rect)) apply(key)
  }
  if (changed) selectedStorageModels.value = [...next]
  state.previous = point
  state.scrollTop = list.scrollTop
}
function scrollStorageDrag(time: number) {
  storageScrollFrame = null
  const state = storageDrag, list = storageList.value
  if (!state?.active || !list || storageBusy.value) return
  const speed = edgeScrollVelocity(state.previous, list.getBoundingClientRect())
  if (!speed) return
  const elapsed = storageScrollTime ? Math.min(32, time - storageScrollTime) : 16
  storageScrollTime = time
  const before = list.scrollTop
  list.scrollTop += speed * elapsed / 1000
  paintStorageSelection(state.previous)
  if (list.scrollTop !== before) storageScrollFrame = requestAnimationFrame(scrollStorageDrag)
}
function handleStoragePointerMove(event: PointerEvent) {
  const state = storageDrag
  if (!state || event.pointerId !== state.pointerId) return
  if (!(event.buttons & 1) || storageBusy.value || document.querySelector('.n-dialog, .n-modal-mask')) { finishStorageDrag(); return }
  const point = { x: event.clientX, y: event.clientY }
  if (!state.active && Math.hypot(point.x - state.origin.x, point.y - state.origin.y) < 5) return
  state.active = true
  event.preventDefault()
  window.getSelection()?.removeAllRanges()
  document.documentElement.classList.add('storage-drag-selecting')
  paintStorageSelection(point)
  if (storageScrollFrame === null) { storageScrollTime = 0; storageScrollFrame = requestAnimationFrame(scrollStorageDrag) }
}
function handleStorageScroll() { if (storageDrag?.active) paintStorageSelection(storageDrag.previous) }
function finishStorageDrag(event?: PointerEvent) {
  if (event && storageDrag && event.pointerId !== storageDrag.pointerId) return
  if (storageDrag && (storageDrag.active || event?.type !== 'pointerup')) storageSuppressClick = true
  storageDrag = null
  if (storageScrollFrame !== null) cancelAnimationFrame(storageScrollFrame)
  storageScrollFrame = null
  storageScrollTime = 0
  document.documentElement.classList.remove('storage-drag-selecting')
}
function cancelStorageDrag() { finishStorageDrag() }
function storagePointerOut(event: PointerEvent) { if (!event.relatedTarget) finishStorageDrag(event) }
function storageDragKeydown(event: KeyboardEvent) { if (event.key === 'Escape') finishStorageDrag() }
function attachStorageListeners() {
  if (storageListenersAttached) return
  storageListenersAttached = true
  window.addEventListener('pointermove', handleStoragePointerMove, { passive: false })
  window.addEventListener('pointerup', finishStorageDrag)
  window.addEventListener('pointercancel', finishStorageDrag)
  window.addEventListener('pointerout', storagePointerOut)
  window.addEventListener('blur', cancelStorageDrag)
  window.addEventListener('resize', cancelStorageDrag)
  window.addEventListener('keydown', storageDragKeydown)
}
function detachStorageListeners() {
  finishStorageDrag()
  storageListenersAttached = false
  window.removeEventListener('pointermove', handleStoragePointerMove)
  window.removeEventListener('pointerup', finishStorageDrag)
  window.removeEventListener('pointercancel', finishStorageDrag)
  window.removeEventListener('pointerout', storagePointerOut)
  window.removeEventListener('blur', cancelStorageDrag)
  window.removeEventListener('resize', cancelStorageDrag)
  window.removeEventListener('keydown', storageDragKeydown)
}
watch(showStorage, show => { if (show) attachStorageListeners(); else detachStorageListeners() })
watch(storageBusy, busy => { if (busy) finishStorageDrag() })
watch([storageSearch, storageSort, storageDownloadedOnly], () => {
  finishStorageDrag()
  if (storageList.value) storageList.value.scrollTop = 0
})
watch(allStorageModels, () => {
  finishStorageDrag()
  selectedStorageModels.value = selectedStorageModels.value.filter(key => (allStorageModelsByKey.value.get(key)?.sizeBytes || 0) > 0)
})
onBeforeUnmount(detachStorageListeners)
onDeactivated(detachStorageListeners)
onActivated(() => { if (showStorage.value) attachStorageListeners() })

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
    { key: 'inference', label: t('models.editInferenceDefaults'), disabled: !canEditModelParameters(model) },
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

/* ---- Model parameter helpers (shared by the single and batch editors) ---- */

/** Native key each custom intent field writes; used to report fields a model does not have. */
const INTENT_NATIVE_KEYS: Record<string, string> = {
  chunkMilliseconds: 'chunk_size',
  overlapBasisPoints: 'overlap_size',
  batchSize: 'batch_size',
  standardize: 'standardize',
  normalize: 'normalize',
}

function findModelEntry(name: string | null | undefined): ModelEntry | null {
  if (!name) return null
  if (selectedInfo.value?.name === name) return selectedInfo.value
  return modelStore.models.find(item => item.name === name) || null
}

function resolutionOf(model: ModelEntry | null | undefined): ParameterResolutionMetadata | undefined {
  return model?.inferenceParamMeta?.resolution
}

function cloneNativeProfile(value: Record<string, unknown> | null | undefined): Record<string, number | boolean> {
  const next: Record<string, number | boolean> = {}
  for (const [key, item] of Object.entries(value || {})) {
    if (typeof item === 'boolean' || (typeof item === 'number' && Number.isFinite(item))) next[key] = item
  }
  return next
}

function sameNativeProfile(a: Record<string, unknown> | null | undefined, b: Record<string, unknown> | null | undefined) {
  const left = cloneNativeProfile(a), right = cloneNativeProfile(b)
  const keys = new Set([...Object.keys(left), ...Object.keys(right)])
  for (const key of keys) if (left[key] !== right[key]) return false
  return true
}

/** Native keys of intent fields that this model cannot take: absent from its parameters or not editable. */
function intentProblems(metadata: ParameterResolutionMetadata | undefined, parameters: NativeParameters, intent: CustomParameterIntent) {
  const keys = [
    ...Object.keys(intent).filter(key => key !== 'vr').map(key => INTENT_NATIVE_KEYS[key] || key),
    ...Object.keys(intent.vr || {}),
  ]
  return keys.filter((key) => {
    if (!Object.prototype.hasOwnProperty.call(parameters, key)) return true
    const kind = metadata?.capabilities[key]?.kind
    return kind !== 'free' && kind !== 'lattice'
  })
}

/** Has saved model-library overrides — this is a marker only and never changes precedence. */
function hasInferenceOverride(model: ModelEntry | null | undefined) {
  const overrides = model?.name ? modelStore.getModelInferenceOverrides(model.name) : undefined
  return Boolean(overrides && Object.keys(overrides).length)
}

/** Only installed, supported models that are not being removed or rewritten can take parameter edits. */
function canEditModelParameters(model: ModelEntry | null | undefined) {
  if (!model || !model.supported || !model.downloaded) return false
  if (isDeletingModel(model.name)) return false
  const task = downloadTasks.value[model.name]
  return !(task && task.status !== 'done')
}

/** An empty profile means "no saved overrides", which removes the entry (and its blue marker). */
function toLibraryOverrides(profile: NativeParameters | undefined): ModelDefaultInferenceParams | undefined {
  if (!profile || !Object.keys(profile).length) return undefined
  const next: Record<string, number | boolean> = { ...profile }
  return next as ModelDefaultInferenceParams
}

function errorText(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

function isRevisionConflict(error: unknown) {
  return errorText(error).includes('MODEL_PARAMETERS_CHANGED')
}

async function mapWithConcurrency<T>(items: string[], limit: number, worker: (item: string) => Promise<T>) {
  const results: PromiseSettledResult<T>[] = new Array(items.length)
  let cursor = 0
  async function next(): Promise<void> {
    while (cursor < items.length) {
      const index = cursor++
      try { results[index] = { status: 'fulfilled', value: await worker(items[index]!) } }
      catch (reason) { results[index] = { status: 'rejected', reason } }
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, () => next()))
  return results
}

function formatNativeValue(key: string, value: number | boolean | undefined, sampleRate?: number) {
  if (value === undefined) return '—'
  if (typeof value === 'boolean') return value ? t('parameterEditor.modelLibrary.on') : t('parameterEditor.modelLibrary.off')
  const text = Number(value.toFixed(6)).toLocaleString(undefined, { maximumFractionDigits: 6 })
  if ((key === 'chunk_size' || key === 'overlap_size') && sampleRate) {
    const seconds = Number((value / sampleRate).toFixed(3)).toLocaleString(undefined, { maximumFractionDigits: 3 })
    return t('parameterEditor.modelLibrary.samplesWithSeconds', { samples: text, seconds })
  }
  return text
}

/* ---- Single model editor ---- */

async function loadInferenceEditorMetadata(force = false) {
  const name = inferenceEditorName.value
  if (!name) return
  inferenceEditorLoading.value = true
  inferenceEditorLoadError.value = ''
  try {
    await modelStore.ensureModelMetadata(name, force)
  } catch (error) {
    if (inferenceEditorName.value === name) inferenceEditorLoadError.value = errorText(error)
  } finally {
    if (inferenceEditorName.value === name) inferenceEditorLoading.value = false
  }
}

function resetInferenceEditorDraft() {
  const name = inferenceEditorName.value
  inferenceLibraryDraft.value = cloneNativeProfile(modelStore.getModelInferenceOverrides(name))
  inferenceEditorRevision.value = modelStore.modelInferenceRevision
}

/** Clear every saved override in the draft (applied on Save, discarded on Cancel). */
function restoreAllInferenceDefaults() {
  inferenceLibraryDraft.value = {}
  inferenceEditorIntent.value = {}
}

async function saveInferenceDefaults() {
  const name = inferenceEditorName.value
  const metadata = inferenceEditorMeta.value
  if (!name || !metadata || inferenceEditorBlocked.value) return
  let profile: NativeParameters
  try {
    profile = applyLibraryIntent(metadata, inferenceLibraryDraft.value, inferenceEditorIntent.value)
  } catch (error) {
    studioLog.warn('Model parameter draft did not resolve', error)
    message.warning(t('parameterEditor.modelLibrary.draftInvalid'))
    return
  }
  inferenceEditorSaving.value = true
  try {
    const refreshed = await modelStore.ensureModelMetadata(name, true)
    if (resolutionOf(refreshed)?.fingerprint !== metadata.fingerprint) {
      message.warning(t('parameterEditor.parametersChanged'))
      return
    }
    await modelStore.replaceModelInferenceOverridesBatch(
      [{ name, overrides: toLibraryOverrides(profile) }],
      inferenceEditorRevision.value,
    )
    inferenceEditorSaving.value = false
    closeInferenceEditor()
    message.success(t('models.inferenceDefaultsSaved'))
  } catch (error) {
    if (isRevisionConflict(error)) {
      // Someone else saved meanwhile: rebase the draft on the stored values and let the user review again.
      resetInferenceEditorDraft()
      message.warning(t('parameterEditor.modelLibrary.revisionChanged'))
      return
    }
    studioLog.error('Failed to persist model inference defaults', error)
    message.error(t('models.inferenceDefaultsSaveFailed'))
  } finally {
    inferenceEditorSaving.value = false
  }
}

/* ---- Batch editor ---- */

const editableBatchSelection = computed(() => batchSelection.value.filter(name => canEditModelParameters(findModelEntry(name))))
const batchSelectableOnPage = computed(() => pagedModels.value.filter(model => canEditModelParameters(model) || isBatchSelected(model.name)))
const batchSelectedOnPage = computed(() => pagedModels.value.filter(model => isBatchSelected(model.name)).length)
const batchOutsidePage = computed(() => batchSelection.value.length - batchSelectedOnPage.value)
const allBatchPageSelected = computed(() => batchSelectableOnPage.value.length > 0 && batchSelectableOnPage.value.every(model => isBatchSelected(model.name)))
const someBatchPageSelected = computed(() => batchSelectedOnPage.value > 0 && !allBatchPageSelected.value)

function isBatchSelected(name: string) {
  return batchSelectionSet.value.has(name)
}

function setBatchSelected(name: string, checked: boolean) {
  if (checked && !canEditModelParameters(modelStore.models.find(model => model.name === name))) return
  if (isBatchSelected(name) === checked) return
  const next = batchSelection.value.filter(item => item !== name)
  if (checked) next.push(name)
  batchSelection.value = next
}

function toggleBatchSelectMode() {
  downloadSelectMode.value = false
  downloadSelection.value = []
  finishBatchDragSelection()
  batchSelectMode.value = !batchSelectMode.value
  if (batchSelectMode.value) showDetail.value = false
  if (!batchSelectMode.value) batchSelection.value = []
}

function selectEditableOnPage(checked: boolean) {
  finishBatchDragSelection()
  const next = new Set(batchSelection.value)
  for (const model of pagedModels.value) {
    if (!checked) next.delete(model.name)
    else if (canEditModelParameters(model)) next.add(model.name)
  }
  batchSelection.value = [...next]
}

function handleModelCardClick(model: ModelEntry, event: MouseEvent | KeyboardEvent) {
  if (event.type === 'keydown') suppressBatchClick = false
  if (downloadSelectMode.value) {
    setDownloadSelected(model, !isDownloadSelected(model))
    return
  }
  if (!batchSelectMode.value) { selectModel(model); return }
  if (event.type === 'click' && window.getSelection()?.toString()) return
  setBatchSelected(model.name, !isBatchSelected(model.name))
}

function resetBatchClickSuppression() { suppressBatchClick = false }

function consumeBatchClick(event: MouseEvent) {
  if (!suppressBatchClick || event.detail === 0) { suppressBatchClick = false; return }
  suppressBatchClick = false
  event.preventDefault()
  event.stopPropagation()
}

function startBatchDragSelection(event: PointerEvent, model: ModelEntry) {
  if (!batchSelectMode.value || event.pointerType !== 'mouse' || event.button !== 0) return
  if (!canEditModelParameters(model) && !isBatchSelected(model.name)) return
  finishBatchDragSelection()
  suppressBatchClick = false
  const origin = { x: event.clientX, y: event.clientY }
  batchDragSelection = { pointerId: event.pointerId, startName: model.name, origin, previous: origin,
    targetSelected: !isBatchSelected(model.name), active: false, visited: new Set() }
}

function handleBatchPointerMove(event: PointerEvent) {
  const state = batchDragSelection
  if (!state || event.pointerId !== state.pointerId) return
  if ((event.buttons & 1) === 0 || !batchSelectMode.value || document.querySelector('[role="dialog"], .n-modal-mask')) {
    finishBatchDragSelection(event)
    return
  }
  const point = { x: event.clientX, y: event.clientY }
  if (!state.active && Math.hypot(point.x - state.origin.x, point.y - state.origin.y) < 5) return
  state.active = true
  event.preventDefault()
  window.getSelection()?.removeAllRanges()
  document.documentElement.classList.add('models-drag-selecting')
  const next = new Set(batchSelection.value)
  const entries = new Map(pagedModels.value.map(model => [model.name, model]))
  const apply = (name: string) => {
    if (state.visited.has(name)) return
    state.visited.add(name)
    const model = entries.get(name)
    if (state.targetSelected) { if (model && canEditModelParameters(model)) next.add(name) }
    else next.delete(name)
  }
  apply(state.startName)
  const viewport = modelGrid.value?.closest('.app-content')?.getBoundingClientRect()
  for (const element of modelGrid.value?.querySelectorAll<HTMLElement>('[data-batch-model]') ?? []) {
    const name = element.dataset.batchModel!
    if (state.visited.has(name)) continue
    const bounds = element.getBoundingClientRect()
    const rect = { left: Math.max(bounds.left, viewport?.left ?? 0), top: Math.max(bounds.top, viewport?.top ?? 0),
      right: Math.min(bounds.right, viewport?.right ?? window.innerWidth), bottom: Math.min(bounds.bottom, viewport?.bottom ?? window.innerHeight) }
    if (rect.right > rect.left && rect.bottom > rect.top && segmentIntersectsRect(state.previous, point, rect)) apply(name)
  }
  if (next.size !== batchSelectionSet.value.size || [...next].some(name => !batchSelectionSet.value.has(name))) batchSelection.value = [...next]
  state.previous = point
}

function finishBatchDragSelection(event?: PointerEvent) {
  const state = batchDragSelection
  if (event && state && event.pointerId !== state.pointerId) return
  if (state && (state.active || event?.type !== 'pointerup')) suppressBatchClick = true
  batchDragSelection = null
  document.documentElement.classList.remove('models-drag-selecting')
}

function handleBatchWindowChange() { finishBatchDragSelection() }
function handleBatchKeydown(event: KeyboardEvent) {
  if (event.defaultPrevented || event.key !== 'Escape' || !batchSelectMode.value || document.querySelector('[role="dialog"], .n-modal-mask')) return
  event.preventDefault()
  toggleBatchSelectMode()
}

function attachBatchListeners() {
  if (batchListenersAttached) return
  batchListenersAttached = true
  window.addEventListener('pointermove', handleBatchPointerMove, { passive: false })
  window.addEventListener('pointerup', finishBatchDragSelection)
  window.addEventListener('pointercancel', finishBatchDragSelection)
  window.addEventListener('blur', handleBatchWindowChange)
  window.addEventListener('resize', handleBatchWindowChange)
  window.addEventListener('keydown', handleBatchKeydown)
}
function detachBatchListeners() {
  finishBatchDragSelection()
  if (!batchListenersAttached) return
  batchListenersAttached = false
  window.removeEventListener('pointermove', handleBatchPointerMove)
  window.removeEventListener('pointerup', finishBatchDragSelection)
  window.removeEventListener('pointercancel', finishBatchDragSelection)
  window.removeEventListener('blur', handleBatchWindowChange)
  window.removeEventListener('resize', handleBatchWindowChange)
  window.removeEventListener('keydown', handleBatchKeydown)
}
watch(batchSelectMode, enabled => { if (enabled && modelPageActive) attachBatchListeners(); else detachBatchListeners() })
watch([pagedModels, modelViewMode], () => finishBatchDragSelection())
onActivated(() => { modelPageActive = true; if (batchSelectMode.value) attachBatchListeners() })
onDeactivated(() => { modelPageActive = false; detachBatchListeners() })
onBeforeUnmount(detachBatchListeners)

const batchReferenceOptions = computed(() => batchSelection.value
  .filter(name => resolutionOf(findModelEntry(name)))
  .map(name => ({ label: name, value: name })))
const batchReferenceModel = computed(() => findModelEntry(batchReferenceName.value))
const batchReferenceMeta = computed(() => resolutionOf(batchReferenceModel.value))
const batchMetadataLoading = computed(() => Object.values(batchLoadState.value).includes('loading'))
const batchIntentEmpty = computed(() => {
  const { vr, ...rest } = batchIntent.value
  return !Object.keys(rest).length && !Object.keys(vr || {}).length
})

function pickBatchReference() {
  if (batchReferenceOptions.value.some(option => option.value === batchReferenceName.value)) return
  batchReferenceName.value = batchReferenceOptions.value[0]?.value || ''
}

async function loadBatchMetadata(names: string[], force = false) {
  const session = batchSession
  batchLoadState.value = { ...batchLoadState.value, ...Object.fromEntries(names.map(name => [name, 'loading' as const])) }
  const results = await mapWithConcurrency(names, BATCH_METADATA_CONCURRENCY, name => modelStore.ensureModelMetadata(name, force))
  if (session !== batchSession) return results
  const next = { ...batchLoadState.value }
  results.forEach((result, index) => {
    const name = names[index]!
    if (result.status === 'fulfilled') delete next[name]
    else {
      next[name] = 'error'
      studioLog.warn(`Failed to load parameter metadata for ${name}`, result.reason)
    }
  })
  batchLoadState.value = next
  pickBatchReference()
  return results
}

function openBatchEditor() {
  if (!batchSelection.value.length) return
  batchSession += 1
  batchIntent.value = {}
  batchPreview.value = null
  batchExcluded.value = []
  batchStaleNames.value = []
  batchLoadState.value = {}
  batchReferenceName.value = ''
  showBatchEditor.value = true
  const names = batchSelection.value.filter(name => canEditModelParameters(findModelEntry(name)))
  pickBatchReference()
  void loadBatchMetadata(names)
}

function closeBatchEditor() {
  if (batchApplying.value) return
  batchSession += 1
  showBatchEditor.value = false
  batchPreview.value = null
}

function handleBatchEditorUpdate(value: boolean) {
  if (!value) closeBatchEditor()
}

function buildBatchRow(name: string, intent: CustomParameterIntent, kind: BatchPreview['kind']): BatchPreviewRow {
  const model = findModelEntry(name)
  const row: BatchPreviewRow = { name, status: 'ok', changes: [], fields: [] }
  if (!canEditModelParameters(model)) return { ...row, status: 'ineligible' }
  const load = batchLoadState.value[name]
  if (load === 'loading') return { ...row, status: 'loading' }
  const metadata = resolutionOf(model)
  if (!metadata) return { ...row, status: load === 'error' ? 'loadFailed' : 'metadataMissing' }
  row.fingerprint = metadata.fingerprint
  row.sampleRate = metadata.sampleRate
  const library = cloneNativeProfile(modelStore.getModelInferenceOverrides(name))
  // Reset removes each model's overrides; the reference model and shared draft do not participate.
  let profile: NativeParameters = {}
  if (kind === 'edit') {
    const requested = resolveParameters(metadata, library, 'custom', intent)
    if (requested.issues.length) return { ...row, status: 'invalid', fields: [...new Set(requested.issues.map(issue => issue.key))] }
    const unsupported = [...new Set([...requested.skipped, ...intentProblems(metadata, requested.parameters, intent)])]
    if (unsupported.length) return { ...row, status: 'notApplicable', fields: unsupported }
    try { profile = applyLibraryIntent(metadata, library, intent) }
    catch { return { ...row, status: 'invalid', fields: [] } }
  }
  const before = resolveParameters(metadata, library, 'model', {}).parameters
  const after = resolveParameters(metadata, profile, 'model', {})
  if (after.issues.length) return { ...row, status: 'invalid', fields: [...new Set(after.issues.map(issue => issue.key))] }
  const keys = new Set([...Object.keys(before), ...Object.keys(after.parameters)])
  for (const key of keys) {
    if (before[key] !== after.parameters[key]) row.changes.push({ key, before: before[key], after: after.parameters[key] })
  }
  row.profile = profile
  if (!row.changes.length && sameNativeProfile(profile, library)) row.status = 'unchanged'
  return row
}

function buildBatchPreview(kind: BatchPreview['kind']) {
  if (batchApplying.value || batchMetadataLoading.value || (kind === 'edit' && batchIntentEmpty.value)) return
  const intent = kind === 'reset' ? {} : JSON.parse(JSON.stringify(batchIntent.value)) as CustomParameterIntent
  batchPreview.value = {
    kind,
    revision: modelStore.modelInferenceRevision,
    intent,
    rows: batchSelection.value.map(name => buildBatchRow(name, intent, kind)),
  }
  batchStaleNames.value = []
  batchExcluded.value = batchExcluded.value.filter(name => batchSelection.value.includes(name))
}

function previewBatch() {
  buildBatchPreview(batchPreview.value?.kind || 'edit')
}

function previewBatchReset() {
  if (batchMetadataLoading.value || batchApplying.value) return
  batchExcluded.value = []
  buildBatchPreview('reset')
}

function backToBatchEdit() {
  batchPreview.value = null
  batchStaleNames.value = []
}

function isBatchRowBlocking(row: BatchPreviewRow) {
  return row.status !== 'ok' && row.status !== 'unchanged'
}

function setBatchExcluded(name: string, excluded: boolean) {
  const next = batchExcluded.value.filter(item => item !== name)
  if (excluded) next.push(name)
  batchExcluded.value = next
}

async function retryBatchMetadata(name: string) {
  await loadBatchMetadata([name], true)
  if (batchPreview.value) previewBatch()
}

const batchIncludedRows = computed(() => (batchPreview.value?.rows || []).filter(row => !batchExcluded.value.includes(row.name)))
const batchBlockingRows = computed(() => batchIncludedRows.value.filter(isBatchRowBlocking))
const batchChangedRows = computed(() => batchIncludedRows.value.filter(row => row.status === 'ok'))
const batchDefaultCount = computed(() => batchIncludedRows.value.filter(row => row.status === 'unchanged').length)
const batchPreviewStale = computed(() => Boolean(batchPreview.value
  && (batchPreview.value.revision !== modelStore.modelInferenceRevision || batchStaleNames.value.length)))
const canApplyBatch = computed(() => Boolean(batchPreview.value)
  && !batchPreviewStale.value
  && !batchBlockingRows.value.length
  && batchChangedRows.value.length > 0
  && !batchApplying.value)

const BATCH_STATUS_KEYS: Record<BatchRowStatus | 'stale', string> = {
  ok: 'parameterEditor.batch.status.ok',
  unchanged: 'parameterEditor.batch.status.unchanged',
  ineligible: 'parameterEditor.batch.status.ineligible',
  loading: 'parameterEditor.batch.status.loading',
  loadFailed: 'parameterEditor.batch.status.loadFailed',
  metadataMissing: 'parameterEditor.batch.status.metadataMissing',
  invalid: 'parameterEditor.batch.status.invalid',
  notApplicable: 'parameterEditor.batch.status.notApplicable',
  stale: 'parameterEditor.batch.status.stale',
}
const BATCH_REASON_KEYS: Partial<Record<BatchRowStatus, string>> = {
  ineligible: 'parameterEditor.batch.reason.ineligible',
  loading: 'parameterEditor.batch.reason.loading',
  loadFailed: 'parameterEditor.batch.reason.loadFailed',
  metadataMissing: 'parameterEditor.batch.reason.metadataMissing',
  invalid: 'parameterEditor.batch.reason.invalid',
  notApplicable: 'parameterEditor.batch.reason.notApplicable',
}

function batchStatusLabel(row: BatchPreviewRow) {
  if (batchPreview.value?.kind === 'reset' && !batchStaleNames.value.includes(row.name)) {
    if (row.status === 'ok') return t('parameterEditor.batch.status.willReset')
    if (row.status === 'unchanged') return t('parameterEditor.batch.status.alreadyDefault')
  }
  return t(BATCH_STATUS_KEYS[batchStaleNames.value.includes(row.name) ? 'stale' : row.status])
}

/** Why a row blocks the batch; the field list names the native keys involved. */
function batchReason(row: BatchPreviewRow) {
  const key = BATCH_REASON_KEYS[row.status]
  if (!key) return ''
  const fields = row.fields.join(', ')
  return fields ? `${t(key)} ${t('parameterEditor.batch.reasonFields', { fields })}` : t(key)
}

function batchStatusType(row: BatchPreviewRow) {
  if (batchStaleNames.value.includes(row.name)) return 'warning'
  if (row.status === 'ok') return 'success'
  if (row.status === 'unchanged') return 'default'
  if (row.status === 'loading') return 'info'
  return 'error'
}

async function applyBatch() {
  const preview = batchPreview.value
  if (!preview || !canApplyBatch.value) return
  const rows = batchChangedRows.value
  batchApplying.value = true
  try {
    if (preview.revision !== modelStore.modelInferenceRevision) {
      message.warning(t('parameterEditor.batch.revisionChanged'))
      return
    }
    // Re-read each model's metadata so a config edited after the preview cannot receive stale native values.
    const names = rows.map(row => row.name)
    const results = await mapWithConcurrency(names, BATCH_METADATA_CONCURRENCY, name => modelStore.ensureModelMetadata(name, true))
    if (batchPreview.value !== preview) return
    const stale = rows.filter((row, index) => {
      const result = results[index]!
      return result.status !== 'fulfilled' || resolutionOf(result.value)?.fingerprint !== row.fingerprint
        || !canEditModelParameters(findModelEntry(row.name))
    }).map(row => row.name)
    if (stale.length) {
      batchStaleNames.value = stale
      message.warning(t('parameterEditor.batch.metadataChanged', { count: stale.length }))
      return
    }
    await modelStore.replaceModelInferenceOverridesBatch(
      rows.map(row => ({
        name: row.name,
        overrides: toLibraryOverrides(row.profile),
      })),
      preview.revision,
    )
    message.success(t(preview.kind === 'reset' ? 'parameterEditor.batch.restored' : 'parameterEditor.batch.applied', { count: rows.length }))
    batchApplying.value = false
    closeBatchEditor()
    batchSelection.value = []
    batchSelectMode.value = false
  } catch (error) {
    if (isRevisionConflict(error)) {
      message.warning(t('parameterEditor.batch.revisionChanged'))
      return
    }
    studioLog.error('Failed to apply batch model parameters', error)
    message.error(t('parameterEditor.batch.applyFailed'))
  } finally {
    batchApplying.value = false
  }
}

// Drop selections whose model disappeared (directory change, removal) so the batch never targets ghosts.
watch(() => modelStore.models.map(model => model.name), (names) => {
  const known = new Set(names)
  if (batchSelection.value.some(name => !known.has(name))) batchSelection.value = batchSelection.value.filter(name => known.has(name))
})

// Editing the shared intent after a preview invalidates that preview.
watch(batchIntent, () => { if (batchPreview.value && !batchApplying.value) backToBatchEdit() }, { deep: true })

function storageModelTypeLabel(item: StorageListItem) {
  if (item.kind === 'catalog') return isDeletingModel(item.id) ? t('models.deleting') : item.downloaded ? t('models.downloaded') : t('models.notDownloaded')
  if (item.role === 'vad') return t('models.storageAsrVadModel')
  if (item.role === 'punctuation') return t('models.storageAsrPunctuationModel')
  return t('models.storageAsrRecognitionModel')
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
  if (!canEditModelParameters(model)) {
    message.warning(t('parameterEditor.modelLibrary.notEditable'))
    return
  }
  inferenceEditorName.value = model.name
  inferenceEditorIntent.value = {}
  inferenceEditorLoadError.value = ''
  resetInferenceEditorDraft()
  showInferenceEditor.value = true
  // Loads metadata only; it never changes the model selected for separation.
  if (!resolutionOf(model)) void loadInferenceEditorMetadata()
}

function closeInferenceEditor() {
  if (inferenceEditorSaving.value) return
  showInferenceEditor.value = false
  inferenceEditorName.value = ''
  inferenceEditorIntent.value = {}
  inferenceLibraryDraft.value = {}
  inferenceEditorLoading.value = false
}

function handleInferenceEditorUpdate(value: boolean) {
  if (!value) closeInferenceEditor()
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

// ---- Batch download: selection mode ----
const downloadSelectMode = ref(false)
const modelSelectionActive = computed(() => batchSelectMode.value || downloadSelectMode.value)
const downloadSelection = ref<string[]>([])
const downloadSelectionSet = computed(() => new Set(downloadSelection.value))
const downloadQueueSet = computed(() => new Set(downloadQueue.value))

function isQueuedDownload(modelName: string) {
  return downloadQueueSet.value.has(modelName)
}

/** Whether a model can be picked for a batch download: a catalog model with nothing in flight. */
function isDownloadSelectable(model: ModelEntry) {
  return modelStore.canQueueDownload(model.name) && !isQueuedDownload(model.name)
}

function isDownloadSelected(model: ModelEntry) {
  return downloadSelectionSet.value.has(model.name)
}

/** Selectable models across every page of the current filter, so "select all" means all results. */
const downloadSelectableModels = computed(() => sortedModels.value.filter(isDownloadSelectable))
const downloadSelectablePageModels = computed(() => pagedModels.value.filter(isDownloadSelectable))
const downloadSelectedModels = computed(() => modelStore.models.filter((model) => downloadSelectionSet.value.has(model.name)))
const downloadSelectedBytes = computed(() => downloadSelectedModels.value.reduce((sum, model) => sum + (model.sizeBytes || 0), 0))
const allDownloadsSelected = computed(() => downloadSelectableModels.value.length > 0
  && downloadSelectableModels.value.every((model) => downloadSelectionSet.value.has(model.name)))
const someDownloadsSelected = computed(() => !allDownloadsSelected.value
  && downloadSelectableModels.value.some((model) => downloadSelectionSet.value.has(model.name)))

function setDownloadSelected(model: ModelEntry, checked: boolean) {
  if (checked && !isDownloadSelectable(model)) return
  downloadSelection.value = checked
    ? Array.from(new Set([...downloadSelection.value, model.name]))
    : downloadSelection.value.filter((name) => name !== model.name)
}

function toggleDownloadSelectAll(checked: boolean) {
  downloadSelection.value = checked ? downloadSelectableModels.value.map((model) => model.name) : []
}

function selectDownloadPage() {
  downloadSelection.value = Array.from(new Set([...downloadSelection.value, ...downloadSelectablePageModels.value.map((model) => model.name)]))
}

function clearDownloadSelection() {
  downloadSelection.value = []
}

function toggleDownloadSelectMode() {
  finishBatchDragSelection()
  batchSelectMode.value = false
  batchSelection.value = []
  downloadSelectMode.value = !downloadSelectMode.value
  if (downloadSelectMode.value) showDetail.value = false
  else downloadSelection.value = []
}

// A selected model can finish downloading elsewhere (or start, or be removed); drop it so the
// count and total size only describe what the batch will actually fetch.
watch(() => modelStore.models.filter(isDownloadSelectable).map(model => model.name), names => {
  if (!downloadSelection.value.length) return
  const selectable = new Set(names)
  const next = downloadSelection.value.filter(name => selectable.has(name))
  if (next.length !== downloadSelection.value.length) downloadSelection.value = next
})

function confirmBatchDownload() {
  const names = downloadSelectedModels.value.filter(isDownloadSelectable).map((model) => model.name)
  if (!names.length) return
  dialog.info({
    title: t('models.batchDownloadConfirmTitle'),
    content: t('models.batchDownloadConfirmContent', {
      count: names.length,
      size: formatBytes(downloadSelectedBytes.value),
      concurrency: BATCH_DOWNLOAD_CONCURRENCY,
    }),
    positiveText: t('common.confirm'),
    negativeText: t('common.cancel'),
    onPositiveClick: () => {
      const added = modelStore.enqueueBatchDownload(names)
      if (added) message.success(t('models.batchDownloadQueued', { count: added }))
      downloadSelection.value = []
      downloadSelectMode.value = false
    },
  })
}

function removeQueuedModel(model: ModelEntry, event?: MouseEvent) {
  event?.stopPropagation()
  modelStore.removeQueuedDownload(model.name)
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
  await refreshStorage(false)
}

async function refreshStorage(force = true) {
  if (storageBusy.value) return
  finishStorageDrag()
  storageError.value = ''
  try {
    await modelStore.loadModelStorageSummary({ force })
  } catch (err) {
    storageError.value = errorText(err)
  }
}

function openModelDir() {
  const path = modelStorageSummary.value?.modelDir || modelDir.value || settings.modelDir
  if (path) void taskStore.revealPath(path)
}

function confirmBatchDelete() {
  finishStorageDrag()
  if (storageBusy.value) return
  const targets = selectedStorageModels.value
    .map(key => allStorageModelsByKey.value.get(key))
    .filter((item): item is StorageListItem => Boolean(item))
    .filter(canSelectStorageItem)
  if (!targets.length) return
  dialog.warning({
    title: t('models.storageBatchDeleteConfirmTitle'),
    content: t('models.storageBatchDeleteConfirmContent', {
      count: targets.length,
      size: formatBytes(targets.reduce((sum, item) => sum + item.sizeBytes, 0)),
    }),
    positiveText: t('models.storageBatchDelete'),
    negativeText: t('common.cancel'),
    onPositiveClick: () => {
      modelStore.deleteStorageModels(targets.map(({ kind, id, name, tool }) => ({ kind, id, name, tool }))).then(() => {
        const failed = new Set(modelStore.batchDeleteState.failedModels)
        selectedStorageModels.value = selectedStorageModels.value.filter((key) => {
          const item = allStorageModelsByKey.value.get(key)
          return Boolean(item && failed.has(item.name) && item.sizeBytes > 0)
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
  finishStorageDrag()
  if (storageBusy.value) return
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

watch(() => route.query.parameters, async value => {
  if (typeof value !== 'string' || !value.trim()) return
  try {
    if (!modelStore.modelsLoaded) await modelStore.loadModels()
    const entry = findModelEntry(value)
    if (entry) openInferenceEditor(entry)
    else message.warning(t('parameterEditor.modelLibrary.notEditable'))
    const query = { ...route.query }
    delete query.parameters
    await router.replace({ query })
  } catch (error) { message.error(errorText(error)) }
}, { immediate: true })
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
      <div
        v-if="batchSelectMode"
        class="header-actions batch-select-bar"
        role="region"
        :aria-label="t('parameterEditor.batch.title')"
      >
        <n-checkbox :checked="allBatchPageSelected" :indeterminate="someBatchPageSelected" :disabled="!batchSelectableOnPage.length"
          :title="t('parameterEditor.batch.selectHint')" @update:checked="selectEditableOnPage">{{ t('parameterEditor.batch.selectPage') }}</n-checkbox>
        <div class="batch-select-bar__selection">
          <span class="batch-select-bar__count" aria-live="polite" aria-atomic="true">
            {{ t('parameterEditor.batch.selectedCount', { count: batchSelection.length }) }}
            <span v-if="batchOutsidePage">{{ t('parameterEditor.batch.outsidePage', { count: batchOutsidePage }) }}</span>
          </span>
          <n-button v-if="batchSelection.length" text size="small" :title="t('parameterEditor.batch.clearAllPages')" @click="batchSelection = []">
            {{ t('parameterEditor.batch.clearSelection') }}
          </n-button>
        </div>
        <n-button type="primary" :disabled="!editableBatchSelection.length" @click="openBatchEditor">
          {{ t('parameterEditor.batch.editSelected') }}
        </n-button>
        <n-button secondary @click="toggleBatchSelectMode">{{ t('parameterEditor.batch.exitSelect') }}</n-button>
      </div>
      <div v-else class="header-actions" data-guide="model-management">
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
        <n-button
          :type="downloadSelectMode ? 'primary' : 'default'"
          secondary
          :aria-pressed="downloadSelectMode"
          @click="toggleDownloadSelectMode"
        >
          <template #icon><n-icon :component="downloadSelectMode ? CloseOutline : CheckboxOutline" /></template>
          {{ downloadSelectMode ? t('models.batchSelectExit') : t('models.batchSelect') }}
        </n-button>
        <n-button secondary @click="openStorageManager">
          <template #icon><n-icon :component="ServerOutline" /></template>
          {{ t('models.storageManage') }}
        </n-button>
        <n-button
          secondary
          :aria-pressed="false"
          @click="toggleBatchSelectMode"
        >
          {{ t('parameterEditor.batch.enterSelect') }}
        </n-button>
        <n-button secondary :loading="isLoading" data-guide="model-library-fallback" @click="loadModels">
          <template #icon><n-icon :component="RefreshOutline" /></template>
          {{ t('models.load') }}
        </n-button>
      </div>
    </div>

    <!-- Filter Toolbar -->
    <div class="toolbar" data-guide="model-filters">
      <div class="toolbar-row">
        <n-input
          v-model:value="search"
          :placeholder="t('models.search')"
          clearable
          class="search-input"
        >
          <template #prefix><n-icon :component="SearchOutline" /></template>
        </n-input>
        <ModelCategorySelect
          v-model:value="category"
          :options="categoryOptions"
          size="small"
          class="category-select"
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

    <!-- Batch selection bar -->
    <div v-if="downloadSelectMode" class="batch-bar" role="region" :aria-label="t('models.batchSelect')">
      <n-checkbox
        :checked="allDownloadsSelected"
        :indeterminate="someDownloadsSelected"
        :disabled="!downloadSelectableModels.length"
        @update:checked="toggleDownloadSelectAll"
      >
        {{ t('models.batchSelectAll', { count: downloadSelectableModels.length }) }}
      </n-checkbox>
      <n-button size="small" quaternary :disabled="!downloadSelectablePageModels.length" @click="selectDownloadPage">
        {{ t('models.batchSelectPage') }}
      </n-button>
      <n-button size="small" quaternary :disabled="!downloadSelection.length" @click="clearDownloadSelection">
        {{ t('models.batchClearSelection') }}
      </n-button>
      <span class="batch-bar__summary text-sm text-muted">
        <template v-if="downloadSelection.length">
          {{ t('models.batchSelectedSummary', { count: downloadSelection.length, size: formatBytes(downloadSelectedBytes) }) }}
        </template>
        <template v-else-if="!downloadSelectableModels.length">{{ t('models.batchNoDownloadable') }}</template>
        <template v-else>{{ t('models.batchSelectHint') }}</template>
      </span>
      <n-button size="small" type="primary" :disabled="!downloadSelection.length" @click="confirmBatchDownload">
        <template #icon><n-icon :component="CloudDownloadOutline" /></template>
        {{ t('models.batchDownloadAction', { count: downloadSelection.length }) }}
      </n-button>
    </div>

    <!-- Waiting batch downloads stay visible after leaving selection mode. -->
    <div v-if="downloadQueue.length" class="batch-queue-bar">
      <span class="text-sm">{{ t('models.downloadQueueSummary', { count: downloadQueue.length }) }}</span>
      <n-button size="tiny" quaternary @click="modelStore.clearDownloadQueue()">
        {{ t('models.clearDownloadQueue') }}
      </n-button>
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
        <div ref="modelGrid" :class="['model-grid', `model-grid--${modelViewMode}`]" @pointerdown.capture="resetBatchClickSuppression" @click.capture="consumeBatchClick">
          <div
            v-for="model in pagedModels"
            :key="model.name"
            :data-batch-model="model.name"
            :data-guide="model.supported ? 'model-library' : undefined"
            :class="['model-card', {
              'model-card--selected': !modelSelectionActive && selectedModel === model.name,
              'model-card--selecting': modelSelectionActive,
              'model-card--batch-selected': (batchSelectMode && isBatchSelected(model.name)) || (downloadSelectMode && isDownloadSelected(model)),
              'model-card--batch-ineligible': (batchSelectMode && !canEditModelParameters(model) && !isBatchSelected(model.name)) || (downloadSelectMode && !isDownloadSelectable(model)),
              'model-card--unsupported': !model.supported,
              'model-card--busy': isModelBusy(model)
            }]"
            tabindex="0"
            role="button"
            :aria-label="t(downloadSelectMode ? 'models.batchSelectModelAria' : batchSelectMode ? 'parameterEditor.batch.selectModelAria' : 'models.viewDetailAria', { name: model.name })"
            :aria-current="!modelSelectionActive && selectedModel === model.name ? 'true' : undefined"
            :aria-pressed="downloadSelectMode ? isDownloadSelected(model) : batchSelectMode ? isBatchSelected(model.name) : undefined"
            :aria-disabled="((batchSelectMode && !canEditModelParameters(model) && !isBatchSelected(model.name)) || (downloadSelectMode && !isDownloadSelectable(model))) ? true : undefined"
            :title="batchSelectMode && !canEditModelParameters(model) ? t('parameterEditor.modelLibrary.notEditable') : undefined"
            @click="handleModelCardClick(model, $event)"
            @contextmenu.stop.prevent="!modelSelectionActive && openModelContextMenu($event, model)"
            @keydown.enter.self.prevent="handleModelCardClick(model, $event)"
            @keydown.space.self.prevent="handleModelCardClick(model, $event)"
          >
          <div v-if="modelSelectionActive" class="mc-batch-select" @pointerdown.stop="startBatchDragSelection($event, model)" @click.stop>
            <n-checkbox v-if="batchSelectMode" class="mc-batch-check" :checked="isBatchSelected(model.name)"
              :disabled="!canEditModelParameters(model) && !isBatchSelected(model.name)"
              :title="canEditModelParameters(model) ? t('parameterEditor.batch.selectModel') : t('parameterEditor.modelLibrary.notEditable')"
              :aria-label="t('parameterEditor.batch.selectModelAria', { name: model.name })"
              @update:checked="(value: boolean) => setBatchSelected(model.name, value)" />
            <n-checkbox v-else class="mc-batch-check" :checked="isDownloadSelected(model)"
              :disabled="!isDownloadSelectable(model)"
              :aria-label="t('models.batchSelectModelAria', { name: model.name })"
              @update:checked="(value: boolean) => setDownloadSelected(model, value)" />
          </div>
          <!-- Card Header -->
          <div class="mc-header">
            <div class="mc-title">
              <span class="mc-name" :title="model.name"><n-tooltip v-if="hasInferenceOverride(model)" trigger="hover">
                <template #trigger>
                  <span
                    class="mc-override-dot"
                    role="img"
                    :aria-label="t('parameterEditor.modelLibrary.customizedBadge')"
                  />
                </template>
                {{ t('parameterEditor.modelLibrary.customizedTooltip') }}
              </n-tooltip><template v-for="(part, index) in modelNameParts(model.name)" :key="index">{{ part }}<wbr /></template></span>
              <span v-if="modelNote(model)" class="mc-note" :title="modelNote(model)">{{ modelNote(model) }}</span>
            </div>
            <div v-if="!modelSelectionActive" class="mc-controls">
              <n-button
                quaternary
                circle
                size="tiny"
                :type="isFavoriteModel(model) ? 'warning' : 'default'"
                :title="isFavoriteModel(model) ? t('models.unfavorite') : t('models.favorite')"
                :aria-label="isFavoriteModel(model) ? t('models.unfavorite') : t('models.favorite')"
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
          </div>
          <!-- Meta Tags -->
          <div class="mc-tags">
            <n-tag v-if="model.architecture" :bordered="false" size="tiny" type="info" round :title="model.architecture">
              {{ model.architecture }}
            </n-tag>
            <n-tag v-if="showModelTypeTag(model)" :bordered="false" size="tiny" round :title="model.modelType || undefined">
              {{ model.modelType }}
            </n-tag>
          </div>

          <!-- Stable field positions keep models comparable across each card row. -->
          <div class="mc-meta">
            <div class="mc-meta-item mc-meta-item--target">
              <span class="mc-label">{{ t('models.targetStem') }}</span>
              <span class="mc-value" :title="targetStemLabel(model)">{{ targetStemLabel(model) || '—' }}</span>
            </div>
            <div class="mc-meta-item mc-meta-item--category">
              <span class="mc-label">{{ t('models.category') }}</span>
              <span class="mc-value" :title="categoryLabel(model) || undefined">{{ categoryLabel(model) || '—' }}</span>
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

            <template v-else-if="!model.downloaded && isQueuedDownload(model.name)">
              <span class="mc-state mc-state--paused">
                <span class="mc-state__dot" />
                {{ t('models.downloadQueued') }}
              </span>
              <div class="mc-actions">
                <n-button size="tiny" quaternary @click.stop="removeQueuedModel(model, $event)">
                  {{ t('models.removeFromQueue') }}
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
        <div v-if="inferenceEditorName" class="model-inference-modal__body">
          <strong class="model-inference-modal__name">{{ inferenceEditorName }}</strong>
          <p class="model-inference-modal__hint">{{ t('parameterEditor.modelLibrary.hint') }}</p>
          <n-alert v-if="!canEditModelParameters(inferenceEditorModel)" type="warning" :bordered="false" class="model-inference-modal__alert">
            {{ t('parameterEditor.modelLibrary.notEditable') }}
          </n-alert>
          <div v-if="!inferenceEditorLoading && !inferenceEditorMeta" class="model-inference-modal__refresh">
            <n-alert :type="inferenceEditorLoadError ? 'error' : 'warning'" :bordered="false">
              {{ inferenceEditorLoadError
                ? t('parameterEditor.modelLibrary.metadataLoadFailed', { error: inferenceEditorLoadError })
                : t('parameterEditor.modelLibrary.metadataRefreshNeeded') }}
            </n-alert>
            <n-button size="small" secondary @click="loadInferenceEditorMetadata(true)">
              <template #icon><n-icon :component="RefreshOutline" /></template>
              {{ t('parameterEditor.modelLibrary.refreshMetadata') }}
            </n-button>
          </div>
          <InferenceParameterFields
            v-else
            :metadata="inferenceEditorMeta"
            :library="inferenceLibraryDraft"
            mode="custom"
            :intent="inferenceEditorIntent"
            :loading="inferenceEditorLoading"
            :disabled="!canEditModelParameters(inferenceEditorModel) || inferenceEditorSaving"
            @update:intent="inferenceEditorIntent = $event"
          />
        </div>
        <template #footer>
          <div class="model-inference-modal__footer">
            <n-button
              secondary
              :disabled="inferenceEditorSaving || (!Object.keys(inferenceLibraryDraft).length && !Object.keys(inferenceEditorIntent).length)"
              @click="restoreAllInferenceDefaults"
            >
              {{ t('models.inferenceDefaultsReset') }}
            </n-button>
            <span class="model-inference-modal__footer-spacer" />
            <n-button secondary :disabled="inferenceEditorSaving" @click="closeInferenceEditor">{{ t('common.cancel') }}</n-button>
            <n-button
              type="primary"
              :loading="inferenceEditorSaving"
              :disabled="inferenceEditorBlocked || !inferenceEditorDirty"
              @click="saveInferenceDefaults"
            >
              {{ t('common.save') }}
            </n-button>
          </div>
        </template>
      </n-card>
    </n-modal>

    <!-- Batch parameter editor: edit a shared sparse intent, preview per model, then save once. -->
    <n-modal :show="showBatchEditor" :mask-closable="!batchApplying" @update:show="handleBatchEditorUpdate">
      <n-card
        class="model-inference-modal model-batch-modal"
        :title="t('parameterEditor.batch.title')"
        :bordered="false"
        :closable="!batchApplying"
        role="dialog"
        aria-modal="true"
        :aria-label="t('parameterEditor.batch.title')"
        @close="closeBatchEditor"
      >
        <div class="model-inference-modal__body">
          <p class="model-inference-modal__hint" aria-live="polite">
            {{ batchPreview?.kind === 'reset'
              ? t('parameterEditor.batch.resetHint', { count: batchChangedRows.length, unchanged: batchDefaultCount })
              : t('parameterEditor.batch.hint', { count: batchSelection.length }) }}
          </p>

          <template v-if="!batchPreview">
            <div class="batch-reference-row">
              <span class="text-sm">{{ t('parameterEditor.batch.referenceModel') }}</span>
              <n-select
                v-model:value="batchReferenceName"
                size="small"
                :options="batchReferenceOptions"
                :loading="batchMetadataLoading"
                :placeholder="t('parameterEditor.batch.referencePlaceholder')"
                :consistent-menu-width="false"
              />
            </div>
            <p class="text-sm text-muted">{{ t('parameterEditor.batch.referenceHint') }}</p>
            <n-alert v-if="!batchMetadataLoading && !batchReferenceMeta" type="warning" :bordered="false">
              {{ t('parameterEditor.batch.noReference') }}
            </n-alert>
            <InferenceParameterFields
              v-else
              :metadata="batchReferenceMeta"
              :library="batchReferenceName ? modelStore.getModelInferenceOverrides(batchReferenceName) : undefined"
              mode="custom"
              :intent="batchIntent"
              :loading="batchMetadataLoading && !batchReferenceMeta"
              @update:intent="batchIntent = $event"
            />
          </template>

          <template v-else>
            <n-alert v-if="batchPreviewStale" type="warning" :bordered="false" class="model-inference-modal__alert">
              {{ t('parameterEditor.batch.previewStale') }}
            </n-alert>
            <n-alert v-else-if="batchBlockingRows.length" type="error" :bordered="false" class="model-inference-modal__alert">
              {{ t('parameterEditor.batch.blocked', { count: batchBlockingRows.length }) }}
            </n-alert>
            <div class="batch-preview-list">
              <div
                v-for="row in batchPreview.rows"
                :key="row.name"
                :class="['batch-preview-row', { 'batch-preview-row--excluded': batchExcluded.includes(row.name), 'batch-preview-row--default': batchPreview.kind === 'reset' && row.status === 'unchanged' }]"
              >
                <div class="batch-preview-row__head">
                  <strong class="batch-preview-row__name" :title="row.name">{{ row.name }}</strong>
                  <n-tag size="small" :bordered="false" round :type="batchStatusType(row)">{{ batchStatusLabel(row) }}</n-tag>
                  <span class="batch-select-bar__spacer" />
                  <n-button
                    v-if="row.status === 'loadFailed' || row.status === 'metadataMissing'"
                    size="tiny"
                    quaternary
                    :disabled="batchApplying"
                    @click="retryBatchMetadata(row.name)"
                  >
                    {{ t('parameterEditor.modelLibrary.refreshMetadata') }}
                  </n-button>
                  <n-button
                    size="tiny"
                    quaternary
                    :disabled="batchApplying"
                    @click="setBatchExcluded(row.name, !batchExcluded.includes(row.name))"
                  >
                    {{ batchExcluded.includes(row.name) ? t('parameterEditor.batch.include') : t('parameterEditor.batch.exclude') }}
                  </n-button>
                </div>
                <p v-if="isBatchRowBlocking(row)" class="batch-preview-row__reason">{{ batchReason(row) }}</p>
                <p v-else-if="batchPreview.kind === 'reset' && row.status === 'ok'" class="batch-preview-row__reason">
                  {{ t('parameterEditor.batch.clearOverrides') }}
                </p>
                <table v-if="row.changes.length" class="batch-preview-table">
                  <thead>
                    <tr>
                      <th>{{ t('parameterEditor.batch.columnField') }}</th>
                      <th>{{ t('parameterEditor.batch.columnBefore') }}</th>
                      <th>{{ t('parameterEditor.batch.columnAfter') }}</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="change in row.changes" :key="change.key">
                      <td><code>{{ change.key }}</code></td>
                      <td>{{ formatNativeValue(change.key, change.before, row.sampleRate) }}</td>
                      <td>{{ formatNativeValue(change.key, change.after, row.sampleRate) }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </template>
        </div>
        <template #footer>
          <div class="model-inference-modal__footer">
            <n-button v-if="!batchPreview" secondary :disabled="batchMetadataLoading || batchApplying" @click="previewBatchReset">
              {{ t('parameterEditor.batch.restoreDefaults') }}
            </n-button>
            <n-button v-if="batchPreview" secondary :disabled="batchApplying" @click="backToBatchEdit">
              {{ t('parameterEditor.batch.backToEdit') }}
            </n-button>
            <span class="model-inference-modal__footer-spacer" />
            <n-button secondary :disabled="batchApplying" @click="closeBatchEditor">{{ t('common.cancel') }}</n-button>
            <n-button
              v-if="!batchPreview || batchPreviewStale"
              type="primary"
              :disabled="(batchPreview?.kind !== 'reset' && batchIntentEmpty) || batchMetadataLoading || batchApplying"
              @click="previewBatch"
            >
              {{ batchPreview ? t('parameterEditor.batch.repreview') : t('parameterEditor.batch.preview') }}
            </n-button>
            <n-button v-else type="primary" :loading="batchApplying" :disabled="!canApplyBatch" @click="applyBatch">
              {{ t(batchPreview.kind === 'reset' ? 'parameterEditor.batch.confirmReset' : 'parameterEditor.batch.confirmApply', { count: batchChangedRows.length }) }}
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

    <n-drawer v-model:show="showStorage" width="min(880px, 100vw)" placement="right" class="storage-drawer"
      :style="{ top: storageDrawerTop, height: `calc(100% - ${storageDrawerTop})` }"
      :z-index="isTutorialFrame() ? 3500 : undefined" :trap-focus="!isTutorialFrame()">
      <n-drawer-content :title="t('models.storageTitle')" closable :body-style="{ overflow: 'hidden' }"
        :body-content-style="{ height: '100%', minHeight: 0, display: 'flex', flexDirection: 'column' }">
        <template #header>
          <div class="storage-heading">
            <span>{{ t('models.storageTitle') }}</span>
            <div class="storage-selection-bar" role="region" :aria-label="t('models.storageBatchDelete')">
              <n-checkbox :checked="allStorageSelected" :indeterminate="someStorageSelected" :disabled="storageBusy || !selectableStorageModels.length"
                :aria-disabled="storageBusy || !selectableStorageModels.length"
                :title="t('models.storageSelectionHint')" @update:checked="selectStorageResults">{{ t('models.storageSelectAll') }}</n-checkbox>
              <div class="storage-selection-bar__count" role="status" aria-live="polite" aria-atomic="true">
                <span>{{ t('models.storageSelected', { count: selectedStorageModels.length, size: formatStorageBytes(selectedStorageBytes) }) }}</span>
                <small v-if="storageOutsideSelection">{{ t('models.storageOutsideSelection', { count: storageOutsideSelection }) }}</small>
              </div>
              <n-button v-if="selectedStorageModels.length" text size="small" :disabled="storageBusy" @click="clearStorageSelection">{{ t('models.storageClearSelection') }}</n-button>
              <n-button class="storage-selection-bar__delete" size="small" type="error" secondary
                :disabled="!selectedStorageModels.length || hasDeletingSelectedStorageModels || storageBusy" @click="confirmBatchDelete">
                <template #icon><n-icon :component="TrashOutline" /></template>{{ t('models.storageDeleteSelected') }}
              </n-button>
            </div>
          </div>
        </template>
        <div class="storage-panel">
          <div class="storage-summary">
            <div class="storage-stat">
              <span>{{ t('models.storageTotal') }}</span>
              <strong>{{ formatStorageBytes(modelStorageSummary?.totalBytes || 0) }}</strong>
            </div>
            <div class="storage-stat">
              <span>{{ t('models.storageDownloadedCount') }}</span>
              <strong>{{ modelStorageSummary?.downloadedCount || 0 }}</strong>
            </div>
            <div class="storage-stat">
              <div class="storage-stat__heading">
                <span>{{ t('models.storageResidual') }}</span>
                <n-button text type="warning" size="tiny" :disabled="!modelStorageSummary?.residualFiles.length || storageBusy" @click="confirmCleanupResidual">{{ t('models.storageCleanup') }}</n-button>
              </div>
              <strong>{{ formatStorageBytes(modelStorageSummary?.residualBytes || 0) }}</strong>
            </div>
          </div>

          <div class="storage-dir">
            <span class="text-muted">{{ t('models.modelDir') }}:</span>
            <code :title="modelStorageSummary?.modelDir || modelDir || settings.modelDir">{{ modelStorageSummary?.modelDir || modelDir || settings.modelDir || '?' }}</code>
            <n-button size="small" quaternary :loading="storageLoading" :disabled="storageBusy" @click="refreshStorage()">
              <template #icon><n-icon :component="RefreshOutline" /></template>{{ t('models.storageRefresh') }}
            </n-button>
            <n-button size="small" quaternary @click="openModelDir">
              <template #icon><n-icon :component="FolderOpenOutline" /></template>{{ t('models.openModelDir') }}
            </n-button>
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
            <label class="storage-downloaded-toggle">
              <n-switch v-model:value="storageDownloadedOnly" size="small" :aria-label="t('models.downloadedOnly')" />
              <span class="text-sm text-muted">{{ t('models.downloadedOnly') }}</span>
            </label>
          </div>

          <n-alert v-if="storageError" type="error" :bordered="false">{{ storageError }}</n-alert>

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

          <div ref="storageList" class="storage-list" :aria-busy="storageLoading" @scroll="handleStorageScroll"
            @pointerdown.capture="storageSuppressClick = false" @click.capture="consumeStorageClick">
            <div
              v-for="item in storageModels"
              :key="item.selectionKey"
              class="storage-row"
              :class="{ 'storage-row--selected': storageSelection.has(item.selectionKey), 'storage-row--disabled': storageBusy || (!canSelectStorageItem(item) && !storageSelection.has(item.selectionKey)) }"
              :data-storage-key="item.selectionKey" role="button" :aria-pressed="storageSelection.has(item.selectionKey)"
              :aria-disabled="storageBusy || (!canSelectStorageItem(item) && !storageSelection.has(item.selectionKey))"
              :tabindex="storageBusy || (!canSelectStorageItem(item) && !storageSelection.has(item.selectionKey)) ? -1 : 0"
              @click="handleStorageRowClick(item)" @keydown.enter.self.prevent="toggleStorageRow(item)" @keydown.space.self.prevent="toggleStorageRow(item)"
            >
              <span class="storage-row__checkbox" @pointerdown="startStorageDrag($event, item)">
              <n-checkbox
                :checked="storageSelection.has(item.selectionKey)" :aria-label="t('models.storageSelectItem', { name: item.name })"
                :disabled="storageBusy || (!canSelectStorageItem(item) && !storageSelection.has(item.selectionKey))"
                :aria-disabled="storageBusy || (!canSelectStorageItem(item) && !storageSelection.has(item.selectionKey))"
                @update:checked="(checked: boolean) => setStorageSelected(item.selectionKey, checked)"
                @click.stop
              />
              </span>
              <div class="storage-row__main">
                <strong :title="item.name">{{ item.name }}</strong>
                <span>{{ t('models.storageFileCount', { count: item.fileCount }) }}<template v-if="!item.downloaded && item.expectedSizeBytes > item.sizeBytes"> · {{ t('models.storageExpectedSize', { size: formatBytes(item.expectedSizeBytes) }) }}</template></span>
              </div>
              <n-tag size="small" :bordered="false" :type="item.kind === 'tool' ? 'info' : isDeletingModel(item.id) ? 'warning' : item.downloaded ? 'success' : 'default'">
                {{ storageModelTypeLabel(item) }}
              </n-tag>
              <strong class="storage-row__size">{{ formatStorageBytes(item.sizeBytes) }}</strong>
            </div>
            <div v-if="storageLoading && !modelStorageSummary" class="empty-state storage-empty"><n-spin size="small" /></div>
            <div v-else-if="!storageModels.length" class="empty-state storage-empty">
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
}

.toolbar-row {
  display: grid;
  /* One column per control: search | category | source | sort | actions. A missing column would
     push the last control into an implicit one and squeeze the switch label onto two lines. */
  grid-template-columns: minmax(240px, 1fr) minmax(150px, 200px) minmax(110px, 150px) minmax(130px, 170px) auto;
  align-items: center;
  gap: 10px;
}

.search-input {
  max-width: none;
  min-width: 0;
}

.toolbar-actions {
  display: flex;
  align-items: center;
  gap: 7px;
  white-space: nowrap;
}

/* ===== Batch selection ===== */
.batch-bar,
.batch-queue-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 12px;
  margin-bottom: 12px;
  padding: 8px 12px;
  border-radius: 10px;
  border: 1px solid color-mix(in srgb, var(--primary) 30%, var(--outline));
  background: color-mix(in srgb, var(--primary-soft) 30%, var(--surface-1));
}

.batch-queue-bar {
  padding: 4px 12px;
  border-color: color-mix(in srgb, var(--outline) 58%, transparent);
  background: color-mix(in srgb, var(--surface-2) 40%, transparent);
}

.batch-bar__summary {
  flex: 1;
  min-width: 160px;
}

/* ===== Category Select ===== */
.category-select {
  width: 100%;
  min-width: 0;
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
  width: 100%;
  min-width: 0;
  flex-shrink: 0;
}

.sort-select {
  width: 100%;
  min-width: 0;
  flex-shrink: 0;
}

/* ===== Layout ===== */
.models-body {
  display: flex;
  flex-direction: column;
  container: model-library / inline-size;
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
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 270px), 1fr));
  align-items: stretch;
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
  min-height: 43px;
  padding-top: 10px;
  border-top: 1px solid color-mix(in srgb, var(--outline) 38%, transparent);
}

@supports (grid-template-rows: subgrid) {
  .model-grid--card .model-card {
    display: grid;
    grid-template-rows: subgrid;
    grid-row: span 4;
  }
}
.model-grid--card .mc-tags { min-height: 20px; flex-wrap: nowrap; }
.model-grid--card .mc-meta {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  grid-template-areas: "target target" "category usage";
  gap: 6px 12px;
}
.model-grid--card .mc-meta-item--target { grid-area: target; }
.model-grid--card .mc-meta-item--category { grid-area: category; }
.model-grid--card .mc-meta-item--usage { grid-area: usage; }
.model-grid--card .mc-meta-item:not(.mc-meta-item--usage) {
  display: grid;
  grid-template-columns: minmax(4em, max-content) minmax(0, 1fr);
}
.mc-meta-item--usage { color: var(--on-surface-muted); font-variant-numeric: tabular-nums; white-space: nowrap; }

/* ---- List: the original two-line row — the name over its details, with tags and actions
   riding on the right. Spreading the same content across one flex line left large gaps between
   the pieces and made long lists harder to scan. ---- */
.model-grid--list .model-card {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(112px, 160px) minmax(230px, 280px) auto;
  grid-template-areas:
    "header tags bottom controls"
    "meta   tags bottom controls";
  align-items: center;
  gap: 8px 14px;
  padding: 14px 16px 17px;
}

.model-grid--list .mc-header { display: contents; }
.model-grid--list .mc-title { grid-area: header; }
.model-grid--list .mc-controls { grid-area: controls; }
.model-grid--list .mc-meta { grid-area: meta; min-width: 0; }
.model-grid--list .mc-tags { grid-area: tags; }
.model-grid--list .mc-bottom { grid-area: bottom; justify-content: flex-end; gap: 12px; }

/* Stack metadata in narrow windows while keeping controls beside the title. */
@container model-library (max-width: 760px) {
  .model-grid--list .model-card {
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-areas:
      "header controls"
      "tags tags"
      "meta meta"
      "bottom bottom";
  }

  .model-grid--list .mc-controls { align-self: start; }

  .model-grid--list .mc-bottom {
    justify-content: space-between;
    padding-top: 10px;
    border-top: 1px solid color-mix(in srgb, var(--outline) 38%, transparent);
  }
}

@container (max-width: 350px) {
  .model-grid--card .mc-speed { display: none; }
  .model-grid--card .model-card--busy .mc-size { display: none; }
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
  container-type: inline-size;
  align-items: center;
  gap: 10px;
  min-width: 0;
  min-height: 30px;
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

.dark-theme .model-card {
  background: var(--surface-1);
  border-color: var(--outline);
}
.dark-theme .model-card:hover {
  background: color-mix(in srgb, var(--surface-2) 50%, var(--surface-1));
  border-color: var(--outline-strong);
  box-shadow: none;
}
.dark-theme .model-card--selected {
  border-color: var(--primary-border) !important;
  background: color-mix(in srgb, var(--primary-soft) 45%, var(--surface-1));
}
.dark-theme .model-card--busy {
  border-color: var(--primary-border);
  background: color-mix(in srgb, var(--primary-softer) 45%, var(--surface-1));
}
.dark-theme .mc-name { color: var(--on-surface-heading); }

/* Card header */
.mc-header {
  display: flex;
  align-items: flex-start;
  gap: 4px;
  min-width: 0;
}

.mc-title {
  display: grid;
  gap: 3px;
  flex: 1;
  min-width: 0;
}

.mc-controls {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

.mc-name {
  display: -webkit-box;
  font-size: 13px;
  font-weight: 600;
  min-width: 0;
  max-width: 100%;
  line-height: 1.5;
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
  opacity: 0.62;
}

.mc-more {
  flex: 0 0 auto;
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
  min-width: 0;
  flex-shrink: 1;
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
  white-space: nowrap;
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
  width: min(660px, calc(100vw - 48px));
  border-radius: 18px;
}

.model-inference-modal__body {
  display: grid;
  gap: 10px;
  min-width: 0;
  max-height: min(64vh, 640px);
  overflow-y: auto;
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

.model-inference-modal__refresh {
  display: grid;
  justify-items: start;
  gap: 8px;
}

.model-batch-modal {
  width: min(720px, calc(100vw - 48px));
}

/* Marker only: "this model has saved library parameters". It carries no precedence meaning. */
.mc-override-dot {
  display: inline-block;
  width: 7px;
  height: 7px;
  margin-right: 6px;
  vertical-align: middle;
  border-radius: 50%;
  background: var(--primary);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--primary) 22%, transparent);
}

.mc-batch-select { position: absolute; top: 9px; left: 10px; width: 32px; height: 32px; display: grid; place-items: center; z-index: 1; }
.mc-batch-check { width: 32px; height: 32px; display: inline-flex; align-items: center; justify-content: center; }
.model-grid--card .model-card--selecting .mc-header { padding-left: 36px; }
.model-grid--list .model-card--selecting { padding-left: 52px; grid-template-columns: minmax(0, 1fr) minmax(112px, 160px) minmax(230px, 280px); grid-template-areas: "header tags bottom" "meta tags bottom"; }
.model-grid--list .mc-batch-select { top: 50%; transform: translateY(-50%); }
.model-card.model-card--batch-selected, .model-card.model-card--batch-selected:hover { border-color: var(--primary-border); background: color-mix(in srgb, var(--primary-soft) 55%, var(--surface-1)); box-shadow: inset 3px 0 var(--primary); }
.model-card--batch-ineligible { cursor: not-allowed; }
.model-card--batch-ineligible .mc-name { color: var(--on-surface-muted); }
@container model-library (max-width: 760px) {
  .model-grid--list .model-card--selecting { grid-template-columns: minmax(0, 1fr); grid-template-areas: "header" "tags" "meta" "bottom"; }
}
:global(html.models-drag-selecting), :global(html.models-drag-selecting *) { user-select: none !important; cursor: pointer !important; }

.header-actions.batch-select-bar,
.storage-selection-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 12px;
  max-width: 100%;
  padding: 8px 10px;
  border: 1px solid color-mix(in srgb, var(--primary-border) 52%, var(--outline));
  border-radius: 12px;
  background: color-mix(in srgb, var(--primary-softer) 42%, var(--surface-1));
}

.batch-select-bar__selection {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 8px;
}

.batch-select-bar__count {
  color: var(--on-surface-muted);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.batch-select-bar__count > span {
  margin-left: 4px;
}

.batch-select-bar__spacer {
  flex: 1 1 auto;
}

.batch-reference-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  gap: 10px;
}

.batch-preview-list {
  display: grid;
  gap: 10px;
}

.batch-preview-row {
  display: grid;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--outline);
  border-radius: 12px;
}

.batch-preview-row--excluded {
  opacity: 0.55;
}

.batch-preview-row--default {
  color: var(--on-surface-muted);
}

.batch-preview-row__head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.batch-preview-row__name {
  overflow: hidden;
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.batch-preview-row__reason {
  margin: 0;
  color: var(--on-surface-muted);
  font-size: 12px;
}

.batch-preview-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.batch-preview-table th,
.batch-preview-table td {
  padding: 4px 6px;
  text-align: left;
  border-top: 1px solid var(--outline);
}

.batch-preview-table th {
  color: var(--on-surface-muted);
  font-weight: 500;
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
  width: var(--modal-width-sm);
  height: min(620px, var(--modal-max-height));
  max-height: var(--modal-max-height);
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
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  gap: 14px;
}

.storage-panel > :not(.storage-list) { flex-shrink: 0; }
.storage-heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; width: 100%; gap: 10px 16px; }
.storage-heading > span { flex-shrink: 0; }

.storage-summary {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}

.storage-stat {
  padding: 12px;
  border: 1px solid var(--outline);
  border-radius: 12px;
  background: var(--surface-1);
}

.storage-stat span {
  display: block;
  font-size: 12px;
  color: var(--on-surface-muted);
}

.storage-stat > span, .storage-stat__heading { min-height: 24px; display: flex; align-items: center; }
.storage-stat__heading { justify-content: space-between; flex-wrap: wrap; gap: 4px; }

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
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--on-surface-muted);
}

.storage-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.storage-downloaded-toggle { display: inline-flex; align-items: center; gap: 8px; white-space: nowrap; }
.storage-selection-bar { margin-left: auto; font-weight: 400; }
.storage-selection-bar__count { display: grid; gap: 2px; color: var(--on-surface-muted); font-size: 13px; font-variant-numeric: tabular-nums; }
.storage-selection-bar__count small { font-size: 11px; }

.storage-toolbar .n-input {
  flex: 1;
  min-width: 220px;
}

.storage-list {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  gap: 8px;
  overflow: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
  padding-right: 4px;
  padding-bottom: 4px;
}

.storage-row {
  display: grid;
  grid-template-columns: 32px minmax(0, 1fr) auto 84px;
  flex-shrink: 0;
  align-items: center;
  gap: 12px;
  padding: 12px;
  border-radius: 12px;
  border: 1px solid var(--outline);
  background: var(--surface-1);
  cursor: pointer;
}

.storage-row:hover:not(.storage-row--disabled) { border-color: var(--primary-border); }
.storage-row:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
.storage-row--selected { border-color: var(--primary-border); background: color-mix(in srgb, var(--primary-soft) 55%, var(--surface-1)); box-shadow: inset 3px 0 var(--primary); }
.storage-row--disabled { cursor: not-allowed; }
.storage-row--disabled .storage-row__main { color: var(--on-surface-muted); }
.storage-row__checkbox { display: grid; place-items: center; width: 32px; height: 32px; }
:global(html.storage-drag-selecting), :global(html.storage-drag-selecting *) { user-select: none !important; cursor: pointer !important; }

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
  text-align: right;
  font-variant-numeric: tabular-nums;
}

@media (max-width: 600px) {
  .storage-row { grid-template-columns: 32px minmax(0, 1fr) auto; gap: 6px 10px; }
  .storage-row > .n-tag { grid-column: 2; justify-self: start; }
  .storage-row__checkbox { grid-row: 1 / span 2; }
  .storage-row__size { grid-column: 3; grid-row: 1 / span 2; }
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
@media (max-width: 1180px) {
  .toolbar-row { grid-template-columns: minmax(0, 1fr) minmax(0, 0.8fr) minmax(0, 0.9fr) auto; }
  .search-input { grid-column: 1 / -1; }
}

@media (max-width: 760px) {
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
@media (max-width: 560px) {
  .sort-select, .toolbar-actions { grid-column: 1 / -1; }
}

@media (max-width: 720px) {
  .batch-reference-row {
    grid-template-columns: 1fr;
  }

  .model-inference-modal__footer-spacer {
    display: none;
  }

  .model-detail-modal {
    width: var(--modal-width-sm);
    height: min(620px, var(--modal-max-height));
    max-height: var(--modal-max-height);
  }

  .model-detail-modal .drawer-footer {
    flex-direction: column;
  }

  .model-detail-modal .drawer-footer :deep(.n-button) {
    width: 100%;
  }
}

</style>
