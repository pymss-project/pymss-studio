<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useDialog, useMessage, type DropdownOption, type SelectInst } from 'naive-ui'
import {
  ArrowRedoOutline,
  ArrowUndoOutline,
  CloseOutline,
  LocateOutline,
  SaveOutline,
  SettingsOutline,
} from '@vicons/ionicons5'
import type { ModelDefaultInferenceParams, ModelEntry } from '@/stores/model'
import AlignedInferenceInputNumber from '@/components/AlignedInferenceInputNumber.vue'
import { inferenceChunkSizeIssue, resolveInferenceChunkStep, resolveInferenceSampleStep } from '@/features/inference/sampleStep'
import { hasInvalidSimpleInferenceNumbers } from '@/workflows/formats'
import {
  configuredStemsFor,
  createDefaultSimpleEditorUi,
  createEnsembleDraft,
  createAudioOperationDraft,
  createStepDraft,
  renderSimpleOutputFilename,
  fitSimpleEditorViewport,
  normalizeSimpleInferenceParams,
  SIMPLE_ENSEMBLE_ALGORITHMS,
  SIMPLE_AUDIO_OPERATIONS,
  isSimpleAudioOperation,
  simpleProcessingInputLimits,
  type SimpleAudioOperation,
  type SimpleEnsembleAlgorithm,
  type SimpleDraft,
  type SimpleEditorPoint,
  type SimpleEnsembleDraft,
  type SimpleStepDraft,
  type SimpleWorkflowInferenceParams,
} from '@/utils/workflowSimple'
import {
  analyzeSimpleModelChangeImpact,
  analyzeSimpleNodeRemovalImpact,
  canMoveSimpleStep,
  canConnectSimple,
  cleanupSimpleDraft,
  connectSimple,
  disconnectSimple,
  moveSimpleStep,
  simpleEnsembleInputTarget,
  simpleSourceStepId,
  simpleSourceStem,
  simpleOutputRef,
  simpleSaveTarget,
  simpleStepInputTarget,
  updateSimpleEnsembleOutputStem,
} from '@/utils/simpleWorkflowEditor'

const draft = defineModel<SimpleDraft>('draft', { required: true })
const name = defineModel<string>('name', { required: true })
const description = defineModel<string>('description', { required: true })

const props = withDefaults(defineProps<{
  models: ModelEntry[]
  modelInferenceOverrides?: Record<string, ModelDefaultInferenceParams>
  saving?: boolean
  formError?: string
  advisory?: string
  canSave?: boolean
  canRun?: boolean
}>(), {
  modelInferenceOverrides: () => ({}),
  saving: false,
  formError: '',
  advisory: '',
  canSave: false,
  canRun: false,
})

const emit = defineEmits<{
  save: []
  close: []
  run: []
}>()

const { t, locale } = useI18n()
const message = useMessage()
const dialog = useDialog()
const connectionMessageKeys: Record<string, string> = {
  'missing-source': 'workflows.simpleConnection.missingSource',
  'missing-target': 'workflows.simpleConnection.missingTarget',
  'invalid-source': 'workflows.simpleConnection.invalidSource',
  'forward-link': 'workflows.simpleConnection.forwardLink',
  'self-link': 'workflows.simpleConnection.selfLink',
  'duplicate-source': 'workflows.simpleConnection.duplicateSource',
  'invalid-save-target': 'workflows.simpleConnection.invalidSaveTarget',
}
const canvasRef = ref<HTMLElement | null>(null)
const worldRef = ref<HTMLElement | null>(null)
type SimpleConnectionTarget = `step:${string}` | `ensemble:${string}:${number}` | `save` | `save:${string}.${string}`
type SimpleConnectionHoverTarget = SimpleConnectionTarget | `output:${string}` | null
type PendingConnection =
  | { direction: 'input'; source: string; label: string }
  | { direction: 'output'; target: Exclude<SimpleConnectionTarget, 'save'>; label: string }
type SimpleNodeType = 'separation' | 'ensemble' | SimpleAudioOperation
const pendingConnection = ref<PendingConnection | null>(null)
const hoverTarget = ref<SimpleConnectionHoverTarget>(null)
const pointerWorld = ref<SimpleEditorPoint>({ x: 500, y: 300 })
const showNodeTypeChooser = ref(false)
const showInferenceEditor = ref(false)
const inferenceEditorStepId = ref('')
const inferenceMode = ref<'global' | 'custom'>('global')
const inferenceDraft = ref<SimpleWorkflowInferenceParams>({})
const initialInferenceDraft = ref<SimpleWorkflowInferenceParams>({})
const initialInferenceParams = ref<SimpleWorkflowInferenceParams | undefined>()
const pendingNodePoint = ref<SimpleEditorPoint | null>(null)
const contextMenuVisible = ref(false)
const contextMenuX = ref(0)
const contextMenuY = ref(0)
const contextMenuPoint = ref<SimpleEditorPoint | null>(null)
const selectedNodeId = ref('')
const history = ref<string[]>([])
const future = ref<string[]>([])
const drag = ref<{ id: string; dx: number; dy: number } | null>(null)
const pan = ref<{ x: number; y: number } | null>(null)
const zoom = computed(() => draft.value.ui.viewport.zoom)
const layoutVersion = ref(0)
const portElements = new Map<string, HTMLElement>()
const selectInstances = new Map<string, SelectInst>()
let layoutFrame = 0
let portResizeObserver: ResizeObserver | null = null
let lastHistoryGroup = ''
let lastHistoryAt = 0

const NODE_WIDTH = 300
const ENSEMBLE_WIDTH = 320
const INPUT_WIDTH = 230
const SAVE_WIDTH = 340
const NODE_HEIGHT_BASE = 166
const PORT_GAP = 26
const SAVE_ROW_GAP = 84

function nodesOverlap(
  first: SimpleEditorPoint,
  firstWidth: number,
  firstHeight: number,
  second: SimpleEditorPoint,
  secondWidth: number,
  secondHeight: number,
  gap = 20,
) {
  return first.x < second.x + secondWidth + gap
    && first.x + firstWidth + gap > second.x
    && first.y < second.y + secondHeight + gap
    && first.y + firstHeight + gap > second.y
}

const modelOptions = computed(() => [...props.models]
  .sort((a, b) => a.name.localeCompare(b.name, locale.value === 'zh-CN' ? 'zh-CN' : 'en'))
  .map(item => ({ label: item.name, value: item.name })))

const defaultNormalizeMode = computed({
  get: () => draft.value.defaultNormalize === null
    ? 'global'
    : draft.value.defaultNormalize ? 'enabled' : 'disabled',
  set: (value: string) => {
    draft.value.defaultNormalize = value === 'global' ? null : value === 'enabled'
  },
})

const defaultNormalizeOptions = computed(() => [
  { label: t('workflows.inferenceFollowGlobal'), value: 'global' },
  { label: t('workflows.inferenceEnabled'), value: 'enabled' },
  { label: t('workflows.inferenceDisabled'), value: 'disabled' },
])

const inferenceEditorStep = computed(() => (
  draft.value.steps.find(step => step.id === inferenceEditorStepId.value) || null
))
const inferenceEditorModel = computed(() => {
  const modelName = inferenceEditorStep.value?.model || ''
  return props.models.find(model => model.name === modelName || model.aliases?.includes(modelName)) || null
})
const inferenceEditorModelType = computed(() => String(
  inferenceEditorStep.value?.modelType?.trim() || inferenceEditorModel.value?.modelType || '',
).trim().toLowerCase())
const inferenceEditorIsVr = computed(() => inferenceEditorModelType.value === 'vr')
const inferenceEditorSampleStep = computed(() => inferenceEditorModel.value?.inferenceParamMeta?.recommendedSampleStep)
const inferenceEditorOverlapStep = computed(() => resolveInferenceSampleStep(inferenceEditorModel.value?.inferenceParamMeta, 1))
const inferenceEditorChunkStep = computed(() => resolveInferenceChunkStep(inferenceEditorModel.value?.inferenceParamMeta, 1024))
const inferenceEditorChunkConstraint = computed(() => inferenceEditorModel.value?.inferenceParamMeta?.chunkSizeConstraint)
const inferenceEditorChunkIssue = computed(() => inferenceChunkSizeIssue(inferenceDraft.value.chunk_size, inferenceEditorModel.value?.inferenceParamMeta))

