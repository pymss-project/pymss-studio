<script setup lang="ts">
/**
 * Litegraph-based node editor for comfy-mss workflows.
 *
 * Replaces the hand-written WorkflowNodeEditor.vue canvas. The graph is edited
 * directly as native comfy-mss JSON; serialize() output is handed to the parent
 * via v-model:definition, and pymss.graph.load_comfy_file consumes it verbatim.
 */
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
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

// All node specs the palette offers (pymss nodes + ComfyUI builtin audio/string nodes).
const ALL_SPECS: Record<string, NodeSpec> = { ...NODE_SPECS, ...BUILTIN_SPECS }

const props = withDefaults(defineProps<{
  modelOptions: { label: string; value: string }[]
  models: ModelEntry[]
  defaultDevice?: string
  defaultFormat?: string
  formError?: string
  /** Non-blocking warning shown next to the toolbar (e.g. missing models) */
  advisory?: string
  canSave?: boolean
}>(), {
  defaultDevice: 'auto',
  defaultFormat: 'wav',
})
const definition = defineModel<Record<string, unknown>>('definition', { required: true })
const emit = defineEmits<{
  save: [definition: Record<string, unknown>]
  close: []
  initialized: []
  'defaults-restored': [defaults: GraphDefaults]
}>()
defineExpose({ snapshotDefinition })

const { t, locale } = useI18n()
const nodeTranslator = t as unknown as NodeTranslator
const canvasEl = ref<HTMLCanvasElement | null>(null)
const graphRef = shallowRef<LGraph | null>(null)
const canvasRef = shallowRef<LGraphCanvas | null>(null)
const showPalette = ref(false)
const paletteQuery = ref('')
const ready = ref(false)
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
function onCanvasKey(e: KeyboardEvent) {
  const action = resolveWorkflowHistoryShortcut(e)
  if (!action) return
  e.preventDefault()
  if (action === 'undo') undo()
  else redo()
}
const canUndo = computed(() => history.value.past.length > 0)
const canRedo = computed(() => history.value.future.length > 0)

// --- theme: dark to match pymss-studio -------------------------------------
function applyTheme() {
  LiteGraph.NODE_DEFAULT_BGCOLOR = '#262b33'
  LiteGraph.NODE_DEFAULT_COLOR = '#cfd3da'
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
  const cx = canvasRef.value?.viewport || [0, 0]
  node.pos = [x ?? (cx[0] + 200), y ?? (cx[1] + 150)]
  graph.add(node)
  syncSeparateNodeStems(node)
  ;(canvasRef.value as any)?.setDirty(true, true)
}

