<script setup lang="ts">
import { computed, useId } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps<{ chunk: number; overlap: number; sampleRate?: number }>()
const { t, locale } = useI18n()
const id = useId()
const LEFT = 76
const WIDTH = 508
const valid = computed(() => Number.isSafeInteger(props.chunk) && props.chunk > 0
  && Number.isSafeInteger(props.overlap) && props.overlap >= 0 && props.overlap < props.chunk)
const geometry = computed(() => {
  const ratio = valid.value ? props.overlap / props.chunk : 0
  const width = WIDTH / (2 - ratio)
  const next = LEFT + width * (1 - ratio)
  return { width, next, shared: width * ratio, end: LEFT + width }
})
const format = (value: number) => new Intl.NumberFormat(locale.value, { maximumFractionDigits: 3 }).format(value)
const hasSeconds = computed(() => !!props.sampleRate && props.sampleRate > 0)
const duration = (samples: number) => {
  const seconds = hasSeconds.value ? samples / props.sampleRate! : undefined
  if (seconds !== undefined && (seconds === 0 || seconds >= .001)) return `${format(seconds)} ${t('parameterEditor.seconds')}`
  return `${format(samples)} ${t('parameterEditor.samplesUnit')}`
}
const chunkValue = computed(() => duration(props.chunk))
const overlapValue = computed(() => duration(props.overlap))
const hopValue = computed(() => duration(props.chunk - props.overlap))
const percent = (value: number) => new Intl.NumberFormat(locale.value, { maximumFractionDigits: 2 }).format(value)
const ratioValue = computed(() => props.overlap / props.chunk * 100 > 99.99
  ? `> ${percent(99.99)}%` : `${percent(props.overlap / props.chunk * 100)}%`)

/** Round axis step (1/2/5 × 10^n) so labels read 0 / 10 / 20 s instead of 9.263 / 18.53. */
function niceStep(total: number, maxTicks = 5) {
  const raw = total / maxTicks
  const magnitude = 10 ** Math.floor(Math.log10(raw))
  return ([1, 2, 5, 10].find(factor => factor * magnitude >= raw) || 10) * magnitude
}
const ticks = computed(() => {
  const totalSamples = props.chunk + (props.chunk - props.overlap)
  const total = hasSeconds.value ? totalSamples / props.sampleRate! : totalSamples
  if (!(total > 0)) return []
  const step = niceStep(total)
  const unit = hasSeconds.value ? t('parameterEditor.seconds') : t('parameterEditor.samplesUnit')
  const values: number[] = []
  for (let value = 0; value <= total + step * 1e-9; value += step) values.push(value)
  return values.map((value, index) => ({
    x: LEFT + WIDTH * value / total,
    anchor: index === 0 ? 'start' : 'middle',
    label: `${new Intl.NumberFormat(locale.value, { maximumFractionDigits: 3 }).format(value)}${index === values.length - 1 ? ` ${unit}` : ''}`,
  }))
})
const description = computed(() => t('parameterEditor.previewDescription', {
  chunk: chunkValue.value, overlap: overlapValue.value, ratio: ratioValue.value, hop: hopValue.value,
}))
// Both rows show the same schematic signal at the same timeline position.
const signalPaths = [15, 39].map(center => Array.from({ length: 100 }, (_, index) => {
  const x = LEFT + 2 + index * 5.08
  const height = 2 + 5 * Math.abs(Math.sin(index * .57) * Math.cos(index * .19))
  return `M${x} ${center - height}V${center + height}`
}).join(''))
</script>