function inferenceNumber(key: keyof SimpleWorkflowInferenceParams, fallback: number) {
  const value = inferenceDraft.value[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function inferenceBoolean(key: keyof SimpleWorkflowInferenceParams, fallback = false) {
  const value = inferenceDraft.value[key]
  return typeof value === 'boolean' ? value : fallback
}

function updateInferenceNumber(key: keyof SimpleWorkflowInferenceParams, value: number | null) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return
  inferenceDraft.value = { ...inferenceDraft.value, [key]: value }
}

function updateInferenceBoolean(key: keyof SimpleWorkflowInferenceParams, value: boolean) {
  inferenceDraft.value = { ...inferenceDraft.value, [key]: value }
}

function modelInferenceDefaults(model: ModelEntry | null): SimpleWorkflowInferenceParams {
  const step = inferenceEditorStep.value
  const defaults = {
    ...(model?.defaultInferenceParams || {}),
    ...(props.modelInferenceOverrides[model?.name || step?.model || ''] || {}),
  } as SimpleWorkflowInferenceParams
  const modelType = inferenceEditorModelType.value
  for (const key of ['batch_size', 'overlap_size', 'chunk_size', 'window_size'] as const) {
    const value = defaults[key]
    if (typeof value === 'number' && value <= 0) delete defaults[key]
  }
  if (defaults.batch_size === undefined) defaults.batch_size = 1
  if (modelType === 'vr') {
    if (defaults.window_size === undefined) defaults.window_size = 512
    if (defaults.aggression === undefined) defaults.aggression = 5
    if (defaults.enable_post_process === undefined) defaults.enable_post_process = false
    if (defaults.post_process_threshold === undefined) defaults.post_process_threshold = 0.2
    if (defaults.high_end_process === undefined) defaults.high_end_process = false
    if (defaults.normalize === undefined) defaults.normalize = false
  } else {
    if (defaults.standardize === undefined) defaults.standardize = false
    if (defaults.normalize === undefined) defaults.normalize = false
  }
  if (defaults.enable_tta === undefined) defaults.enable_tta = false
  if (draft.value.defaultNormalize !== null) defaults.normalize = draft.value.defaultNormalize
  return defaults
}

function openInferenceEditor(step: SimpleStepDraft) {
  inferenceEditorStepId.value = step.id
  inferenceMode.value = step.inferenceParams ? 'custom' : 'global'
  initialInferenceParams.value = step.inferenceParams ? { ...step.inferenceParams } : undefined
  initialInferenceDraft.value = {
    ...modelInferenceDefaults(inferenceEditorModel.value),
    ...(step.inferenceParams || {}),
  }
  inferenceDraft.value = { ...initialInferenceDraft.value }
  showInferenceEditor.value = true
}

function updateInferenceFollowGlobal(value: boolean) {
  if (value) {
    inferenceDraft.value = modelInferenceDefaults(inferenceEditorModel.value)
    initialInferenceDraft.value = { ...inferenceDraft.value }
    initialInferenceParams.value = undefined
    inferenceMode.value = 'global'
    return
  }
  inferenceMode.value = 'custom'
}

function closeInferenceEditor() {
  showInferenceEditor.value = false
  inferenceEditorStepId.value = ''
}

function saveInferenceEditor() {
  const step = inferenceEditorStep.value
  if (!step) return
  if (inferenceMode.value === 'custom' && inferenceEditorChunkIssue.value) {
    message.warning(t('inference.invalidChunkSize', inferenceEditorChunkIssue.value))
    return
  }
  if (inferenceMode.value === 'global') {
    step.inferenceParams = undefined
  } else {
    const original = initialInferenceParams.value
    const params = original
      ? Object.fromEntries(Object.entries(inferenceDraft.value).filter(([key, value]) => (
          Object.hasOwn(original, key)
          || value !== initialInferenceDraft.value[key as keyof SimpleWorkflowInferenceParams]
        )))
      : inferenceDraft.value
    const invalid = Object.entries(params).find(([key, value]) => hasInvalidSimpleInferenceNumbers({ [key]: value }))
    if (invalid) {
      message.warning(t('workflows.invalidInferenceNumber', { parameter: invalid[0], value: String(invalid[1]) }))
      return
    }
    step.inferenceParams = normalizeSimpleInferenceParams(params)
  }
  recordHistory()
  closeInferenceEditor()
}

const ensembleAlgorithmOptions = computed(() => {
  const labels: Record<SimpleEnsembleAlgorithm, string> = {
    avg_wave: t('workflows.ensembleAlgorithms.avg_wave'),
    median_wave: t('workflows.ensembleAlgorithms.median_wave'),
    min_wave: t('workflows.ensembleAlgorithms.min_wave'),
    max_wave: t('workflows.ensembleAlgorithms.max_wave'),
    avg_fft: t('workflows.ensembleAlgorithms.avg_fft'),
    median_fft: t('workflows.ensembleAlgorithms.median_fft'),
    min_fft: t('workflows.ensembleAlgorithms.min_fft'),
    max_fft: t('workflows.ensembleAlgorithms.max_fft'),
  }
  return SIMPLE_ENSEMBLE_ALGORITHMS.map(value => ({ value, label: labels[value] }))
})

const audioOperationCopy = computed(() => ({
  sum: {
    title: t('workflows.audioOperations.sum.title'),
    description: t('workflows.audioOperations.sum.description'),
    hint: t('workflows.audioOperations.sum.hint'),
  },
  subtract: {
    title: t('workflows.audioOperations.subtract.title'),
    description: t('workflows.audioOperations.subtract.description'),
    hint: t('workflows.audioOperations.subtract.hint'),
  },
  invert: {
    title: t('workflows.audioOperations.invert.title'),
    description: t('workflows.audioOperations.invert.description'),
    hint: t('workflows.audioOperations.invert.hint'),
  },
}))

function processingNodeLabel(ensemble: SimpleEnsembleDraft) {
  return isSimpleAudioOperation(ensemble.algorithm)
    ? audioOperationCopy.value[ensemble.algorithm].title
    : t('workflows.ensembleNode')
}

function processingInputLabel(ensemble: SimpleEnsembleDraft, index: number) {
  if (ensemble.algorithm === 'subtract') return index === 0 ? 'A' : 'B'
  return t('workflows.ensembleInput', { index: index + 1 })
}

const stepOutputOptions = computed(() => draft.value.steps.flatMap(step => step.stems.map(stem => ({
  value: simpleOutputRef(step.id, stem),
  label: `${step.model || step.id} · ${stem}`,
}))))

const ensembleOutputOptions = computed(() => draft.value.ensembles.flatMap(ensemble => {
  const stem = ensemble.outputStem.trim()
  return stem ? [{
    value: simpleOutputRef(ensemble.id, stem),
    label: `${processingNodeLabel(ensemble)} · ${stem}`,
  }] : []
}))

function ensembleSourceOptionsFor(ensemble: SimpleEnsembleDraft) {
  const index = draft.value.ensembles.findIndex(item => item.id === ensemble.id)
  return [
    { value: 'input', label: t('workflows.originalInput') },
    ...stepOutputOptions.value,
    ...draft.value.ensembles.slice(0, Math.max(0, index)).flatMap(source => {
      const stem = source.outputStem.trim()
      return stem ? [{
        value: simpleOutputRef(source.id, stem),
        label: `${processingNodeLabel(source)} · ${stem}`,
      }] : []
    }),
  ]
}

function stepInputOptions(step: SimpleStepDraft, index: number) {
  const options = [
    { label: t('workflows.originalInput'), value: 'input' },
    ...draft.value.steps.slice(0, index).flatMap(source => source.stems.map(stem => ({
      label: `${source.model || source.id} · ${stem}`,
      value: simpleOutputRef(source.id, stem),
    }))),
    ...draft.value.ensembles.flatMap(ensemble => {
      const stem = ensemble.outputStem.trim()
      if (!stem) return []
      const value = simpleOutputRef(ensemble.id, stem)
      return canConnectSimple(draft.value, value, simpleStepInputTarget(step.id)).ok
        ? [{ label: `${processingNodeLabel(ensemble)} · ${stem}`, value }]
        : []
    }),
  ]
  return options
}

function ensembleSourceLabel(source: string) {
  if (source === 'input') return t('workflows.originalInput')
  return stepOutputOptions.value.find(option => option.value === source)?.label
    || ensembleOutputOptions.value.find(option => option.value === source)?.label
    || source
    || t('workflows.ensembleInputPlaceholder')
}

const nodeTypeMenuOptions = computed<DropdownOption[]>(() => [
  { key: 'separation', label: t('workflows.separationNode') },
  { key: 'ensemble', label: t('workflows.ensembleNode') },
  ...SIMPLE_AUDIO_OPERATIONS.map(key => ({ key, label: audioOperationCopy.value[key].title })),
])

function preferredEnsembleSources(limit = 2) {
  for (const stem of draft.value.steps.flatMap(step => step.stems)) {
    const matches = draft.value.steps.flatMap(step => {
      const matchedStem = step.stems.find(item => item.toLowerCase() === stem.toLowerCase())
      return matchedStem ? [simpleOutputRef(step.id, matchedStem)] : []
    })
    if (matches.length >= limit) return matches.slice(0, limit)
  }
  if (limit >= 2 && stepOutputOptions.value.length) {
    return ['input', stepOutputOptions.value[0].value]
  }
  return stepOutputOptions.value.slice(0, limit).map(option => option.value)
}

function snapshot() {
  return JSON.stringify(draft.value)
}

function restore(serialized: string) {
  const restored = JSON.parse(serialized) as SimpleDraft
  draft.value = restored
  cleanupSimpleDraft(draft.value)
}

function recordHistory(group = '', coalesceMs = 500) {
  const current = snapshot()
  const previous = history.value[history.value.length - 1]
  if (current === previous) return
  const now = Date.now()
  if (group && group === lastHistoryGroup && now - lastHistoryAt <= coalesceMs && history.value.length > 1) {
    history.value = [...history.value.slice(0, -1), current]
  } else {
    history.value = [...history.value, current].slice(-80)
  }
  future.value = []
  lastHistoryGroup = group
  lastHistoryAt = now
}

function undo() {
  if (history.value.length <= 1) return
  lastHistoryGroup = ''
  lastHistoryAt = 0
  const current = history.value[history.value.length - 1]
  future.value = [current, ...future.value].slice(0, 80)
  history.value = history.value.slice(0, -1)
  restore(history.value[history.value.length - 1])
}

function redo() {
  const next = future.value[0]
  if (!next) return
  lastHistoryGroup = ''
  lastHistoryAt = 0
  future.value = future.value.slice(1)
  history.value = [...history.value, next].slice(-80)
  restore(next)
}

function nodePoint(id: string): SimpleEditorPoint {
  return draft.value.ui.nodes[id] || { x: 360, y: 190 }
}

function setNodePoint(id: string, point: SimpleEditorPoint) {
  draft.value.ui.nodes = { ...draft.value.ui.nodes, [id]: { x: Math.round(point.x), y: Math.round(point.y) } }
  scheduleLayoutRefresh()
}

function scheduleLayoutRefresh() {
  if (layoutFrame) return
  if (typeof requestAnimationFrame !== 'function') {
    layoutVersion.value += 1
    return
  }
  layoutFrame = requestAnimationFrame(() => {
    layoutFrame = 0
    layoutVersion.value += 1
  })
}

function setPortElement(key: string, element: unknown) {
  if (element && typeof element === 'object' && 'getBoundingClientRect' in element) {
    const node = element as HTMLElement
    portElements.set(key, node)
    portResizeObserver?.observe(node)
  } else {
    portElements.delete(key)
  }
  scheduleLayoutRefresh()
}

function setSelectInstance(key: string, instance: unknown) {
  if (instance && typeof instance === 'object' && 'blur' in instance) {
    selectInstances.set(key, instance as SelectInst)
  } else {
    selectInstances.delete(key)
  }
}

function closeSelectMenus() {
  selectInstances.forEach((instance) => instance.blur())
}

function portPoint(key: string): SimpleEditorPoint | null {
  void layoutVersion.value
  const element = portElements.get(key)
  const world = worldRef.value
  if (!element || !world) return null
  const socket = element.classList.contains('simple-save-row__port')
    ? element
    : element.querySelector<HTMLElement>('i') || element
  const worldRect = world.getBoundingClientRect()
  const socketRect = socket.getBoundingClientRect()
  return {
    x: (socketRect.left + socketRect.width / 2 - worldRect.left) / zoom.value,
    y: (socketRect.top + socketRect.height / 2 - worldRect.top) / zoom.value,
  }
}

function outputPortKey(stepId: string, stem: string) {
  return `output:${simpleOutputRef(stepId, stem)}`
}

function ensembleInputPortKey(ensembleId: string, index: number) {
  return `ensemble-input:${ensembleId}:${index}`
}

function savePortKey(stepId: string, stem: string) {
  return `save:${simpleOutputRef(stepId, stem)}`
}

function nodeStyle(id: string) {
  const point = nodePoint(id)
  return { left: `${point.x}px`, top: `${point.y}px` }
}

function stepHeight(step: SimpleStepDraft) {
  return NODE_HEIGHT_BASE + Math.max(0, step.stems.length - 2) * PORT_GAP
}

function ensembleHeight(ensemble: SimpleEnsembleDraft) {
  return (isSimpleAudioOperation(ensemble.algorithm) ? 174 : 206) + ensemble.inputs.length * 42
}

type SaveEntry = {
  source: string
  stem: string
  sourceLabel: string
  outputName: string
  step?: SimpleStepDraft
  ensemble?: SimpleEnsembleDraft
}

function saveEntries() {
  const stepEntries: SaveEntry[] = draft.value.steps.flatMap(step => Object.keys(step.save || {})
    .filter(stem => step.stems.some(item => item.toLowerCase() === stem.toLowerCase()))
    .map(stem => ({
      source: simpleOutputRef(step.id, stem),
      stem,
      sourceLabel: step.model || step.id,
      outputName: step.outputNames[stem] ?? '%filename%_%stem%_%model%',
      step,
    })))
  const ensembleEntries: SaveEntry[] = draft.value.ensembles
    .filter(ensemble => ensemble.save && ensemble.outputStem.trim())
    .map(ensemble => ({
      source: simpleOutputRef(ensemble.id, ensemble.outputStem.trim()),
      stem: ensemble.outputStem.trim(),
      sourceLabel: processingNodeLabel(ensemble),
      outputName: ensemble.outputName ?? '%filename%_%stem%_Ensemble',
      ensemble,
    }))
  return [...stepEntries, ...ensembleEntries]
}

function saveFilenamePreview(entry: SaveEntry, index: number) {
  const processingModels: Record<SimpleAudioOperation, string> = {
    sum: 'AudioSum', subtract: 'AudioSubtract', invert: 'AudioInvert',
  }
  return renderSimpleOutputFilename(entry.outputName, {
    stem: entry.stem,
    model: entry.step?.model || (entry.ensemble
      ? isSimpleAudioOperation(entry.ensemble.algorithm) ? processingModels[entry.ensemble.algorithm] : 'Ensemble'
      : entry.sourceLabel),
    stepId: entry.step?.id || entry.ensemble?.id,
    index: index + 1,
    inputName: 'input.wav',
    outputFormat: draft.value.defaultFormat,
  })
}

function saveHeight() {
  return 132 + Math.max(1, saveEntries().length) * SAVE_ROW_GAP
}

function outputPoint(step: SimpleStepDraft, stem: string) {
  const measured = portPoint(outputPortKey(step.id, stem))
  if (measured) return measured
  const index = Math.max(0, step.stems.findIndex(item => item.toLowerCase() === stem.toLowerCase()))
  const point = nodePoint(step.id)
  return { x: point.x + NODE_WIDTH, y: point.y + 112 + index * PORT_GAP }
}

function ensembleInputPoint(ensemble: SimpleEnsembleDraft, index: number) {
  const measured = portPoint(ensembleInputPortKey(ensemble.id, index))
  if (measured) return measured
  const point = nodePoint(ensemble.id)
  return { x: point.x, y: point.y + 116 + index * 42 }
}

function ensembleOutputPoint(ensemble: SimpleEnsembleDraft) {
  const measured = portPoint(outputPortKey(ensemble.id, ensemble.outputStem.trim()))
  if (measured) return measured
  const point = nodePoint(ensemble.id)
  return { x: point.x + ENSEMBLE_WIDTH, y: point.y + ensembleHeight(ensemble) - 34 }
}

function inputPoint(step: SimpleStepDraft) {
  const measured = portPoint(`input:${step.id}`)
  if (measured) return measured
  const point = nodePoint(step.id)
  return { x: point.x, y: point.y + 76 }
}

function inputOutputPoint() {
  const measured = portPoint('input:output')
  if (measured) return measured
  const point = nodePoint('input')
  return { x: point.x + INPUT_WIDTH, y: point.y + 78 }
}

function saveInputPoint(sourceId: string, stem: string) {
  const measured = portPoint(savePortKey(sourceId, stem))
  if (measured) return measured
  const point = nodePoint('save')
  const source = simpleOutputRef(sourceId, stem)
  const index = saveEntries().findIndex(item => item.source === source)
  return { x: point.x, y: point.y + 106 + Math.max(0, index) * SAVE_ROW_GAP }
}

function targetPoint(target: Exclude<SimpleConnectionTarget, 'save'>): SimpleEditorPoint | null {
  if (target.startsWith('step:')) {
    const step = draft.value.steps.find(item => item.id === target.slice('step:'.length))
    return step ? inputPoint(step) : null
  }
  if (target.startsWith('ensemble:')) {
    const match = /^ensemble:(.+):(\d+)$/.exec(target)
    const ensemble = match ? draft.value.ensembles.find(item => item.id === match[1]) : null
    return ensemble && match ? ensembleInputPoint(ensemble, Number(match[2])) : null
  }
  const value = target.slice('save:'.length)
  return saveInputPoint(simpleSourceStepId(value), simpleSourceStem(value))
}

function sourcePoint(source: string) {
  if (source === 'input') return inputOutputPoint()
  const sourceId = simpleSourceStepId(source)
  const stem = simpleSourceStem(source)
  const sourceIdKey = sourceId.toLowerCase()
  const step = draft.value.steps.find(item => item.id.toLowerCase() === sourceIdKey)
  if (step) return outputPoint(step, stem)
  const ensemble = draft.value.ensembles.find(item => item.id.toLowerCase() === sourceIdKey)
  return ensemble && ensemble.outputStem.trim().toLowerCase() === stem.toLowerCase()
    ? ensembleOutputPoint(ensemble)
    : null
}

function pathBetween(source: SimpleEditorPoint, target: SimpleEditorPoint) {
  const distance = Math.max(70, Math.abs(target.x - source.x) * 0.45)
  return `M ${source.x} ${source.y} C ${source.x + distance} ${source.y}, ${target.x - distance} ${target.y}, ${target.x} ${target.y}`
}

const connections = computed(() => {
  void layoutVersion.value
  const items: Array<{ id: string; path: string; source: string; target: Exclude<SimpleConnectionTarget, 'save'> }> = []
  const movingTarget = pendingConnection.value?.direction === 'output' ? pendingConnection.value.target : null
  draft.value.steps.forEach((step) => {
    const source = sourcePoint(step.input)
    if (source && movingTarget !== simpleStepInputTarget(step.id)) {
      items.push({ id: `step:${step.id}`, path: pathBetween(source, inputPoint(step)), source: step.input, target: simpleStepInputTarget(step.id) })
    }
  })
  draft.value.ensembles.forEach((ensemble) => {
    ensemble.inputs.forEach((input, index) => {
      const source = sourcePoint(input.source)
      const target = simpleEnsembleInputTarget(ensemble.id, index)
      if (source && movingTarget !== target) {
        items.push({ id: target, path: pathBetween(source, ensembleInputPoint(ensemble, index)), source: input.source, target })
      }
    })
  })
  saveEntries().forEach((entry) => {
    const source = sourcePoint(entry.source)
    const target = simpleSaveTarget(simpleSourceStepId(entry.source), simpleSourceStem(entry.source))
    if (source && movingTarget !== target) {
      items.push({
        id: `save:${entry.source}`,
        path: pathBetween(source, saveInputPoint(simpleSourceStepId(entry.source), entry.stem)),
        source: entry.source,
        target,
      })
    }
  })
  return items
})

const pendingPath = computed(() => {
  const pending = pendingConnection.value
  if (!pending) return null
  if (pending.direction === 'input') {
    const source = sourcePoint(pending.source)
    return source ? pathBetween(source, pointerWorld.value) : null
  }
  const target = targetPoint(pending.target)
  return target ? pathBetween(pointerWorld.value, target) : null
})

function canvasWorldPoint(event: { clientX: number; clientY: number }): SimpleEditorPoint {
  const rect = canvasRef.value?.getBoundingClientRect()
  if (!rect) return { x: 0, y: 0 }
  return {
    x: (event.clientX - rect.left - draft.value.ui.viewport.x) / zoom.value,
    y: (event.clientY - rect.top - draft.value.ui.viewport.y) / zoom.value,
  }
}

function updatePointer(event: PointerEvent) {
  pointerWorld.value = canvasWorldPoint(event)
  const element = (event.target as HTMLElement | null)
  if (pendingConnection.value?.direction === 'output') {
    const source = element?.closest<HTMLElement>('[data-simple-source]')?.dataset.simpleSource
    hoverTarget.value = source ? `output:${source}` : null
    return
  }
  const target = element?.closest<HTMLElement>('[data-simple-target]')
  const value = target?.dataset.simpleTarget
  hoverTarget.value = value === 'save' || value?.startsWith('step:') || value?.startsWith('ensemble:') || value?.startsWith('save:')
    ? value as SimpleConnectionHoverTarget
    : null
}

function beginCanvasPan(event: PointerEvent) {
  if (event.button !== 0 || pendingConnection.value || (event.target as HTMLElement)?.closest('[data-simple-node]')) return
  pan.value = { x: event.clientX - draft.value.ui.viewport.x, y: event.clientY - draft.value.ui.viewport.y }
  ;(event.currentTarget as HTMLElement)?.setPointerCapture(event.pointerId)
}

function moveCanvas(event: PointerEvent) {
  updatePointer(event)
  if (drag.value) {
    const point = canvasWorldPoint(event)
    setNodePoint(drag.value.id, { x: point.x - drag.value.dx, y: point.y - drag.value.dy })
    return
  }
  if (pan.value) {
    draft.value.ui.viewport = { ...draft.value.ui.viewport, x: event.clientX - pan.value.x, y: event.clientY - pan.value.y }
  }
}

function endCanvasPointer(event: PointerEvent) {
  if (drag.value || pan.value) recordHistory()
  drag.value = null
  pan.value = null
  if (pendingConnection.value && !(event.target as HTMLElement)?.closest('[data-simple-target]')) pendingConnection.value = null
  hoverTarget.value = null
}

function beginNodeDrag(id: string, event: PointerEvent) {
  if (event.button !== 0 || pendingConnection.value) return
  const point = canvasWorldPoint(event)
  const node = nodePoint(id)
  drag.value = { id, dx: point.x - node.x, dy: point.y - node.y }
  selectedNodeId.value = [...draft.value.steps, ...draft.value.ensembles].some(node => node.id === id) ? id : ''
  ;(event.currentTarget as HTMLElement)?.setPointerCapture(event.pointerId)
}

function beginConnection(source: string, event: PointerEvent) {
  if (event.button !== 0) return
  if (pendingConnection.value) return
  event.preventDefault()
  pendingConnection.value = { direction: 'input', source, label: source === 'input' ? t('workflows.originalInput') : source }
  hoverTarget.value = null
  updatePointer(event)
}

function beginEdgeConnection(edge: { source: string }, event: PointerEvent) {
  // LiteGraph uses Shift+drag on a cable segment to start a new cable while
  // keeping the original connection. Keep the same modifier-based affordance
  // and reserve a plain click for disconnecting the selected edge.
  if (!event.shiftKey) return
  event.preventDefault()
  beginConnection(edge.source, event)
}

function disconnectEdge(edge: { target: Exclude<SimpleConnectionTarget, 'save'> }, event: MouseEvent) {
  // Shift-click is reserved for LiteGraph-style cable dragging. A plain
  // click keeps the simple editor's quick disconnect affordance.
  if (event.shiftKey) return
  disconnectTarget(edge.target)
}

function beginInputConnection(step: SimpleStepDraft, event: PointerEvent) {
  if (event.button !== 0 || pendingConnection.value) return
  event.preventDefault()
  pendingConnection.value = {
    direction: 'output',
    target: simpleStepInputTarget(step.id),
    label: step.input || t('workflows.stepInputPlaceholder'),
  }
  hoverTarget.value = null
  updatePointer(event)
}

function beginEnsembleInputConnection(ensemble: SimpleEnsembleDraft, index: number, event: PointerEvent) {
  if (event.button !== 0 || pendingConnection.value) return
  event.preventDefault()
  pendingConnection.value = {
    direction: 'output',
    target: simpleEnsembleInputTarget(ensemble.id, index),
    label: ensemble.inputs[index]?.source || t('workflows.ensembleInputPlaceholder'),
  }
  hoverTarget.value = null
  updatePointer(event)
}

function beginSaveConnection(source: string, label: string, event: PointerEvent) {
  if (event.button !== 0 || pendingConnection.value) return
  event.preventDefault()
  pendingConnection.value = {
    direction: 'output',
    target: simpleSaveTarget(simpleSourceStepId(source), simpleSourceStem(source)),
    label,
  }
  hoverTarget.value = null
  updatePointer(event)
}

function finishConnection(target: SimpleConnectionTarget, event: PointerEvent) {
  event.stopPropagation()
  const pending = pendingConnection.value
  if (!pending) return
  if (pending.direction !== 'input') {
    pendingConnection.value = null
    hoverTarget.value = null
    return
  }
  const resolvedTarget = target === 'save'
    ? 'save'
    : target
  const check = canConnectSimple(draft.value, pending.source, resolvedTarget)
  if (!check.ok) {
    message.warning(t(connectionMessageKeys[check.reason] || 'workflows.invalidConnection'))
    pendingConnection.value = null
    hoverTarget.value = null
    return
  }
  connectSimple(draft.value, pending.source, resolvedTarget)
  cleanupSimpleDraft(draft.value)
  pendingConnection.value = null
  hoverTarget.value = null
  recordHistory()
}

function finishOutputConnection(source: string, event: PointerEvent) {
  event.stopPropagation()
  const pending = pendingConnection.value
  if (!pending) return
  // Dropping a new output-to-input cable on another output is an invalid
  // target. Clear the transient state here because the output button stops
  // propagation and the canvas-level pointerup handler will not run.
  if (pending.direction !== 'output') {
    pendingConnection.value = null
    hoverTarget.value = null
    return
  }
  const target = pending.target
  const check = target.startsWith('save:')
    ? canConnectSimple(draft.value, source, 'save')
    : canConnectSimple(draft.value, source, target)
  if (!check.ok) {
    message.warning(t(connectionMessageKeys[check.reason] || 'workflows.invalidConnection'))
    pendingConnection.value = null
    hoverTarget.value = null
    return
  }
  if (target.startsWith('save:')) {
    const previousValue = target.slice('save:'.length)
    const previousStep = draft.value.steps.find(item => item.id === simpleSourceStepId(previousValue))
    const previousEnsemble = draft.value.ensembles.find(item => item.id === simpleSourceStepId(previousValue))
    const previousName = previousStep?.outputNames[simpleSourceStem(previousValue)] || previousEnsemble?.outputName
    disconnectSimple(draft.value, target)
    connectSimple(draft.value, source, 'save')
    if (previousName?.trim()) {
      const nextStep = draft.value.steps.find(item => item.id === simpleSourceStepId(source))
      const nextEnsemble = draft.value.ensembles.find(item => item.id === simpleSourceStepId(source))
      const nextStem = simpleSourceStem(source)
      if (nextStep && nextStem) nextStep.outputNames = { ...nextStep.outputNames, [nextStem]: previousName }
      else if (nextEnsemble) nextEnsemble.outputName = previousName
    }
  } else {
    connectSimple(draft.value, source, target)
  }
  cleanupSimpleDraft(draft.value)
  pendingConnection.value = null
  hoverTarget.value = null
  recordHistory()
}

function disconnectTarget(target: Exclude<SimpleConnectionTarget, 'save'>) {
  if (disconnectSimple(draft.value, target)) {
    recordHistory()
  }
}

function toggleSave(step: SimpleStepDraft, stem: string) {
  const existing = Object.prototype.hasOwnProperty.call(step.save, stem)
  if (existing) {
    const next = { ...step.save }
    delete next[stem]
    step.save = next
  } else {
    const check = canConnectSimple(draft.value, simpleOutputRef(step.id, stem), 'save')
    if (!check.ok) return
    step.save = { ...step.save, [stem]: 'Default' }
    if (!step.outputNames[stem]) step.outputNames = { ...step.outputNames, [stem]: '%filename%_%stem%_%model%' }
  }
  recordHistory()
}

function modelStems(modelName: string) {
  const item = props.models.find(model => model.name === modelName)
  return configuredStemsFor(item)
}

function confirmDestructiveChange(
  impact: { connections: number; savedOutputs: number },
  apply: () => void,
) {
  if (impact.connections + impact.savedOutputs === 0) {
    apply()
    return
  }
  dialog.warning({
    title: t('workflows.simpleDestructiveChangeTitle'),
    content: t('workflows.simpleDestructiveChangeHint', impact),
    positiveText: t('workflows.simpleDestructiveChangeContinue'),
    negativeText: t('common.cancel'),
    positiveButtonProps: { type: 'warning' },
    onPositiveClick: apply,
  })
}

function applyModelUpdate(step: SimpleStepDraft, value: string, stems: string[]) {
  const oldNames = step.outputNames || {}
  const oldNamesByStem = new Map(Object.entries(oldNames).map(([key, value]) => [key.toLowerCase(), value]))
  const oldSaveByStem = new Map(Object.entries(step.save || {}).map(([key, value]) => [key.toLowerCase(), value]))
  step.model = value
  step.modelType = props.models.find(model => model.name === value || model.aliases?.includes(value))?.modelType || undefined
  step.stems = stems
  // Per-step inference values belong to the previous model. A model change
  // deliberately returns the step to the global fallback chain.
  step.inferenceParams = undefined
  step.outputNames = Object.fromEntries(stems.map(stem => [stem, oldNamesByStem.get(stem.toLowerCase()) || '%filename%_%stem%_%model%']))
  const nextSave: Record<string, string> = {}
  stems.forEach((stem) => {
    const value = oldSaveByStem.get(stem.toLowerCase())
    if (value?.trim()) nextSave[stem] = value
  })
  step.save = nextSave
  cleanupSimpleDraft(draft.value)
  recordHistory()
}

function updateStepInput(step: SimpleStepDraft, value: string) {
  const check = canConnectSimple(draft.value, value, simpleStepInputTarget(step.id))
  if (!check.ok) return
  step.input = value
  cleanupSimpleDraft(draft.value)
  recordHistory()
}

function updateOutputName(step: SimpleStepDraft, stem: string, value: string) {
  step.outputNames = { ...step.outputNames, [stem]: value }
  recordHistory(`output-name:${step.id}:${stem}`, 900)
}

function updateSaveOutputName(entry: SaveEntry, value: string) {
  if (entry.step) {
    updateOutputName(entry.step, entry.stem, value)
    return
  }
  if (!entry.ensemble) return
  entry.ensemble.outputName = value
  recordHistory(`output-name:${entry.ensemble.id}`, 900)
}

function updateEnsembleWeight(ensemble: SimpleEnsembleDraft, index: number, value: number | null) {
  ensemble.inputs[index].weight = typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : 1
  recordHistory(`ensemble-weight:${ensemble.id}:${index}`, 700)
}

function updateEnsembleAlgorithm(ensemble: SimpleEnsembleDraft, value: string) {
  const algorithm = SIMPLE_ENSEMBLE_ALGORITHMS.find(item => item === value)
  if (!algorithm) return
  ensemble.algorithm = algorithm
  recordHistory()
}

function updateEnsembleStem(ensemble: SimpleEnsembleDraft, value: string) {
  updateSimpleEnsembleOutputStem(draft.value, ensemble, value)
  scheduleLayoutRefresh()
  recordHistory(`ensemble-stem:${ensemble.id}`, 900)
}

function addEnsembleInput(ensemble: SimpleEnsembleDraft) {
  if (ensemble.inputs.length >= simpleProcessingInputLimits(ensemble.algorithm).max) return
  const used = new Set(ensemble.inputs.map(input => input.source.toLowerCase()))
  const options = ensembleSourceOptionsFor(ensemble)
  const candidates = ensemble.algorithm === 'sum'
    ? [...options.filter(option => option.value !== 'input'), ...options.filter(option => option.value === 'input')]
    : options
  const source = candidates.find(option => !used.has(option.value.toLowerCase()))?.value || ''
  ensemble.inputs = [...ensemble.inputs, { source, weight: 1 }]
  recordHistory()
}

function removeEnsembleInput(ensemble: SimpleEnsembleDraft, index: number) {
  if (ensemble.inputs.length <= simpleProcessingInputLimits(ensemble.algorithm).min) return
  ensemble.inputs = ensemble.inputs.filter((_, inputIndex) => inputIndex !== index)
  cleanupSimpleDraft(draft.value)
  recordHistory()
}

function updateModel(step: SimpleStepDraft, modelName: string | null) {
  const value = String(modelName || '')
  if (value === step.model) return
  const stems = modelStems(value)
  const impact = analyzeSimpleModelChangeImpact(draft.value, step.id, stems)
  confirmDestructiveChange(impact, () => applyModelUpdate(step, value, stems))
}

function toggleEnsembleSave(ensemble: SimpleEnsembleDraft) {
  const source = simpleOutputRef(ensemble.id, ensemble.outputStem.trim())
  if (ensemble.save) {
    disconnectSimple(draft.value, simpleSaveTarget(ensemble.id, ensemble.outputStem.trim()))
  } else {
    const check = canConnectSimple(draft.value, source, 'save')
    if (!check.ok) return
    connectSimple(draft.value, source, 'save')
  }
  recordHistory()
}

function closeNodeContextMenu() {
  contextMenuVisible.value = false
}

function openNodeTypeChooser(at?: SimpleEditorPoint) {
  closeNodeContextMenu()
  pendingNodePoint.value = at || null
  showNodeTypeChooser.value = true
}

function addNodeOfType(type: SimpleNodeType) {
  const point = pendingNodePoint.value || undefined
  if (type === 'separation') addStep(point)
  else addEnsemble(point, type === 'ensemble' ? undefined : type)
  pendingNodePoint.value = null
  showNodeTypeChooser.value = false
  closeNodeContextMenu()
}

function openCanvasContextMenu(event: MouseEvent) {
  const target = event.target instanceof Element ? event.target : null
  if (target?.closest('[data-simple-node], .simple-node-editor__edge-group')) return
  event.preventDefault()
  contextMenuPoint.value = canvasWorldPoint(event)
  contextMenuVisible.value = false
  contextMenuX.value = event.clientX
  contextMenuY.value = event.clientY
  requestAnimationFrame(() => {
    contextMenuVisible.value = true
  })
}

function handleNodeContextMenuSelect(key: string | number) {
  const point = contextMenuPoint.value || undefined
  if (key === 'ensemble') addEnsemble(point)
  else if (key === 'separation') addStep(point)
  else if (isSimpleAudioOperation(key)) addEnsemble(point, key)
  contextMenuPoint.value = null
  closeNodeContextMenu()
}

function addStep(at?: SimpleEditorPoint) {
  const step = createStepDraft(draft.value.steps.length)
  const usedIds = new Set([
    ...draft.value.steps.map(item => item.id),
    ...draft.value.ensembles.map(item => item.id),
  ])
  let suffix = draft.value.steps.length + 1
  let nextId = `step${suffix}`
  while (usedIds.has(nextId)) nextId = `step${++suffix}`
  step.id = nextId
  const previous = draft.value.steps[draft.value.steps.length - 1]
  if (previous?.stems[0]) step.input = simpleOutputRef(previous.id, previous.stems[0])
  draft.value.steps = [...draft.value.steps, step]
  const placementCandidates = [
    ...draft.value.steps.slice(0, -1).map(item => ({ point: nodePoint(item.id), width: NODE_WIDTH })),
    ...draft.value.ensembles.map(item => ({ point: nodePoint(item.id), width: ENSEMBLE_WIDTH })),
  ]
  const previousNode = placementCandidates.reduce<{ point: SimpleEditorPoint; width: number } | null>(
    (rightmost, candidate) => !rightmost || candidate.point.x + candidate.width > rightmost.point.x + rightmost.width
      ? candidate
      : rightmost,
    null,
  )
  const previousPoint = previousNode?.point || { x: 360, y: 190 }
  const point = at
    ? { x: Math.max(24, at.x - NODE_WIDTH / 2), y: Math.max(24, at.y - 70) }
    : { x: previousPoint.x + (previousNode?.width || NODE_WIDTH) + 30, y: previousPoint.y }
  const savePoint = nodePoint('save')
  const moveSave = nodesOverlap(point, NODE_WIDTH, stepHeight(step), savePoint, SAVE_WIDTH, saveHeight())
  const nextSavePoint = at
    ? { x: savePoint.x, y: point.y + stepHeight(step) + 30 }
    : { x: point.x + NODE_WIDTH + 30, y: savePoint.y }
  draft.value.ui.nodes = {
    ...draft.value.ui.nodes,
    [step.id]: point,
    ...(moveSave ? { save: nextSavePoint } : {}),
  }
  recordHistory()
  if (!at) void nextTick(() => fitView(false))
}

function addEnsemble(at?: SimpleEditorPoint, operation?: SimpleAudioOperation) {
  const ensemble = operation
    ? createAudioOperationDraft(operation, draft.value.ensembles.length)
    : createEnsembleDraft(draft.value.ensembles.length)
  const usedIds = new Set([
    ...draft.value.steps.map(item => item.id),
    ...draft.value.ensembles.map(item => item.id),
  ])
  let suffix = draft.value.ensembles.length + 1
  const prefix = operation || 'ensemble'
  let nextId = `${prefix}${suffix}`
  while (usedIds.has(nextId)) nextId = `${prefix}${++suffix}`
  ensemble.id = nextId
  const preferredSources = operation === 'sum'
    ? stepOutputOptions.value.slice(0, 2).map(option => option.value)
    : operation === 'subtract'
      ? ['input', stepOutputOptions.value.find(option => /^vocals?$/i.test(simpleSourceStem(option.value)))?.value || stepOutputOptions.value[0]?.value || '']
      : operation === 'invert'
        ? [stepOutputOptions.value[0]?.value || 'input']
        : preferredEnsembleSources(ensemble.inputs.length)
  const preferredStems = preferredSources.map(simpleSourceStem).filter(Boolean)
  if (!operation && preferredStems.length === ensemble.inputs.length && preferredStems.every(stem => stem.toLowerCase() === preferredStems[0].toLowerCase())) {
    ensemble.outputStem = preferredStems[0]
  }
  ensemble.inputs = ensemble.inputs.map((input, index) => ({
    ...input,
    source: preferredSources[index] || '',
  }))
  draft.value.ensembles = [...draft.value.ensembles, ensemble]
  const previous = draft.value.ensembles[draft.value.ensembles.length - 2]
  const lastStep = draft.value.steps[draft.value.steps.length - 1]
  const previousPoint = previous
    ? nodePoint(previous.id)
    : lastStep
      ? nodePoint(lastStep.id)
      : { x: 360, y: 190 }
  const point = at
    ? { x: Math.max(24, at.x - ENSEMBLE_WIDTH / 2), y: Math.max(24, at.y - 70) }
    : { x: previousPoint.x + 350, y: previousPoint.y }
  const savePoint = nodePoint('save')
  const moveSave = nodesOverlap(point, ENSEMBLE_WIDTH, ensembleHeight(ensemble), savePoint, SAVE_WIDTH, saveHeight())
  const nextSavePoint = at
    ? { x: savePoint.x, y: point.y + ensembleHeight(ensemble) + 30 }
    : { x: point.x + ENSEMBLE_WIDTH + 30, y: savePoint.y }
  draft.value.ui.nodes = {
    ...draft.value.ui.nodes,
    [ensemble.id]: point,
    ...(moveSave ? { save: nextSavePoint } : {}),
  }
  recordHistory()
  if (!at) void nextTick(() => fitView(false))
}

function handleCanvasDoubleClick(event: MouseEvent) {
  const target = event.target instanceof Element ? event.target : null
  if (target?.closest('[data-simple-node], .simple-node-editor__edges, button, input, select, textarea')) return
  event.preventDefault()
  openNodeTypeChooser(canvasWorldPoint(event))
}

function applyRemoveStep(step: SimpleStepDraft) {
  if (draft.value.steps.length <= 1) return
  draft.value.steps = draft.value.steps.filter(item => item.id !== step.id)
  const nodes = { ...draft.value.ui.nodes }
  delete nodes[step.id]
  draft.value.ui.nodes = nodes
  cleanupSimpleDraft(draft.value)
  selectedNodeId.value = ''
  recordHistory()
}

function removeStep(step: SimpleStepDraft) {
  if (draft.value.steps.length <= 1) return
  confirmDestructiveChange(
    analyzeSimpleNodeRemovalImpact(draft.value, step.id),
    () => applyRemoveStep(step),
  )
}

function moveStep(step: SimpleStepDraft, offset: -1 | 1) {
  const index = draft.value.steps.findIndex(item => item.id === step.id)
  const sibling = draft.value.steps[index + offset]
  if (!sibling) return
  const stepPoint = nodePoint(step.id)
  const siblingPoint = nodePoint(sibling.id)
  if (!moveSimpleStep(draft.value, step.id, offset)) return
  draft.value.ui.nodes = {
    ...draft.value.ui.nodes,
    [step.id]: siblingPoint,
    [sibling.id]: stepPoint,
  }
  scheduleLayoutRefresh()
  recordHistory()
}

function applyRemoveEnsemble(ensemble: SimpleEnsembleDraft) {
  draft.value.ensembles = draft.value.ensembles.filter(item => item.id !== ensemble.id)
  const nodes = { ...draft.value.ui.nodes }
  delete nodes[ensemble.id]
  draft.value.ui.nodes = nodes
  cleanupSimpleDraft(draft.value)
  selectedNodeId.value = ''
  recordHistory()
}

function removeEnsemble(ensemble: SimpleEnsembleDraft) {
  confirmDestructiveChange(
    analyzeSimpleNodeRemovalImpact(draft.value, ensemble.id),
    () => applyRemoveEnsemble(ensemble),
  )
}

function autoLayout() {
  const nodes: Record<string, SimpleEditorPoint> = {
    input: { x: 64, y: 250 },
  }
  const stepById = new Map(draft.value.steps.map(step => [step.id, step]))
  const ensembleById = new Map(draft.value.ensembles.map(ensemble => [ensemble.id, ensemble]))
  const depths = new Map<string, number>()
  const visiting = new Set<string>()
  const sourceDepth = (source: string): number => {
    if (!source.trim() || source === 'input') return 0
    const sourceId = simpleSourceStepId(source)
    const step = stepById.get(sourceId)
    if (step) return nodeDepth(step.id, 'step')
    const ensemble = ensembleById.get(sourceId)
    return ensemble ? nodeDepth(ensemble.id, 'ensemble') : 0
  }
  const nodeDepth = (id: string, kind: 'step' | 'ensemble'): number => {
    const cached = depths.get(id)
    if (cached !== undefined) return cached
    if (visiting.has(id)) return 1
    visiting.add(id)
    const depth = kind === 'step'
      ? sourceDepth(stepById.get(id)?.input || 'input') + 1
      : Math.max(0, ...(ensembleById.get(id)?.inputs || []).map(input => sourceDepth(input.source))) + 1
    visiting.delete(id)
    depths.set(id, depth)
    return depth
  }
  draft.value.steps.forEach(step => nodeDepth(step.id, 'step'))
  draft.value.ensembles.forEach(ensemble => nodeDepth(ensemble.id, 'ensemble'))
  const nextY = new Map<number, number>()
  const place = (id: string, depth: number, height: number) => {
    const y = nextY.get(depth) ?? 120
    nodes[id] = { x: 360 + Math.max(0, depth - 1) * 360, y }
    nextY.set(depth, y + height + 36)
  }
  draft.value.steps.forEach(step => place(step.id, depths.get(step.id) || 1, stepHeight(step)))
  draft.value.ensembles.forEach(ensemble => place(ensemble.id, depths.get(ensemble.id) || 1, ensembleHeight(ensemble)))
  const maxDepth = Math.max(1, ...depths.values())
  nodes.save = { x: 360 + maxDepth * 360, y: 220 }
  draft.value.ui = { ...draft.value.ui, nodes, viewport: { x: 0, y: 0, zoom: 1 } }
  recordHistory()
  void nextTick(() => fitView(false))
}

function fitView(remember = true) {
  const canvas = canvasRef.value
  if (!canvas || canvas.clientWidth <= 0 || canvas.clientHeight <= 0) return

  const currentZoom = Math.max(0.01, zoom.value)
  const bounds = Array.from(canvas.querySelectorAll<HTMLElement>('[data-simple-node]')).flatMap((element) => {
    const id = element.dataset.simpleNode
    if (!id) return []
    const point = nodePoint(id)
    const rect = element.getBoundingClientRect()
    const width = rect.width / currentZoom
    const height = rect.height / currentZoom
    return width > 0 && height > 0 ? [{ x: point.x, y: point.y, width, height }] : []
  })
  if (!bounds.length) return

  const viewport = fitSimpleEditorViewport(bounds, canvas.clientWidth, canvas.clientHeight)
  if (
    Math.abs(viewport.x - draft.value.ui.viewport.x) < 0.01
    && Math.abs(viewport.y - draft.value.ui.viewport.y) < 0.01
    && Math.abs(viewport.zoom - draft.value.ui.viewport.zoom) < 0.001
  ) return
  draft.value.ui.viewport = viewport
  if (remember) recordHistory()
  else if (history.value.length) history.value[history.value.length - 1] = snapshot()
}

function handleWheel(event: WheelEvent) {
  event.preventDefault()
  closeSelectMenus()
  closeNodeContextMenu()
  const oldZoom = zoom.value
  const nextZoom = Math.min(1.8, Math.max(0.45, oldZoom * (event.deltaY > 0 ? 0.9 : 1.1)))
  const point = canvasWorldPoint(event)
  const rect = canvasRef.value?.getBoundingClientRect()
  if (!rect) return
  const screenX = event.clientX - rect.left
  const screenY = event.clientY - rect.top
  draft.value.ui.viewport = {
    x: screenX - point.x * nextZoom,
    y: screenY - point.y * nextZoom,
    zoom: nextZoom,
  }
  recordHistory('viewport-wheel', 350)
}

function isEditableKeyboardTarget(target: EventTarget | null) {
  if (!(target instanceof Element)) return false
  return Boolean(target.closest('input, textarea, select, [contenteditable="true"], [contenteditable=""], [role="textbox"]'))
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && pendingConnection.value) {
    pendingConnection.value = null
    hoverTarget.value = null
    return
  }
  if (event.defaultPrevented || event.isComposing || isEditableKeyboardTarget(event.target)) return
  if (!(event.ctrlKey || event.metaKey)) return
  if (event.key.toLowerCase() === 'z') { event.preventDefault(); event.shiftKey ? redo() : undo() }
  if (event.key.toLowerCase() === 'y') { event.preventDefault(); redo() }
}

