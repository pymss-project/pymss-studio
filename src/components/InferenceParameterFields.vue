<script setup lang="ts">
import { computed, useId } from 'vue'
import { useI18n } from 'vue-i18n'
import { resolveParameters, type CustomParameterIntent, type ParameterMode, type ParameterResolutionMetadata } from '@/features/inference/parameters'
import ParameterSlider from '@/components/ParameterSlider.vue'
import InferenceChunkPreview from '@/components/InferenceChunkPreview.vue'

const props = defineProps<{
  metadata?: ParameterResolutionMetadata
  library?: Record<string, unknown>
  mode: ParameterMode
  intent: CustomParameterIntent
  loading?: boolean
  disabled?: boolean
}>()
const emit = defineEmits<{ 'update:intent': [value: CustomParameterIntent] }>()
const { t } = useI18n()
const fieldId = useId()
const resolved = computed(() => resolveParameters(props.metadata, props.library, props.mode, props.intent))
const parameters = computed(() => resolved.value.parameters)
const readonly = computed(() => props.mode === 'model')
const has = (key: string) => Object.prototype.hasOwnProperty.call(parameters.value, key)
const supported = (key: string) => ['free', 'lattice'].includes(props.metadata?.capabilities[key]?.kind || '')
const number = (key: string) => typeof parameters.value[key] === 'number' ? parameters.value[key] as number : 0
const sr = computed(() => props.metadata?.sampleRate)
// Values derived from sample counts are rounded for display (63945 / 502740 → 12.72 %, not 12.7192982456);
// the exact sample counts stay visible next to them.
const round = (value: number, digits: number) => Number(value.toFixed(digits))
const chunkSeconds = computed(() => props.intent.chunkMilliseconds !== undefined && props.mode === 'custom'
  ? props.intent.chunkMilliseconds / 1000 : sr.value ? round(number('chunk_size') / sr.value, 3) : null)
const overlapPercent = computed(() => props.intent.overlapBasisPoints !== undefined && props.mode === 'custom'
  ? props.intent.overlapBasisPoints / 100 : number('chunk_size') ? round(number('overlap_size') / number('chunk_size') * 100, 2) : null)
const chunkRange = computed(() => ({ min: Math.min(1, chunkSeconds.value || 1), max: Math.max(30, Math.ceil(chunkSeconds.value || 0)) }))
const overlapRange = computed(() => ({ min: Math.min(1, overlapPercent.value ?? 1), max: Math.max(95, Math.ceil(overlapPercent.value || 0)) }))

const fields = [
  { key: 'batch_size', intent: 'batchSize', label: 'inference.batchSize', kind: 'number', min: 1, max: 32 },
  { key: 'standardize', intent: 'standardize', label: 'inference.standardize', kind: 'boolean' },
  { key: 'normalize', intent: 'normalize', label: 'inference.normalize', kind: 'boolean' },
  { key: 'window_size', label: 'inference.vrWindowSize', kind: 'number', min: 1 },
  { key: 'aggression', label: 'inference.vrAggression', kind: 'number', min: 0, max: 100 },
  { key: 'enable_post_process', label: 'inference.vrEnablePostProcess', kind: 'boolean' },
  { key: 'post_process_threshold', label: 'inference.vrPostProcessThreshold', kind: 'number', min: 0, max: 1 },
  { key: 'high_end_process', label: 'inference.vrHighEndProcess', kind: 'boolean' },
] as const

function change(key: keyof CustomParameterIntent, value: number | boolean) {
  emit('update:intent', { ...props.intent, [key]: value })
}
function updateSize(key: 'chunkMilliseconds' | 'overlapBasisPoints', value: number | null) {
  if (value === null || !Number.isFinite(value)) return
  change(key, Math.round(value * (key === 'chunkMilliseconds' ? 1000 : 100)))
}
function updateField(field: typeof fields[number], value: number | boolean | null) {
  if (value === null) return
  if ('intent' in field) change(field.intent, value)
  else {
    emit('update:intent', { ...props.intent, vr: { ...props.intent.vr, [field.key]: value } })
  }
}
function source(key: string) { return t(`parameterEditor.source.${resolved.value.sources[key] || 'model-config'}`) }
/** Only values that differ from the model's own config get a marker; everything else stays quiet. */
function marked(key: string) { return ['custom', 'model-library'].includes(resolved.value.sources[key] || '') }
function unavailable(key: string) { return t(`parameterEditor.capability.${props.metadata?.capabilities[key]?.kind || 'unknown'}`) }
const format = (v: number) => Number(v.toFixed(6)).toLocaleString(undefined, { maximumFractionDigits: 6 })
const compact = (v: number | null) => v === null ? '—' : v.toLocaleString(undefined, { maximumFractionDigits: 2 })
const inputWidth = (value: number | null) => ({ width: `${Math.max(2.2, Math.min(12, String(value ?? '').length + .3))}ch` })
</script>

