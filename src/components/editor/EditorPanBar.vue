<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  value: number
  label: string
  valueText: string
  resetValue?: number
  disabled?: boolean
}>(), { resetValue: 0, disabled: false })
const emit = defineEmits<{
  'update:value': [value: number]
  dragstart: []
  dragend: []
}>()

const MIN = -1
const MAX = 1
// Dragging across the full bar sweeps the full range; Shift gives fine control.
const FINE_FACTOR = 0.2
const KEY_STEP = 0.05
const FINE_KEY_STEP = 0.01

const clamp = (value: number) => Math.min(MAX, Math.max(MIN, Math.round(value * 100) / 100))
const position = computed(() => (clamp(props.value) - MIN) / (MAX - MIN) * 100)
const fillStyle = computed(() => {
  const start = Math.min(50, position.value)
  return { left: `${start}%`, width: `${Math.abs(position.value - 50)}%` }
})

let drag: { pointerId: number; startX: number; startValue: number; width: number; changed: boolean } | null = null

function apply(value: number) {
  const next = clamp(value)
  if (next !== props.value) emit('update:value', next)
}
function applyOnce(value: number) {
  if (props.disabled || clamp(value) === props.value) return
  emit('dragstart')
  apply(value)
  emit('dragend')
}

// Relative drag: a stray click never moves the pan, as on a hardware-style pan control.
function onPointerDown(event: PointerEvent) {
  if (props.disabled || event.button !== 0) return
  event.preventDefault()
  const element = event.currentTarget as HTMLElement
  element.focus({ preventScroll: true })
  element.setPointerCapture(event.pointerId)
  drag = { pointerId: event.pointerId, startX: event.clientX, startValue: props.value, width: Math.max(1, element.clientWidth), changed: false }
}
function onPointerMove(event: PointerEvent) {
  if (!drag || event.pointerId !== drag.pointerId) return
  const scale = event.shiftKey ? FINE_FACTOR : 1
  const next = drag.startValue + (event.clientX - drag.startX) / drag.width * (MAX - MIN) * scale
  if (!drag.changed && clamp(next) !== props.value) {
    drag.changed = true
    emit('dragstart')
  }
  if (drag.changed) apply(next)
}
function onPointerEnd(event: PointerEvent) {
  if (!drag || event.pointerId !== drag.pointerId) return
  const changed = drag.changed
  drag = null
  if (changed) emit('dragend')
}
function onKeydown(event: KeyboardEvent) {
  const step = event.shiftKey ? FINE_KEY_STEP : KEY_STEP
  const next = event.key === 'ArrowRight' || event.key === 'ArrowUp' ? props.value + step
    : event.key === 'ArrowLeft' || event.key === 'ArrowDown' ? props.value - step
      : event.key === 'Home' ? MIN
        : event.key === 'End' ? MAX
          : event.key === 'Delete' || event.key === 'Backspace' ? props.resetValue : null
  if (next === null) return
  event.preventDefault()
  event.stopPropagation()
  applyOnce(next)
}
</script>

<template>
  <div class="pan-bar" :class="{ 'pan-bar--disabled': disabled }">
    <div
      class="pan-bar__track"
      role="slider"
      :tabindex="disabled ? -1 : 0"
      :aria-label="label"
      :aria-valuemin="MIN"
      :aria-valuemax="MAX"
      :aria-valuenow="clamp(value)"
      :aria-valuetext="valueText"
      :aria-disabled="disabled || undefined"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerEnd"
      @pointercancel="onPointerEnd"
      @lostpointercapture="onPointerEnd"
      @dblclick="applyOnce(resetValue)"
      @keydown="onKeydown"
    >
      <span class="pan-bar__fill" :style="fillStyle" />
      <span class="pan-bar__center" />
      <span class="pan-bar__marker" :style="{ left: `${position}%` }" />
    </div>
    <span class="pan-bar__value">{{ valueText }}</span>
  </div>
</template>

<style scoped>
.pan-bar { display: grid; gap: 3px; min-width: 0; }
.pan-bar__track {
  position: relative;
  height: 16px;
  overflow: hidden;
  border: 1px solid var(--outline);
  border-radius: 3px;
  background: var(--surface-2);
  cursor: ew-resize;
  touch-action: none;
}
.pan-bar__track:focus-visible { outline: 2px solid var(--primary); outline-offset: 1px; }
.pan-bar__fill { position: absolute; top: 0; bottom: 0; background: color-mix(in srgb, var(--primary) 28%, transparent); }
.pan-bar__center { position: absolute; top: 3px; bottom: 3px; left: 50%; width: 1px; background: var(--outline-strong); }
.pan-bar__marker { position: absolute; top: 1px; bottom: 1px; width: 2px; margin-left: -1px; border-radius: 1px; background: var(--primary); }
.pan-bar__value { color: var(--on-surface-muted); font-size: 10px; font-weight: 500; font-variant-numeric: tabular-nums; line-height: 1; text-align: center; }
.pan-bar--disabled { opacity: .55; }
.pan-bar--disabled .pan-bar__track { cursor: default; }
</style>