function cancelPendingConnection() {
  pendingConnection.value = null
  hoverTarget.value = null
}

watch(() => [draft.value.defaultDevice, draft.value.defaultFormat, draft.value.defaultNormalize], () => {
  recordHistory()
})

watch(showNodeTypeChooser, (show) => {
  if (!show) pendingNodePoint.value = null
})

onMounted(() => {
  if (!draft.value.ui) draft.value.ui = createDefaultSimpleEditorUi(draft.value.steps, draft.value.ensembles)
  cleanupSimpleDraft(draft.value)
  history.value = [snapshot()]
  if (typeof ResizeObserver !== 'undefined') {
    portResizeObserver = new ResizeObserver(scheduleLayoutRefresh)
    portElements.forEach(element => portResizeObserver?.observe(element))
  }
  scheduleLayoutRefresh()
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('pointerup', cancelPendingConnection)
})

onBeforeUnmount(() => {
  if (layoutFrame) cancelAnimationFrame(layoutFrame)
  layoutFrame = 0
  portResizeObserver?.disconnect()
  portResizeObserver = null
  selectInstances.clear()
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('pointerup', cancelPendingConnection)
})
</script>

<template>
  <div class="simple-node-editor">
    <header class="simple-node-editor__topbar">
      <div class="simple-node-editor__title">
        <span>{{ t('workflows.simpleCreator') }}</span>
        <n-input v-model:value="name" size="small" :placeholder="t('workflows.namePlaceholder')" />
      </div>
      <div class="simple-node-editor__actions">
        <n-button size="small" secondary :disabled="history.length <= 1" @click="undo"><template #icon><n-icon :component="ArrowUndoOutline" /></template>{{ t('common.undo') }}</n-button>
        <n-button size="small" secondary :disabled="!future.length" @click="redo"><template #icon><n-icon :component="ArrowRedoOutline" /></template>{{ t('common.redo') }}</n-button>
        <n-button size="small" secondary @click="openNodeTypeChooser()">{{ t('workflows.addStep') }}</n-button>
        <n-button size="small" secondary @click="autoLayout"><template #icon><n-icon :component="LocateOutline" /></template>{{ t('workflows.autoLayout') }}</n-button>
        <n-button size="small" type="primary" :loading="saving" :disabled="!canSave" @click="emit('save')"><template #icon><n-icon :component="SaveOutline" /></template>{{ t('common.save') }}</n-button>
      </div>
    </header>

    <div class="simple-node-editor__meta">
      <n-input v-model:value="description" size="small" :placeholder="t('workflows.descriptionPlaceholder')" />
      <label><span>{{ t('workflows.defaultDevice') }}</span><n-select :ref="(instance: unknown) => setSelectInstance('default-device', instance)" v-model:value="draft.defaultDevice" size="small" :options="[{ label: 'Auto', value: 'auto' }, { label: 'CPU', value: 'cpu' }, { label: 'CUDA', value: 'cuda' }, { label: 'MPS', value: 'mps' }, { label: 'MLX', value: 'mlx' }]" /></label>
      <label><span>{{ t('workflows.defaultFormat') }}</span><n-select :ref="(instance: unknown) => setSelectInstance('default-format', instance)" v-model:value="draft.defaultFormat" size="small" :options="[{ label: 'WAV', value: 'wav' }, { label: 'FLAC', value: 'flac' }, { label: 'MP3', value: 'mp3' }, { label: 'M4A', value: 'm4a' }]" /></label>
      <label><span>{{ t('workflows.defaultNormalize') }}</span><n-select v-model:value="defaultNormalizeMode" size="small" :options="defaultNormalizeOptions" /></label>
    </div>

    <div ref="canvasRef" class="simple-node-editor__canvas" @wheel="handleWheel" @pointermove="moveCanvas" @pointerup="endCanvasPointer" @pointercancel="endCanvasPointer" @pointerdown="beginCanvasPan" @dblclick="handleCanvasDoubleClick" @contextmenu.prevent="openCanvasContextMenu">
      <div class="simple-node-editor__hint">{{ pendingConnection ? t('workflows.connectingFrom') + ': ' + pendingConnection.label : t('workflows.simpleEditorHint') }}</div>
      <div ref="worldRef" class="simple-node-editor__world" :style="{ transform: `translate(${draft.ui.viewport.x}px, ${draft.ui.viewport.y}px) scale(${draft.ui.viewport.zoom})` }">
        <svg class="simple-node-editor__edges" width="2600" height="1600" viewBox="0 0 2600 1600" aria-hidden="true">
          <g v-for="edge in connections" :key="edge.id" class="simple-node-editor__edge-group">
            <path :d="edge.path" class="simple-node-editor__edge" @click.stop="disconnectEdge(edge, $event)" />
            <path :d="edge.path" class="simple-node-editor__edge-hit" @pointerdown.stop="beginEdgeConnection(edge, $event)" @click.stop="disconnectEdge(edge, $event)" />
          </g>
          <path v-if="pendingPath" :d="pendingPath" class="simple-node-editor__edge simple-node-editor__edge--pending" />
        </svg>

        <article class="simple-node simple-node--input" data-simple-node="input" :style="{ ...nodeStyle('input'), width: `${INPUT_WIDTH}px` }" @pointerdown.stop="beginNodeDrag('input', $event)">
          <header><span>{{ t('workflows.inputNode') }}</span><strong>{{ t('workflows.originalInput') }}</strong></header>
          <button :ref="el => setPortElement('input:output', el)" class="simple-port simple-port--output" :class="{ 'simple-port--source-target': pendingConnection?.direction === 'output' && hoverTarget === 'output:input' }" data-simple-source="input" type="button" @pointerdown.stop="beginConnection('input', $event)" @pointerup.stop="finishOutputConnection('input', $event)"><span>{{ t('workflows.audioOutput') }}</span><i /></button>
        </article>

        <article v-for="(step, index) in draft.steps" :key="step.id" class="simple-node simple-node--step" :class="{ 'simple-node--selected': selectedNodeId === step.id }" :style="{ ...nodeStyle(step.id), width: `${NODE_WIDTH}px`, minHeight: `${stepHeight(step)}px` }" :data-simple-node="step.id" @pointerdown.stop="beginNodeDrag(step.id, $event)">
          <header>
            <div><span>{{ t('workflows.separationNode') }} {{ index + 1 }}</span><strong>{{ step.model || t('workflows.stepModelPlaceholder') }}</strong></div>
            <span class="simple-node__header-actions">
              <button type="button" class="simple-icon-button" :title="t('workflows.moveStepEarlier')" :disabled="!canMoveSimpleStep(draft, step.id, -1)" @pointerdown.stop @click.stop="moveStep(step, -1)">↑</button>
              <button type="button" class="simple-icon-button" :title="t('workflows.moveStepLater')" :disabled="!canMoveSimpleStep(draft, step.id, 1)" @pointerdown.stop @click.stop="moveStep(step, 1)">↓</button>
              <button
                type="button"
                class="simple-icon-button simple-inference-settings"
                :class="{ 'simple-inference-settings--custom': Boolean(step.inferenceParams) }"
                :title="`${t('workflows.stepInferenceParams')} · ${t(step.inferenceParams ? 'workflows.inferenceWorkflowCustom' : 'workflows.inferenceFollowGlobal')}`"
                @pointerdown.stop
                @click.stop="openInferenceEditor(step)"
              >
                <n-icon :component="SettingsOutline" />
                <span v-if="step.inferenceParams" class="simple-inference-settings__dot" aria-hidden="true" />
              </button>
              <button type="button" class="simple-icon-button" :title="t('workflows.removeStep')" :disabled="draft.steps.length <= 1" @pointerdown.stop @click.stop="removeStep(step)"><n-icon :component="CloseOutline" /></button>
            </span>
          </header>
          <div class="simple-node__input-wrap" @pointerdown.stop>
            <button :ref="el => setPortElement(`input:${step.id}`, el)" class="simple-port simple-port--input" :class="{ 'simple-port--target': pendingConnection?.direction === 'input' && hoverTarget === `step:${step.id}` }" type="button" :data-simple-target="`step:${step.id}`" @pointerdown.stop="beginInputConnection(step, $event)" @pointerup.stop="finishConnection(`step:${step.id}`, $event)"><i /><span>{{ step.input || t('workflows.stepInputPlaceholder') }}</span></button>
          </div>
          <div class="simple-node__body" @pointerdown.stop>
            <label><span>{{ t('workflows.stepModel') }}</span><n-select :ref="(instance: unknown) => setSelectInstance(`step-model:${step.id}`, instance)" :value="step.model" size="small" filterable :options="modelOptions" :placeholder="t('workflows.stepModelPlaceholder')" @update:value="updateModel(step, $event)" /></label>
            <label><span>{{ t('workflows.stepInput') }}</span><n-select :ref="(instance: unknown) => setSelectInstance(`step-input:${step.id}`, instance)" :value="step.input" size="small" :options="stepInputOptions(step, index)" @update:value="updateStepInput(step, String($event || ''))" /></label>
          </div>
          <div class="simple-node__outputs">
            <div v-for="stem in step.stems" :key="stem" class="simple-output-row">
              <button :ref="el => setPortElement(outputPortKey(step.id, stem), el)" class="simple-port simple-port--output" :class="{ 'simple-port--source-target': pendingConnection?.direction === 'output' && hoverTarget === `output:${simpleOutputRef(step.id, stem)}` }" :data-simple-source="simpleOutputRef(step.id, stem)" type="button" @pointerdown.stop="beginConnection(simpleOutputRef(step.id, stem), $event)" @pointerup.stop="finishOutputConnection(simpleOutputRef(step.id, stem), $event)"><span>{{ stem }}</span><i /></button>
              <n-button size="tiny" :type="step.save[stem] ? 'primary' : 'default'" secondary @pointerdown.stop @click.stop="toggleSave(step, stem)">{{ step.save[stem] ? t('workflows.savedOutput') : t('workflows.saveStems') }}</n-button>
            </div>
            <span v-if="!step.stems.length" class="simple-node__empty">{{ t('workflows.noStemPorts') }}</span>
          </div>
        </article>

        <article v-for="(ensemble, index) in draft.ensembles" :key="ensemble.id" class="simple-node simple-node--ensemble" :class="{ 'simple-node--selected': selectedNodeId === ensemble.id, 'simple-node--audio-operation': isSimpleAudioOperation(ensemble.algorithm) }" :style="{ ...nodeStyle(ensemble.id), width: `${ENSEMBLE_WIDTH}px`, minHeight: `${ensembleHeight(ensemble)}px` }" :data-simple-node="ensemble.id" :data-audio-operation="isSimpleAudioOperation(ensemble.algorithm) ? ensemble.algorithm : undefined" @pointerdown.stop="beginNodeDrag(ensemble.id, $event)">
          <header><div><span>{{ processingNodeLabel(ensemble) }} {{ index + 1 }}</span><strong>{{ ensemble.outputStem || t('workflows.ensembleStemPlaceholder') }}</strong></div><button type="button" class="simple-icon-button" :title="isSimpleAudioOperation(ensemble.algorithm) ? t('workflows.removeAudioOperation') : t('workflows.removeEnsemble')" @pointerdown.stop @click.stop="removeEnsemble(ensemble)"><n-icon :component="CloseOutline" /></button></header>
          <div class="simple-node__body" @pointerdown.stop>
            <p v-if="isSimpleAudioOperation(ensemble.algorithm)" class="simple-audio-operation-hint">{{ audioOperationCopy[ensemble.algorithm].hint }}</p>
            <label v-else><span>{{ t('workflows.ensembleType') }}</span><n-select :ref="(instance: unknown) => setSelectInstance(`ensemble-algorithm:${ensemble.id}`, instance)" :value="ensemble.algorithm" size="small" :options="ensembleAlgorithmOptions" @update:value="updateEnsembleAlgorithm(ensemble, String($event || ''))" /></label>
            <label><span>{{ t('workflows.ensembleStem') }}</span><n-input :value="ensemble.outputStem" size="small" :placeholder="t('workflows.ensembleStemPlaceholder')" @update:value="updateEnsembleStem(ensemble, $event)" /></label>
          </div>
          <div class="simple-ensemble-inputs" @pointerdown.stop>
            <div v-for="(input, inputIndex) in ensemble.inputs" :key="inputIndex" class="simple-ensemble-input-row" :class="{ 'simple-ensemble-input-row--audio-operation': isSimpleAudioOperation(ensemble.algorithm) }">
              <button :ref="el => setPortElement(ensembleInputPortKey(ensemble.id, inputIndex), el)" class="simple-port simple-port--input simple-ensemble-input-row__port" :class="{ 'simple-port--target': pendingConnection?.direction === 'input' && hoverTarget === simpleEnsembleInputTarget(ensemble.id, inputIndex) }" type="button" :data-simple-target="simpleEnsembleInputTarget(ensemble.id, inputIndex)" @pointerdown.stop="beginEnsembleInputConnection(ensemble, inputIndex, $event)" @pointerup.stop="finishConnection(simpleEnsembleInputTarget(ensemble.id, inputIndex), $event)"><i /><span>{{ processingInputLabel(ensemble, inputIndex) }}</span></button>
              <span class="simple-ensemble-input-row__source" :title="ensembleSourceLabel(input.source)">{{ ensembleSourceLabel(input.source) }}</span>
              <n-input-number v-if="!isSimpleAudioOperation(ensemble.algorithm)" :value="input.weight" size="tiny" :min="0.01" :step="0.1" :show-button="false" :placeholder="t('workflows.ensembleWeight')" @update:value="updateEnsembleWeight(ensemble, inputIndex, $event)" />
              <button v-if="simpleProcessingInputLimits(ensemble.algorithm).max > simpleProcessingInputLimits(ensemble.algorithm).min" type="button" class="simple-icon-button" :title="t('workflows.removeEnsembleInput')" :disabled="ensemble.inputs.length <= simpleProcessingInputLimits(ensemble.algorithm).min" @pointerdown.stop @click.stop="removeEnsembleInput(ensemble, inputIndex)"><n-icon :component="CloseOutline" /></button>
            </div>
            <n-button v-if="simpleProcessingInputLimits(ensemble.algorithm).max > simpleProcessingInputLimits(ensemble.algorithm).min" size="tiny" secondary :disabled="ensemble.inputs.length >= simpleProcessingInputLimits(ensemble.algorithm).max" @click.stop="addEnsembleInput(ensemble)">{{ t('workflows.addEnsembleInput') }}</n-button>
          </div>
          <div class="simple-ensemble-output-row">
            <span class="simple-ensemble-output-row__label">{{ ensemble.outputStem || t('workflows.ensembleOutput') }}</span>
            <n-button size="tiny" :type="ensemble.save ? 'primary' : 'default'" secondary @pointerdown.stop @click.stop="toggleEnsembleSave(ensemble)">{{ ensemble.save ? t('workflows.savedOutput') : t('workflows.saveStems') }}</n-button>
            <button :ref="el => setPortElement(outputPortKey(ensemble.id, ensemble.outputStem.trim()), el)" class="simple-ensemble-output-port" :class="{ 'simple-ensemble-output-port--target': pendingConnection?.direction === 'output' && hoverTarget === `output:${simpleOutputRef(ensemble.id, ensemble.outputStem.trim())}` }" :aria-label="ensemble.outputStem || t('workflows.ensembleOutput')" :data-simple-source="simpleOutputRef(ensemble.id, ensemble.outputStem.trim())" type="button" @pointerdown.stop="beginConnection(simpleOutputRef(ensemble.id, ensemble.outputStem.trim()), $event)" @pointerup.stop="finishOutputConnection(simpleOutputRef(ensemble.id, ensemble.outputStem.trim()), $event)"><i /></button>
          </div>
        </article>

        <article class="simple-node simple-node--save" data-simple-node="save" :style="{ ...nodeStyle('save'), width: `${SAVE_WIDTH}px`, minHeight: `${saveHeight()}px` }" @pointerdown.stop="beginNodeDrag('save', $event)">
          <header><div><span>{{ t('workflows.saveNode') }}</span><strong>{{ t('workflows.generatedOutputs') }}</strong></div><span class="simple-node__count">{{ saveEntries().length }}</span></header>
          <button class="simple-save-drop-target" :class="{ 'simple-save-drop-target--active': pendingConnection && hoverTarget === 'save' }" type="button" data-simple-target="save" @pointerdown.stop @pointerup="finishConnection('save', $event)">{{ t('workflows.dropStemToSave') }}</button>
          <div v-for="(entry, index) in saveEntries()" :key="entry.source" class="simple-save-row">
            <i :ref="el => setPortElement(savePortKey(simpleSourceStepId(entry.source), entry.stem), el)" class="simple-save-row__port" :class="{ 'simple-save-row__port--active': pendingConnection?.direction === 'output' && pendingConnection.target === simpleSaveTarget(simpleSourceStepId(entry.source), entry.stem), 'simple-save-row__port--target': pendingConnection?.direction === 'input' && hoverTarget === simpleSaveTarget(simpleSourceStepId(entry.source), entry.stem) }" :data-simple-target="simpleSaveTarget(simpleSourceStepId(entry.source), entry.stem)" @pointerdown.stop="beginSaveConnection(entry.source, `${entry.sourceLabel} · ${entry.stem}`, $event)" @pointerup.stop="finishConnection(simpleSaveTarget(simpleSourceStepId(entry.source), entry.stem), $event)" />
            <div class="simple-save-row__details">
              <div class="simple-save-row__source">
                <span class="simple-save-row__source-model" :title="entry.sourceLabel">{{ entry.sourceLabel }}</span>
                <span class="simple-save-row__source-separator">·</span>
                <strong>{{ entry.stem }}</strong>
              </div>
              <div class="simple-save-row__preview" :title="saveFilenamePreview(entry, index)">
                <span class="simple-save-row__preview-label">{{ t('workflows.saveFilenamePreview') }}</span>
                <code>{{ saveFilenamePreview(entry, index) }}</code>
              </div>
              <n-input size="tiny" :value="entry.outputName" :placeholder="'%filename%_%stem%_%model%'" @pointerdown.stop @update:value="updateSaveOutputName(entry, $event)" />
            </div>
          </div>
          <span v-if="!saveEntries().length" class="simple-node__empty">{{ t('workflows.saveStemsPlaceholder') }}</span>
        </article>
      </div>
    </div>

    <footer class="simple-node-editor__footer">
      <span v-if="formError" class="simple-node-editor__error">{{ formError }}</span>
      <span v-else-if="advisory" class="simple-node-editor__warning">{{ advisory }}</span>
      <span v-else>{{ t('workflows.simpleEditorHint') }}</span>
      <div><n-button secondary :disabled="!canRun || saving" @click="emit('run')">{{ t('workflows.runWorkflowAction') }}</n-button><n-button quaternary @click="fitView">{{ t('workflows.fitView') }}</n-button></div>
    </footer>

    <n-modal v-model:show="showNodeTypeChooser" preset="card" :title="t('workflows.chooseNodeTypeTitle')" class="simple-node-type-modal" style="width: min(640px, calc(100vw - 32px))" :z-index="5000">
      <p class="simple-node-type-modal__hint">{{ t('workflows.chooseNodeTypeHint') }}</p>
      <div class="simple-node-type-modal__choices">
        <button type="button" @click="addNodeOfType('separation')">
          <span>{{ t('workflows.separationNode') }}</span>
          <strong>{{ t('workflows.separationNodeChoiceTitle') }}</strong>
          <small>{{ t('workflows.separationNodeChoiceDescription') }}</small>
        </button>
        <button type="button" @click="addNodeOfType('ensemble')">
          <span>Ensemble</span>
          <strong>{{ t('workflows.ensembleNode') }}</strong>
          <small>{{ t('workflows.ensembleNodeChoiceDescription') }}</small>
        </button>
        <button v-for="operation in SIMPLE_AUDIO_OPERATIONS" :key="operation" type="button" :data-add-audio-operation="operation" @click="addNodeOfType(operation)">
          <span>{{ t('workflows.audioProcessingNode') }}</span>
          <strong>{{ audioOperationCopy[operation].title }}</strong>
          <small>{{ audioOperationCopy[operation].description }}</small>
        </button>
      </div>
    </n-modal>

    <n-modal :show="showInferenceEditor" @update:show="(value: boolean) => !value && closeInferenceEditor()">
      <n-card
        class="simple-inference-modal"
        :title="t('workflows.stepInferenceTitle')"
        :bordered="false"
        closable
        role="dialog"
        aria-modal="true"
        style="width: min(620px, calc(100vw - 32px))"
        @close="closeInferenceEditor"
      >
        <div v-if="inferenceEditorStep" class="simple-inference-modal__body">
          <div class="simple-inference-modal__top">
            <p class="simple-inference-modal__model" :title="inferenceEditorStep.model">
              <span>{{ t('workflows.stepModel') }}</span>
              <strong>{{ inferenceEditorStep.model }}</strong>
            </p>
            <label class="simple-inference-follow">
              <span>{{ t('workflows.inferenceFollowGlobalShort') }}</span>
              <n-switch
                :value="inferenceMode === 'global'"
                size="small"
                @update:value="updateInferenceFollowGlobal"
              />
            </label>
          </div>

          <div class="simple-inference-fields" :class="{ 'simple-inference-fields--locked': inferenceMode === 'global' }">
            <div class="simple-inference-grid">
              <label>
                <span>{{ t('inference.batchSize') }}</span>
                <n-input-number :value="inferenceNumber('batch_size', 1)" :min="1" :max="32" :precision="0" :disabled="inferenceMode === 'global'" @update:value="updateInferenceNumber('batch_size', $event)" />
              </label>
              <template v-if="inferenceEditorIsVr">
                <label>
                  <span>{{ t('inference.vrWindowSize') }}</span>
                  <n-input-number :value="inferenceNumber('window_size', 512)" :min="1" :precision="0" :disabled="inferenceMode === 'global'" @update:value="updateInferenceNumber('window_size', $event)" />
                </label>
                <label>
                  <span>{{ t('inference.vrAggression') }}</span>
                  <n-input-number :value="inferenceNumber('aggression', 5)" :min="0" :precision="0" :disabled="inferenceMode === 'global'" @update:value="updateInferenceNumber('aggression', $event)" />
                </label>
                <label>
                  <span>{{ t('inference.vrPostProcessThreshold') }}</span>
                  <n-input-number :value="inferenceNumber('post_process_threshold', 0.2)" :min="0" :max="1" :step="0.01" :disabled="inferenceMode === 'global'" @update:value="updateInferenceNumber('post_process_threshold', $event)" />
                </label>
              </template>
              <template v-else>
                <label>
                  <span>{{ t('inference.overlapSize') }}</span>
                  <AlignedInferenceInputNumber :value="inferenceNumber('overlap_size', 0)" :step="inferenceEditorOverlapStep" :alignment-step="inferenceEditorSampleStep" :disabled="inferenceMode === 'global'" @update:value="updateInferenceNumber('overlap_size', $event)" />
                </label>
                <label>
                  <span>{{ t('inference.chunkSize') }}</span>
                  <AlignedInferenceInputNumber :value="inferenceNumber('chunk_size', 0)" :step="inferenceEditorChunkStep" :alignment-step="inferenceEditorChunkConstraint?.step || inferenceEditorSampleStep" :alignment-offset="inferenceEditorChunkConstraint?.offset" :alignment-min="inferenceEditorChunkConstraint?.min" :disabled="inferenceMode === 'global'" @update:value="updateInferenceNumber('chunk_size', $event)" />
                  <n-text v-if="inferenceEditorChunkIssue" type="warning">{{ t('inference.invalidChunkSize', inferenceEditorChunkIssue) }}</n-text>
                </label>
              </template>
            </div>

            <div class="simple-inference-switches">
              <label v-if="!inferenceEditorIsVr"><n-switch :value="inferenceBoolean('standardize')" size="small" :disabled="inferenceMode === 'global'" @update:value="updateInferenceBoolean('standardize', $event)" /><span>{{ t('inference.standardize') }}</span></label>
              <label><n-switch :value="inferenceBoolean('normalize')" size="small" :disabled="inferenceMode === 'global'" @update:value="updateInferenceBoolean('normalize', $event)" /><span>{{ t('inference.normalize') }}</span></label>
              <label><n-switch :value="inferenceBoolean('enable_tta')" size="small" :disabled="inferenceMode === 'global'" @update:value="updateInferenceBoolean('enable_tta', $event)" /><span>{{ t('workflows.inferenceEnableTta') }}</span></label>
              <label v-if="inferenceEditorIsVr"><n-switch :value="inferenceBoolean('high_end_process')" size="small" :disabled="inferenceMode === 'global'" @update:value="updateInferenceBoolean('high_end_process', $event)" /><span>{{ t('inference.vrHighEndProcess') }}</span></label>
              <label v-if="inferenceEditorIsVr"><n-switch :value="inferenceBoolean('enable_post_process')" size="small" :disabled="inferenceMode === 'global'" @update:value="updateInferenceBoolean('enable_post_process', $event)" /><span>{{ t('inference.vrEnablePostProcess') }}</span></label>
            </div>
            <div v-if="inferenceMode === 'global'" class="simple-inference-fields__mask" aria-hidden="true" />
          </div>
        </div>
        <template #footer>
          <div class="simple-inference-modal__footer">
            <n-button secondary @click="closeInferenceEditor">{{ t('common.cancel') }}</n-button>
            <n-button type="primary" :disabled="!inferenceEditorStep" @click="saveInferenceEditor">{{ t('common.save') }}</n-button>
          </div>
        </template>
      </n-card>
    </n-modal>

    <n-dropdown
      placement="bottom-start"
      trigger="manual"
      :x="contextMenuX"
      :y="contextMenuY"
      :options="nodeTypeMenuOptions"
      :show="contextMenuVisible"
      @clickoutside="closeNodeContextMenu"
      @select="handleNodeContextMenuSelect"
    />
  </div>