<template>
  <div class="parameter-fields" :aria-busy="loading">
    <div v-if="loading" class="parameter-skeleton">
      <n-skeleton height="108px" :sharp="false" /><n-skeleton height="108px" :sharp="false" />
      <n-skeleton v-for="row in 3" :key="row" class="parameter-skeleton__row" height="34px" :sharp="false" />
    </div>
    <n-alert v-else-if="!metadata" type="warning" :bordered="false">{{ t('parameterEditor.metadataUnavailable') }}</n-alert>
    <template v-else>
      <div v-if="has('chunk_size') || has('overlap_size')" class="parameter-panel">
      <div class="parameter-primary-grid">
        <section v-if="has('chunk_size')" class="parameter-primary" data-parameter-guide="chunk_size">
          <div class="parameter-heading">
            <label :for="`${fieldId}-chunk`">{{ t('parameterEditor.chunkSeconds') }}</label>
            <span v-if="marked('chunk_size')" class="parameter-source" :data-source="resolved.sources.chunk_size" role="img" :aria-label="source('chunk_size')" :title="source('chunk_size')" />
          </div>
          <div class="parameter-value-row">
            <div class="parameter-number">
              <n-input-number v-if="!readonly && supported('chunk_size') && sr" class="parameter-value-input" :style="inputWidth(chunkSeconds)" :value="chunkSeconds" :input-props="{ id: `${fieldId}-chunk` }" :show-button="false" :bordered="false" :disabled="disabled" :min="0.001" :max="metadata.capabilities.chunk_size?.max && sr ? metadata.capabilities.chunk_size.max / sr : undefined" :step="0.5" @update:value="(v: number | null) => updateSize('chunkMilliseconds', v)" />
              <output v-else :id="`${fieldId}-chunk`" class="parameter-value">{{ compact(chunkSeconds) }}</output>
              <span class="parameter-unit">{{ t('parameterEditor.seconds') }}</span>
            </div>
            <span class="parameter-samples" :title="t('parameterEditor.actualChunk', { samples: format(number('chunk_size')), seconds: sr ? format(number('chunk_size') / sr) : '—' })">{{ format(number('chunk_size')) }} {{ t('parameterEditor.samplesUnit') }}</span>
          </div>
          <ParameterSlider v-if="!readonly && supported('chunk_size') && sr" :label="t('parameterEditor.chunkSeconds')" :value="chunkSeconds || 0" :min="chunkRange.min" :max="chunkRange.max" :step="0.5" :disabled="disabled" @update:value="v => updateSize('chunkMilliseconds', v)" />
          <p v-if="resolved.adjustments.chunk_size" class="parameter-warning" :title="t('parameterEditor.snappedChunk', { seconds: chunkSeconds })">{{ t('parameterEditor.actualChunk', { samples: format(number('chunk_size')), seconds: sr ? format(number('chunk_size') / sr) : '—' }) }}</p>
          <small v-if="!supported('chunk_size') || !sr" class="parameter-note">{{ !sr ? t('parameterEditor.sampleRateMissing') : unavailable('chunk_size') }}</small>
        </section>
        <section v-if="has('overlap_size')" class="parameter-primary" data-parameter-guide="overlap_size">
          <div class="parameter-heading">
            <label :for="`${fieldId}-overlap`">{{ t('parameterEditor.overlapPercent') }}</label>
            <span v-if="marked('overlap_size')" class="parameter-source" :data-source="resolved.sources.overlap_size" role="img" :aria-label="source('overlap_size')" :title="source('overlap_size')" />
          </div>
          <div class="parameter-value-row">
            <div class="parameter-number">
              <n-input-number v-if="!readonly && supported('overlap_size') && (overlapPercent || intent.overlapBasisPoints !== undefined)" class="parameter-value-input" :style="inputWidth(overlapPercent)" :value="overlapPercent" :input-props="{ id: `${fieldId}-overlap` }" :show-button="false" :bordered="false" :disabled="disabled" :min="0.01" :max="99" :step="1" @update:value="(v: number | null) => updateSize('overlapBasisPoints', v)" />
              <output v-else :id="`${fieldId}-overlap`" class="parameter-value">{{ compact(overlapPercent) }}</output>
              <span class="parameter-unit">%</span>
            </div>
            <span class="parameter-samples" :title="t('parameterEditor.actualOverlap', { samples: format(number('overlap_size')), percent: overlapPercent === null ? '—' : format(number('overlap_size') / number('chunk_size') * 100) })">{{ format(number('overlap_size')) }} {{ t('parameterEditor.samplesUnit') }}</span>
          </div>
          <ParameterSlider v-if="!readonly && supported('overlap_size') && (overlapPercent || intent.overlapBasisPoints !== undefined)" :label="t('parameterEditor.overlapPercent')" :value="overlapPercent || 0" :min="overlapRange.min" :max="overlapRange.max" :step="1" :disabled="disabled" @update:value="v => updateSize('overlapBasisPoints', v)" />
          <n-button v-if="!readonly && supported('overlap_size') && overlapPercent === 0 && intent.overlapBasisPoints === undefined" text size="tiny" :disabled="disabled" @click="change('overlapBasisPoints', 5000)">{{ t('parameterEditor.customizeOverlap') }}</n-button>
          <small v-if="!supported('overlap_size')" class="parameter-note">{{ unavailable('overlap_size') }}</small>
          <p v-if="(overlapPercent || 0) > 95" class="parameter-warning">{{ t('parameterEditor.highOverlap', { coverage: format(number('chunk_size') / Math.max(1, number('chunk_size') - number('overlap_size'))) }) }}</p>
        </section>
      </div>
      <InferenceChunkPreview v-if="has('chunk_size') && has('overlap_size')" class="parameter-preview" :chunk="number('chunk_size')" :overlap="number('overlap_size')" :sample-rate="sr" />
      </div>
      <div class="parameter-secondary-grid">
        <template v-for="field in fields" :key="field.key">
          <section v-if="has(field.key)" class="parameter-tile" :data-parameter-guide="field.key">
            <div class="parameter-tile__copy">
              <label :for="`${fieldId}-${field.key}`">{{ t(field.label) }}</label>
              <span v-if="marked(field.key)" class="parameter-source" :data-source="resolved.sources[field.key]" role="img" :aria-label="source(field.key)" :title="source(field.key)" />
            </div>
            <small v-if="!supported(field.key)" class="parameter-note">{{ unavailable(field.key) }}</small>
            <output v-if="readonly || !supported(field.key)" :id="`${fieldId}-${field.key}`" class="parameter-tile__value" :class="{ 'parameter-tile__value--on': field.kind === 'boolean' && parameters[field.key] }">{{ field.kind === 'boolean' ? t(parameters[field.key] ? 'parameterEditor.modelLibrary.on' : 'parameterEditor.modelLibrary.off') : format(number(field.key)) }}</output>
            <n-switch v-else-if="field.kind === 'boolean'" :id="`${fieldId}-${field.key}`" size="small" :aria-label="t(field.label)" :value="parameters[field.key] === true" :disabled="disabled" @update:value="(v: boolean) => updateField(field, v)" />
            <n-input-number v-else size="small" class="parameter-tile__input" :value="number(field.key)" :input-props="{ id: `${fieldId}-${field.key}` }" :min="metadata.capabilities[field.key]?.min" :max="metadata.capabilities[field.key]?.max" :precision="field.key === 'post_process_threshold' ? undefined : 0" :step="metadata.capabilities[field.key]?.step || (field.key === 'post_process_threshold' ? 0.01 : 1)" :disabled="disabled" @update:value="(v: number | null) => updateField(field, v)" />
          </section>
        </template>
      </div>
      <n-alert v-for="issue in resolved.issues" :key="`${issue.key}:${issue.code}`" type="error" :bordered="false">{{ t(`parameterEditor.error.${issue.code}`, { field: issue.key }) }}</n-alert>
      <n-alert v-if="resolved.skipped.length" type="info" :bordered="false">{{ t('parameterEditor.skipped', { fields: resolved.skipped.join(', ') }) }}</n-alert>
    </template>
  </div>
