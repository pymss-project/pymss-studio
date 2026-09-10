<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { ArrowUndoOutline, ArrowRedoOutline, CloseOutline, LocateOutline, SaveOutline } from '@vicons/ionicons5'
import type { ModelEntry } from '@/stores/model'
import {
  configuredStemsFor,
  createDefaultSimpleEditorUi,
  createStepDraft,
  renderSimpleOutputFilename,
  fitSimpleEditorViewport,
  type SimpleDraft,
  type SimpleEditorPoint,
  type SimpleStepDraft,
} from '@/utils/workflowSimple'
import {
  canConnectSimple,
  cleanupSimpleDraft,
  connectSimple,
  disconnectSimple,
  simpleSourceStepId,
  simpleSourceStem,
  simpleOutputRef,
  simpleSaveTarget,
  simpleStepInputTarget,
} from '@/utils/simpleWorkflowEditor'

const draft = defineModel<SimpleDraft>('draft', { required: true })
const name = defineModel<string>('name', { required: true })
const description = defineModel<string>('description', { required: true })

const props = withDefaults(defineProps<{
  models: ModelEntry[]
  saving?: boolean
  formError?: string
  canSave?: boolean
}>(), {
  saving: false,
  formError: '',
  canSave: false,
})

const emit = defineEmits<{
  save: []
  close: []
  run: []
}>()

const { t, locale } = useI18n()
const message = useMessage()
const connectionMessageKeys: Record<string, string> = {
  'missing-source': 'workflows.simpleConnection.missingSource',
  'missing-target': 'workflows.simpleConnection.missingTarget',
  'invalid-source': 'workflows.simpleConnection.invalidSource',
  'forward-link': 'workflows.simpleConnection.forwardLink',
  'self-link': 'workflows.simpleConnection.selfLink',
  'invalid-save-target': 'workflows.simpleConnection.invalidSaveTarget',
}
const canvasRef = ref<HTMLElement | null>(null)
const worldRef = ref<HTMLElement | null>(null)
type SimpleConnectionTarget = `step:${string}` | `save` | `save:${string}.${string}`
type SimpleConnectionHoverTarget = SimpleConnectionTarget | `output:${string}` | null
type PendingConnection =
  | { direction: 'input'; source: string; label: string }
  | { direction: 'output'; target: Exclude<SimpleConnectionTarget, 'save'>; label: string }
const pendingConnection = ref<PendingConnection | null>(null)
const hoverTarget = ref<SimpleConnectionHoverTarget>(null)
const pointerWorld = ref<SimpleEditorPoint>({ x: 500, y: 300 })
const selectedStepId = ref('')
const history = ref<string[]>([])
const future = ref<string[]>([])
const drag = ref<{ id: string; dx: number; dy: number } | null>(null)
const pan = ref<{ x: number; y: number } | null>(null)
const zoom = computed(() => draft.value.ui.viewport.zoom)
const layoutVersion = ref(0)
const portElements = new Map<string, HTMLElement>()
let layoutFrame = 0
let portResizeObserver: ResizeObserver | null = null

const NODE_WIDTH = 300
const INPUT_WIDTH = 230
const SAVE_WIDTH = 340
const NODE_HEIGHT_BASE = 166
const PORT_GAP = 26
const SAVE_ROW_GAP = 84

const modelOptions = computed(() => [...props.models]
  .sort((a, b) => a.name.localeCompare(b.name, locale.value === 'zh-CN' ? 'zh-CN' : 'en'))
  .map(item => ({ label: item.name, value: item.name })))

function snapshot() {
  return JSON.stringify(draft.value)
}

function restore(serialized: string) {
  const restored = JSON.parse(serialized) as SimpleDraft
  draft.value = restored
  cleanupSimpleDraft(draft.value)
}

function recordHistory() {
  const current = snapshot()
  const previous = history.value[history.value.length - 1]
  if (current === previous) return
  history.value = [...history.value, current].slice(-80)
  future.value = []
}

