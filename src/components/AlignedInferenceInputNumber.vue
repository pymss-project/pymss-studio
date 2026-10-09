<script setup lang="ts">
import { onBeforeUnmount, onMounted } from 'vue'
import { alignInferenceStepChange } from '@/features/inference/sampleStep'

const props = withDefaults(defineProps<{
  value: number | null
  step: number
  alignmentStep?: number
  alignmentOffset?: number
  alignmentMin?: number
  min?: number
  max?: number
  disabled?: boolean
}>(), {
  alignmentStep: undefined,
  alignmentOffset: 0,
  alignmentMin: 0,
  min: 0,
  max: 1_048_576,
  disabled: false,
})

const emit = defineEmits<{
  'update:value': [value: number | null]
  blur: []
}>()

let stepDirection: -1 | 0 | 1 = 0
let resetTimer: ReturnType<typeof setTimeout> | null = null

function clearResetTimer() {
  if (!resetTimer) return
  clearTimeout(resetTimer)
  resetTimer = null
}

function resetDirection() {
  clearResetTimer()
  stepDirection = 0
}

function scheduleDirectionReset() {
  clearResetTimer()
  resetTimer = setTimeout(resetDirection, 0)
}

function handlePointerDown(event: PointerEvent) {
  clearResetTimer()
  const root = event.currentTarget
  const target = event.target
  if (!(root instanceof HTMLElement) || !(target instanceof Element)) {
    stepDirection = 0
    return
  }
  const button = target.closest('button')
  const buttons = [...root.querySelectorAll('button')]
  if (!button || !buttons.includes(button)) {
    stepDirection = 0
    return
  }
  stepDirection = button === buttons.at(-1) ? 1 : -1
}

function handleKeyDown(event: KeyboardEvent) {
  if (event.key === 'ArrowUp') stepDirection = 1
  else if (event.key === 'ArrowDown') stepDirection = -1
  else stepDirection = 0
}

function handleUpdate(value: number | null) {
  const next = stepDirection
    ? alignInferenceStepChange(props.value, value, props.alignmentStep, props.step, props.alignmentOffset, props.alignmentMin)
    : value
  emit('update:value', next)
}

function handleBlur() {
  resetDirection()
  emit('blur')
}

onMounted(() => {
  window.addEventListener('pointerup', scheduleDirectionReset)
  window.addEventListener('pointercancel', scheduleDirectionReset)
  window.addEventListener('blur', resetDirection)
})

onBeforeUnmount(() => {
  window.removeEventListener('pointerup', scheduleDirectionReset)
  window.removeEventListener('pointercancel', scheduleDirectionReset)
  window.removeEventListener('blur', resetDirection)
  resetDirection()
})
</script>

<template>
  <div
    class="aligned-inference-input"
    @pointerdown.capture="handlePointerDown"
    @keydown.capture="handleKeyDown"
    @keyup.capture="resetDirection"
  >
    <n-input-number
      :value="value"
      :min="min"
      :max="max"
      :step="step"
      :precision="0"
      :disabled="disabled"
      @update:value="handleUpdate"
      @blur="handleBlur"
    />
  </div>
</template>

<style scoped>
.aligned-inference-input,
.aligned-inference-input :deep(.n-input-number) {
  width: 100%;
}
</style>
