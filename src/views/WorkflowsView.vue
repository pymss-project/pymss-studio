<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useDialog, useMessage, type DropdownOption } from 'naive-ui'
import { useI18n } from 'vue-i18n'
import {
  AlertCircleOutline,
  CheckmarkCircle,
  CubeOutline,
  DownloadOutline,
  EllipsisHorizontalOutline,
  GitNetworkOutline,
  InformationCircleOutline,
  ListOutline,
  MusicalNotesOutline,
  OpenOutline,
  PlayOutline,
  SearchOutline,
  SettingsOutline,
} from '@vicons/ionicons5'
import { invoke } from '@tauri-apps/api/core'
import packageMeta from '../../package.json'
import { storeToRefs } from 'pinia'
import WorkflowCreateChooser from '@/components/workflow/WorkflowCreateChooser.vue'
import WorkflowOverviewPreview from '@/components/workflow/WorkflowOverviewPreview.vue'
import WorkflowInfo from '@/components/workflow/WorkflowInfo.vue'
import WorkflowDescriptionDialog from '@/components/workflow/WorkflowDescriptionDialog.vue'
import WorkflowDescriptionPreview from '@/components/workflow/WorkflowDescriptionPreview.vue'
import WorkflowDescriptionEditor from '@/components/workflow/WorkflowDescriptionEditor.vue'
import { useModelStore, type ModelEntry } from '@/stores/model'
import { useWorkflowNodeStore } from '@/stores/workflowNodes'
import {
  WorkflowGuidConflictError,
  WorkflowRevisionConflictError,
  useWorkflowStore,
  type WorkflowEntry,
} from '@/stores/workflow'
import {
  analyzeComfyOverview,
  analyzeSimpleWorkflow,
  hydrateSimpleWorkflow,
  type SimpleWorkflowReasonCode,
} from '@/utils/workflowSimple'
import { WorkflowEditorInUseError } from '@/utils/workflowEditorState'
import { themeIsDark } from '@/utils/theme'
import { hasInvalidSimpleStructure, isGraphWorkflowDefinition, isSimpleWorkflowDefinition } from '@/workflows/formats'
import { litegraphToComfy } from '@/litegraph/graphAdapter'
import { createWorkflowMetadata, legacyWorkflowDescription, parseWorkflowFile, resolveWorkflowDescription, serializeWorkflowFile, type WorkflowMetadata } from '@/workflows/metadata'
import { isTauriRuntime } from '@/utils/appStore'
import {
  countWorkflowSaveOutputs,
  getWorkflowDefinitionIssue,
} from '@/workflows/runtimeDefinition'

const { t } = useI18n()
const message = useMessage()
const dialog = useDialog()
const workflow = useWorkflowStore()
const model = useModelStore()
const workflowNodes = useWorkflowNodeStore()
void workflowNodes.load().catch(() => undefined)
const { workflows, selectedWorkflowId, selectedWorkflow, simpleEditorOpenWorkflowId } = storeToRefs(workflow)
const pendingModelDownloads = ref(new Set<string>())
let modelCatalogLoad: Promise<void> | null = null
const editingId = ref('')
const name = ref('')
const query = ref('')
type WorkflowTypeFilter = 'all' | 'simple' | 'advanced'
type WorkflowKind = Exclude<WorkflowTypeFilter, 'all'> | 'invalid'
const typeFilter = ref<WorkflowTypeFilter>('all')
const importFileInputRef = ref<HTMLInputElement | null>(null)
const createChooserOpen = ref(false)
const settingsOpen = ref(false)
const settingsClosing = ref(false)
const nameSaving = ref(false)
const descriptionEditor = ref<InstanceType<typeof WorkflowDescriptionEditor> | null>(null)
let descriptionFocusRequested = false
let pendingDescriptionEdit = ''
let pendingNameSave: Promise<boolean> | null = null
const overviewInfoOpen = ref(false)
const overviewInfoButton = ref<HTMLButtonElement | null>(null)
const moreActionsElement = ref<HTMLElement | null>(null)
const contextInfoOpen = ref(false)
const contextInfoWorkflowId = ref('')
const contextInfoX = ref(0)
const contextInfoY = ref(0)
const contextInfoWorkflow = computed(() => workflows.value.find(item => item.id === contextInfoWorkflowId.value))
let contextMenuTrigger: HTMLElement | null = null
let contextInfoReturnFocus: HTMLElement | null = null
const exportDialogOpen = ref(false)
const exportBusy = ref(false)
const exportWorkflowId = ref('')
const exportMetadata = ref<WorkflowMetadata>({ guid: '' })
const exportStudioVersion = ref(packageMeta.version)
const descriptionDialogOpen = ref(false)
const descriptionWorkflowId = ref('')
type WorkflowCreateType = 'simple' | 'advanced'
const contextMenuX = ref(0)
const contextMenuY = ref(0)
const contextMenuVisible = ref(false)
const contextWorkflow = ref<WorkflowEntry | null>(null)
type WorkflowDefaultsPatch = {
  defaultDevice?: string
  defaultFormat?: string
  defaultNormalize?: boolean | null
}
const updatingWorkflowDefaults = ref(false)
const pendingWorkflowDefaults = new Map<string, WorkflowDefaultsPatch>()
const workflowDefaultsQueue: string[] = []
let unmounted = false

const deviceOptions = [
  { label: 'Auto', value: 'auto' },
  { label: 'CPU', value: 'cpu' },
  { label: 'CUDA', value: 'cuda' },
  { label: 'MPS', value: 'mps' },
  { label: 'MLX', value: 'mlx' },
]
const formatOptions = [
  { label: 'WAV', value: 'wav' },
  { label: 'FLAC', value: 'flac' },
  { label: 'MP3', value: 'mp3' },
  { label: 'M4A', value: 'm4a' },
]
const normalizeOptions = computed(() => [
  { label: t('workflows.inferenceFollowGlobal'), value: 'global' },
  { label: t('workflows.inferenceEnabled'), value: 'enabled' },
  { label: t('workflows.inferenceDisabled'), value: 'disabled' },
])

function normalizeMode(value: boolean | null) {
  return value === null ? 'global' : value ? 'enabled' : 'disabled'
}

function workflowKind(item: WorkflowEntry): WorkflowKind {
  if (isSimpleWorkflowDefinition(item.definition)) return 'simple'
  if (isGraphWorkflowDefinition(item.definition)) return 'advanced'
  return 'invalid'
}

const workflowTypeCounts = computed(() => ({
  all: workflows.value.length,
  simple: workflows.value.filter(item => workflowKind(item) === 'simple').length,
  advanced: workflows.value.filter(item => workflowKind(item) === 'advanced').length,
}))
const workflowTypeFilterOptions = computed(() => [
  { value: 'all' as const, label: t('workflows.typeAll'), count: workflowTypeCounts.value.all },
  { value: 'simple' as const, label: t('workflows.typeSimpleShort'), count: workflowTypeCounts.value.simple },
  { value: 'advanced' as const, label: t('workflows.typeAdvancedShort'), count: workflowTypeCounts.value.advanced },
])

const filteredWorkflows = computed(() => {
  const value = query.value.trim().toLowerCase()
  return workflows.value.filter(item => {
    if (typeFilter.value !== 'all' && workflowKind(item) !== typeFilter.value) return false
    if (!value) return true
    return (
      item.name.toLowerCase().includes(value)
      || resolveWorkflowDescription(item).toLowerCase().includes(value)
      || legacyWorkflowDescription(item).toLowerCase().includes(value)
    )
  })
})

const workflowGroups = computed(() => [
  {
    kind: 'simple' as const,
    label: t('workflows.typeSimple'),
    items: filteredWorkflows.value.filter(item => workflowKind(item) === 'simple'),
  },
  {
    kind: 'advanced' as const,
    label: t('workflows.typeAdvanced'),
    items: filteredWorkflows.value.filter(item => workflowKind(item) === 'advanced'),
  },
  {
    kind: 'invalid' as const,
    label: t('workflows.typeInvalid'),
    items: filteredWorkflows.value.filter(item => workflowKind(item) === 'invalid'),
  },
].filter(group => group.items.length))

const workflowListMetaMap = computed(() => Object.fromEntries(workflows.value.map((item) => {
  if (isGraphWorkflowDefinition(item.definition)) {
    const overview = analyzeComfyOverview(item.definition, workflowNodes.saveNodeTypes)
    return [item.id, overview
      ? t('workflows.listMetaAdvanced', { nodes: overview.nodeCount, outputs: overview.outputCount })
      : t('workflows.typeAdvanced')]
  }
  if (isSimpleWorkflowDefinition(item.definition)) {
    const draft = hydrateSimpleWorkflow(item.definition)
    return [item.id, t('workflows.listMetaSimple', {
      steps: draft.steps.length,
      ensembles: draft.ensembles.length,
      outputs: countWorkflowSaveOutputs(item.definition, workflowNodes.saveNodeTypes),
    })]
  }
  return [item.id, t('workflows.workflowValidationTitle')]
})))

function workflowListMeta(item: WorkflowEntry) {
  return workflowListMetaMap.value[item.id] || ''
}

function workflowTypeLabel(item: WorkflowEntry) {
  const kind = workflowKind(item)
  if (kind === 'simple') return t('workflows.typeSimpleShort')
  if (kind === 'advanced') return t('workflows.typeAdvancedShort')
  return t('workflows.typeInvalidShort')
}

function setWorkflowTypeFilter(value: WorkflowTypeFilter) {
  typeFilter.value = value
}

const isNodeEditorOpen = computed(() => Boolean(workflow.editorFor(selectedWorkflowId.value)))
const isAnyEditorOpen = computed(() => workflow.hasAnyEditorOpen)

