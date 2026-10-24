<script setup lang="ts">
import { createLogger } from '@/utils/logger'

/**
 * Litegraph-based node editor for comfy-mss workflows.
 *
 * Replaces the hand-written WorkflowNodeEditor.vue canvas. The graph is edited
 * directly as native comfy-mss JSON; serialize() output is handed to the parent
 * via v-model:definition, and pymss.graph.load_comfy_file consumes it verbatim.
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { AddOutline, ArrowRedoOutline, ArrowUndoOutline, ExpandOutline, SearchOutline } from '@vicons/ionicons5'
import { invoke } from '@tauri-apps/api/core'
import { LGraph, LGraphCanvas, LiteGraph, createBounds, type LGraphNode } from '@comfyorg/litegraph'
import '@comfyorg/litegraph/style.css'
import {
  registerPymssNodes,
  setSeparateStems,
  applyModelOptions,
  refreshNodeModelOptions,
  localizePymssNode,
  setPymssNodeTranslator,
  applyPymssPortColors,
  NODE_SPECS,
  BUILTIN_SPECS,
} from '@/litegraph/registerNodes'
import { litegraphToComfy, comfyToLitegraph } from '@/litegraph/graphAdapter'
import { installCanvasDpi } from '@/litegraph/canvasDpi'
import {
  createWorkflowHistory,
  recordWorkflowSnapshot,
  redoWorkflowHistory,
  replaceWorkflowHistoryCurrent,
  resolveWorkflowHistoryShortcut,
  undoWorkflowHistory,
} from '@/litegraph/history'
import { parseModelStems } from '@/utils/workflowSimple'
import type { ModelEntry } from '@/stores/model'
import type { NodeSpec } from '@/litegraph/nodeSpecs'
import { translateNodeCategory, translateNodeTitle, type NodeTranslator } from '@/litegraph/nodeLocalization'
import { isGraphWorkflowDefinition } from '@/workflows/formats'
import { applyGraphDefaultWidgets, type GraphDefaults } from '@/workflows/graphDefaults'
import { registerPluginNodeSpecs } from '@/litegraph/registerNodes'

const studioLog = createLogger('ui.components.workflow.WorkflowNodeEditorLite')

// All node specs the palette offers (pymss + ComfyUI builtin + plugin-contributed).
const ALL_SPECS: Record<string, NodeSpec> = { ...NODE_SPECS, ...BUILTIN_SPECS }

type PluginNodeDecl = {
  type?: string
  title?: string
  category?: string
  description?: string
  registered?: boolean
  pluginId?: string
  inputs?: { name?: string; type?: string; widget?: string }[]
  outputs?: { name?: string; type?: string }[]
  widgets?: { name?: string; type?: string; default?: unknown; min?: number; max?: number; step?: number; options?: string[] }[]
}

/** Plugin node types the worker reports as not registered (plugin broken or
 *  its runtime missing). Nodes of these types on canvas are flagged. */
const unregisteredPluginNodes = ref<Map<string, string>>(new Map())

/** Fetch plugin node declarations and merge them into the spec pool. */
async function loadPluginNodeSpecs() {
  if (typeof window === 'undefined' || !('__TAURI_INTERNALS__' in window)) return
  try {
    const response = await invoke<{ nodes?: PluginNodeDecl[] }>('list_nodes')
    const declarations = Array.isArray(response?.nodes) ? response.nodes : []
    const PORT_VALUES = ['AUDIO', 'STRING', 'COMBO', 'BOOLEAN', 'PYMSS_MSS_PARAMS', 'PYMSS_VR_PARAMS'] as const
    type PortName = typeof PORT_VALUES[number]
    const toPort = (value: unknown): PortName => ((PORT_VALUES as readonly string[]).includes(String(value)) ? String(value) : 'AUDIO') as PortName
    const specs: NodeSpec[] = declarations.map(decl => ({
      type: String(decl.type || ''),
      title: String(decl.title || decl.type || ''),
      category: String(decl.category || 'pymss/plugins'),
      inputs: (decl.inputs || []).map(port => ({
        name: String(port.name || ''),
        type: toPort(port.type),
        widget: port.widget ? { name: String(port.widget) } : undefined,
      })),
      outputs: (decl.outputs || []).map(port => ({
        name: String(port.name || ''),
        type: toPort(port.type),
      })),
      widgets: (decl.widgets || []).map(widget => ({
        name: String(widget.name || ''),
        type: (['toggle', 'combo', 'number'].includes(String(widget.type)) ? String(widget.type) : 'text') as 'text',
        default: widget.default as string | number | boolean,
        options: widget.options,
      })),
    })).filter(spec => spec.type)
    registerPluginNodeSpecs(specs)
    for (const spec of specs) ALL_SPECS[spec.type] = spec
    const broken = new Map<string, string>()
    for (const decl of declarations) {
      if (decl.type && decl.registered === false) broken.set(decl.type, decl.pluginId || decl.type)
    }
    unregisteredPluginNodes.value = broken
    flagUnregisteredNodes()
  } catch (error) {
    console.warn('[workflow] plugin nodes unavailable', error)
  }
}