</template>

<style scoped>
.simple-node-editor {
  height: 100%;
  min-height: 0;
  display: grid;
  grid-template-rows: auto auto minmax(0, 1fr) auto;
  gap: 10px;
  padding: 0 12px 12px;
  overflow: hidden;
  background: var(--surface);
  color: var(--on-surface);
}

.simple-node-editor__topbar,
.simple-node-editor__meta,
.simple-node-editor__footer {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  border: 1px solid color-mix(in srgb, var(--outline) 78%, transparent);
  border-radius: 16px;
  background: color-mix(in srgb, var(--surface-1) 92%, var(--primary-soft));
  box-shadow:
    inset 0 0 0 1px color-mix(in srgb, var(--outline) 32%, transparent),
    0 14px 34px color-mix(in srgb, var(--surface) 10%, transparent);
}

.simple-node-editor__topbar {
  min-height: 58px;
  justify-content: space-between;
}

.simple-node-editor__title {
  display: grid;
  grid-template-columns: auto minmax(200px, min(380px, 42vw));
  align-items: center;
  gap: 10px;
  min-width: 260px;
}

.simple-node-editor__title > span {
  color: var(--on-surface-muted);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: .02em;
  white-space: nowrap;
}

.simple-node-editor__title :deep(.n-input) {
  width: 100%;
}