function workflowDefinitionError(definition: Record<string, unknown>): string {
  const issue = getWorkflowDefinitionIssue(definition, workflowNodes.saveNodeTypes)
  if (issue === 'steps-required') return t('workflows.stepsRequired')
  if (issue === 'no-save-outputs') {
    if (workflowNodes.loading) return t('common.loading')
    if (workflowNodes.error) return t('workflows.nodeMetadataUnavailable', { error: workflowNodes.error })
    return t('workflows.workflowNoSaveOutputs')
  }
  if (issue === 'invalid-definition') return t('workflows.workflowFormatInvalid')
  if (issue === 'invalid-format') return t('workflows.workflowFormatInvalid')
  return ''
}
function workflowRunBlocked(definition: Record<string, unknown>) {
  return Boolean(workflowDefinitionError(definition))
}

// ---- Per-item lightweight status (memoized) ----
const workflowStatusMap = computed(() => Object.fromEntries(
  workflows.value.map(item => [item.id, workflowRunBlocked(item.definition)]),
))
function isWorkflowBlocked(item: WorkflowEntry) {
  return Boolean(workflowStatusMap.value[item.id])
}

function workflowMenuOptionsFor(current: WorkflowEntry | null): DropdownOption[] {
  if (!current) return []
  const isSimpleDefinition = isSimpleWorkflowDefinition(current.definition)
  const canEditSimple = isSimpleDefinition && analyzeSimpleWorkflow(current.definition).editable
  const definitionError = workflowDefinitionError(current.definition)
  const locked = Boolean(workflow.editorFor(current.id))
  const options: DropdownOption[] = [
    {
      key: 'edit',
      label: isSimpleDefinition ? t('workflows.simpleMode') : t('workflows.openAdvancedEditor'),
      disabled: locked || (isSimpleDefinition && !canEditSimple),
    },
  ]
  if (current.metadata) options.push({ key: 'info', label: t('workflows.workflowInfo') })
  if (resolveWorkflowDescription(current) || legacyWorkflowDescription(current)) options.push({ key: 'view-description', label: t('workflows.viewFullDescription') })
  options.push({ key: 'notes', label: t('workflows.editDescription') })
  options.push(
    { type: 'divider', key: 'workflow-actions-divider' },
    { key: 'duplicate', label: t('workflows.duplicate') },
    { key: 'export', label: t('workflows.exportWorkflow') },
    { key: 'delete', label: t('workflows.deleteConfirm'), disabled: locked },
  )
  return options
}

const workflowMenuOptions = computed(() => workflowMenuOptionsFor(contextWorkflow.value))
const selectedWorkflowMenuOptions = computed(() => workflowMenuOptionsFor(selectedWorkflow.value))
const selectedDescriptionPreview = computed(() => resolveWorkflowDescription(selectedWorkflow.value))
const selectedPublicationPreview = computed(() => {
  const metadata = selectedWorkflow.value?.metadata
  return [
    metadata?.author,
    metadata?.version ? `${t('workflows.workflowReleaseVersion')} ${metadata.version}` : '',
  ].filter(Boolean).join(' · ')
})

// ---- Selected workflow overview data (simple-mode details; comfy graphs
// show a read-only overview since their structure is free-form) ----
const selectedComfyOverview = computed(() => analyzeComfyOverview(selectedWorkflow.value?.definition, workflowNodes.saveNodeTypes))
const isComfyWorkflow = computed(() => isGraphWorkflowDefinition(selectedWorkflow.value?.definition))
const isSimpleWorkflow = computed(() => isSimpleWorkflowDefinition(selectedWorkflow.value?.definition))
const selectedWorkflowKind = computed<WorkflowKind>(() => selectedWorkflow.value
  ? workflowKind(selectedWorkflow.value)
  : 'invalid')
const selectedDraft = computed(() =>
  isSimpleWorkflowDefinition(selectedWorkflow.value?.definition)
    ? hydrateSimpleWorkflow(selectedWorkflow.value?.definition)
    : null)
const selectedSummary = computed((): { error: string } | null =>
  selectedWorkflow.value ? { error: workflowDefinitionError(selectedWorkflow.value.definition) } : null)
type WorkflowModelItem = { name: string; entry: ModelEntry | null; downloaded: boolean }

function workflowModelEntry(name: string) {
  return model.models.find(item => item.name === name)
    || model.models.find(item => item.aliases?.includes(name)) || null
}

const selectedModels = computed<WorkflowModelItem[]>(() => {
  const names = isComfyWorkflow.value
    ? selectedComfyOverview.value?.models || []
    : selectedDraft.value?.steps.map(step => step.model.trim()) || []
  const seen = new Set<string>()
  const list: WorkflowModelItem[] = []
  for (const name of names) {
    const entry = workflowModelEntry(name)
    const key = entry?.name || name
    if (!name || seen.has(key)) continue
    seen.add(key)
    list.push({ name, entry, downloaded: Boolean(entry?.supported && entry.downloaded) })
  }
  return list
})

function workflowModelDownloadTask(item: WorkflowModelItem) {
  return model.downloadTasks[item.entry?.name || item.name]
}

function workflowModelDownloadBusy(item: WorkflowModelItem) {
  return pendingModelDownloads.value.has(item.name)
    || ['preparing', 'downloading'].includes(workflowModelDownloadTask(item)?.status || '')
}

function workflowModelDownloadBlocked(item: WorkflowModelItem) {
  if (item.entry?.source === 'user') return t('models.customFilesMissing')
  if (item.entry && !item.entry.supported) return item.entry.unsupportedReason || t('models.unsupported')
  if (item.entry && model.deleteTasks[item.entry.name]?.status === 'deleting') return t('models.deleting')
  if (!item.entry && model.modelsLoaded) return t('workflows.modelDownloadUnavailable')
  return ''
}

function workflowModelDownloadLabel(item: WorkflowModelItem) {
  const task = workflowModelDownloadTask(item)
  if (task?.status === 'downloading') {
    const progress = Number.isFinite(task.progress) ? Math.max(0, Math.min(100, Math.round(task.progress))) : 0
    return `${t('models.cardDownloading')} ${progress}%`
  }
  if (workflowModelDownloadBusy(item)) return t('models.cardDownloadPreparing')
  if (item.entry?.source === 'user') return t('models.customFilesMissing')
  if (item.entry && !item.entry.supported) return t('models.unsupported')
  if (item.entry && model.deleteTasks[item.entry.name]?.status === 'deleting') return t('models.deleting')
  if (task?.status === 'error') return t('models.cardDownloadError')
  if (task?.status === 'paused') return t('models.cardDownloadPaused')
  if (task?.status === 'interrupted') return t('models.cardDownloadInterrupted')
  return t('workflows.modelNotDownloadedShort')
}

function workflowModelDownloadAction(item: WorkflowModelItem) {
  const status = workflowModelDownloadTask(item)?.status
  if (status === 'error') return t('common.retry')
  if (status === 'paused' || status === 'interrupted' || status === 'cancelled') return t('common.resume')
  return t('common.download')
}

async function downloadWorkflowModel(item: WorkflowModelItem) {
  if (workflowModelDownloadBusy(item)) return
  pendingModelDownloads.value.add(item.name)
  try {
    let entry = workflowModelEntry(item.name)
    if (!model.modelsLoaded) {
      if (!modelCatalogLoad) modelCatalogLoad = model.loadModels().finally(() => { modelCatalogLoad = null })
      await modelCatalogLoad
      entry = workflowModelEntry(item.name)
    }
    if (entry?.supported && entry.downloaded) return
    const current = { name: item.name, entry, downloaded: Boolean(entry?.downloaded) }
    const blocked = workflowModelDownloadBlocked(current)
    if (blocked) {
      message.error(`${item.name}: ${blocked}`)
      return
    }
    if (!entry || entry.downloaded) return
    const started = await model.downloadModel(entry.name)
    if (started) message.success(`${t('models.downloadStarted')}: ${entry.name}`)
  } catch (error) {
    message.error(`${item.name}: ${error instanceof Error ? error.message : String(error)}`)
  } finally {
    pendingModelDownloads.value.delete(item.name)
  }
}
const selectedError = computed(() => selectedSummary.value?.error || '')
const missingModelCount = computed(() => selectedModels.value.filter(item => !item.downloaded).length)
const selectedReady = computed(() => Boolean(selectedWorkflow.value) && !selectedError.value)
const selectedSimpleAnalysis = computed(() => selectedWorkflow.value
  ? analyzeSimpleWorkflow(selectedWorkflow.value.definition)
  : null)
const selectedSimpleReasons = computed(() => selectedSimpleAnalysis.value?.reasonCodes || [])

const simpleReasonKeys: Record<SimpleWorkflowReasonCode, string> = {
  graph_workflow: 'workflows.simpleReasonGraphWorkflow',
  advanced_parameters: 'workflows.simpleReasonAdvancedParameters',
  invalid_definition: 'workflows.simpleReasonInvalidDefinition',
}

function simpleReasonLabel(reason: SimpleWorkflowReasonCode) {
  return t(simpleReasonKeys[reason])
}