function undo() {
  if (history.value.length <= 1) return
  const current = history.value[history.value.length - 1]
  future.value = [current, ...future.value].slice(0, 80)
  history.value = history.value.slice(0, -1)
  restore(history.value[history.value.length - 1])
}

function redo() {
  const next = future.value[0]
  if (!next) return
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

function saveEntries() {
  return draft.value.steps.flatMap(step => Object.keys(step.save || {})
    .filter(stem => step.stems.some(item => item.toLowerCase() === stem.toLowerCase()))
    .map(stem => ({ step, stem })))
}

function saveFilenamePreview(step: SimpleStepDraft, stem: string, index: number) {
  return renderSimpleOutputFilename(step.outputNames?.[stem], {
    stem,
    model: step.model || step.id,
    stepId: step.id,
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

function saveInputPoint(stepId: string, stem: string) {
  const measured = portPoint(savePortKey(stepId, stem))
  if (measured) return measured
  const point = nodePoint('save')
  const index = saveEntries().findIndex(item => item.step.id === stepId && item.stem === stem)
  return { x: point.x, y: point.y + 106 + Math.max(0, index) * SAVE_ROW_GAP }
}

function targetPoint(target: Exclude<SimpleConnectionTarget, 'save'>): SimpleEditorPoint | null {
  if (target.startsWith('step:')) {
    const step = draft.value.steps.find(item => item.id === target.slice('step:'.length))
    return step ? inputPoint(step) : null
  }
  const value = target.slice('save:'.length)
  return saveInputPoint(simpleSourceStepId(value), simpleSourceStem(value))
}

function sourcePoint(source: string) {
  if (source === 'input') return inputOutputPoint()
  const step = draft.value.steps.find(item => item.id === source.slice(0, source.lastIndexOf('.')))
  const stem = source.slice(source.lastIndexOf('.') + 1)
  return step ? outputPoint(step, stem) : null
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
  saveEntries().forEach(({ step, stem }) => {
    const source = sourcePoint(simpleOutputRef(step.id, stem))
    const target = simpleSaveTarget(step.id, stem)
    if (source && movingTarget !== target) {
      items.push({ id: `save:${step.id}.${stem}`, path: pathBetween(source, saveInputPoint(step.id, stem)), source: simpleOutputRef(step.id, stem), target })
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
  hoverTarget.value = value === 'save' || value?.startsWith('step:') || value?.startsWith('save:')
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
  selectedStepId.value = draft.value.steps.some(step => step.id === id) ? id : ''
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

function beginSaveConnection(step: SimpleStepDraft, stem: string, event: PointerEvent) {
  if (event.button !== 0 || pendingConnection.value) return
  event.preventDefault()
  pendingConnection.value = {
    direction: 'output',
    target: simpleSaveTarget(step.id, stem),
    label: `${step.model || step.id} · ${stem}`,
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
    const previousName = previousStep?.outputNames[simpleSourceStem(previousValue)]
    disconnectSimple(draft.value, target)
    connectSimple(draft.value, source, 'save')
    if (previousName?.trim()) {
      const nextStep = draft.value.steps.find(item => item.id === simpleSourceStepId(source))
      const nextStem = simpleSourceStem(source)
      if (nextStep && nextStem) nextStep.outputNames = { ...nextStep.outputNames, [nextStem]: previousName }
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

function updateModel(step: SimpleStepDraft, modelName: string | null) {
  const value = String(modelName || '')
  const stems = modelStems(value)
  const oldNames = step.outputNames || {}
  const oldNamesByStem = new Map(Object.entries(oldNames).map(([key, value]) => [key.toLowerCase(), value]))
  const oldSaveByStem = new Map(Object.entries(step.save || {}).map(([key, value]) => [key.toLowerCase(), value]))
  step.model = value
  step.stems = stems
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
  step.outputNames = { ...step.outputNames, [stem]: value.trim() || '%filename%_%stem%_%model%' }
  recordHistory()
}

function addStep(at?: SimpleEditorPoint) {
  const step = createStepDraft(draft.value.steps.length)
  const usedIds = new Set(draft.value.steps.map(item => item.id))
  let suffix = draft.value.steps.length + 1
  let nextId = `step${suffix}`
  while (usedIds.has(nextId)) nextId = `step${++suffix}`
  step.id = nextId
  const previous = draft.value.steps[draft.value.steps.length - 1]
  if (previous?.stems[0]) step.input = simpleOutputRef(previous.id, previous.stems[0])
  draft.value.steps = [...draft.value.steps, step]
  const previousPoint = previous ? nodePoint(previous.id) : { x: 360, y: 190 }
  const point = at
    ? { x: Math.max(24, at.x - NODE_WIDTH / 2), y: Math.max(24, at.y - 70) }
    : { x: previousPoint.x + 340, y: previousPoint.y }
  draft.value.ui.nodes = { ...draft.value.ui.nodes, [step.id]: point }
  recordHistory()
}

function handleCanvasDoubleClick(event: MouseEvent) {
  const target = event.target instanceof Element ? event.target : null
  if (target?.closest('[data-simple-node], .simple-node-editor__edges, button, input, select, textarea')) return
  event.preventDefault()
  addStep(canvasWorldPoint(event))
}

function removeStep(step: SimpleStepDraft) {
  if (draft.value.steps.length <= 1) return
  draft.value.steps = draft.value.steps.filter(item => item.id !== step.id)
  const nodes = { ...draft.value.ui.nodes }
  delete nodes[step.id]
  draft.value.ui.nodes = nodes
  cleanupSimpleDraft(draft.value)
  selectedStepId.value = ''
  recordHistory()
}

function autoLayout() {
  const nodes: Record<string, SimpleEditorPoint> = { input: { x: 64, y: 250 }, save: { x: 360 + draft.value.steps.length * 340, y: 250 } }
  draft.value.steps.forEach((step, index) => { nodes[step.id] = { x: 360 + index * 340, y: 220 + (index % 2) * 120 } })
  draft.value.ui = { ...draft.value.ui, nodes, viewport: { x: 0, y: 0, zoom: 1 } }
  recordHistory()
}

function fitView() {
  const canvas = canvasRef.value
  if (!canvas || canvas.clientWidth <= 0 || canvas.clientHeight <= 0) return

  const currentZoom = Math.max(0.01, zoom.value)
  const stepIds = draft.value.steps.map(step => step.id)
  let stepIndex = 0
  const bounds = Array.from(canvas.querySelectorAll<HTMLElement>('[data-simple-node]')).flatMap((element) => {
    const id = element.dataset.simpleNode === 'step' ? stepIds[stepIndex++] : element.dataset.simpleNode
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
  recordHistory()
}

function handleWheel(event: WheelEvent) {
  event.preventDefault()
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
  recordHistory()
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && pendingConnection.value) {
    pendingConnection.value = null
    hoverTarget.value = null
    return
  }
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

onMounted(() => {
  if (!draft.value.ui) draft.value.ui = createDefaultSimpleEditorUi(draft.value.steps)
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
        <n-button size="small" secondary @click="addStep">{{ t('workflows.addStep') }}</n-button>
        <n-button size="small" secondary @click="autoLayout"><template #icon><n-icon :component="LocateOutline" /></template>{{ t('workflows.autoLayout') }}</n-button>
        <n-button size="small" type="primary" :loading="saving" :disabled="!canSave" @click="emit('save')"><template #icon><n-icon :component="SaveOutline" /></template>{{ t('common.save') }}</n-button>
      </div>
    </header>

    <div class="simple-node-editor__meta">
      <n-input v-model:value="description" size="small" :placeholder="t('workflows.descriptionPlaceholder')" />
      <label><span>{{ t('workflows.defaultDevice') }}</span><n-select v-model:value="draft.defaultDevice" size="small" :options="[{ label: 'Auto', value: 'auto' }, { label: 'CPU', value: 'cpu' }, { label: 'CUDA', value: 'cuda' }, { label: 'MPS', value: 'mps' }, { label: 'MLX', value: 'mlx' }]" /></label>
      <label><span>{{ t('workflows.defaultFormat') }}</span><n-select v-model:value="draft.defaultFormat" size="small" :options="[{ label: 'WAV', value: 'wav' }, { label: 'FLAC', value: 'flac' }, { label: 'MP3', value: 'mp3' }, { label: 'M4A', value: 'm4a' }]" /></label>
    </div>

    <div ref="canvasRef" class="simple-node-editor__canvas" @wheel="handleWheel" @pointermove="moveCanvas" @pointerup="endCanvasPointer" @pointercancel="endCanvasPointer" @pointerdown="beginCanvasPan" @dblclick="handleCanvasDoubleClick">
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

        <article v-for="(step, index) in draft.steps" :key="step.id" class="simple-node simple-node--step" :class="{ 'simple-node--selected': selectedStepId === step.id }" :style="{ ...nodeStyle(step.id), width: `${NODE_WIDTH}px`, minHeight: `${stepHeight(step)}px` }" data-simple-node="step" @pointerdown.stop="beginNodeDrag(step.id, $event)">
          <header><div><span>{{ t('workflows.separationNode') }} {{ index + 1 }}</span><strong>{{ step.model || t('workflows.stepModelPlaceholder') }}</strong></div><button type="button" class="simple-icon-button" :title="t('workflows.removeStep')" :disabled="draft.steps.length <= 1" @pointerdown.stop @click.stop="removeStep(step)"><n-icon :component="CloseOutline" /></button></header>
          <div class="simple-node__input-wrap" @pointerdown.stop>
            <button :ref="el => setPortElement(`input:${step.id}`, el)" class="simple-port simple-port--input" :class="{ 'simple-port--target': pendingConnection?.direction === 'input' && hoverTarget === `step:${step.id}` }" type="button" :data-simple-target="`step:${step.id}`" @pointerdown.stop="beginInputConnection(step, $event)" @pointerup.stop="finishConnection(`step:${step.id}`, $event)"><i /><span>{{ step.input || t('workflows.stepInputPlaceholder') }}</span></button>
          </div>
          <div class="simple-node__body" @pointerdown.stop>
            <label><span>{{ t('workflows.stepModel') }}</span><n-select :value="step.model" size="small" filterable :options="modelOptions" :placeholder="t('workflows.stepModelPlaceholder')" @update:value="updateModel(step, $event)" /></label>
            <label><span>{{ t('workflows.stepInput') }}</span><n-select :value="step.input" size="small" :options="[{ label: t('workflows.originalInput'), value: 'input' }, ...draft.steps.slice(0, index).flatMap(source => source.stems.map(stem => ({ label: `${source.model || source.id} · ${stem}`, value: simpleOutputRef(source.id, stem) })))]" @update:value="updateStepInput(step, String($event || ''))" /></label>
          </div>
          <div class="simple-node__outputs">
            <div v-for="stem in step.stems" :key="stem" class="simple-output-row">
              <button :ref="el => setPortElement(outputPortKey(step.id, stem), el)" class="simple-port simple-port--output" :class="{ 'simple-port--source-target': pendingConnection?.direction === 'output' && hoverTarget === `output:${simpleOutputRef(step.id, stem)}` }" :data-simple-source="simpleOutputRef(step.id, stem)" type="button" @pointerdown.stop="beginConnection(simpleOutputRef(step.id, stem), $event)" @pointerup.stop="finishOutputConnection(simpleOutputRef(step.id, stem), $event)"><span>{{ stem }}</span><i /></button>
              <n-button size="tiny" :type="step.save[stem] ? 'primary' : 'default'" secondary @pointerdown.stop @click.stop="toggleSave(step, stem)">{{ step.save[stem] ? t('workflows.saved') : t('workflows.saveStems') }}</n-button>
            </div>
            <span v-if="!step.stems.length" class="simple-node__empty">{{ t('workflows.noStemPorts') }}</span>
          </div>
        </article>

        <article class="simple-node simple-node--save" data-simple-node="save" :style="{ ...nodeStyle('save'), width: `${SAVE_WIDTH}px`, minHeight: `${saveHeight()}px` }" @pointerdown.stop="beginNodeDrag('save', $event)">
          <header><div><span>{{ t('workflows.saveNode') }}</span><strong>{{ t('workflows.generatedOutputs') }}</strong></div><span class="simple-node__count">{{ saveEntries().length }}</span></header>
          <button class="simple-save-drop-target" :class="{ 'simple-save-drop-target--active': pendingConnection && hoverTarget === 'save' }" type="button" data-simple-target="save" @pointerdown.stop @pointerup="finishConnection('save', $event)">{{ t('workflows.dropStemToSave') }}</button>
          <div v-for="(entry, index) in saveEntries()" :key="`${entry.step.id}:${entry.stem}`" class="simple-save-row">
            <i :ref="el => setPortElement(savePortKey(entry.step.id, entry.stem), el)" class="simple-save-row__port" :class="{ 'simple-save-row__port--active': pendingConnection?.direction === 'output' && pendingConnection.target === simpleSaveTarget(entry.step.id, entry.stem), 'simple-save-row__port--target': pendingConnection?.direction === 'input' && hoverTarget === simpleSaveTarget(entry.step.id, entry.stem) }" :data-simple-target="simpleSaveTarget(entry.step.id, entry.stem)" @pointerdown.stop="beginSaveConnection(entry.step, entry.stem, $event)" @pointerup.stop="finishConnection(simpleSaveTarget(entry.step.id, entry.stem), $event)" />
            <div class="simple-save-row__details">
              <div class="simple-save-row__source">
                <span class="simple-save-row__source-model" :title="entry.step.model || entry.step.id">{{ entry.step.model || entry.step.id }}</span>
                <span class="simple-save-row__source-separator">·</span>
                <strong>{{ entry.stem }}</strong>
              </div>
              <div class="simple-save-row__preview" :title="saveFilenamePreview(entry.step, entry.stem, index)">
                <span class="simple-save-row__preview-label">{{ t('workflows.saveFilenamePreview') }}</span>
                <code>{{ saveFilenamePreview(entry.step, entry.stem, index) }}</code>
              </div>
              <n-input size="tiny" :value="entry.step.outputNames[entry.stem]" :placeholder="'%filename%_%stem%_%model%'" @pointerdown.stop @update:value="updateOutputName(entry.step, entry.stem, $event)" />
            </div>
          </div>
          <span v-if="!saveEntries().length" class="simple-node__empty">{{ t('workflows.saveStemsPlaceholder') }}</span>
        </article>
      </div>
    </div>

    <footer class="simple-node-editor__footer">
      <span v-if="formError" class="simple-node-editor__error">{{ formError }}</span>
      <span v-else>{{ t('workflows.simpleEditorHint') }}</span>
      <div><n-button secondary :disabled="!canSave || saving" @click="emit('run')">{{ t('workflows.runWorkflowAction') }}</n-button><n-button quaternary @click="fitView">{{ t('workflows.fitView') }}</n-button></div>
    </footer>
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
  overflow: hidden;
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
.simple-icon-button:focus-visible,
.simple-port:focus-visible,
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

@media (max-width: 900px) {
  .simple-node-editor__meta { flex-wrap: wrap; }
  .simple-node-editor__switch { margin-left: 0; }
  .simple-node-editor__topbar { align-items: flex-start; flex-direction: column; }
  .simple-node-editor__title { width: 100%; grid-template-columns: auto minmax(0, 1fr); }
  .simple-node-editor__footer { align-items: flex-start; flex-direction: column; }
  .simple-node-editor__footer > span { width: 100%; }
  .simple-node-editor__footer > div { width: 100%; flex-wrap: wrap; }
}
</style>