.simple-node-editor__actions {
  display: flex;
  gap: 7px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.simple-node-editor__meta {
  min-height: 52px;
  padding: 8px 12px;
  border: 1px solid color-mix(in srgb, var(--outline) 70%, transparent);
  border-radius: 12px;
  border-bottom-color: color-mix(in srgb, var(--outline) 72%, transparent);
  background: color-mix(in srgb, var(--surface-1) 94%, var(--surface-2));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 24%, transparent);
}

.simple-node-editor__meta > :first-child {
  flex: 1 1 auto;
  min-width: 200px;
}

.simple-node-editor__meta label {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--on-surface-muted);
  font-size: 11px;
  white-space: nowrap;
}

.simple-node-editor__meta label :deep(.n-select) {
  width: 120px;
}

.simple-node-editor__switch {
  margin-left: auto;
}

.simple-node-editor__canvas {
  position: relative;
  min-height: 0;
  overflow: clip;
  border: 1px solid color-mix(in srgb, var(--outline) 78%, transparent);
  border-radius: 16px;
  cursor: grab;
  background-color: var(--surface);
  background-image:
    linear-gradient(color-mix(in srgb, var(--outline) 50%, transparent) 1px, transparent 1px),
    linear-gradient(90deg, color-mix(in srgb, var(--outline) 50%, transparent) 1px, transparent 1px);
  background-size: 32px 32px;
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 22%, transparent);
}