async function flushWorkflowDefaultsQueue() {
  if (updatingWorkflowDefaults.value) return
  updatingWorkflowDefaults.value = true
  try {
    while (workflowDefaultsQueue.length) {
      const workflowId = workflowDefaultsQueue[0]
      const patch = pendingWorkflowDefaults.get(workflowId)
      pendingWorkflowDefaults.delete(workflowId)
      if (!patch) {
        workflowDefaultsQueue.shift()
        continue
      }
      const current = workflows.value.find(item => item.id === workflowId)
      const analysis = current ? analyzeSimpleWorkflow(current.definition) : null
      if (!current || !analysis?.editable) {
        workflowDefaultsQueue.shift()
        continue
      }
      const draft = hydrateSimpleWorkflow(current.definition)
      const definition = JSON.parse(JSON.stringify(current.definition)) as Record<string, unknown>
      const defaults = definition.defaults && typeof definition.defaults === 'object' && !Array.isArray(definition.defaults)
        ? definition.defaults as Record<string, unknown>
        : {}
      defaults.device = patch.defaultDevice ?? draft.defaultDevice
      defaults.output_format = patch.defaultFormat ?? draft.defaultFormat
      const inference = defaults.inference_params && typeof defaults.inference_params === 'object' && !Array.isArray(defaults.inference_params)
        ? { ...(defaults.inference_params as Record<string, unknown>) }
        : {}
      const normalize = patch.defaultNormalize !== undefined ? patch.defaultNormalize : draft.defaultNormalize
      if (normalize === null) delete inference.normalize
      else inference.normalize = normalize
      if (Object.keys(inference).length) defaults.inference_params = inference
      else delete defaults.inference_params
      definition.defaults = defaults
      try {
        const entry = await workflow.saveWorkflow({
          id: current.id,
          name: current.name,
          description: current.description,
          definition,
          expectedUpdatedAt: current.updatedAt,
        })
        // The selected workflow may change while persistence is in flight. Do
        // not overwrite the newly selected editor fields with the old entry.
        if (selectedWorkflowId.value === current.id) editWorkflow(entry)
      } catch (error) {
        pendingWorkflowDefaults.delete(workflowId)
        workflowDefaultsQueue.shift()
        message.error(error instanceof WorkflowEditorInUseError ? t('workflows.editorInUseHint') : error instanceof Error ? error.message : String(error))
        continue
      }
      if (!pendingWorkflowDefaults.has(workflowId)) workflowDefaultsQueue.shift()
    }
  } finally {
    updatingWorkflowDefaults.value = false
    if (workflowDefaultsQueue.length) void flushWorkflowDefaultsQueue()
  }
}

async function updateSelectedWorkflowDefaults(patch: WorkflowDefaultsPatch) {
  const current = selectedWorkflow.value
  if (!current || !selectedDraft.value || !selectedSimpleAnalysis.value?.editable || isNodeEditorOpen.value) return
  if (!await saveMeta() || selectedWorkflowId.value !== current.id) return
  const pending = pendingWorkflowDefaults.get(current.id)
  if (!pending) workflowDefaultsQueue.push(current.id)
  pendingWorkflowDefaults.set(current.id, { ...pending, ...patch })
  void flushWorkflowDefaultsQueue()
}

function updateSelectedDefaultDevice(value: string | number | null) {
  updateSelectedWorkflowDefaults({ defaultDevice: String(value || 'auto') })
}

function updateSelectedDefaultFormat(value: string | number | null) {
  updateSelectedWorkflowDefaults({ defaultFormat: String(value || 'wav') })
}

function updateSelectedDefaultNormalize(value: string | number | boolean | null) {
  updateSelectedWorkflowDefaults({
    defaultNormalize: value === 'global' ? null : typeof value === 'boolean' ? value : value === 'enabled',
  })
}


// ---- Selection + quick meta edit ----
function syncWorkflowDetails(item: WorkflowEntry | null) {
  editingId.value = item?.id || ''
  name.value = item?.name || ''
}

function editWorkflow(item: WorkflowEntry) {
  syncWorkflowDetails(item)
  workflow.selectWorkflow(item.id)
}

async function saveMeta() {
  const id = selectedWorkflow.value?.id
  if (descriptionEditor.value && !await descriptionEditor.value.flush()) return false
  if (selectedWorkflow.value?.id !== id) return true
  return saveWorkflowName()
}

function saveWorkflowName(): Promise<boolean> {
  if (pendingNameSave) return pendingNameSave
  pendingNameSave = persistWorkflowName().finally(() => { pendingNameSave = null })
  return pendingNameSave
}

async function persistWorkflowName() {
  const current = selectedWorkflow.value
  if (!current || isNodeEditorOpen.value) return true
  const targetId = current.id
  const trimmedName = name.value.trim()
  if (!trimmedName) {
    name.value = current.name
    return true
  }
  if (trimmedName === current.name) return true
  nameSaving.value = true
  try {
    const entry = await workflow.saveWorkflow({
      id: targetId,
      name: trimmedName,
      definition: current.definition,
      expectedUpdatedAt: current.updatedAt,
    })
    // Guard against a race: if the user switched workflows while saveWorkflow was
    // awaiting, do not clobber the newly selected workflow's displayed fields.
    if (selectedWorkflowId.value !== targetId) return true
    editingId.value = entry.id
    name.value = entry.name
    return true
  } catch (error) {
    if (error instanceof WorkflowRevisionConflictError && selectedWorkflowId.value === targetId) {
      syncWorkflowDetails(selectedWorkflow.value)
    }
    message.error(error instanceof WorkflowEditorInUseError ? t('workflows.editorInUseHint') : error instanceof WorkflowRevisionConflictError
      ? t('workflows.revisionConflictTitle')
       : error instanceof Error ? error.message : t('workflows.saveFailed'))
    return false
  } finally {
    nameSaving.value = false
  }
}

async function closeWorkflowSettings(keepDraft = false) {
  if (settingsClosing.value) return
  settingsClosing.value = true
  try {
    if (!keepDraft && descriptionEditor.value && !await descriptionEditor.value.flush()) return
    if (!await saveWorkflowName()) return
    settingsOpen.value = false
  } finally { settingsClosing.value = false }
}

function handleSettingsShow(show: boolean) {
  if (!show) void closeWorkflowSettings()
}

async function focusDescriptionEditor() {
  if (!descriptionFocusRequested) return
  await descriptionEditor.value?.focus()
  descriptionFocusRequested = false
}

async function openEditorWindow(kind: 'simple' | 'node', workflowId: string, newWorkflow: boolean) {
  try {
    if (typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window) {
      const result = await invoke(`open_workflow_${kind}_editor_window`, { payload: { workflowId, newWorkflow } })
      workflow.recordEditorOpened(result, kind === 'node' ? 'advanced' : kind, newWorkflow ? '__new__' : workflowId)
    } else {
      const query = newWorkflow ? 'new=1' : `workflowId=${encodeURIComponent(workflowId)}`
      const popup = window.open(`#/workflow-${kind}-editor?${query}`, `workflow-${kind}-editor-${workflowId || 'new'}`, 'popup=yes,width=1440,height=900')
      if (!popup) throw new Error(t('workflows.editorWindowOpenFailed'))
      popup.focus()
    }
    if (!(typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window)) workflow.recordEditorOpened(null, kind === 'node' ? 'advanced' : kind, newWorkflow ? '__new__' : workflowId)
  } catch (error) {
    message.error(error instanceof Error ? error.message : t('workflows.editorWindowOpenFailed'))
  }
}

async function openSimpleEditor(options: { forceNew?: boolean; workflowId?: string } = {}) {
  const forceNew = options.forceNew === true
  const workflowId = forceNew ? '' : (options.workflowId || selectedWorkflow.value?.id || selectedWorkflowId.value || '')
  const isNewWorkflow = forceNew || !workflowId
  await openEditorWindow('simple', workflowId, isNewWorkflow)
}

function createSimpleWorkflow() {
  void openSimpleEditor({ forceNew: true })
}

function openWorkflowCreateChooser() {
  if (isAnyEditorOpen.value) return
  createChooserOpen.value = true
}

async function createAdvancedWorkflow() {
  editingId.value = ''
  name.value = ''
  await openNodeEditor({ forceNew: true, skipWarning: true })
}

function selectWorkflowCreateType(type: WorkflowCreateType) {
  if (type === 'simple') {
    createSimpleWorkflow()
    return
  }
  void createAdvancedWorkflow()
}

function editSimpleWorkflow(item: WorkflowEntry) {
  if (!analyzeSimpleWorkflow(item.definition).editable) return
  void openSimpleEditor({ workflowId: item.id })
}

function selectWorkflowFromList(item: WorkflowEntry) {
  // Selecting a row only changes the overview. Opening an editor is an
  // explicit action from the overview or the context menu.
  editWorkflow(item)
}

function openWorkflowContextMenu(event: MouseEvent, item: WorkflowEntry) {
  overviewInfoOpen.value = false
  contextInfoOpen.value = false
  contextMenuTrigger = event.currentTarget as HTMLElement
  contextWorkflow.value = item
  contextMenuVisible.value = false
  contextMenuX.value = event.clientX
  contextMenuY.value = event.clientY
  window.requestAnimationFrame(() => {
    contextMenuVisible.value = true
  })
}

function closeWorkflowContextMenu() {
  contextMenuVisible.value = false
  contextWorkflow.value = null
}

function closeOverviewInfo() {
  overviewInfoOpen.value = false
  overviewInfoButton.value?.focus({ preventScroll: true })
}

function closeContextInfo() {
  contextInfoOpen.value = false
  if (contextInfoReturnFocus?.isConnected) contextInfoReturnFocus.focus({ preventScroll: true })
}

function openWorkflowEditor(item: WorkflowEntry) {
  editWorkflow(item)
  if (isSimpleWorkflowDefinition(item.definition)) {
    if (!analyzeSimpleWorkflow(item.definition).editable) {
      message.warning(t('workflows.simpleEditUnsupported'))
      return
    }
    editSimpleWorkflow(item)
    return
  }
  void openNodeEditor({ workflowId: item.id })
}

function reopenActiveEditor() {
  const editor = workflow.editorFor(selectedWorkflowId.value) || workflow.openedEditors.at(-1)
  if (!editor) return
  if (editor.kind === 'simple') void openSimpleEditor({ workflowId: editor.workflowId, forceNew: !editor.workflowId })
  else void openNodeEditor({ workflowId: editor.workflowId, forceNew: !editor.workflowId })
}