// --- search palette --------------------------------------------------------
const paletteCategories = computed(() => {
  locale.value
  const groups: Record<string, { type: string; title: string }[]> = {}
  for (const spec of Object.values(ALL_SPECS)) {
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
  if (data.nodes.length && graph.nodes.length === 0) {
    throw new Error('LiteGraph did not restore any nodes from the workflow definition')
  }
  // After configure, rebuild dynamic stem outputs for separate nodes and
  // populate model_name combos with the current downloaded list.
  for (const node of graph.nodes as any[]) configureGraphNode(node)
  restoreViewport()
}

/**
 * Initialize the canvas exactly once. A malformed or partially migrated
 * persisted graph must not leave a running, empty canvas; seed an editable
 * starter graph as a recovery point and keep the original error in the log.
 */
function initializeGraph() {
  const graph = graphRef.value
  if (!graph || graphInitialized) return

  try {
    if (isGraphWorkflowDefinition(definition.value) && Array.isArray(definition.value.nodes)
      && definition.value.nodes.length) {
      loadDefinition(definition.value)
      awaitingInitialDefinition = false
    } else {
      // New workflows and malformed/legacy values both get a usable graph.
      // The parent view rejects legacy formats before this component is shown.
      seedStarterGraph()
      awaitingInitialDefinition = true
    }
  } catch (error) {
    console.error('[workflow-node-editor] failed to restore graph', error)
    graph.clear()
    seedStarterGraph()
    awaitingInitialDefinition = true
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
    console.error('[workflow-node-editor] failed to hydrate late graph definition', error)
    graph.clear()
    seedStarterGraph()
  }
})

// --- lifecycle -------------------------------------------------------------
let disposeCanvasDpi: (() => void) | null = null

onMounted(() => {
  registerPymssNodes(nodeTranslator)
  applyTheme()
  if (!canvasEl.value) return
  const graph = new LGraph()
  const canvas = new LGraphCanvas(canvasEl.value, graph)
  disposeCanvasDpi = installCanvasDpi(canvas)
  applyPymssPortColors(canvas)
  graphRef.value = graph
  canvasRef.value = canvas
  // Hide the default search-on-double-click; we use our own palette.
  // 保留 litegraph 默认交互: 双击空白/拖线释放弹节点搜索框,右键弹菜单。
  // 样式由 @comfyorg/litegraph/style.css 提供(在组件 <style> 外全局引入)。
  // Track link/property edits so separate node stems follow the chosen model.
  graph.onNodeAdded = (node: any) => {
    if (ready.value && !configuringGraph) {
      applyGraphDefaultWidgets([node], {
        device: props.defaultDevice,
        outputFormat: props.defaultFormat,
      }, { device: true, outputFormat: true })
    }
    configureGraphNode(node)
    if (ready.value) scheduleSnap()
  }
  canvasEl.value.addEventListener('keydown', onCanvasKey)
  // Load existing definition (e.g. reopening an editor) or seed a starter graph.
  initializeGraph()
})

onBeforeUnmount(() => {
  if (pendingSnap) cancelAnimationFrame(pendingSnap)
  pendingSnap = 0
  disposeCanvasDpi?.()
  disposeCanvasDpi = null
  canvasEl.value?.removeEventListener('keydown', onCanvasKey)
  try { graphRef.value?.stop?.() } catch { /* ignore */ }
  canvasRef.value?.stopRendering?.()
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
  for (const node of (graphRef.value?.nodes || []) as any[]) localizePymssNode(node, nodeTranslator)
  ;(canvasRef.value as any)?.setDirty(true, true)
})

watch(
  [() => props.defaultDevice, () => props.defaultFormat],
  ([device, outputFormat]) => {
    if (!ready.value || !graphRef.value) return
    const current = readGraphDefaults()
    const changed = {
      device: device !== current.device,
      outputFormat: outputFormat !== current.outputFormat,
    }
    if (!changed.device && !changed.outputFormat) return
    applyGraphDefaultWidgets(graphRef.value.nodes as any[], {
      device,
      outputFormat,
    }, changed)
    writeGraphDefaults({ device, outputFormat })
    ;(canvasRef.value as any)?.setDirty(true, true)
    scheduleSnap()
  },
)

// Propagate canvas edits up to the parent v-model (debounced via rAF).
let pendingSnap = 0
function scheduleSnap() {
  if (pendingSnap) return
  pendingSnap = requestAnimationFrame(() => {
    pendingSnap = 0
    definition.value = snapshotDefinition()
    snapshotForHistory()
  })
}
watch(ready, (v) => {
  if (!v || !graphRef.value) return
  ;(graphRef.value as any).onAfterChange = scheduleSnap
})

function onSave() {
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
}

function configureGraphNode(node: LGraphNode) {
  ;(node as any).onModelNameChanged = () => {
    syncSeparateNodeStems(node)
    ;(canvasRef.value as any)?.setDirty(true, true)
  }
  syncSeparateNodeStems(node)
  refreshNodeModelOptions(node, modelValues.value)
  localizePymssNode(node, nodeTranslator)
}
</script>

<template>
  <div class="litegraph-editor">
    <div class="toolbar">
      <n-button size="small" @click="showPalette = !showPalette">{{ t('workflows.addNode') || 'Add node' }}</n-button>
      <n-button size="small" :disabled="!canUndo" @click="undo">{{ t('common.undo') || 'Undo' }}</n-button>
      <n-button size="small" :disabled="!canRedo" @click="redo">{{ t('common.redo') || 'Redo' }}</n-button>
      <n-button size="small" @click="onSave" :disabled="!props.canSave" type="primary">{{ t('common.save') }}</n-button>
      <n-button size="small" @click="onClose">{{ t('common.close') }}</n-button>
      <span v-if="props.formError" class="err">{{ props.formError }}</span>
      <span v-if="props.advisory" class="warn">{{ props.advisory }}</span>
    </div>

    <div v-if="showPalette" class="palette">
      <input v-model="paletteQuery" class="palette-input" :placeholder="t('workflows.advancedEditor.searchNodes')" autofocus />
      <div class="palette-list">
        <div v-for="g in paletteFiltered" :key="g.category" class="palette-group">
          <div class="palette-cat">{{ g.category }}</div>
          <button v-for="item in g.items" :key="item.type" class="palette-item" @click="onAddNodeClick(item.type)">
            {{ item.title }}
          </button>
        </div>
      </div>
    </div>

    <div class="canvas-wrap">
      <canvas ref="canvasEl" class="lg-canvas" tabindex="0" :aria-label="t('workflows.nodeEditor')" />
    </div>
  </div>
</template>

<style scoped>
.litegraph-editor { display: flex; flex-direction: column; height: 100%; background: #16181d; color: #e6e8eb; }
.toolbar { display: flex; align-items: center; gap: 8px; padding: 8px; border-bottom: 1px solid #2a2e36; background: #1c1f26; }
.err { color: #ff6b6b; font-size: 12px; margin-left: auto; }
.warn { color: #e0b34a; font-size: 12px; margin-left: auto; }
.palette { position: absolute; top: 48px; left: 12px; width: 260px; max-height: 60%; background: #1c1f26; border: 1px solid #3a3f4a; border-radius: 6px; z-index: 10; display: flex; flex-direction: column; }
.palette-input { margin: 8px; padding: 6px 8px; background: #16181d; border: 1px solid #3a3f4a; color: #e6e8eb; border-radius: 4px; }
.palette-list { overflow-y: auto; padding: 0 8px 8px; }
.palette-cat { color: #8b92a0; font-size: 11px; text-transform: uppercase; padding: 8px 4px 4px; }
.palette-item { display: block; width: 100%; text-align: left; background: transparent; border: none; color: #cfd3da; padding: 6px 8px; border-radius: 4px; cursor: pointer; font-size: 13px; }
.palette-item:hover { background: #2a2e36; }
.canvas-wrap { position: relative; flex: 1; overflow: hidden; }
.lg-canvas { width: 100%; height: 100%; display: block; }
</style>