/** Mark on-canvas nodes whose plugin executor is missing: red-tinted so the
 *  user sees WHY the workflow would fail before running it. */
function flagUnregisteredNodes() {
  const graph = graphRef.value
  if (!graph) return
  for (const node of graph.nodes || []) {
    const missing = unregisteredPluginNodes.value.get(String(node.type))
    if (missing) {
      node.color = '#5a2d2d'
      node.bgcolor = '#3b1f1f'
      ;(node as any).pymssMissingPlugin = missing
    }
  }
}

const props = withDefaults(defineProps<{
  readonly?: boolean
  modelOptions: { label: string; value: string }[]
  models: ModelEntry[]
  defaultDevice?: string
  defaultFormat?: string
  formError?: string
  /** Non-blocking warning shown next to the toolbar (e.g. missing models) */
  advisory?: string
  canSave?: boolean
  saving?: boolean
  createStarter?: boolean
  runningNodeIds?: readonly string[]
}>(), {
  defaultDevice: 'auto',
  defaultFormat: 'wav',
})
const definition = defineModel<Record<string, unknown>>('definition', { required: true })
const emit = defineEmits<{
  run: []
  save: [definition: Record<string, unknown>]
  close: []
  initialized: []
  'defaults-restored': [defaults: GraphDefaults]
  'load-error': [message: string]
}>()
defineExpose({ snapshotDefinition })

const { t, locale } = useI18n()
const message = useMessage()
const nodeTranslator = t as unknown as NodeTranslator
const canvasEl = ref<HTMLCanvasElement | null>(null)
const graphRef = shallowRef<LGraph | null>(null)
const canvasRef = shallowRef<LGraphCanvas | null>(null)
const showPalette = ref(false)
const paletteQuery = ref('')
const paletteInput = ref<HTMLInputElement | null>(null)
const paletteList = ref<HTMLElement | null>(null)
const paletteIndex = ref(0)
const loadError = ref('')
const ready = ref(false)
function drawRunningNodes(ctx: CanvasRenderingContext2D) {
  const canvas = canvasRef.value
  const ids = props.runningNodeIds
  if (!canvas || !ids?.length || loadError.value) return
  const running = new Set(ids)
  const scale = canvas.ds.scale
  ctx.save()
  ctx.strokeStyle = '#67d98b'
  ctx.lineWidth = 2.5 / scale
  const padding = 4 / scale
  for (const node of canvas.visible_nodes) {
    if (!running.has(String(node.id))) continue
    const [x, y, width, height] = node.boundingRect
    ctx.beginPath()
    ctx.roundRect(x - padding, y - padding, width + padding * 2, height + padding * 2, 8)
    ctx.stroke()
  }
  ctx.restore()
}
watch(() => props.runningNodeIds, () => canvasRef.value?.setDirty(true, true), { deep: true })
let configuringGraph = false
let graphInitialized = false
let awaitingInitialDefinition = false

// --- undo/redo (serialize snapshots) --------------------------------------
const history = shallowRef(createWorkflowHistory())

function serializeGraph() {
  const graph = graphRef.value
  return graph ? JSON.stringify(graph.serialize()) : ''
}

function resetHistory() {
  history.value = createWorkflowHistory(serializeGraph())
}

function readGraphDefaults(): GraphDefaults {
  const extra = ((graphRef.value as any)?.extra || {}) as Record<string, unknown>
  const defaults = (extra.appDefaults || {}) as Record<string, unknown>
  return {
    device: String(defaults.device || props.defaultDevice || 'auto'),
    outputFormat: String(defaults.output_format || props.defaultFormat || 'wav'),
  }
}

function writeGraphDefaults(defaults: GraphDefaults) {
  const graph = graphRef.value as any
  if (!graph) return
  const extra = (graph.extra || {}) as Record<string, unknown>
  const current = (extra.appDefaults || {}) as Record<string, unknown>
  graph.extra = {
    ...extra,
    appDefaults: {
      ...current,
      device: defaults.device || 'auto',
      output_format: defaults.outputFormat || 'wav',
    },
  }
}