function handleWorkflowMenuSelect(key: string | number, current: WorkflowEntry | null) {
  if (key === 'info' && current?.metadata) {
    const fromContextMenu = contextMenuVisible.value
    const bounds = moreActionsElement.value?.getBoundingClientRect()
    contextInfoX.value = fromContextMenu ? contextMenuX.value : bounds?.left ?? 0
    contextInfoY.value = fromContextMenu ? contextMenuY.value : bounds?.top ?? 0
    contextInfoReturnFocus = fromContextMenu ? contextMenuTrigger : moreActionsElement.value?.querySelector('button') ?? null
    overviewInfoOpen.value = false
    contextInfoWorkflowId.value = current.id
    contextInfoOpen.value = true
  }
  closeWorkflowContextMenu()
  if (!current) return

  if (key === 'notes') {
    openWorkflowDescription(current, true)
    return
  }
  if (key === 'view-description') {
    openWorkflowDescription(current)
    return
  }
  if (key === 'edit') {
    openWorkflowEditor(current)
    return
  }
  if (key === 'run') {
    editWorkflow(current)
    openWorkflowEditor(current)
    return
  }
  if (key === 'duplicate') {
    void duplicateWorkflow(current)
    return
  }
  if (key === 'export') {
    void exportWorkflowEntry(current)
    return
  }
  if (key === 'delete') {
    deleteWorkflow(current)
  }
}

function confirmAdvancedWorkflowWarning() {
  return new Promise<boolean>((resolve) => {
    let settled = false
    const finish = (value: boolean) => {
      if (settled) return
      settled = true
      resolve(value)
    }
    dialog.warning({
      title: t('workflows.advancedWarningTitle'),
      content: t('workflows.advancedWarningContent'),
      positiveText: t('workflows.advancedWarningContinue'),
      negativeText: t('workflows.advancedWarningCancel'),
      positiveButtonProps: { type: 'warning' },
      negativeButtonProps: { secondary: true },
      onPositiveClick: () => finish(true),
      onNegativeClick: () => finish(false),
      onClose: () => finish(false),
    })
  })
}

// ---- Node editor bridge ----
async function openNodeEditor(options: { forceNew?: boolean; workflowId?: string; skipWarning?: boolean } = {}) {
  if (!options.skipWarning) {
    const confirmed = await confirmAdvancedWorkflowWarning()
    if (!confirmed) return
  }

  const forceNew = options.forceNew === true
  // The overview's selected entry is the authoritative target. `editingId`
  // can briefly lag while the store reloads after a standalone editor closes;
  // preferring it here could reopen a different (or legacy) graph as blank.
  const workflowId = forceNew
    ? ''
    : (options.workflowId || selectedWorkflow.value?.id || selectedWorkflowId.value || editingId.value || '')
  const isNewWorkflow = forceNew || !workflowId
  await openEditorWindow('node', workflowId, isNewWorkflow)
}

function syncRefreshedWorkflowDetails(selected: WorkflowEntry | null) {
  const target = selected
    || workflows.value.find(item => item.id === editingId.value)
    || workflows.value[0]
  syncWorkflowDetails(target || null)
}

// ---- Actions ----
async function duplicateWorkflow(item: WorkflowEntry) {
  const current = item
  if (!current) return
  const entry = await workflow.duplicateWorkflow(current.id)
  if (entry) {
    editWorkflow(entry)
    message.success(t('workflows.duplicated'))
  }
}

function deleteWorkflow(item: WorkflowEntry) {
  const current = item
  if (!current) return
  dialog.warning({
    title: t('workflows.deleteTitle'),
    content: t('workflows.deleteHint', { name: current.name }),
    positiveText: t('workflows.deleteConfirm'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      const deletedId = current.id
      try {
        await workflow.deleteWorkflow(current.id)
        if (simpleEditorOpenWorkflowId.value === deletedId) workflow.markSimpleEditorClosed()
        message.success(t('workflows.deleted'))
      } catch (error) {
        message.error(error instanceof WorkflowEditorInUseError ? t('workflows.editorInUseHint') : error instanceof WorkflowRevisionConflictError
          ? t('workflows.revisionConflictTitle')
          : error instanceof Error ? error.message : t('workflows.saveFailed'))
      }
    },
  })
}

function runSelected() {
  const current = selectedWorkflow.value
  if (!current) return
  if (selectedError.value) {
    message.warning(selectedError.value)
    return
  }
  workflow.selectWorkflow(current.id)
  openWorkflowEditor(current)
}

// ---- comfy-mss import / export ----
function triggerImportWorkflow() {
  importFileInputRef.value?.click()
}

function workflowFileBasename(fileName: string) {
  return fileName
    .replace(/\.(?:comfy-mss|pymss-workflow)\.json$/iu, '')
    .replace(/\.[^.]+$/u, '')
    .trim()
}

