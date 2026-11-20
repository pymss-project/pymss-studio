<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { AddOutline, RemoveOutline, ScanOutline } from '@vicons/ionicons5'
import { buildWorkflowPreview, type WorkflowPreviewNode } from '@/workflows/preview'

const props = defineProps<{ definition: Record<string, unknown> }>()
const { t } = useI18n()
const graph = computed(() => buildWorkflowPreview(props.definition, key => t(key)))
const canvas = ref<SVGSVGElement | null>(null)
const viewport = ref<HTMLElement | null>(null)
const size = ref({ width: 640, height: 360 })
const zoom = ref(1)
const pan = ref({ x: 0, y: 0 })
const dragging = ref(false)
let drag: { pointer: number; x: number; y: number; startX: number; startY: number } | null = null
let observer: ResizeObserver | null = null
const bounds = computed(() => {
  const nodes = graph.value.nodes
  if (!nodes.length) return { left: 0, top: 0, width: 640, height: 360 }
  const left = Math.min(...nodes.map(node => node.x))
  const top = Math.min(...nodes.map(node => node.y))
  return {
    left, top,
    width: Math.max(...nodes.map(node => node.x + node.width)) - left,
    height: Math.max(...nodes.map(node => node.y + node.height)) - top,
  }
})
const fitScale = computed(() => Math.max(0.001, Math.min(
  Math.max(1, size.value.width - 64) / bounds.value.width,
  Math.max(1, size.value.height - 72) / bounds.value.height,
  1.2,
)))
const maxZoom = computed(() => Math.max(4, 1.2 / fitScale.value))
const scale = computed(() => fitScale.value * zoom.value)
const viewBox = computed(() => {
  const width = size.value.width / scale.value
  const height = size.value.height / scale.value
  return `${bounds.value.left + bounds.value.width / 2 - width / 2 - pan.value.x} ${bounds.value.top + bounds.value.height / 2 - height / 2 - pan.value.y} ${width} ${height}`
})
const nodeMap = computed(() => new Map(graph.value.nodes.map(node => [node.id, node])))
const kindLabels = {
  input: 'workflows.previewKinds.input', separate: 'workflows.previewKinds.separate',
  process: 'workflows.previewKinds.process', output: 'workflows.previewKinds.output', other: 'workflows.previewKinds.other',
}
const ports = computed(() => {
  const result = new Map(graph.value.nodes.map(node => [node.id, { inputs: new Set<number>(), outputs: new Set<number>() }]))
  for (const link of graph.value.links) {
    result.get(link.source)!.outputs.add(link.sourceSlot)
    result.get(link.target)!.inputs.add(link.targetSlot)
  }
  return result
})
function portY(node: WorkflowPreviewNode, slot: number, count: number) {
  return node.y + (slot + 1) * node.height / (count + 1)
}
const links = computed(() => graph.value.links.map(link => {
  const source = nodeMap.value.get(link.source)!
  const target = nodeMap.value.get(link.target)!
  const x1 = source.x + source.width
  const y1 = portY(source, link.sourceSlot, source.outputs)
  const x2 = target.x
  const y2 = portY(target, link.targetSlot, target.inputs)
  const bend = Math.max(36, Math.abs(x2 - x1) * 0.45)
  return { ...link, path: `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}` }
}))
function fit() {
  zoom.value = 1
  pan.value = { x: 0, y: 0 }
}
function changeZoom(factor: number, anchor?: { x: number; y: number }) {
  const previousScale = scale.value
  zoom.value = Math.min(maxZoom.value, Math.max(0.5, zoom.value * factor))
  if (anchor) {
    const delta = 1 / scale.value - 1 / previousScale
    pan.value = { x: pan.value.x + anchor.x * delta, y: pan.value.y + anchor.y * delta }
  }
}
function startPan(event: PointerEvent) {
  if (event.button !== 0) return
  drag = { pointer: event.pointerId, x: event.clientX, y: event.clientY, startX: pan.value.x, startY: pan.value.y }
  canvas.value?.setPointerCapture(event.pointerId)
  dragging.value = true
}
function movePan(event: PointerEvent) {
  if (!drag || drag.pointer !== event.pointerId) return
  pan.value = { x: drag.startX + (event.clientX - drag.x) / scale.value, y: drag.startY + (event.clientY - drag.y) / scale.value }
}
function endPan() {
  drag = null
  dragging.value = false
}
function wheel(event: WheelEvent) {
  if (!graph.value.nodes.length || !canvas.value) return
  event.preventDefault()
  if (!event.deltaY) return
  const rect = canvas.value.getBoundingClientRect()
  const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? size.value.height : 1
  const delta = Math.min(500, Math.max(-500, event.deltaY * unit))
  changeZoom(Math.exp(-delta * 0.002), {
    x: ((event.clientX - rect.left) / rect.width - 0.5) * size.value.width,
    y: ((event.clientY - rect.top) / rect.height - 0.5) * size.value.height,
  })
  if (drag) {
    drag = { pointer: drag.pointer, x: event.clientX, y: event.clientY, startX: pan.value.x, startY: pan.value.y }
  }
}
watch(graph, fit)
onMounted(() => {
  observer = new ResizeObserver(([entry]) => {
    if (entry.contentRect.width > 0 && entry.contentRect.height > 0) {
      size.value = { width: entry.contentRect.width, height: entry.contentRect.height }
    }
  })
  if (viewport.value) observer.observe(viewport.value)
})
onBeforeUnmount(() => observer?.disconnect())
</script>