.simple-node-editor__canvas:active { cursor: grabbing; }

.simple-node-editor__hint {
  position: absolute;
  z-index: 4;
  top: 12px;
  left: 16px;
  max-width: min(620px, calc(100% - 32px));
  padding: 7px 10px;
  border: 1px solid color-mix(in srgb, var(--outline) 78%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, var(--surface-1) 94%, transparent);
  color: var(--on-surface-muted);
  box-shadow: 0 6px 20px color-mix(in srgb, var(--surface) 14%, transparent);
  font-size: 12px;
  pointer-events: none;
}

.simple-node-editor__world {
  position: absolute;
  top: 0;
  left: 0;
  width: 2600px;
  height: 1600px;
  transform-origin: 0 0;
}

.simple-node-editor__edges {
  position: absolute;
  inset: 0;
  overflow: visible;
  color: color-mix(in srgb, var(--primary-strong) 62%, var(--on-surface-muted));
  pointer-events: none;
}

.simple-node-editor__edge {
  fill: none;
  stroke: currentColor;
  stroke-width: 2.2;
  stroke-linecap: round;
  stroke-linejoin: round;
  opacity: .84;
  pointer-events: stroke;
  cursor: pointer;
}

.simple-node-editor__edge-hit {
  fill: none;
  stroke: transparent;
  stroke-width: 13;
  pointer-events: stroke;
  cursor: pointer;
  touch-action: none;
}