function snapshotForHistory() {
  history.value = recordWorkflowSnapshot(history.value, serializeGraph())
}
function restoreSnapshot(snap: string) {
  const graph = graphRef.value
  if (!graph) return
  const data = JSON.parse(snap)
  configuringGraph = true
  try {
    graph.configure(comfyToLitegraph(data))
  } finally {
    configuringGraph = false
  }
  for (const node of (graph.nodes as any[])) configureGraphNode(node)
  restoreViewport()
  // configure() invokes graph change callbacks; restoring history must not
  // create another undo entry from those callbacks.
  if (pendingSnap) cancelAnimationFrame(pendingSnap)
  pendingSnap = 0
  const restored = serializeGraph()
  history.value = replaceWorkflowHistoryCurrent(history.value, restored)
  definition.value = snapshotDefinition()
  emit('defaults-restored', readGraphDefaults())
  ;(canvasRef.value as any)?.setDirty(true, true)
}
function undo() {
  const step = undoWorkflowHistory(history.value)
  if (!step.snapshot) return
  history.value = step.history
  restoreSnapshot(step.snapshot)
}
function redo() {
  const step = redoWorkflowHistory(history.value)
  if (!step.snapshot) return
  history.value = step.history
  restoreSnapshot(step.snapshot)
}
function guardReadonlyKeyboard(event: KeyboardEvent) {
  if (!props.readonly) return
  if (canvasRef.value) canvasRef.value.read_only = true
  if (event.key === 'Escape' || event.key === 'Tab') return
  event.preventDefault()
  event.stopImmediatePropagation()
}
function restoreReadonlyAfterKeyup() {
  if (props.readonly) queueMicrotask(() => { if (canvasRef.value) canvasRef.value.read_only = true })
}
function onCanvasKey(e: KeyboardEvent) {
  if (props.readonly) return
  const action = resolveWorkflowHistoryShortcut(e)
  if (!action) return
  e.preventDefault()
  if (action === 'undo') undo()
  else redo()
}
function onEditorKey(event: KeyboardEvent) {
  if (props.readonly || event.isComposing || !(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 's') return
  event.preventDefault()
  if (ready.value && props.canSave && !props.saving) onSave()
}
const canUndo = computed(() => history.value.past.length > 0)
const canRedo = computed(() => history.value.future.length > 0)

// --- theme: dark to match pymss-studio -------------------------------------
function applyTheme() {
  LiteGraph.NODE_DEFAULT_BGCOLOR = '#262b33'
  LiteGraph.NODE_DEFAULT_COLOR = '#303947'
  LiteGraph.NODE_TITLE_COLOR = '#e6e8eb'
  LiteGraph.LINK_COLOR = '#5b8def'
  LiteGraph.ROUND_RADIUS = 6
  ;(LiteGraph as any).NODE_DEFAULT_TITLECOLOR = '#e6e8eb'
  ;(LiteGraph as any).NODE_TITLE_BOXCOLOR = '#5b8def'
  ;(LiteGraph as any).NODE_TEXT_COLOR = '#e6e8eb'
  ;(LiteGraph as any).EVENT_LINK_COLOR = '#9b6dff'
}

// --- model -> stems lookup for separate nodes ------------------------------
function stemsForModel(modelNameRaw: string): string[] {
  // comfy-mss serializes model widgets as "[category] filename" — strip the
  // annotation prefix before matching against the catalog.
  const modelName = modelNameRaw.replace(/^\[[^\]]*\]\s*/, '').trim()
  const m = props.models.find((x) => x.name === modelName || x.aliases?.includes(modelName))
  if (!m) return []
  const parsed = parseModelStems(m.configInstruments || m.configTargetInstrument || m.targetStem)
  if (parsed.length >= 2) return parsed
  const target = (m.targetStem || '').trim()
  if (target) {
    return [target, target.toLowerCase() === 'vocals' ? 'instrumental' : 'other']
  }
  return ['stem_1', 'stem_2']
}

function syncSeparateNodeStems(node: LGraphNode) {
  const spec = ALL_SPECS[String(node.type)] || ALL_SPECS[String(node.type).replace(/^pymss_/, '')]
  if (!spec?.dynamicStems) return
  const modelName = String((node as any).properties?.model_name
    || (node as any).widgets?.find((w: any) => w.name === 'model_name')?.value
    || '')
  const stems = stemsForModel(modelName)
  if (!stems.length) return

  const desiredOutputNames = stems.flatMap(stem => [`${stem} (Audio)`, `${stem} (String)`])
  const currentOutputNames = (node.outputs || []).map(output => String(output.name || ''))
  if (
    currentOutputNames.length !== desiredOutputNames.length
    || currentOutputNames.some((name, index) => name !== desiredOutputNames[index])
  ) {
    setSeparateStems(node, stems)
  }
}