function downloadJsonFile(fileName: string, content: string) {
  const blob = new Blob([content], { type: 'application/json;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = fileName
  anchor.click()
  URL.revokeObjectURL(url)
}

function workflowSlug(value: string) {
  const normalized = value.trim().replace(/[<>:"/\\|?*\x00-\x1f]+/g, '_')
  return normalized || 'workflow'
}

async function handleImportWorkflow(event: Event) {
  const input = event.target as HTMLInputElement | null
  const file = input?.files?.[0]
  if (!file) return
  try {
    const text = await file.text()
    const { definition: parsed, metadata } = parseWorkflowFile(text)
    if (!isGraphWorkflowDefinition(parsed) && !isSimpleWorkflowDefinition(parsed)) {
      throw new Error(t('workflows.workflowImportInvalid'))
    }
    if (isSimpleWorkflowDefinition(parsed) && hasInvalidSimpleStructure(parsed)) {
      throw new Error(t('workflows.workflowImportInvalid'))
    }
    const entry = await workflow.saveWorkflow({
      name: workflowFileBasename(file.name) || t('workflows.newWorkflow'),
      description: '',
      definition: parsed,
      metadata,
    })
    editWorkflow(entry)
    message.success(t('workflows.workflowImportSuccess'))
  } catch (error) {
    message.error(`${t('workflows.workflowImportFailed')}: ${error instanceof Error ? error.message : String(error)}`)
  } finally {
    if (input) input.value = ''
  }
}

function openWorkflowDescription(current: WorkflowEntry, editing = false) {
  overviewInfoOpen.value = false
  contextInfoOpen.value = false
  closeWorkflowContextMenu()
  if (editing) {
    if (descriptionDialogOpen.value) {
      pendingDescriptionEdit = current.id
      descriptionDialogOpen.value = false
    } else void openDescriptionSettings(current)
    return
  }
  descriptionWorkflowId.value = current.id
  descriptionDialogOpen.value = true
}

async function openDescriptionSettings(current: WorkflowEntry) {
  if (settingsOpen.value && selectedWorkflowId.value !== current.id) {
    await closeWorkflowSettings()
    if (settingsOpen.value) return
  }
  if (selectedWorkflowId.value !== current.id) editWorkflow(current)
  await nextTick()
  descriptionFocusRequested = true
  if (settingsOpen.value) await focusDescriptionEditor()
  else settingsOpen.value = true
}

function editDescriptionFromReader() {
  const current = workflows.value.find(item => item.id === descriptionWorkflowId.value)
  if (current) openWorkflowDescription(current, true)
}

function afterDescriptionReaderLeave() {
  const current = workflows.value.find(item => item.id === pendingDescriptionEdit)
  pendingDescriptionEdit = ''
  if (current) void openDescriptionSettings(current)
}

async function confirmExportWorkflow() {
  if (exportBusy.value) return
  exportBusy.value = true
  let fileSaved = false
  try {
    const current = await workflow.exportWorkflow(exportWorkflowId.value, {
      ...exportMetadata.value,
      studioVersion: exportStudioVersion.value,
    }, async (entry, metadata) => {
      const definition = entry.definition
      const payload = isGraphWorkflowDefinition(definition)
        ? { ...definition, ...litegraphToComfy(definition) }
        : definition
      const suffix = isGraphWorkflowDefinition(definition)
        ? 'comfy-mss.json'
        : 'pymss-workflow.json'
      const fileName = `${workflowSlug(entry.name || t('workflows.untitled'))}.${suffix}`
      const content = serializeWorkflowFile(payload, metadata)
      if (isTauriRuntime()) {
        const savedPath = await invoke<string | null>('save_text_file_dialog', {
          defaultName: fileName,
          content,
        })
        if (!savedPath) return false
      } else {
        downloadJsonFile(fileName, content)
      }
      fileSaved = true
      return true
    })
    if (!current) return
    exportMetadata.value = { ...current.metadata! }
    exportDialogOpen.value = false
    message.success(t('workflows.workflowExportSuccess'))
  } catch (error) {
    if (error instanceof WorkflowGuidConflictError) {
      exportMetadata.value.guid = error.actualGuid
      message.warning(t('workflows.workflowExportGuidConflict'))
      return
    }
    const label = fileSaved ? t('workflows.workflowExportMetadataSaveFailed') : t('workflows.workflowExportFailed')
    message.error(`${label}: ${error instanceof Error ? error.message : String(error)}`)
  } finally {
    exportBusy.value = false
  }
}

async function exportWorkflowEntry(current: WorkflowEntry) {
  if (exportBusy.value || exportDialogOpen.value) return
  exportWorkflowId.value = current.id
  exportMetadata.value = createWorkflowMetadata(current.metadata)
  exportStudioVersion.value = packageMeta.version
  exportDialogOpen.value = true
  if (!isTauriRuntime()) return
  exportBusy.value = true
  try {
    const info = await invoke<{ version: string }>('get_build_info')
    exportStudioVersion.value = info.version || packageMeta.version
  } catch {
    // Development builds can use the version embedded by Vite.
  } finally {
    exportBusy.value = false
  }
}

watch(selectedWorkflowId, () => {
  settingsOpen.value = false
  overviewInfoOpen.value = false
  contextInfoOpen.value = false
})
watch(() => contextInfoWorkflow.value?.metadata, metadata => {
  if (!metadata) contextInfoOpen.value = false
})
watch(() => selectedWorkflow.value?.metadata, metadata => {
  if (!metadata) overviewInfoOpen.value = false
})

const stopEditorCloseSubscription = workflow.$onAction(({ name: action, after }) => {
  if (action !== 'handleEditorClosed') return
  after(() => {
    if (unmounted) return
    syncRefreshedWorkflowDetails(workflow.selectedWorkflow)
  })
})

onUnmounted(() => {
  unmounted = true
  stopEditorCloseSubscription()
  closeWorkflowContextMenu()
})

watch([() => selectedWorkflow.value?.id, () => selectedWorkflow.value?.name], () => {
  // A reload restores the editor's persisted selection. Hydrate the overview
  // without selecting (and persisting) an older entry during that refresh.
  syncRefreshedWorkflowDetails(selectedWorkflow.value)
}, { immediate: true })
</script>

<template>
  <div class="page workflows-page">
    <input
      ref="importFileInputRef"
      class="wf-hidden-file-input"
      type="file"
      accept=".json,application/json"
      @change="handleImportWorkflow"
    >

    <header class="page-header-compact">
      <div>
        <h1>{{ t('workflows.title') }}</h1>
        <p>{{ t('workflows.subtitle') }}</p>
      </div>
      <div class="workflows-page__header-actions" data-guide="workflow-actions">
        <n-button secondary @click="triggerImportWorkflow">
          <template #icon><n-icon :component="OpenOutline" /></template>
          {{ t('workflows.importWorkflow') }}
        </n-button>
        <n-button
          class="workflow-create-button"
          :type="themeIsDark ? 'default' : 'primary'"
          :secondary="themeIsDark"
          :disabled="isAnyEditorOpen"
          @click="openWorkflowCreateChooser"
        >
          <span class="workflow-create-button__mark" aria-hidden="true" />
          {{ t('workflows.newWorkflow') }}
        </n-button>
      </div>
    </header>

    <div class="console">
      <aside class="console__rail">
        <div class="wf-list-head">
          <n-input v-model:value="query" clearable :placeholder="t('workflows.searchPlaceholder')">
            <template #prefix><n-icon :component="SearchOutline" /></template>
          </n-input>
          <div class="wf-type-filter" role="tablist" :aria-label="t('workflows.typeFilterLabel')">
            <button
              v-for="option in workflowTypeFilterOptions"
              :key="option.value"
              type="button"
              role="tab"
              :aria-selected="typeFilter === option.value"
              :class="{ active: typeFilter === option.value }"
              @click="setWorkflowTypeFilter(option.value)"
            >
              <span>{{ option.label }}</span>
              <small>{{ option.count }}</small>
            </button>
          </div>
        </div>
        <div class="wf-list-scroll">
          <div v-if="filteredWorkflows.length" class="wf-list">
            <section v-for="group in workflowGroups" :key="group.kind" class="wf-list-group">
              <header class="wf-list-group__head">
                <span>{{ group.label }}</span>
                <small>{{ group.items.length }}</small>
              </header>
              <button
                v-for="item in group.items"
                :key="item.id"
                type="button"
                class="wf-row"
                :class="[
                  `wf-row--${workflowKind(item)}`,
                  { 'wf-row--active': item.id === selectedWorkflowId },
                ]"
                @click="selectWorkflowFromList(item)"
                @contextmenu.stop.prevent="openWorkflowContextMenu($event, item)"
              >
                <span class="wf-row__icon">
                  <n-icon :component="workflowKind(item) === 'simple' ? ListOutline : GitNetworkOutline" />
                </span>
                <span class="wf-row__main">
                  <strong>{{ item.name }}</strong>
                  <span class="wf-row__meta">
                    <small class="wf-row__type">{{ workflowTypeLabel(item) }}</small>
                    <small class="wf-row__summary">{{ workflowListMeta(item) }}</small>
                  </span>
                </span>
                <span
                  class="wf-row__dot"
                  :class="isWorkflowBlocked(item) ? 'wf-row__dot--warn' : 'wf-row__dot--ok'"
                  :title="isWorkflowBlocked(item) ? t('workflows.workflowValidationTitle') : t('workflows.statusReady')"
                />
              </button>
            </section>
          </div>
          <div v-else class="wf-empty">
            <n-icon :component="GitNetworkOutline" />
            <strong>{{ t('workflows.emptyTitle') }}</strong>
            <span>{{ t('workflows.emptyDesc') }}</span>
            <n-button class="workflow-create-button" :type="themeIsDark ? 'default' : 'primary'" :secondary="themeIsDark" size="small" @click="openWorkflowCreateChooser">
              <span class="workflow-create-button__mark" aria-hidden="true" />
              {{ t('workflows.newWorkflow') }}
            </n-button>
          </div>
        </div>
      </aside>

      <main class="console__stage" data-guide="workflow-overview">
        <div v-if="!selectedWorkflow" class="wf-overview-empty">
          <n-icon :component="GitNetworkOutline" />
          <strong>{{ t('workflows.overviewEmptyTitle') }}</strong>
          <span>{{ t('workflows.overviewEmptyDesc') }}</span>
          <n-button class="workflow-create-button" type="primary" @click="openWorkflowCreateChooser">
            <span class="workflow-create-button__mark" aria-hidden="true" />
            {{ t('workflows.newWorkflow') }}
          </n-button>
        </div>

        <template v-else>
          <div class="wf-overview">
            <div class="wf-overview__top">
              <div class="wf-overview__heading">
                <span class="wf-overview__icon" :class="`wf-overview__icon--${selectedWorkflowKind}`">
                  <n-icon :component="isSimpleWorkflow ? ListOutline : GitNetworkOutline" />
                </span>
                <h2 class="wf-overview__name" :title="selectedWorkflow.name">{{ selectedWorkflow.name }}</h2>
                <span class="wf-kind-badge" :class="`wf-kind-badge--${selectedWorkflowKind}`">
                  <n-icon :component="isSimpleWorkflow ? ListOutline : GitNetworkOutline" />
                  {{ selectedWorkflow ? workflowTypeLabel(selectedWorkflow) : '' }}
                </span>
                <span
                  class="wf-status"
                  :class="selectedReady ? 'wf-status--ok' : 'wf-status--warn'"
                >
                  <n-icon :component="selectedReady ? CheckmarkCircle : AlertCircleOutline" />
                  {{ selectedReady ? t('workflows.statusReady') : t('workflows.workflowValidationTitle') }}
                </span>
              </div>
              <n-popover v-if="selectedWorkflow.metadata" v-model:show="overviewInfoOpen" trigger="click" placement="bottom-start"
                :show-arrow="false" to="body">
                <template #trigger>
                  <button ref="overviewInfoButton" class="wf-overview__publication" type="button"
                    :title="selectedPublicationPreview || t('workflows.workflowInfo')" :aria-label="t('workflows.workflowInfo')"
                    :aria-expanded="overviewInfoOpen" aria-haspopup="dialog" aria-controls="workflow-overview-info"
                    @click="closeWorkflowContextMenu(); contextInfoOpen = false" @keydown.esc.stop.prevent="closeOverviewInfo">
                    <span>{{ selectedPublicationPreview || t('workflows.workflowInfo') }}</span>
                    <n-icon :component="InformationCircleOutline" />
                  </button>
                </template>
                <WorkflowInfo v-if="overviewInfoOpen" id="workflow-overview-info" :key="selectedWorkflow.id"
                  :workflow-name="selectedWorkflow.name" :metadata="selectedWorkflow.metadata" @close="closeOverviewInfo"
                  :description="selectedDescriptionPreview" @edit-description="openWorkflowDescription(selectedWorkflow, true)"
                  @view-description="openWorkflowDescription(selectedWorkflow)" />
              </n-popover>
              <WorkflowDescriptionPreview v-if="selectedDescriptionPreview" class="wf-overview__description" :text="selectedDescriptionPreview" @view="openWorkflowDescription(selectedWorkflow)" />
              <n-button class="wf-settings-button" secondary :disabled="isNodeEditorOpen" @click="settingsOpen = true">
                <template #icon><n-icon :component="SettingsOutline" /></template>
                {{ t('workflows.settingsTitle') }}
              </n-button>
            </div>

            <WorkflowOverviewPreview :key="selectedWorkflow.id" :definition="selectedWorkflow.definition" />

            <button v-if="missingModelCount" class="wf-model-warning" type="button" @click="settingsOpen = true">
              <n-icon :component="AlertCircleOutline" />
              <span>{{ t('workflows.modelsMissingCount', { count: missingModelCount }) }}</span>
            </button>

            <div v-if="selectedError" class="wf-validation">
              <n-icon :component="AlertCircleOutline" />
              <span>{{ selectedError }}</span>
            </div>

            <section v-if="isSimpleWorkflow && selectedSimpleAnalysis && !selectedSimpleAnalysis.editable" class="wf-simple-blockers">
              <strong>{{ t('workflows.advancedModeRequired') }}</strong>
              <ul>
                <li v-for="reason in selectedSimpleReasons" :key="reason">{{ simpleReasonLabel(reason) }}</li>
              </ul>
            </section>
          </div>

          <footer class="wf-actionbar">
            <div class="wf-actionbar__primary">
              <n-button v-if="selectedSimpleAnalysis?.editable || isComfyWorkflow" type="primary" size="large" @click="openWorkflowEditor(selectedWorkflow)">
                <template #icon><n-icon :component="selectedSimpleAnalysis?.editable ? ListOutline : GitNetworkOutline" /></template>
                {{ t('workflows.openEditorAction') }}
              </n-button>
            </div>
            <div ref="moreActionsElement" class="wf-actionbar__more">
              <n-dropdown :key="selectedWorkflow.id" trigger="click" placement="top-end" to="body"
                :options="selectedWorkflowMenuOptions" @select="(key: string | number) => handleWorkflowMenuSelect(key, selectedWorkflow)">
                <n-button quaternary @click="closeWorkflowContextMenu">
                  <template #icon><n-icon :component="EllipsisHorizontalOutline" /></template>
                  {{ t('workflows.moreActions') }}
                </n-button>
              </n-dropdown>
            </div>
          </footer>

        </template>

        <div v-if="isNodeEditorOpen" class="wf-lock">
          <div class="wf-lock__card">
            <n-icon :component="GitNetworkOutline" />
            <strong>{{ t('workflows.nodeEditorOpenedTitle') }}</strong>
            <span>{{ t('workflows.nodeEditorOpenedHint') }}</span>
            <n-button secondary @click="reopenActiveEditor">{{ t('workflows.backToNodeEditor') }}</n-button>
          </div>
        </div>
      </main>
    </div>

    <n-dropdown
      placement="bottom-start"
      trigger="manual"
      :x="contextMenuX"
      :y="contextMenuY"
      :options="workflowMenuOptions"
      :show="contextMenuVisible"
      @clickoutside="closeWorkflowContextMenu"
      @select="(key: string | number) => handleWorkflowMenuSelect(key, contextWorkflow)"
    />

    <WorkflowCreateChooser
      v-model:show="createChooserOpen"
      @select="selectWorkflowCreateType"
    />

    <n-popover :show="contextInfoOpen && !!contextInfoWorkflow?.metadata" trigger="manual" placement="bottom-start"
      :x="contextInfoX" :y="contextInfoY" :show-arrow="false" to="body" @clickoutside="contextInfoOpen = false">
      <WorkflowInfo v-if="contextInfoOpen && contextInfoWorkflow?.metadata" :key="contextInfoWorkflow.id"
        :workflow-name="contextInfoWorkflow.name" :metadata="contextInfoWorkflow.metadata" @close="closeContextInfo"
        :description="resolveWorkflowDescription(contextInfoWorkflow)" @edit-description="openWorkflowDescription(contextInfoWorkflow, true)"
        @view-description="openWorkflowDescription(contextInfoWorkflow)" />
    </n-popover>

    <WorkflowDescriptionDialog v-model:show="descriptionDialogOpen" :workflow-id="descriptionWorkflowId" @edit="editDescriptionFromReader" @after-leave="afterDescriptionReaderLeave" />

    <n-modal v-model:show="exportDialogOpen" preset="card" :title="t('workflows.exportWorkflow')"
      style="width: min(520px, calc(100vw - 48px)); max-height: calc(100dvh - 112px)"
      :content-style="{ minHeight: 0, overflow: 'auto' }" :bordered="false"
      :mask-closable="!exportBusy" :close-on-esc="!exportBusy" :closable="!exportBusy"
      role="dialog" aria-modal="true" :aria-label="t('workflows.exportWorkflow')">
      <n-form label-placement="top" @submit.prevent="confirmExportWorkflow">
        <n-form-item :label="t('workflows.workflowAuthor')" label-for="workflow-export-author">
          <n-input v-model:value="exportMetadata.author" :maxlength="200" :disabled="exportBusy"
            :input-props="{ id: 'workflow-export-author' }" />
        </n-form-item>
        <n-form-item :label="t('workflows.workflowReleaseVersion')" label-for="workflow-export-version">
          <n-input v-model:value="exportMetadata.version" placeholder="1.0.0" :maxlength="64" :disabled="exportBusy"
            :input-props="{ id: 'workflow-export-version' }" />
        </n-form-item>
      </n-form>
      <dl class="workflow-export-metadata">
        <div><dt>GUID</dt><dd><code>{{ exportMetadata.guid }}</code></dd></div>
        <div><dt>{{ t('workflows.workflowStudioVersion') }}</dt><dd>{{ exportStudioVersion }}</dd></div>
      </dl>
      <template #footer>
        <n-space justify="end">
          <n-button :disabled="exportBusy" @click="exportDialogOpen = false">{{ t('common.cancel') }}</n-button>
          <n-button type="primary" :loading="exportBusy" @click="confirmExportWorkflow">{{ t('workflows.exportWorkflow') }}</n-button>
        </n-space>
      </template>
    </n-modal>

    <n-modal :show="settingsOpen" preset="card" class="wf-settings-modal" :title="t('workflows.settingsTitle')"
      :mask-closable="!settingsClosing" :close-on-esc="!settingsClosing" :closable="!settingsClosing"
      @update:show="handleSettingsShow" @after-enter="focusDescriptionEditor"
      style="width: min(640px, calc(100vw - 48px)); max-height: calc(100dvh - 112px)"
      :content-style="{ minHeight: 0, overflow: 'auto' }"
      :bordered="false" role="dialog" aria-modal="true" :aria-label="t('workflows.settingsTitle')">
        <div v-if="selectedWorkflow" class="wf-settings">
          <p class="wf-muted">{{ t('workflows.settingsAutosave') }}</p>
          <section class="wf-section">
            <label for="workflow-settings-name">{{ t('workflows.settingsName') }}</label>
            <n-input :input-props="{ id: 'workflow-settings-name' }" v-model:value="name" :disabled="isNodeEditorOpen || updatingWorkflowDefaults || settingsClosing || nameSaving"
              :placeholder="t('workflows.untitled')" @blur="saveMeta" @keydown.enter="(event: KeyboardEvent) => (event.target as HTMLElement)?.blur()" />
            <WorkflowDescriptionEditor v-if="settingsOpen" :key="selectedWorkflow.id" ref="descriptionEditor" :workflow-id="selectedWorkflow.id" :disabled="settingsClosing" @keep-draft="closeWorkflowSettings(true)" />
          </section>
          <section v-if="selectedDraft && selectedSimpleAnalysis?.editable" class="wf-section">
            <h3>{{ t('workflows.runParams') }}</h3>
            <div class="wf-param-grid">
              <div class="wf-param">
                <label>{{ t('workflows.defaultDevice') }}</label>
                <n-select :value="selectedDraft.defaultDevice" :options="deviceOptions" :to="true" :disabled="isNodeEditorOpen || updatingWorkflowDefaults"
                  :loading="updatingWorkflowDefaults" :aria-label="t('workflows.defaultDevice')" @update:value="updateSelectedDefaultDevice" />
              </div>
              <div class="wf-param">
                <label>{{ t('workflows.defaultFormat') }}</label>
                <n-select :value="selectedDraft.defaultFormat" :options="formatOptions" :to="true" :disabled="isNodeEditorOpen || updatingWorkflowDefaults"
                  :loading="updatingWorkflowDefaults" :aria-label="t('workflows.defaultFormat')" @update:value="updateSelectedDefaultFormat" />
              </div>
              <div class="wf-param">
                <label>{{ t('workflows.defaultNormalize') }}</label>
                <n-select :value="normalizeMode(selectedDraft.defaultNormalize)" :options="normalizeOptions" :to="true" :disabled="isNodeEditorOpen || updatingWorkflowDefaults"
                  :loading="updatingWorkflowDefaults" :aria-label="t('workflows.defaultNormalize')" @update:value="updateSelectedDefaultNormalize" />
              </div>
            </div>
          </section>
          <p v-else-if="isComfyWorkflow" class="wf-muted">{{ t('workflows.comfyReadOnlyHint') }}</p>
          <section class="wf-section">
            <h3>{{ t('workflows.modelsUsed') }}</h3>
            <div v-if="selectedModels.length" class="wf-chips">
              <span v-for="item in selectedModels" :key="item.name" class="wf-chip" :class="{ 'wf-chip--warn': !item.downloaded }" :title="item.name">
                <n-icon :component="CubeOutline" />
                <span class="wf-chip__name">{{ item.name }}</span>
                <template v-if="!item.downloaded">
                  <small v-if="workflowModelDownloadBlocked(item)" :title="workflowModelDownloadBlocked(item)">{{ workflowModelDownloadLabel(item) }}</small>
                  <button v-else class="wf-chip__download" type="button" :disabled="workflowModelDownloadBusy(item)"
                    :aria-busy="workflowModelDownloadBusy(item)"
                    :aria-label="`${workflowModelDownloadAction(item)} ${item.name}`"
                    :title="model.downloadErrors[item.entry?.name || item.name] || `${workflowModelDownloadAction(item)} ${item.name}`"
                    @click.stop="downloadWorkflowModel(item)">
                    <span class="wf-chip__download-status">{{ workflowModelDownloadLabel(item) }}</span>
                    <span class="wf-chip__download-action"><n-icon :component="DownloadOutline" />{{ workflowModelDownloadAction(item) }}</span>
                  </button>
                </template>
              </span>
            </div>
            <p v-else class="wf-muted">{{ t('workflows.noModelsConfigured') }}</p>
          </section>
          <section v-if="isComfyWorkflow && selectedComfyOverview?.inputSlots?.length" class="wf-section">
            <h3>{{ t('workflows.inputSlots') }}</h3>
            <div class="wf-chips">
              <span v-for="slot in selectedComfyOverview.inputSlots" :key="slot" class="wf-chip" :title="slot">
                <n-icon :component="MusicalNotesOutline" />
                <span class="wf-chip__name">{{ slot }}</span>
              </span>
            </div>
          </section>
        </div>
    </n-modal>

  </div>
</template>

<style scoped>
.workflow-export-metadata { display: grid; gap: 12px; margin: 0; padding-top: 12px; border-top: 1px solid var(--outline); }
.workflow-export-metadata > div { display: grid; gap: 4px; }
.workflow-export-metadata dt { color: var(--on-surface-muted); font-size: 12px; }
.workflow-export-metadata dd { margin: 0; font-size: 13px; overflow-wrap: anywhere; user-select: text; }
.workflow-export-metadata code { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
.workflows-page {
  max-width: var(--page-max-width);
  margin: 0 auto;
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 0;
}

.wf-hidden-file-input {
  display: none;
}

.workflows-page__header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 0 0 auto;
}

.workflow-create-button {
  --n-border-radius: 9px;
  font-weight: 650;
  letter-spacing: 0.01em;
  box-shadow: 0 5px 14px color-mix(in srgb, var(--primary) 22%, transparent);
  transition: transform 160ms ease, box-shadow 160ms ease, filter 160ms ease;
}

.workflow-create-button:hover {
  transform: translateY(-1px);
  filter: saturate(1.06);
  box-shadow: 0 8px 18px color-mix(in srgb, var(--primary) 28%, transparent);
}

.workflow-create-button:active {
  transform: translateY(1px) scale(0.985);
  box-shadow: 0 3px 9px color-mix(in srgb, var(--primary) 18%, transparent);
}

.dark-theme .workflow-create-button,
.dark-theme .workflow-create-button:hover,
.dark-theme .workflow-create-button:active {
  box-shadow: none;
}

.workflow-create-button__mark {
  position: relative;
  display: inline-block;
  flex: 0 0 14px;
  width: 16px;
  height: 14px;
  margin-right: 1px;
  color: currentColor;
}

.workflow-create-button__mark::before,
.workflow-create-button__mark::after {
  position: absolute;
  top: 50%;
  left: 50%;
  width: 11px;
  height: 1.5px;
  border-radius: 1px;
  background: currentColor;
  content: '';
  transform: translate(-50%, -50%);
}

.workflow-create-button__mark::after {
  transform: translate(-50%, -50%) rotate(90deg);
}

/* ============ Console grid ============ */
.console {
  flex: 1 1 auto;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(300px, 360px) minmax(0, 1fr);
  gap: 16px;
}

/* ============ Rail (list) ============ */
.console__rail {
  min-height: 0;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  gap: 12px;
  padding: 15px 14px;
  border-radius: 18px;
  background: color-mix(in srgb, var(--surface-1) 78%, transparent);
  box-shadow:
    inset 0 0 0 1px color-mix(in srgb, var(--outline) 80%, transparent),
    var(--shadow-panel);
}

.wf-list-head {
  display: grid;
  gap: 10px;
}

.wf-type-filter {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 4px;
  padding: 4px;
  border-radius: 11px;
  background: color-mix(in srgb, var(--surface-2) 46%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 58%, transparent);
}

.wf-type-filter button {
  min-width: 0;
  height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--on-surface-muted);
  cursor: pointer;
  font: inherit;
  font-size: 11px;
  font-weight: 600;
  transition: background 150ms ease, color 150ms ease, transform 150ms ease;
}

.wf-type-filter button:hover {
  color: var(--on-surface);
  background: color-mix(in srgb, var(--surface-1) 66%, transparent);
}

.wf-type-filter button:active {
  transform: scale(0.98);
}

.wf-type-filter button:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--primary) 72%, transparent);
  outline-offset: 1px;
}