.simple-node-editor__edge-group:hover .simple-node-editor__edge {
  stroke: var(--primary-strong);
  opacity: 1;
}

.simple-node-editor__edge--pending {
  stroke: var(--primary);
  stroke-dasharray: 7 7;
  pointer-events: none;
}

.simple-node {
  position: absolute;
  display: grid;
  align-content: start;
  gap: 8px;
  padding: 12px;
  border: 1px solid color-mix(in srgb, var(--outline) 92%, transparent);
  border-radius: 13px;
  background: var(--surface-1);
  box-shadow: var(--shadow-soft);
  user-select: none;
}

.simple-node--input {
  border-color: color-mix(in srgb, var(--success) 60%, var(--outline));
  background: color-mix(in srgb, var(--success) 8%, var(--surface-1));
}

.simple-node--step {
  border-color: color-mix(in srgb, var(--primary) 60%, var(--outline));
  background: color-mix(in srgb, var(--primary-soft) 62%, var(--surface-1));
}

.simple-node--ensemble {
  border-color: color-mix(in srgb, #8b5cf6 62%, var(--outline));
  background: color-mix(in srgb, #8b5cf6 9%, var(--surface-1));
}

.simple-node--audio-operation {
  border-color: color-mix(in srgb, var(--success) 60%, var(--outline));
  background: color-mix(in srgb, var(--success) 6%, var(--surface-1));
}

.simple-audio-operation-hint { margin: 0; color: var(--on-surface-muted); font-size: 11px; line-height: 1.5; }

.simple-node--save {
  border-color: color-mix(in srgb, var(--warning) 64%, var(--outline));
  background: color-mix(in srgb, var(--warning) 8%, var(--surface-1));
}

.simple-node--selected {
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--primary) 76%, transparent), var(--shadow-soft);
}

.simple-node header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 8px;
  padding-bottom: 8px;
  border-bottom: 1px solid color-mix(in srgb, var(--outline) 74%, transparent);
}

.simple-node header div {
  min-width: 0;
  display: grid;
  gap: 3px;
}

.simple-node header span {
  color: var(--on-surface-muted);
  font-size: 11px;
  font-weight: 600;
}

.simple-node--input header span { color: var(--success); }
.simple-node--step header span { color: var(--primary-strong); }
.simple-node--ensemble header span { color: color-mix(in srgb, #8b5cf6 80%, var(--on-surface)); }
.simple-node--audio-operation header span { color: var(--success); }
.simple-node--save header span { color: var(--warning); }

.simple-node header strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--on-surface);
  font-size: 14px;
  font-weight: 700;
}

.simple-icon-button {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--on-surface-muted);
  cursor: pointer;
}