// --- node creation ---------------------------------------------------------
function addNode(type: string, x?: number, y?: number) {
  const graph = graphRef.value
  if (!graph) return
  const node = LiteGraph.createNode(type)
  if (!node) return
  const canvas = canvasRef.value
  graph.add(node)
  syncSeparateNodeStems(node)
  const scale = canvas?.ds.scale || 1
  const offset = canvas?.ds.offset || [0, 0]
  node.pos = [
    x ?? (canvasEl.value?.clientWidth || 0) / (2 * scale) - offset[0] - node.size[0] / 2,
    y ?? (canvasEl.value?.clientHeight || 0) / (2 * scale) - offset[1] - node.size[1] / 2,
  ]
  canvas?.selectNode(node)
  canvas?.setDirty(true, true)
}

// --- search palette --------------------------------------------------------
const paletteCategories = computed(() => {
  locale.value
  const groups: Record<string, { type: string; title: string }[]> = {}
  for (const spec of Object.values(ALL_SPECS)) {
    if (spec.hidden) continue
    const cat = translateNodeCategory(spec.category || 'pymss', nodeTranslator)
    ;(groups[cat] ||= []).push({
      type: spec.type,
      title: translateNodeTitle(spec, nodeTranslator),
    })
  }
  return Object.entries(groups).map(([category, items]) => ({ category, items }))
})
const paletteFiltered = computed(() => {
  const q = paletteQuery.value.trim().toLowerCase()
  if (!q) return paletteCategories.value
  return paletteCategories.value
    .map((g) => ({ ...g, items: g.items.filter((i) => i.title.toLowerCase().includes(q) || i.type.toLowerCase().includes(q)) }))
    .filter((g) => g.items.length)
})
const paletteItems = computed(() => paletteFiltered.value.flatMap(group => group.items))
watch(paletteQuery, () => { paletteIndex.value = 0 })
watch(showPalette, async (show) => {
  if (!show) return
  paletteQuery.value = ''
  paletteIndex.value = 0
  await nextTick()
  paletteInput.value?.focus()
})
function onPaletteKey(event: KeyboardEvent) {
  if (event.isComposing) return
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    showPalette.value = false
    canvasEl.value?.focus()
  } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    paletteIndex.value = Math.max(0, Math.min(paletteItems.value.length - 1,
      paletteIndex.value + (event.key === 'ArrowDown' ? 1 : -1)))
    void nextTick(() => paletteList.value?.querySelector('.palette-item--active')?.scrollIntoView({ block: 'nearest' }))
  } else if (event.key === 'Enter' && event.target === paletteInput.value && !event.isComposing) {
    event.preventDefault()
    const item = paletteItems.value[paletteIndex.value]
    if (item) onAddNodeClick(item.type)
  }
}

function fitView() {
  const canvas = canvasRef.value
  const graph = graphRef.value
  if (!canvas || !graph) return
  for (const node of graph.nodes) node.updateArea(canvas.ctx)
  const bounds = createBounds(canvas.positionableItems)
  if (bounds) canvas.ds.fitToBounds(bounds, { zoom: 0.85 })
  canvas.setDirty(true, true)
}

// --- (de)serialize ---------------------------------------------------------
function restoreViewport() {
  const canvas = canvasRef.value
  const graph = graphRef.value
  if (!canvas || !graph) return
  const viewport = graph.extra?.ds
  if (viewport && Number.isFinite(viewport.scale) && viewport.scale > 0
    && Array.isArray(viewport.offset) && viewport.offset.length === 2
    && viewport.offset.every(Number.isFinite)) {
    canvas.ds.scale = viewport.scale
    canvas.ds.offset = [viewport.offset[0], viewport.offset[1]]
  } else if (graph.nodes.length) {
    // Bounds are otherwise populated by the first render, after this initial load.
    for (const node of graph.nodes) node.updateArea(canvas.ctx)
    const bounds = createBounds(canvas.positionableItems)
    if (bounds) canvas.ds.fitToBounds(bounds, { zoom: 0.9 })
  }
  canvas.setDirty(true, true)
}

function snapshotDefinition(): Record<string, unknown> {
  if (loadError.value) return definition.value
  const graph = graphRef.value
  if (!graph) return {}
  const canvas = canvasRef.value
  if (canvas) {
    graph.extra = {
      ...graph.extra,
      ds: { scale: canvas.ds.scale, offset: [...canvas.ds.offset] },
    }
  }
  return litegraphToComfy(graph.serialize()) as unknown as Record<string, unknown>
}