.wf-type-filter button.active {
  color: var(--on-surface);
  background: color-mix(in srgb, var(--surface-1) 92%, transparent);
  box-shadow: 0 2px 8px color-mix(in srgb, var(--primary) 10%, transparent);
}

.wf-type-filter small {
  min-width: 15px;
  color: inherit;
  font-size: 10px;
  font-variant-numeric: tabular-nums;
  opacity: 0.72;
}

.wf-list-scroll {
  min-height: 0;
  overflow: auto;
  margin: 0 -4px;
  padding: 0 4px;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
  scrollbar-width: thin;
  scrollbar-color: color-mix(in srgb, var(--on-surface-muted) 44%, transparent) transparent;
}

@supports selector(::-webkit-scrollbar) {
  .wf-list-scroll {
    scrollbar-width: auto;
    scrollbar-color: auto;
  }
}

.wf-list-scroll::-webkit-scrollbar {
  width: 9px;
  height: 9px;
}

.wf-list-scroll::-webkit-scrollbar-track {
  border-radius: 999px;
  background: transparent;
}

.wf-list-scroll::-webkit-scrollbar-thumb {
  min-height: 28px;
  border: 2px solid transparent;
  border-radius: 999px;
  background: color-mix(in srgb, var(--on-surface-muted) 44%, transparent) padding-box;
}