<template>
  <figure v-if="valid" class="chunk-preview" :aria-label="t('parameterEditor.previewTitle')" :aria-describedby="`${id}-description`">
    <svg viewBox="0 0 600 76" aria-hidden="true" class="chunk-preview__diagram">
      <title>{{ t(overlap > 0 ? 'parameterEditor.previewSharedHint' : 'parameterEditor.previewNoOverlapHint') }}</title>
      <defs>
        <clipPath :id="`${id}-first`"><rect :x="LEFT" y="6" :width="geometry.width" height="18" rx="3" /></clipPath>
        <clipPath :id="`${id}-next`"><rect :x="geometry.next" y="30" :width="geometry.width" height="18" rx="3" /></clipPath>
      </defs>
      <rect v-if="overlap > 0" :x="geometry.next" y="2" :width="geometry.shared" height="50" rx="2" class="chunk-preview__shared" />
      <text x="0" y="19" class="chunk-preview__label">{{ t('parameterHelp.block', { index: 1 }) }}</text>
      <text x="0" y="43" class="chunk-preview__label">{{ t('parameterHelp.block', { index: 2 }) }}</text>
      <rect :x="LEFT" y="6" :width="geometry.width" height="18" rx="3" class="chunk-preview__block" />
      <rect :x="geometry.next" y="30" :width="geometry.width" height="18" rx="3" class="chunk-preview__block" />
      <path v-for="(path, row) in signalPaths" :key="row" :d="path" :clip-path="`url(#${id}-${row === 0 ? 'first' : 'next'})`" class="chunk-preview__signal" />
      <g v-if="overlap > 0" class="chunk-preview__boundary">
        <line :x1="geometry.next" :x2="geometry.next" y1="2" y2="52" />
        <line :x1="geometry.end" :x2="geometry.end" y1="2" y2="52" />
      </g>
      <line :x1="LEFT" :x2="LEFT + WIDTH" y1="58" y2="58" class="chunk-preview__axis" />
      <g v-for="tick in ticks" :key="tick.x" class="chunk-preview__tick">
        <line :x1="tick.x" :x2="tick.x" y1="58" y2="61" />
        <text :x="tick.x" y="73" :text-anchor="tick.anchor">{{ tick.label }}</text>
      </g>
    </svg>
    <figcaption>
      <span v-if="overlap > 0" class="chunk-preview__legend chunk-preview__legend--shared"><i aria-hidden="true" />{{ t('parameterEditor.previewOverlap') }} {{ overlapValue }} · {{ ratioValue }}</span>
      <span class="chunk-preview__legend">{{ t('parameterEditor.previewHop') }} {{ hopValue }}</span>
    </figcaption>
    <p :id="`${id}-description`" class="chunk-preview__accessible">{{ description }}</p>
  </figure>
</template>

<style scoped>
.chunk-preview { margin: 0; min-width: 0; }
.chunk-preview__diagram { display: block; width: 100%; height: auto; overflow: visible; }
.chunk-preview figcaption { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 2px 16px; margin-top: 4px; }
.chunk-preview__legend { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; font-variant-numeric: tabular-nums; color: var(--on-surface-muted); }
.chunk-preview__legend i { width: 8px; height: 8px; border-radius: 2px; background: color-mix(in srgb, var(--primary) 32%, transparent); }
.chunk-preview__legend--shared { color: var(--primary-strong); }
.chunk-preview__block { fill: color-mix(in srgb, var(--primary) 5%, var(--surface-1)); stroke: color-mix(in srgb, var(--primary) 40%, var(--outline)); }
.chunk-preview__shared { fill: var(--primary); opacity: .16; }
.chunk-preview__signal { fill: none; stroke: color-mix(in srgb, var(--primary) 72%, var(--on-surface-muted)); stroke-width: 1.7; stroke-linecap: round; }
.chunk-preview__label { fill: var(--on-surface-muted); font-size: 11px; }
.chunk-preview__boundary { stroke: var(--primary); stroke-width: 1; stroke-dasharray: 3 3; opacity: .6; }
.chunk-preview__axis, .chunk-preview__tick line { stroke: var(--outline-strong); }
.chunk-preview__tick text { fill: var(--on-surface-muted); font-size: 10px; font-variant-numeric: tabular-nums; }
.chunk-preview__accessible { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
</style>
