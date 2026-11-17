<script setup lang="ts">
import { computed, onBeforeUnmount, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { vSliderA11y } from '@/directives/sliderAccessibility'

defineOptions({ inheritAttrs: false })
const props = withDefaults(defineProps<{
  value: number
  resetValue: number
  label: string
  valueText?: string
  resetLabel: string
  disabled?: boolean
}>(), { disabled: false })
const emit = defineEmits<{
  'update:value': [value: number]
  dragstart: []
  dragend: []
}>()
const { t } = useI18n()
const resetTitle = computed(() => t('editor.sliderResetAction', { label: props.label, value: props.resetLabel }))
const gestureTitle = computed(() => t('editor.sliderResetHint', { value: props.resetLabel }))

let dragging = false
let interactionStarted = false
let mousePress = false
let completedMouseClick = false
let clickHasHistory = false
let secondMousePress = false
let ignoreHandlePressUpdate = false

function clearClick() {
  completedMouseClick = false
  clickHasHistory = false
  secondMousePress = false
  mousePress = false
}

function isHandle(target: EventTarget | null) {
  return target instanceof Element && Boolean(target.closest('[role="slider"]'))
}

function handleMouseDown(event: MouseEvent) {
  if (props.disabled || event.button !== 0) return
  if (event.altKey) {
    event.preventDefault()
    event.stopPropagation()
    clearClick()
    applyReset()
    return
  }
  if (event.detail === 2 && completedMouseClick) {
    event.preventDefault()
    event.stopPropagation()
    secondMousePress = true
    return
  }
  clearClick()
  mousePress = event.detail === 1 && isHandle(event.target)
  if (mousePress) {
    // Pressing a thumb edge must not move the parameter before a possible reset.
    // Keep the guard until bubbling reaches this wrapper: native event dispatch
    // can run microtasks between the capture and target listeners.
    ignoreHandlePressUpdate = true
  }
}

function finishMouseDown() {
  ignoreHandlePressUpdate = false
}

function beginDrag() {
  dragging = true
  interactionStarted = false
}

function updateValue(value: number) {
  if (props.disabled || ignoreHandlePressUpdate || value === props.value) return
  if (!dragging || !interactionStarted) {
    emit('dragstart')
    interactionStarted = true
  }
  emit('update:value', value)
  if (!dragging) {
    interactionStarted = false
    emit('dragend')
  }
}

function endDrag() {
  if (!dragging) return
  dragging = false
  const changed = interactionStarted
  interactionStarted = false
  if (changed) emit('dragend')
  completedMouseClick = mousePress
  clickHasHistory = changed
  mousePress = false
}

function applyReset(reuseHistory = false) {
  if (props.disabled || props.value === props.resetValue) return
  const ongoingInteraction = dragging && interactionStarted
  if (!reuseHistory && !ongoingInteraction) emit('dragstart')
  emit('update:value', props.resetValue)
  if (!reuseHistory && !ongoingInteraction) emit('dragend')
}

function resetFromButton() {
  clearClick()
  applyReset()
}

function handleDoubleClick(event: MouseEvent) {
  if (props.disabled || event.button !== 0 || !isHandle(event.target)) return
  // A rail click can move the thumb under the pointer. It must stay a rail edit,
  // rather than turning into a reset on the second click.
  if (event.detail !== 0 && !secondMousePress) return
  event.preventDefault()
  event.stopPropagation()
  const reuseHistory = event.detail === 2 && secondMousePress && clickHasHistory
  clearClick()
  applyReset(reuseHistory)
}

watch(() => props.disabled, disabled => {
  if (!disabled) return
  endDrag()
  clearClick()
})
onBeforeUnmount(endDrag)
</script>

<template>
  <div class="editor-parameter-slider">
    <div
      class="editor-parameter-slider__control"
      :title="gestureTitle"
      @mousedown.capture="handleMouseDown"
      @mousedown="finishMouseDown"
      @dblclick.capture="handleDoubleClick"
      @focusout="clearClick"
      @keydown.capture="clearClick"
      @touchstart.capture="clearClick"
    >
      <n-slider
        v-bind="$attrs"
        v-slider-a11y="{ label, valueText: valueText ?? String(value) }"
        :value="value"
        :disabled="disabled"
        @update:value="updateValue"
        @dragstart="beginDrag"
        @dragend="endDrag"
      />
    </div>
    <button
      class="editor-parameter-slider__reset"
      type="button"
      :disabled="disabled || value === resetValue"
      :title="resetTitle"
      :aria-label="resetTitle"
      @keydown.space.stop
      @click.stop="resetFromButton"
    >{{ t('editor.sliderReset') }}</button>
  </div>
</template>

<style scoped>
.editor-parameter-slider { display: flex; align-items: center; gap: 8px; min-width: 0; }
.editor-parameter-slider__control { flex: 1; min-width: 0; }
.editor-parameter-slider__reset {
  flex: none;
  min-height: 24px;
  padding: 0 6px;
  border: 1px solid var(--outline);
  border-radius: 4px;
  background: transparent;
  color: var(--on-surface-muted);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}
.editor-parameter-slider__reset:enabled:hover { color: var(--primary); border-color: var(--primary-border); background: var(--primary-soft); }
.editor-parameter-slider__reset:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.editor-parameter-slider__reset:disabled { opacity: 0.5; cursor: default; }
</style>