<template>
  <section class="workflow-preview" :aria-label="t('workflows.previewTitle')">
    <header class="workflow-preview__head">
      <div class="workflow-preview__heading">
        <h3>{{ t('workflows.previewTitle') }}</h3>
        <span v-if="graph.nodes.length">{{ t('workflows.previewSummary', { nodes: graph.nodes.length, links: graph.links.length }) }}</span>
      </div>
      <div v-if="graph.nodes.length" class="workflow-preview__controls">
        <n-button quaternary size="small" :disabled="zoom <= 0.5" :aria-label="t('workflows.previewZoomOut')" :title="t('workflows.previewZoomOut')" @click="changeZoom(1 / 1.25)">
          <template #icon><n-icon :component="RemoveOutline" /></template>
        </n-button>
        <n-button quaternary size="small" :disabled="zoom >= maxZoom" :aria-label="t('workflows.previewZoomIn')" :title="t('workflows.previewZoomIn')" @click="changeZoom(1.25)">
          <template #icon><n-icon :component="AddOutline" /></template>
        </n-button>
        <n-button quaternary size="small" :aria-label="t('workflows.fitView')" :title="t('workflows.fitView')" @click="fit">
          <template #icon><n-icon :component="ScanOutline" /></template>
        </n-button>
      </div>
    </header>
    <div ref="viewport" class="workflow-preview__body">
      <svg ref="canvas" class="workflow-preview__canvas" :class="{ 'is-dragging': dragging }" :viewBox="viewBox" role="img" :aria-label="t('workflows.previewTitle')"
        @pointerdown="startPan" @pointermove="movePan" @pointerup="endPan" @pointercancel="endPan" @lostpointercapture="endPan" @wheel="wheel">
        <g class="workflow-preview__links" fill="none">
          <path v-for="link in links" :key="link.id" :d="link.path" :class="{ 'is-data': link.type !== 'AUDIO' }" />
        </g>
        <g v-for="node in graph.nodes" :key="node.id" class="workflow-preview__node" :class="`workflow-preview__node--${node.kind}`" :transform="`translate(${node.x}, ${node.y})`">
          <title>{{ node.title }}{{ node.detail ? ` · ${node.detail}` : '' }}</title>
          <rect :width="node.width" :height="node.height" rx="10" />
          <foreignObject x="16" y="13" :width="node.width - 32" :height="node.height - 22">
            <div xmlns="http://www.w3.org/1999/xhtml" class="workflow-preview__node-copy">
              <span class="workflow-preview__node-kind">{{ t(kindLabels[node.kind]) }}
                <i v-if="node.customParameters" class="workflow-preview__parameter-dot" role="img"
                  :title="t('workflows.inferenceWorkflowCustom')" :aria-label="t('workflows.inferenceWorkflowCustom')" />
              </span>
              <strong>{{ node.title }}</strong>
              <span class="workflow-preview__node-detail">{{ node.detail }}</span>
            </div>
          </foreignObject>
          <circle v-for="slot in ports.get(node.id)?.inputs" :key="`in-${slot}`" :cx="0" :cy="(slot + 1) * node.height / (node.inputs + 1)" r="3.5" />
          <circle v-for="slot in ports.get(node.id)?.outputs" :key="`out-${slot}`" :cx="node.width" :cy="(slot + 1) * node.height / (node.outputs + 1)" r="3.5" />
        </g>
      </svg>
      <div v-if="!graph.nodes.length" class="workflow-preview__empty">
        <n-icon :component="ScanOutline" />
        <strong>{{ t('workflows.previewEmpty') }}</strong>
      </div>
      <span v-else class="workflow-preview__hint">{{ t('workflows.previewHint') }}</span>
    </div>
  </section>
