<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { AddOutline, RemoveOutline } from '@vicons/ionicons5'
import { alignInferenceStepChange } from '@/features/inference/sampleStep'
import { useNumberInputAccessibility } from '@/composables/useNumberInputAccessibility'

const props = defineProps<{
  id: string
  label: string
  value: number | null
  inherited?: number
  placeholder: string
  step: number
  /** Full-precision duration behind the rounded seconds shown in the input. */
  stepBase?: number
  min?: number
  max?: number
  precision?: number
  disabled?: boolean
  clearable?: boolean
  alignmentStep?: number
  alignmentOffset?: number
  alignmentMin?: number
}>()
const emit = defineEmits<{ 'update:value': [value: number | null] }>()
const { t } = useI18n()
const vNumberA11y = useNumberInputAccessibility()
// A precise input can contain an uncommitted number until Enter or blur.
const typed = ref('')
const displayed = computed(() => props.value ?? props.inherited ?? null)
const base = computed(() => typed.value.trim() ? Number(typed.value) : props.stepBase ?? props.value ?? props.inherited)
watch(() => [props.value, props.inherited], () => { typed.value = '' })

function nextValue(direction: number) {
  const current = base.value
  if (current === undefined || !Number.isFinite(current) || props.disabled) return
  let min = Math.max(props.min ?? -Infinity, props.alignmentMin ?? -Infinity)
  let max = props.max ?? Infinity
  const grid = props.alignmentStep, origin = props.alignmentOffset ?? 0
  if (grid && grid > 0) {
    min = origin + Math.ceil((min - origin) / grid) * grid
    max = origin + Math.floor((max - origin) / grid) * grid
  }
  const raw = Number((current + direction * props.step).toFixed(props.stepBase === undefined ? props.precision ?? 12 : 12))
  const aligned = alignInferenceStepChange(current, raw, grid, props.step, origin, props.alignmentMin)
  if (aligned === null || min > max) return
  const next = Math.max(min, Math.min(max, aligned))
  if ((next - current) * direction > 0) return next
}
const lower = computed(() => nextValue(-1))
const higher = computed(() => nextValue(1))
function apply(value: number | undefined) {
  if (value === undefined) return
  typed.value = ''
  emit('update:value', value)
}
function commitTypedValue() {
  // Naive UI suppresses its change event when a user explicitly types the displayed value.
  if (props.value === null && typed.value.trim() && Number(typed.value) === props.inherited) {
    emit('update:value', props.inherited!)
  }
  typed.value = ''
}
</script>

<template>
  <n-input-number v-number-a11y="label" size="small" :value="displayed" :placeholder="placeholder"
    :input-props="{ id }" :min="min" :max="max" :step="step" :precision="precision" :clearable="clearable && value !== null"
    :disabled="disabled" :show-button="false" :keyboard="{ ArrowUp: false, ArrowDown: false }"
    @input="typed = ($event.target as HTMLInputElement).value" @blur="commitTypedValue" @keydown.enter="commitTypedValue"
    @keydown.up.prevent="apply(higher)" @keydown.down.prevent="apply(lower)" @update:value="(value: number | null) => emit('update:value', value)">
    <template #suffix>
      <span class="step-buttons" @mousedown.prevent>
        <n-button text tabindex="-1" :disabled="lower === undefined" :aria-label="t('common.decreaseValue', { parameter: label })"
          @click.stop="apply(lower)"><n-icon :component="RemoveOutline" /></n-button>
        <n-button text tabindex="-1" :disabled="higher === undefined" :aria-label="t('common.increaseValue', { parameter: label })"
          @click.stop="apply(higher)"><n-icon :component="AddOutline" /></n-button>
      </span>
    </template>
  </n-input-number>
</template>

<style scoped>
.step-buttons { display: inline-flex; align-items: center; gap: 2px; }
.step-buttons button { width: 20px; height: 22px; color: var(--on-surface-muted); }
</style>