function loadDefinition(def: Record<string, unknown>) {
  const graph = graphRef.value
  if (!graph) return
  graph.clear()
  const data = comfyToLitegraph(def)
  configuringGraph = true
  try {
    graph.configure(data)
  } finally {
    configuringGraph = false
  }
  if (graph.nodes.length !== data.nodes.length) {
    throw new Error(t('workflows.advancedEditor.incompleteGraph'))
  }
  // After configure, rebuild dynamic stem outputs for separate nodes and
  // populate model_name combos with the current downloaded list.
  for (const node of graph.nodes as any[]) configureGraphNode(node)
  restoreViewport()
}

function reportLoadError(error: unknown) {
  loadError.value = error instanceof Error ? error.message : String(error)
  ready.value = false
  graphRef.value?.stop()
  if (canvasRef.value) {
    canvasRef.value.read_only = true
    canvasRef.value.stopRendering()
  }
  emit('load-error', loadError.value)
}

/** Failed imports retain their original definition; only new graphs receive a starter. */
function initializeGraph() {
  const graph = graphRef.value
  if (!graph || graphInitialized) return

  try {
    if (isGraphWorkflowDefinition(definition.value) && Array.isArray(definition.value.nodes)
      && (definition.value.nodes.length || !props.createStarter)) {
      loadDefinition(definition.value)
      awaitingInitialDefinition = false
    } else if (props.createStarter) {
      // The parent rejects non-graph formats before mounting this component.
      seedStarterGraph()
      awaitingInitialDefinition = true
    } else {
      throw new Error(t('workflows.graphEditorFormatRequired'))
    }
  } catch (error) {
    studioLog.error('[workflow-node-editor] failed to restore graph', error)
    reportLoadError(error)
    return
  }

  graph.start()
  ready.value = true
  graphInitialized = true
  writeGraphDefaults({ device: props.defaultDevice, outputFormat: props.defaultFormat })
  // Push the first snapshot up so the parent has a clean comfy dict immediately.
  definition.value = snapshotDefinition()
  // The loaded/seeded graph is the baseline for the first undoable edit.
  resetHistory()
  emit('initialized')
}

// A standalone window can receive its persisted definition one tick after the
// canvas mounts. If that happens, hydrate the live graph as soon as the
// definition arrives instead of leaving the canvas at N: 0.
watch(definition, () => {
  const graph = graphRef.value
  if (!ready.value || !graph || !awaitingInitialDefinition) return
  if (!isGraphWorkflowDefinition(definition.value) || !Array.isArray(definition.value.nodes)
    || !definition.value.nodes.length) return
  // Ignore the component's own initial v-model snapshot. A standalone parent
  // may still replace it with the persisted graph on a later tick; only that
  // different definition should replace the starter graph.
  if (JSON.stringify(definition.value) === JSON.stringify(snapshotDefinition())) return
  try {
    loadDefinition(definition.value)
    awaitingInitialDefinition = false
    resetHistory()
    emit('initialized')
    ;(canvasRef.value as any)?.setDirty(true, true)
  } catch (error) {
    studioLog.error('[workflow-node-editor] failed to hydrate late graph definition', error)
    reportLoadError(error)
  }
})

// --- lifecycle -------------------------------------------------------------
let disposeCanvasDpi: (() => void) | null = null
let executionResizeObserver: ResizeObserver | null = null
function disableEmptyCanvasDoubleClick(event: PointerEvent) {
  const canvas = canvasRef.value
  const graph = graphRef.value
  if (!canvas || !graph || event.button !== 0) return
  const [x, y] = canvas.graph_mouse
  if (!graph.getNodeOnPos(x, y, canvas.visible_nodes) && !graph.getGroupOnPos(x, y)) {
    canvas.pointer.onDoubleClick = undefined
  }
}
function fitExecutionCanvas() {
  const canvas = canvasRef.value
  const element = canvasEl.value
  const nodes = graphRef.value?.nodes || []
  if (!canvas || !element || !nodes.length) return
  const minX = Math.min(...nodes.map(node => node.pos[0]))
  const minY = Math.min(...nodes.map(node => node.pos[1] - 30))
  const width = Math.max(...nodes.map(node => node.pos[0] + node.size[0])) - minX
  const height = Math.max(...nodes.map(node => node.pos[1] + node.size[1])) - minY
  const scale = Math.max(.1, Math.min(1, (element.clientWidth - 64) / Math.max(1, width), (element.clientHeight - 64) / Math.max(1, height)))
  canvas.ds.scale = scale
  canvas.ds.offset[0] = (element.clientWidth / scale - width) / 2 - minX
  canvas.ds.offset[1] = (element.clientHeight / scale - height) / 2 - minY
  canvas.setDirty(true, true)
}