</template>

<style scoped>
.workflow-preview__parameter-dot { display: inline-block; width: 7px; height: 7px; margin-inline-start: 3px; vertical-align: middle; border-radius: 50%; background: var(--primary); }
.workflow-preview { display: flex; flex-direction: column; min-height: 300px; flex: 1; border: 1px solid var(--outline); border-radius: 12px; overflow: hidden; }
.workflow-preview__head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 16px; background: var(--surface-1); border-bottom: 1px solid var(--outline); }
.workflow-preview__heading { min-width: 0; display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.workflow-preview__heading h3 { margin: 0; font-size: 12px; font-weight: 650; }
.workflow-preview__heading > span { font-size: 11px; color: var(--on-surface-muted); font-variant-numeric: tabular-nums; }
.workflow-preview__controls { display: flex; flex-shrink: 0; gap: 2px; }
.workflow-preview__body { position: relative; flex: 1; min-height: 250px; background-color: color-mix(in srgb, var(--surface-2) 48%, var(--surface-1)); background-image: radial-gradient(circle, color-mix(in srgb, var(--on-surface-muted) 19%, transparent) 0.8px, transparent 0.8px); background-size: 18px 18px; }
.workflow-preview__canvas { display: block; position: absolute; inset: 0; width: 100%; height: 100%; cursor: grab; touch-action: none; user-select: none; }
.workflow-preview__canvas.is-dragging { cursor: grabbing; }
.workflow-preview__links path { stroke: color-mix(in srgb, var(--primary) 68%, var(--outline)); stroke-width: 2; }
.workflow-preview__links path.is-data { stroke: var(--on-surface-muted); opacity: 0.45; stroke-dasharray: 5 4; }
.workflow-preview__node rect { fill: var(--surface-1); stroke: color-mix(in srgb, var(--primary) 24%, var(--outline)); stroke-width: 1.3; }
.workflow-preview__node--separate rect { stroke: color-mix(in srgb, var(--primary) 64%, var(--outline)); }
.workflow-preview__node--output rect { fill: color-mix(in srgb, var(--primary-soft) 28%, var(--surface-1)); }
.workflow-preview__node circle { fill: var(--surface-1); stroke: var(--primary); stroke-width: 1.8; }
.workflow-preview__node-copy { display: grid; gap: 5px; color: var(--on-surface); line-height: 1.25; }
.workflow-preview__node-kind { font-size: 10px; font-weight: 650; color: var(--primary-strong); }
.workflow-preview__node-copy strong { font-size: 13px; font-weight: 650; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.workflow-preview__node-detail { font-size: 10px; color: var(--on-surface-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.workflow-preview__hint { position: absolute; left: 14px; bottom: 12px; font-size: 10px; color: var(--on-surface-muted); pointer-events: none; }
.workflow-preview__empty { position: absolute; inset: 0; display: grid; align-content: center; justify-items: center; gap: 12px; color: var(--on-surface-muted); pointer-events: none; }
.workflow-preview__empty .n-icon { font-size: 32px; opacity: 0.55; }
.workflow-preview__empty strong { font-size: 13px; font-weight: 500; }
</style>