.simple-icon-button:hover { background: color-mix(in srgb, var(--danger) 12%, transparent); color: var(--danger); }
.simple-icon-button:disabled { opacity: .35; cursor: not-allowed; }
.simple-node__header-actions { display: flex; align-items: center; gap: 2px; }
.simple-node__header-actions .simple-icon-button:not(:last-child):not(:disabled):hover {
  background: color-mix(in srgb, var(--primary) 12%, transparent);
  color: var(--primary-strong);
}
.simple-icon-button:focus-visible,
.simple-port:focus-visible,
.simple-ensemble-output-port:focus-visible,
.simple-save-drop-target:focus-visible,
.simple-save-row__port:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }

.simple-node__body { display: grid; gap: 8px; }

.simple-node__input-wrap {
  position: relative;
  min-height: 26px;
}

.simple-node__body label {
  display: grid;
  gap: 4px;
  color: var(--on-surface-muted);
  font-size: 11px;
}

.simple-node__body :deep(.n-select) { min-width: 0; }

.simple-inference-settings { position: relative; }
.simple-inference-settings:hover { background: color-mix(in srgb, var(--primary) 12%, transparent) !important; color: var(--primary-strong) !important; }
.simple-inference-settings--custom { background: color-mix(in srgb, var(--primary) 10%, transparent); color: var(--primary-strong); }
.simple-inference-settings__dot { position: absolute; top: 3px; right: 3px; width: 5px; height: 5px; border: 1px solid var(--surface-1); border-radius: 50%; background: var(--primary); box-shadow: 0 0 0 1px color-mix(in srgb, var(--primary) 28%, transparent); }

.simple-port {
  position: relative;
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  min-height: 26px;
  padding: 3px 6px;
  border: 0;
  border-radius: 6px;
  background: color-mix(in srgb, var(--surface-2) 64%, transparent);
  color: var(--on-surface);
  cursor: crosshair;
  touch-action: none;
  user-select: none;
  text-align: left;
  font-size: 12px;
  transition: background 140ms ease, outline-color 140ms ease;
}

.simple-port:hover { background: color-mix(in srgb, var(--primary-soft) 60%, var(--surface-2)); }
.simple-port--target { outline: 2px solid var(--primary); background: var(--primary-soft); }
.simple-port--source-target { outline: 2px solid var(--primary-strong); background: color-mix(in srgb, var(--primary-soft) 76%, var(--surface-2)); }

.simple-port i,
.simple-save-row__port {
  width: 9px;
  height: 9px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--primary);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 22%, transparent);
}

.simple-port--input {
  position: relative;
  top: 0;
  left: -13px;
  width: 150px;
  height: 26px;
  min-height: 26px;
  gap: 4px;
  padding-left: 0;
  background: transparent;
}
.simple-port--input:hover { background: transparent; }
.simple-port--input span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.simple-port--input i { background: var(--primary-strong); transform: translateX(-4px); }
.simple-port--output { justify-content: space-between; }
.simple-port--output i { order: 2; }
.simple-node--input .simple-port--output i { background: var(--success); box-shadow: 0 0 0 3px color-mix(in srgb, var(--success) 22%, transparent); }

.simple-node__outputs { display: grid; gap: 4px; margin-top: 5px; }
.simple-output-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 5px; align-items: center; }
.simple-output-row :deep(.n-button) { font-size: 11px; }
.simple-ensemble-inputs { display: grid; gap: 7px; }
.simple-ensemble-input-row { display: grid; grid-template-columns: 76px minmax(0, 1fr) 54px 24px; gap: 5px; align-items: center; }
.simple-ensemble-input-row--audio-operation { grid-template-columns: 76px minmax(0, 1fr) auto; }
.simple-ensemble-input-row__port { left: -13px; width: 89px; font-size: 10px; }
.simple-ensemble-input-row__source { min-width: 0; overflow: hidden; padding: 3px 6px; border-radius: 6px; background: color-mix(in srgb, var(--surface-2) 56%, transparent); color: var(--on-surface-muted); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.simple-ensemble-input-row :deep(.n-input-number) { width: 54px; }
.simple-ensemble-input-row :deep(.n-input__input-el) { text-align: center; }
.simple-ensemble-output-row { position: relative; min-height: 26px; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px; align-items: center; margin-top: 2px; padding-right: 8px; }
.simple-ensemble-output-row__label { min-width: 0; overflow: hidden; color: var(--on-surface); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.simple-ensemble-output-port { position: absolute; top: 50%; right: -18px; width: 20px; height: 26px; display: grid; place-items: center; padding: 0; border: 0; background: transparent; cursor: crosshair; transform: translateY(-50%); touch-action: none; }
.simple-ensemble-output-port i { width: 9px; height: 9px; border-radius: 50%; background: var(--primary); box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 22%, transparent); }
.simple-ensemble-output-port--target i { outline: 2px solid var(--primary-strong); outline-offset: 2px; }
.simple-node__empty { color: var(--on-surface-muted); font-size: 12px; }
.simple-node__count { color: var(--warning) !important; }

.simple-save-drop-target {
  min-height: 30px;
  border: 1px dashed color-mix(in srgb, var(--warning) 70%, var(--outline));
  border-radius: 7px;
  background: color-mix(in srgb, var(--warning) 8%, transparent);
  color: color-mix(in srgb, var(--warning) 88%, var(--on-surface));
  cursor: crosshair;
  font-size: 12px;
}

.simple-save-drop-target:hover,
.simple-save-drop-target--active { outline: 2px solid var(--warning); background: color-mix(in srgb, var(--warning) 16%, transparent); }

.simple-save-row {
  display: grid;
  grid-template-columns: 12px minmax(0, 1fr);
  gap: 8px;
  align-items: start;
  min-height: 78px;
  padding: 5px 0;
  color: var(--on-surface);
  font-size: 11px;
}

.simple-save-row + .simple-save-row { border-top: 1px solid color-mix(in srgb, var(--warning) 18%, var(--outline)); }
.simple-save-row__details { min-width: 0; display: grid; gap: 5px; }
.simple-save-row__source { min-width: 0; display: flex; align-items: center; gap: 5px; line-height: 1.2; }
.simple-save-row__source-model { min-width: 0; overflow: hidden; color: var(--on-surface-muted); text-overflow: ellipsis; white-space: nowrap; }
.simple-save-row__source-separator { color: color-mix(in srgb, var(--on-surface-muted) 65%, transparent); }
.simple-save-row__source strong { flex: 0 0 auto; color: var(--warning); font-size: 11px; font-weight: 700; }
.simple-save-row__preview { min-width: 0; display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 6px; padding: 4px 6px; border: 1px dashed color-mix(in srgb, var(--warning) 42%, var(--outline)); border-radius: 6px; background: color-mix(in srgb, var(--warning) 7%, var(--surface-1)); }
.simple-save-row__preview-label { color: color-mix(in srgb, var(--warning) 78%, var(--on-surface)); font-size: 10px; font-weight: 700; white-space: nowrap; }
.simple-save-row__preview code { min-width: 0; overflow: hidden; color: var(--on-surface); text-overflow: ellipsis; white-space: nowrap; font-family: inherit; font-size: 10px; }
.simple-save-row :deep(.n-input) { min-width: 0; width: 100%; }
.simple-save-row__port { background: var(--warning); box-shadow: 0 0 0 3px color-mix(in srgb, var(--warning) 24%, transparent); cursor: crosshair; touch-action: none; }
.simple-save-row__port--active { outline: 2px solid var(--warning); outline-offset: 2px; }
.simple-save-row__port--target { outline: 2px solid var(--primary); outline-offset: 2px; }

.simple-node-editor__footer {
  justify-content: space-between;
  min-height: 52px;
  border-top: 1px solid color-mix(in srgb, var(--outline) 78%, transparent);
  border-bottom: 1px solid color-mix(in srgb, var(--outline) 78%, transparent);
  border-radius: 12px;
  background: var(--surface-1);
  color: var(--on-surface-muted);
  font-size: 11px;
}

.simple-node-editor__footer > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.simple-node-editor__footer > div { display: flex; gap: 8px; flex: 0 0 auto; }
.simple-node-editor__error { color: var(--danger); }
.simple-node-editor__warning { color: var(--warning); }

.simple-node-type-modal {
  z-index: 4000 !important;
  background: var(--surface-1) !important;
  box-shadow: 0 24px 70px color-mix(in srgb, #000 28%, transparent) !important;
}

.simple-node-type-modal__hint {
  margin: 0 0 14px;
  color: var(--on-surface-muted);
  font-size: 13px;
}

.simple-inference-modal {
  overflow: hidden;
  border: 1px solid color-mix(in srgb, var(--outline) 72%, transparent) !important;
  border-radius: 14px !important;
  background: var(--surface-1) !important;
  box-shadow: 0 24px 68px color-mix(in srgb, #000 24%, transparent) !important;
}

.simple-inference-modal :deep(.n-card-header) { padding: 18px 20px 10px; }
.simple-inference-modal :deep(.n-card-header__main) { color: var(--on-surface); font-size: 17px; font-weight: 680; letter-spacing: -.02em; }
.simple-inference-modal :deep(.n-card__content) { padding: 4px 20px 18px; }
.simple-inference-modal :deep(.n-card__footer) { padding: 12px 20px 16px; border-top: 1px solid color-mix(in srgb, var(--outline) 54%, transparent); }
.simple-inference-modal__body { display: grid; gap: 14px; }
.simple-inference-modal__top { min-width: 0; display: flex; justify-content: space-between; align-items: center; gap: 16px; }
.simple-inference-modal__model { min-width: 0; display: flex; align-items: baseline; gap: 8px; margin: 0; }
.simple-inference-modal__model span { flex: 0 0 auto; color: var(--on-surface-muted); font-size: 10px; }
.simple-inference-modal__model strong { min-width: 0; overflow: hidden; color: var(--on-surface); text-overflow: ellipsis; white-space: nowrap; font-size: 12px; font-weight: 650; }
.simple-inference-follow { flex: 0 0 auto; display: inline-flex; align-items: center; gap: 8px; color: var(--on-surface); font-size: 11px; font-weight: 620; }
.simple-inference-fields { position: relative; display: grid; gap: 12px; padding-top: 2px; }
.simple-inference-fields--locked .simple-inference-grid,
.simple-inference-fields--locked .simple-inference-switches { filter: saturate(.72); }
.simple-inference-fields__mask { position: absolute; z-index: 2; inset: -4px; border-radius: 10px; background: color-mix(in srgb, var(--surface-1) 32%, transparent); cursor: not-allowed; }
.simple-inference-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; padding-top: 2px; }
.simple-inference-grid label { min-width: 0; display: grid; gap: 5px; color: var(--on-surface-muted); font-size: 10px; font-weight: 600; }
.simple-inference-grid :deep(.n-input-number),
.simple-inference-grid :deep(.aligned-inference-input) { width: 100%; }
.simple-inference-switches { display: flex; flex-wrap: wrap; gap: 8px 16px; padding-top: 2px; }
.simple-inference-switches label { display: inline-flex; align-items: center; gap: 7px; color: var(--on-surface-muted); font-size: 10px; font-weight: 600; }
.simple-inference-modal__footer { display: flex; justify-content: flex-end; gap: 8px; }

.simple-node-type-modal__choices {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.simple-node-type-modal__choices button {
  min-width: 0;
  display: grid;
  gap: 6px;
  padding: 16px;
  border: 1px solid color-mix(in srgb, var(--outline) 84%, transparent);
  border-radius: 12px;
  background: color-mix(in srgb, var(--surface-2) 54%, transparent);
  color: var(--on-surface);
  cursor: pointer;
  text-align: left;
  transition: border-color 140ms ease, background 140ms ease, transform 140ms ease;
}

.simple-node-type-modal__choices button:hover,
.simple-node-type-modal__choices button:focus-visible {
  border-color: var(--primary);
  background: color-mix(in srgb, var(--primary-soft) 56%, var(--surface-2));
  outline: none;
  transform: translateY(-1px);
}

.simple-node-type-modal__choices button > span {
  color: var(--primary-strong);
  font-size: 11px;
  font-weight: 700;
}

.simple-node-type-modal__choices button > strong {
  font-size: 15px;
}

.simple-node-type-modal__choices button > small {
  color: var(--on-surface-muted);
  font-size: 12px;
  line-height: 1.55;
}

@media (max-width: 900px) {
  .simple-node-editor__meta { flex-wrap: wrap; }
  .simple-node-editor__switch { margin-left: 0; }
  .simple-node-editor__topbar { align-items: flex-start; flex-direction: column; }
  .simple-node-editor__title { width: 100%; grid-template-columns: auto minmax(0, 1fr); }
  .simple-node-editor__footer { align-items: flex-start; flex-direction: column; }
  .simple-node-editor__footer > span { width: 100%; }
  .simple-node-editor__footer > div { width: 100%; flex-wrap: wrap; }
  .simple-node-type-modal__choices { grid-template-columns: 1fr; }
  .simple-inference-grid { grid-template-columns: 1fr; }
}
</style>