onMounted(() => {
  registerPymssNodes(nodeTranslator)
  void loadPluginNodeSpecs()
  applyTheme()
  // Re-flag once the graph is configured (plugin specs may arrive after).
  if (!canvasEl.value) return
  const graph = new LGraph()
  const canvas = new LGraphCanvas(canvasEl.value, graph)
  canvas.show_info = false
  disposeCanvasDpi = installCanvasDpi(canvas)
  applyPymssPortColors(canvas)
  graphRef.value = graph
  canvasRef.value = canvas
  // Transient host display only; node styles, selection and serialized JSON stay native.
  canvas.onDrawForeground = drawRunningNodes
  // Track link/property edits so separate node stems follow the chosen model.
  graph.onNodeAdded = (node: any) => {
    const missing = unregisteredPluginNodes.value.get(String(node.type))
    if (missing) {
      node.color = '#5a2d2d'
      node.bgcolor = '#3b1f1f'
      ;(node as any).pymssMissingPlugin = missing
    }
    if (ready.value && !configuringGraph) {
      applyGraphDefaultWidgets([node], {
        device: props.defaultDevice,
        outputFormat: props.defaultFormat,
      }, { device: true, outputFormat: true })
    }
    configureGraphNode(node)
    if (ready.value) scheduleSnap()
  }
  if (canvasRef.value) canvasRef.value.read_only = Boolean(props.readonly)
  // Run after LiteGraph's capture listener has selected this gesture's actions.
  canvasEl.value.addEventListener('pointerdown', disableEmptyCanvasDoubleClick, true)
  document.addEventListener('keyup', restoreReadonlyAfterKeyup, true)
  canvasEl.value.addEventListener('keydown', onCanvasKey)
  window.addEventListener('keydown', onEditorKey)
  // Load existing definition (e.g. reopening an editor) or seed a starter graph.
  initializeGraph()
  if (props.readonly && typeof ResizeObserver !== 'undefined') {
    executionResizeObserver = new ResizeObserver(fitExecutionCanvas)
    executionResizeObserver.observe(canvasEl.value)
  }
})

onBeforeUnmount(() => {
  if (pendingSnap) cancelAnimationFrame(pendingSnap)
  pendingSnap = 0
  disposeCanvasDpi?.()
  disposeCanvasDpi = null
  executionResizeObserver?.disconnect()
  canvasEl.value?.removeEventListener('pointerdown', disableEmptyCanvasDoubleClick, true)
  canvasEl.value?.removeEventListener('keydown', onCanvasKey)
  window.removeEventListener('keydown', onEditorKey)
  document.removeEventListener('keyup', restoreReadonlyAfterKeyup, true)
  try { graphRef.value?.stop?.() } catch { /* ignore */ }
  canvasRef.value?.stopRendering?.()
  canvasRef.value?.unbindEvents()
  graphRef.value = null
  canvasRef.value = null
})

function seedStarterGraph() {
  const graph = graphRef.value
  if (!graph) return
  const load = LiteGraph.createNode('pymss_load_audio'); if (!load) return
  load.pos = [80, 200]; graph.add(load)
  const params = LiteGraph.createNode('pymss_mss_params'); if (!params) return
  params.pos = [80, 400]; graph.add(params)
  const sep = LiteGraph.createNode('mss_separate'); if (!sep) return
  sep.pos = [460, 280]; graph.add(sep)
  const save = LiteGraph.createNode('pymss_save_audio'); if (!save) return
  save.pos = [840, 280]; graph.add(save)
  applyGraphDefaultWidgets([sep, save], {
    device: props.defaultDevice,
    outputFormat: props.defaultFormat,
  }, { device: true, outputFormat: true })
  load.connect(0, sep, 0)
  params.connect(0, sep, 1)
  sep.connect(0, save, 0)
}

// Keep separate-node stem outputs in sync when the model_name widget changes,
// and keep the model_name combo populated with the downloaded models.
const modelValues = computed(() => props.modelOptions.map((o) => o.value))
watch(modelValues, (values) => { applyModelOptions(values) }, { immediate: true })
function refreshAllModelOptions() {
  const values = modelValues.value
  applyModelOptions(values)
  for (const n of (graphRef.value?.nodes || []) as any[]) refreshNodeModelOptions(n, values)
}
watch(() => props.models, () => {
  refreshAllModelOptions()
  for (const n of (graphRef.value?.nodes || []) as any[]) syncSeparateNodeStems(n)
}, { deep: false })

watch(locale, () => {
  setPymssNodeTranslator(nodeTranslator)
  for (const node of graphRef.value?.nodes || []) {
    localizePymssNode(node, nodeTranslator)
    updatePathPickerLabel(node)
  }
  ;(canvasRef.value as any)?.setDirty(true, true)
})

