<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { dbPosition, levelDb, METER_MAX_DB } from '@/utils/editorMeter'

const props = withDefaults(defineProps<{
  /** Linear sample peak per channel; a single value is shown on both bars. */
  levels: readonly number[]
  orientation?: 'vertical' | 'horizontal'
}>(), { orientation: 'vertical' })

const { t } = useI18n()

// Peak ballistics: a rise shows at once and a fall eases off at a constant rate, so the meter
// moves continuously whether levels arrive every animation frame or from the audio host.
const RELEASE_DB_PER_SECOND = 20
const FLOOR_DB = -60

const displayed = ref<[number, number]>([FLOOR_DB, FLOOR_DB])
let frame = 0
let lastFrame = 0

function targetDb(channel: 0 | 1) {
  const level = props.levels[channel] ?? props.levels[0] ?? 0
  return Math.max(FLOOR_DB, Math.min(METER_MAX_DB, levelDb(level)))
}

function step(now: number) {
  const elapsed = lastFrame ? Math.min(0.1, (now - lastFrame) / 1000) : 0
  lastFrame = now
  const fall = RELEASE_DB_PER_SECOND * elapsed
  const next = ([0, 1] as const).map(channel => {
    const target = targetDb(channel)
    const current = displayed.value[channel]
    return target >= current ? target : Math.max(target, current - fall)
  }) as [number, number]
  displayed.value = next
  if (next.some(db => db > FLOOR_DB)) {
    frame = requestAnimationFrame(step)
  } else {
    frame = 0
    lastFrame = 0
  }
}

watch(() => [props.levels[0], props.levels[1]], () => {
  if (!frame) frame = requestAnimationFrame(step)
}, { immediate: true })

onBeforeUnmount(() => cancelAnimationFrame(frame))

function unlitTransform(db: number) {
  const unlit = 1 - dbPosition(db)
  return props.orientation === 'vertical' ? `scaleY(${unlit})` : `scaleX(${unlit})`
}
</script>

<template>
  <div class="level-meter" :class="`level-meter--${orientation}`">
    <span
      v-for="(db, channel) in displayed"
      :key="channel"
      class="level-meter__bar"
      :class="{ 'level-meter__bar--over': db > 0 }"
      role="meter"
      :aria-label="t(channel === 0 ? 'editor.panLeft' : 'editor.panRight')"
      :aria-valuemin="FLOOR_DB"
      :aria-valuemax="METER_MAX_DB"
      :aria-valuenow="Math.round(targetDb(channel as 0 | 1))"
    >
      <i class="level-meter__unlit" :style="{ transform: unlitTransform(db) }" />
    </span>
  </div>
</template>

<style scoped>
.level-meter {
  display: grid;
  gap: var(--level-meter-gap, 2px);
  min-width: 0;
  min-height: 0;
}

.level-meter--vertical { grid-template-columns: repeat(2, minmax(0, 1fr)); height: 100%; }
.level-meter--horizontal { grid-template-rows: repeat(2, minmax(0, 1fr)); width: 100%; }

/* The lit colours are fixed to the scale: green to −9, amber to 0 dBFS, red above. */
.level-meter__bar {
  position: relative;
  overflow: hidden;
  border-radius: var(--level-meter-radius, 2px);
  border: var(--level-meter-border, 1px solid var(--outline));
  /* Keep the lit colours inside the border, which is translucent. */
  background-clip: padding-box;
  background-color: var(--level-meter-unlit, var(--surface-2));
  background-image: linear-gradient(to top, rgb(92 214 132) 0%, rgb(92 214 132) 54%, rgb(255 190 102) 74%, rgb(255 170 102) 84%, rgb(255 106 106) 86%, rgb(255 80 80) 100%);
}

.level-meter--horizontal .level-meter__bar {
  background-image: linear-gradient(to right, rgb(92 214 132) 0%, rgb(92 214 132) 54%, rgb(255 190 102) 74%, rgb(255 170 102) 84%, rgb(255 106 106) 86%, rgb(255 80 80) 100%);
}

.level-meter__bar--over { border-color: rgb(255 106 106); }

.level-meter__unlit {
  position: absolute;
  inset: 0;
  background: var(--level-meter-unlit, var(--surface-2));
  transform-origin: top;
  will-change: transform;
}

.level-meter--horizontal .level-meter__unlit { transform-origin: right; }
</style>
