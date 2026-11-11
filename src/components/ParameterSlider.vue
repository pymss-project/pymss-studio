<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch, watchEffect } from 'vue'
import { useI18n } from 'vue-i18n'
import { useSettingsStore } from '@/stores/settings'

const props = defineProps<{ value: number; min: number; max: number; step: number; label: string; disabled?: boolean }>()
const emit = defineEmits<{ 'update:value': [value: number] }>()
const settings = useSettingsStore()
const { t } = useI18n()
const slider = ref<{ $el: HTMLElement } | null>(null)
const dragging = ref(false)
const rebounding = ref(false)
const direction = ref(0)
const reducedMotion = ref(false)
let motionQuery: MediaQueryList | undefined
const syncMotion = () => { reducedMotion.value = motionQuery?.matches ?? false }
onMounted(() => {
  motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
  syncMotion()
  motionQuery.addEventListener('change', syncMotion)
})
onBeforeUnmount(() => motionQuery?.removeEventListener('change', syncMotion))
const moving = computed(() => settings.animationsEnabled && !reducedMotion.value && !props.disabled)

function update(value: number | number[]) {
  if (typeof value !== 'number') return
  if (dragging.value) direction.value = Math.sign(value - props.value)
  emit('update:value', value)
}
function startDrag() {
  rebounding.value = false
  dragging.value = true
  direction.value = 0
}
function endDrag() {
  rebounding.value = dragging.value && moving.value
  dragging.value = false
}
watch(moving, () => { rebounding.value = false })
// Naive's slider labels its root rather than the focusable thumb; name the native handle.
watchEffect(() => {
  const label = t('parameterEditor.sliderLabel', { parameter: props.label })
  slider.value?.$el.querySelector('[role="slider"]')?.setAttribute('aria-label', label)
}, { flush: 'post' })
</script>

<template>
  <n-slider
    ref="slider"
    class="parameter-slider" :class="{ 'is-dragging': dragging && moving, 'is-rebounding': rebounding && moving, 'is-still': !moving }"
    :style="{ '--jelly-lean': `${direction * -7}deg` }"
    :value="value" :min="min" :max="max" :step="step" :disabled="disabled" :tooltip="false"
    @update:value="update" @dragstart="startDrag" @dragend="endDrag"
  >
    <template #thumb>
      <span class="n-slider-handle parameter-slider__thumb" @animationend="rebounding = false"><i /></span>
    </template>
  </n-slider>
</template>

<style scoped>
.parameter-slider { --n-rail-height: 6px !important; --n-handle-size: 20px !important; padding: 11px 0; }
.parameter-slider :deep(.n-slider-rail) { border-radius: 99px; background: color-mix(in srgb, var(--outline-strong) 70%, var(--surface-2)); }
.parameter-slider :deep(.n-slider-rail__fill) { border-radius: inherit; background: var(--primary); }
.parameter-slider__thumb.n-slider-handle {
  display: grid; place-items: center; width: 20px; height: 20px;
  background: var(--surface-1); border: 2px solid var(--primary); border-radius: 50%;
  box-shadow: 0 2px 5px color-mix(in srgb, var(--primary) 18%, transparent);
  transform-origin: center; transition: transform 180ms cubic-bezier(.2,.8,.3,1.5), box-shadow 160ms;
}
.parameter-slider__thumb i { width: 4px; height: 4px; border-radius: 50%; background: var(--primary); }
.parameter-slider :deep(.n-slider-handle-wrapper:focus-visible) { outline: 2px solid var(--primary); outline-offset: 5px; border-radius: 50%; }
.is-dragging .parameter-slider__thumb { transform: scale(1.28,.82) rotate(var(--jelly-lean)); box-shadow: 0 3px 10px color-mix(in srgb, var(--primary) 28%, transparent); }
.is-rebounding .parameter-slider__thumb { animation: parameter-slider-release 440ms both; }
@keyframes parameter-slider-release {
  0% { transform: scale(1.28,.82) rotate(var(--jelly-lean)); }
  28% { transform: scale(.86,1.16); }
  52% { transform: scale(1.08,.94); }
  76% { transform: scale(.98,1.025); }
  100% { transform: scale(1); }
}
.is-still .parameter-slider__thumb { animation: none; transition: none; transform: none; }
@media (prefers-reduced-motion: reduce) {
  .parameter-slider .parameter-slider__thumb { animation: none; transition: none; transform: none; }
}
</style>