// Propagate canvas edits up to the parent v-model (debounced via rAF).
let pendingSnap = 0
function scheduleSnap() {
  if (props.readonly || loadError.value) return
  if (pendingSnap) return
  pendingSnap = requestAnimationFrame(() => {
    pendingSnap = 0
    if (loadError.value) return
    definition.value = snapshotDefinition()
    snapshotForHistory()
  })
}
watch(ready, (v) => {
  if (!v || !graphRef.value) return
  ;(graphRef.value as any).onAfterChange = scheduleSnap
})

function onSave() {
  if (!ready.value || loadError.value) return
  definition.value = snapshotDefinition()
  emit('save', definition.value)
}
function onClose() {
  emit('close')
}
function onAddNodeClick(type: string) {
  addNode(type)
  showPalette.value = false
  paletteQuery.value = ''
  canvasEl.value?.focus()
}

function updatePathPickerLabel(node: LGraphNode) {
  const picker = node.widgets?.find(widget => (widget as any).__nativePathPicker)
  if (picker) picker.label = t(node.type === 'pymss_load_audio_batch' ? 'workbench.chooseFolder' : 'workflows.advancedEditor.chooseAudioFile')
}

function configureGraphNode(node: LGraphNode) {
  ;(node as any).onModelNameChanged = () => {
    syncSeparateNodeStems(node)
    ;(canvasRef.value as any)?.setDirty(true, true)
  }
  syncSeparateNodeStems(node)
  refreshNodeModelOptions(node, modelValues.value)
  localizePymssNode(node, nodeTranslator)
  if (!props.readonly && !node.widgets?.some(widget => (widget as any).__nativePathPicker)
    && ['pymss_load_audio', 'pymss_load_audio_batch', 'LoadAudio'].includes(String(node.type))) {
    const batch = node.type === 'pymss_load_audio_batch'
    const pathWidget = node.widgets?.find(widget => ['audio', 'audio_path', 'filename', 'folder'].includes(widget.name))
    if (!pathWidget) return
    let picking = false
    const picker = node.addCustomWidget({
      type: 'button', name: 'studio_choose_path', value: '', options: {}, y: 0, serialize: false,
      callback: async () => {
        if (picking) return
        picking = true
        try {
          const path = await invoke<string | null>(batch ? 'pick_input_folder' : 'pick_single_audio_file')
          const graph = graphRef.value
          if (!path || !graph || graph.getNodeById(node.id) !== node) return
          graph.beforeChange()
          pathWidget.value = path
          const inputName = node.widgets?.find(widget => widget.name === 'input_name')
          if (inputName) inputName.value = ''
          graph.afterChange()
          scheduleSnap()
          canvasRef.value?.setDirty(true, true)
        } catch (error) { message.error(String(error)) }
        finally { picking = false }
      },
    })
    ;(picker as any).__nativePathPicker = true
    updatePathPickerLabel(node)
    node.setSize([node.size[0], Math.max(node.size[1], node.computeSize()[1])])
  }
}
</script>

<template>
  <div class="litegraph-editor" @keydown.capture="guardReadonlyKeyboard" @keyup.capture="guardReadonlyKeyboard">
    <header v-if="!readonly" class="toolbar">
      <div class="toolbar__title"><slot name="title" /></div>
      <div class="toolbar__actions">
        <div class="toolbar__group">
          <n-button size="small" quaternary circle :title="t('common.undo')" :aria-label="t('common.undo')" :disabled="!ready || !canUndo" @click="undo"><template #icon><n-icon :component="ArrowUndoOutline" /></template></n-button>
          <n-button size="small" quaternary circle :title="t('common.redo')" :aria-label="t('common.redo')" :disabled="!ready || !canRedo" @click="redo"><template #icon><n-icon :component="ArrowRedoOutline" /></template></n-button>
          <n-popover v-model:show="showPalette" trigger="click" placement="bottom-start" :show-arrow="false" :raw="true">
            <template #trigger><n-button size="small" secondary :disabled="!ready" aria-haspopup="dialog" :aria-expanded="showPalette"><template #icon><n-icon :component="AddOutline" /></template>{{ t('workflows.addNode') }}</n-button></template>
            <section class="palette" role="dialog" :aria-label="t('workflows.addNode')" @keydown="onPaletteKey">
              <div class="palette__search"><n-icon :component="SearchOutline" aria-hidden="true" /><input ref="paletteInput" v-model="paletteQuery" class="palette-input" :aria-label="t('workflows.advancedEditor.searchNodes')" :placeholder="t('workflows.advancedEditor.searchNodes')" /></div>
              <div ref="paletteList" class="palette-list">
                <div v-for="group in paletteFiltered" :key="group.category" class="palette-group">
                  <div class="palette-cat">{{ group.category }}</div>
                  <button v-for="item in group.items" :key="item.type" class="palette-item" :class="{ 'palette-item--active': paletteItems[paletteIndex]?.type === item.type }" type="button" @click="onAddNodeClick(item.type)">{{ item.title }}<small>{{ item.type }}</small></button>
                </div>
                <n-empty v-if="!paletteItems.length" size="small" :description="t('workflows.advancedEditor.noMatchingNodes')" />
              </div>
              <div class="palette__help">{{ t('workflows.advancedEditor.paletteKeyboardHint') }}</div>
            </section>
          </n-popover>
        </div>
        <div class="toolbar__group">
          <n-button size="small" quaternary :disabled="!ready" @click="fitView"><template #icon><n-icon :component="ExpandOutline" /></template>{{ t('workflows.fitView') }}</n-button>
          <slot name="settings-controls" />
        </div>
        <div class="toolbar__group toolbar__group--run"><slot name="run-controls" /></div>
      </div>
      <span v-if="formError && !loadError" class="toolbar__notice err" role="alert">{{ formError }}</span>
      <span v-else-if="advisory && !loadError" class="toolbar__notice warn">{{ advisory }}</span>
      <slot name="execution-progress" />
    </header>
    <slot v-else name="execution-progress" />
    <div class="canvas-wrap">
      <canvas ref="canvasEl" class="lg-canvas" tabindex="0" :aria-label="t('workflows.nodeEditor')" />
      <div v-if="loadError" class="canvas-error" role="alert">
        <n-result status="error" :title="t('workflows.advancedEditor.loadFailed')" :description="loadError">
          <template #footer><n-button secondary @click="onClose">{{ t('workflows.returnToWorkflows') }}</n-button></template>
        </n-result>
      </div>
    </div>
  </div>