</template>

<style scoped>
.parameter-fields { --parameter-card: color-mix(in srgb, var(--surface-2) 65%, var(--surface-1)); display: grid; gap: 10px; min-width: 0; container-type: inline-size; }
.parameter-skeleton { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.parameter-skeleton__row { grid-column: 1 / -1; }
.parameter-panel { display: grid; gap: 10px; min-width: 0; padding: 14px 16px 12px; border-radius: 12px; background: var(--parameter-card); }
.parameter-primary-grid { display: grid; grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr); gap: 28px; }
.parameter-primary { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.parameter-preview { padding-top: 10px; border-top: 1px solid color-mix(in srgb, var(--outline) 80%, transparent); }
.parameter-heading, .parameter-tile__copy { display: flex; align-items: center; gap: 6px; min-width: 0; }
.parameter-heading label, .parameter-tile label { font-size: 13px; font-weight: 500; color: var(--on-surface); }
/* Source marker: filled = custom in this dialog, ring = saved in model library. Text lives in the tooltip. */
.parameter-source { flex: 0 0 auto; width: 6px; height: 6px; border-radius: 50%; box-shadow: inset 0 0 0 1.5px var(--primary); cursor: help; }
.parameter-source[data-source="custom"] { background: var(--primary); }
.parameter-value-row { display: flex; align-items: baseline; flex-wrap: wrap; gap: 3px 10px; min-width: 0; }
.parameter-number { display: inline-flex; align-items: baseline; gap: 5px; min-width: 0; max-width: 100%; }
.parameter-value, .parameter-value-input { min-height: 32px; font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums; letter-spacing: -.5px; color: var(--on-surface-heading); }
.parameter-value-input { flex: 0 1 auto; min-width: 0; max-width: calc(100cqi - 70px); }
.parameter-number .parameter-value-input { border-radius: 0; box-shadow: none; }
.parameter-samples { font-size: 11px; font-variant-numeric: tabular-nums; color: var(--on-surface-muted); }
.parameter-unit { font-size: 13px; font-weight: 500; letter-spacing: 0; color: var(--on-surface-muted); }
.parameter-value-input :deep(.n-input) { background: transparent; font-size: inherit; border-radius: 0; box-shadow: none; --n-border-radius: 0 !important; }
.parameter-value-input :deep(.n-input:hover), .parameter-value-input :deep(.n-input--focus) { box-shadow: inset 0 -1px var(--primary); }
.parameter-value-input :deep(.n-input__input-el) { font-weight: 600; font-variant-numeric: tabular-nums; padding: 0; caret-color: var(--primary); }
.parameter-value-input :deep(.n-input-wrapper) { padding-left: 0; padding-right: 0; }
.parameter-value-input :deep(.n-input__state-border) { display: none; }
.parameter-note { display: block; font-size: 11px; line-height: 1.5; color: var(--on-surface-muted); }
.parameter-warning { margin: 0; font-size: 11px; line-height: 1.5; color: var(--warning); }
.parameter-secondary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.parameter-secondary-grid:empty { display: none; }
.parameter-tile { display: flex; flex-direction: column; justify-content: space-between; align-items: flex-start; gap: 8px; min-width: 0; min-height: 72px; padding: 10px 12px; border-radius: 12px; background: var(--parameter-card); }
.parameter-tile__input { width: 100%; }
.parameter-tile__value { font-size: 14px; font-weight: 600; font-variant-numeric: tabular-nums; color: var(--on-surface-muted); white-space: nowrap; }
.parameter-tile__value--on { color: var(--primary-strong); }
@container (max-width: 470px) {
  .parameter-primary-grid { grid-auto-flow: row; gap: 14px; }
  .parameter-skeleton { grid-template-columns: 1fr; }
}
</style>