.wf-list-scroll::-webkit-scrollbar-thumb:hover {
  background: color-mix(in srgb, var(--primary) 64%, transparent) padding-box;
}

.wf-list-scroll::-webkit-scrollbar-button,
.wf-list-scroll::-webkit-scrollbar-corner {
  display: none;
  width: 0;
  height: 0;
  background: transparent;
}

.wf-list {
  display: grid;
  gap: 14px;
}

.wf-list-group {
  display: grid;
  gap: 6px;
}

.wf-list-group__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 5px;
  color: var(--on-surface-muted);
  font-size: 11px;
  font-weight: 650;
}

.wf-list-group__head small {
  font-size: 10px;
  font-variant-numeric: tabular-nums;
  opacity: 0.72;
}

.wf-row {
  display: grid;
  grid-template-columns: 30px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px;
  border: 1px solid transparent;
  border-radius: 13px;
  background: color-mix(in srgb, var(--surface-2) 38%, transparent);
  color: var(--on-surface);
  cursor: pointer;
  text-align: left;
  font: inherit;
  transition: background 140ms ease, border-color 140ms ease;
}

.wf-row:hover {
  background: color-mix(in srgb, var(--surface-2) 60%, transparent);
}

.wf-row--active {
  border-color: color-mix(in srgb, var(--primary) 40%, var(--outline));
  background:
    linear-gradient(180deg, color-mix(in srgb, var(--primary-soft) 26%, transparent), transparent 74%),
    color-mix(in srgb, var(--surface-2) 62%, transparent);
  box-shadow: inset 3px 0 0 color-mix(in srgb, var(--primary) 78%, transparent);
}

.wf-row__icon {
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  border-radius: 9px;
  color: color-mix(in srgb, var(--primary-strong) 76%, var(--on-surface-muted));
  background: color-mix(in srgb, var(--primary-soft) 32%, var(--surface-2));
}

.wf-row--advanced .wf-row__icon {
  color: color-mix(in srgb, var(--primary-strong) 88%, var(--on-surface));
  background: color-mix(in srgb, var(--surface-1) 70%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary) 24%, var(--outline));
}

.wf-row--invalid .wf-row__icon {
  color: var(--warning);
  background: color-mix(in srgb, var(--warning) 12%, var(--surface-2));
}

.wf-row__main {
  min-width: 0;
  display: grid;
  gap: 2px;
}

.wf-row__main strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 600;
}

.wf-row__meta {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
}

.wf-row__type {
  flex: 0 0 auto;
  padding: 1px 5px;
  border-radius: 5px;
  color: color-mix(in srgb, var(--primary-strong) 82%, var(--on-surface));
  background: color-mix(in srgb, var(--primary-soft) 40%, transparent);
  font-size: 9px;
  font-weight: 700;
}

.wf-row--advanced .wf-row__type {
  background: transparent;
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary) 28%, var(--outline));
}

.wf-row--invalid .wf-row__type {
  color: var(--warning);
  background: color-mix(in srgb, var(--warning) 12%, transparent);
}

.wf-row__summary {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--on-surface-muted);
  font-size: 10px;
}

.wf-row__dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  flex: 0 0 auto;
}

.wf-row__dot--ok {
  background: var(--success);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--success) 18%, transparent);
}

.wf-row__dot--warn {
  background: var(--warning);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--warning) 18%, transparent);
}

.wf-empty {
  min-height: 240px;
  display: grid;
  place-items: center;
  align-content: center;
  gap: 10px;
  text-align: center;
  color: var(--on-surface-muted);
}

.wf-empty .n-icon {
  font-size: 40px;
  color: var(--primary-strong);
}

.wf-empty strong {
  color: var(--on-surface);
  font-size: 14px;
}

.wf-empty span {
  font-size: 12px;
  max-width: 220px;
  line-height: 1.5;
}

/* ============ Stage (overview) ============ */
.console__stage {
  position: relative;
  min-height: 0;
  min-width: 0;
  display: flex;
  flex-direction: column;
  border-radius: 18px;
  background: color-mix(in srgb, var(--surface-1) 78%, transparent);
  box-shadow:
    inset 0 0 0 1px color-mix(in srgb, var(--outline) 80%, transparent),
    var(--shadow-panel);
  overflow: hidden;
}

.dark-theme .console__rail,
.dark-theme .console__stage {
  background: var(--surface-1);
  box-shadow: inset 0 0 0 1px var(--outline);
}

.wf-overview-empty {
  flex: 1;
  display: grid;
  place-items: center;
  align-content: center;
  gap: 12px;
  padding: 32px;
  text-align: center;
  color: var(--on-surface-muted);
}

.wf-overview-empty .n-icon {
  font-size: 52px;
  color: var(--primary-strong);
}

.wf-overview-empty strong {
  color: var(--on-surface);
  font-size: 17px;
}

.wf-overview-empty span {
  font-size: 13px;
  max-width: 340px;
  line-height: 1.55;
}

.wf-overview {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: 22px;
}