</template>

<style scoped>
.litegraph-editor { display: flex; flex-direction: column; height: 100%; min-height: 0; gap: 10px; color: var(--on-surface); }
.toolbar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px 16px; padding: 10px 16px; border: 1px solid var(--outline); border-radius: 16px; background: var(--surface-1); }
.toolbar__title { display: flex; align-items: center; flex: 1 1 200px; min-width: 0; }
.toolbar__actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.toolbar__group { display: inline-flex; align-items: center; gap: 6px; }
.toolbar__group + .toolbar__group { padding-left: 10px; border-left: 1px solid var(--outline); }
.toolbar__notice { flex-basis: 100%; font-size: 11px; }
.err { color: var(--danger); }
.warn { color: var(--warning); }
.palette { width: min(320px, calc(100vw - 32px)); max-height: min(580px, calc(100vh - 120px)); display: flex; flex-direction: column; border: 1px solid var(--outline-strong); border-radius: 12px; background: var(--surface-1); color: var(--on-surface); box-shadow: var(--shadow-panel); overflow: hidden; }
.palette__search { display: flex; align-items: center; gap: 8px; padding: 12px; border-bottom: 1px solid var(--outline); color: var(--on-surface-muted); }
.palette-input { min-width: 0; width: 100%; padding: 5px 0; border: 0; outline: none; background: transparent; color: var(--on-surface); font: inherit; font-size: 12px; }
.palette__search:focus-within { border-bottom-color: var(--primary); }
.palette-list { min-height: 0; overflow-y: auto; padding: 4px 8px 8px; }
.palette-list :deep(.n-empty) { padding: 20px 0; }
.palette-cat { color: var(--on-surface-muted); font-size: 11px; font-weight: 600; padding: 12px 8px 5px; }
.palette-item { display: grid; gap: 2px; width: 100%; text-align: left; background: transparent; border: none; color: var(--on-surface); padding: 7px 8px; border-radius: 6px; cursor: pointer; font: inherit; font-size: 12px; }
.palette-item small { color: var(--on-surface-muted); font-size: 10px; overflow-wrap: anywhere; }
.palette-item:hover, .palette-item--active { background: var(--primary-softer); }
.palette-item:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
.palette__help { padding: 9px 12px; border-top: 1px solid var(--outline); color: var(--on-surface-muted); font-size: 10px; }
.canvas-error { position: absolute; inset: 0; z-index: 2; display: grid; place-items: center; padding: 28px; background: var(--surface-1); overflow: auto; }
.canvas-error :deep(.n-result) { max-width: 640px; overflow-wrap: anywhere; }
.canvas-wrap { position: relative; flex: 1; min-height: 0; overflow: hidden; border: 1px solid var(--outline); border-radius: 14px; background: #16181d; }
.lg-canvas { width: 100%; height: 100%; display: block; }
.toolbar > :deep(.workflow-execution-progress) { flex-basis: 100%; }
</style>