/* --- heading + meta edit --- */
.wf-overview__top {
  flex: 0 0 auto;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
}

.wf-overview__name {
  flex: 1;
  min-width: 0;
  margin: 0;
  font-size: 22px;
  line-height: 1.4;
  letter-spacing: -0.02em;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  max-height: 2.8em;
  overflow: hidden;
  overflow-wrap: anywhere;
}

.wf-overview__description {
  grid-column: 1 / -1;
}

.wf-overview__publication {
  grid-column: 1 / -1;
  justify-self: start;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  max-width: 100%;
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--on-surface-muted);
  font: inherit;
  font-size: 12px;
  line-height: 1.5;
  text-align: left;
  cursor: pointer;
}

.wf-overview__publication span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.wf-overview__publication .n-icon { flex-shrink: 0; font-size: 15px; }
.wf-overview__publication:hover { color: var(--primary-strong); }
.wf-overview__publication:focus-visible { outline: 2px solid var(--primary); outline-offset: 4px; border-radius: 2px; }

.wf-settings-button { grid-column: 2; grid-row: 1; align-self: start; margin-top: 3px; }
.wf-settings-modal .wf-param-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.wf-settings-modal .wf-param:last-child { grid-column: 1 / -1; }
.wf-settings { display: grid; gap: 28px; }
.wf-settings .wf-section > label, .wf-settings .wf-param > label { color: var(--on-surface-muted); font-size: 12px; font-weight: 550; }
.wf-model-warning { display: flex; align-items: center; gap: 7px; flex-shrink: 0; border: 0; padding: 0; background: transparent; color: var(--warning); font: inherit; font-size: 12px; text-align: left; cursor: pointer; }
.wf-model-warning:hover { text-decoration: underline; }
.wf-model-warning:focus-visible { outline: 2px solid var(--primary); outline-offset: 4px; }

.wf-overview__heading {
  grid-column: 1;
  grid-row: 1;
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
  flex-wrap: wrap;
}

.wf-overview__icon {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  flex: 0 0 auto;
  border-radius: 12px;
  font-size: 20px;
  color: color-mix(in srgb, var(--primary-strong) 82%, var(--on-surface));
  background: color-mix(in srgb, var(--primary-soft) 34%, var(--surface-2));
}

.wf-overview__icon--advanced {
  background: color-mix(in srgb, var(--surface-2) 68%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary) 24%, var(--outline));
}

.wf-overview__icon--invalid {
  color: var(--warning);
  background: color-mix(in srgb, var(--warning) 12%, var(--surface-2));
}

.wf-kind-badge {
  flex: 0 0 auto;
  height: 25px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 0 9px;
  border-radius: 7px;
  color: color-mix(in srgb, var(--primary-strong) 84%, var(--on-surface));
  background: color-mix(in srgb, var(--primary-soft) 38%, transparent);
  font-size: 10px;
  font-weight: 700;
}

.wf-kind-badge .n-icon {
  font-size: 13px;
}

.wf-kind-badge--advanced {
  background: transparent;
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary) 30%, var(--outline));
}

.wf-kind-badge--invalid {
  color: var(--warning);
  background: color-mix(in srgb, var(--warning) 12%, transparent);
}

.wf-status {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  padding: 0 12px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
}

.wf-status .n-icon {
  font-size: 15px;
}

.wf-status--ok {
  color: color-mix(in srgb, var(--success) 82%, white 6%);
  background: color-mix(in srgb, var(--success) 15%, transparent);
}

.wf-status--warn {
  color: color-mix(in srgb, var(--warning) 84%, white 6%);
  background: color-mix(in srgb, var(--warning) 15%, transparent);
}

/* --- sections --- */
.wf-section {
  display: grid;
  gap: 10px;
}

.wf-section h3 {
  margin: 0;
  color: var(--on-surface-muted);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.wf-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.wf-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  padding: 6px 12px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--surface-2) 56%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 64%, transparent);
  font-size: 12px;
}

.wf-chip .n-icon {
  flex: 0 0 auto;
  font-size: 14px;
  color: color-mix(in srgb, var(--primary-strong) 78%, var(--on-surface-muted));
}

.wf-chip__name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.wf-chip small,
.wf-chip__download {
  flex: 0 0 auto;
  padding: 1px 7px;
  border-radius: 999px;
  color: color-mix(in srgb, var(--warning) 86%, white 6%);
  background: color-mix(in srgb, var(--warning) 18%, transparent);
  font-size: 10px;
  font-weight: 700;
}

.wf-chip__download { display: inline-grid; align-items: center; border: 0; font-family: inherit; line-height: 1.5; white-space: nowrap; cursor: pointer; }
.wf-chip__download-status, .wf-chip__download-action { grid-area: 1 / 1; }
.wf-chip__download-action { display: inline-flex; visibility: hidden; align-items: center; justify-content: center; gap: 3px; }
.wf-chip__download:not(:disabled):is(:hover, :focus-visible) { color: var(--primary-strong); background: var(--primary-soft); }
.wf-chip__download:not(:disabled):is(:hover, :focus-visible) .wf-chip__download-status { visibility: hidden; }
.wf-chip__download:not(:disabled):is(:hover, :focus-visible) .wf-chip__download-action { visibility: visible; }
.wf-chip__download:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.wf-chip__download:disabled { cursor: default; opacity: 0.75; }
.wf-chip .wf-chip__download .n-icon { font-size: 11px; color: inherit; }

.wf-chip--warn {
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--warning) 40%, var(--outline));
}

.wf-chip--warn .n-icon {
  color: color-mix(in srgb, var(--warning) 78%, var(--on-surface-muted));
}

.wf-muted {
  margin: 0;
  color: var(--on-surface-muted);
  font-size: 12px;
}

.wf-param-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 18px;
}

.wf-param {
  display: grid;
  gap: 8px;
}

.wf-param span {
  color: var(--on-surface-muted);
  font-size: 11px;
  font-weight: 600;
}

.wf-param strong {
  font-size: 14px;
  font-weight: 600;
}

.wf-batch-list {
  display: grid;
  gap: 8px;
}

.wf-batch {
  display: grid;
  gap: 3px;
  padding: 10px 12px;
  border-radius: 12px;
  background: color-mix(in srgb, var(--surface-2) 42%, transparent);
}

.wf-batch strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 600;
}

.wf-batch span {
  color: var(--on-surface-muted);
  font-size: 12px;
}

.wf-batch-count {
  color: color-mix(in srgb, var(--primary-strong) 84%, var(--on-surface));
  font-size: 12px;
  font-weight: 600;
}

.wf-validation {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 12px 14px;
  border-radius: 12px;
  border: 1px solid color-mix(in srgb, var(--warning) 48%, var(--outline));
  background: color-mix(in srgb, var(--warning) 12%, transparent);
  color: color-mix(in srgb, var(--warning) 84%, white 8%);
  font-size: 12px;
  line-height: 1.5;
}

.wf-validation .n-icon {
  flex: 0 0 auto;
  margin-top: 1px;
  font-size: 16px;
}

.wf-simple-blockers {
  display: grid;
  gap: 8px;
  padding: 12px 14px;
  border: 1px solid color-mix(in srgb, var(--outline) 88%, transparent);
  border-radius: 12px;
  background: color-mix(in srgb, var(--surface-2) 42%, transparent);
}

.wf-simple-blockers strong {
  font-size: 12px;
}

.wf-simple-blockers ul {
  display: grid;
  gap: 4px;
  margin: 0;
  padding-left: 18px;
  color: var(--on-surface-muted);
  font-size: 12px;
  line-height: 1.45;
}

/* --- action bar --- */
.wf-actionbar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  padding: 14px 22px;
  border-top: 1px solid color-mix(in srgb, var(--outline) 60%, transparent);
  background: color-mix(in srgb, var(--surface-2) 40%, transparent);
}

.wf-actionbar__primary,
.wf-actionbar__more {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

/* --- node editor lock --- */
.wf-lock {
  position: absolute;
  inset: 0;
  z-index: 8;
  display: grid;
  place-items: center;
  padding: 24px;
  background: color-mix(in srgb, var(--surface-1) 74%, transparent);
  backdrop-filter: blur(10px) saturate(1.08);
}

.wf-lock__card {
  width: min(360px, 100%);
  display: grid;
  justify-items: center;
  gap: 10px;
  padding: 26px;
  text-align: center;
  border-radius: 18px;
  background: color-mix(in srgb, var(--surface-2) 84%, transparent);
  box-shadow: 0 18px 44px rgba(0, 0, 0, 0.14);
}

.wf-lock__card .n-icon {
  font-size: 34px;
  color: var(--primary-strong);
}

.wf-lock__card strong {
  font-size: 15px;
}

.wf-lock__card span {
  color: var(--on-surface-muted);
  font-size: 13px;
  line-height: 1.6;
}

/* ============ Responsive ============ */
@media (max-width: 1100px) {
  .console { grid-template-columns: minmax(240px, 280px) minmax(0, 1fr); }
}

@media (max-width: 960px) {
  .console {
    grid-template-columns: minmax(230px, 30%) minmax(0, 1fr);
  }
  .wf-overview__top { padding-right: 0; }
  .wf-settings-button { position: static; justify-self: start; }
  .wf-overview { padding: 18px; }
  .wf-overview__name { font-size: 19px; }

  .wf-param-grid {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 680px) {
  .console { display: flex; flex-direction: column; overflow: auto; }
  .console__rail { flex: 0 0 230px; }
  .console__stage { flex: 0 0 560px; }
  .workflows-page__header-actions { gap: 6px; flex-wrap: wrap; }
}

@media (max-width: 520px) {
  .wf-settings-modal .wf-param-grid { grid-template-columns: 1fr; }
}
</style>
