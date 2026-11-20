<script setup lang="ts">
import { computed, useId } from 'vue'
import { useI18n } from 'vue-i18n'
import { useNumberInputAccessibility } from '@/composables/useNumberInputAccessibility'
import { resolveParameters, type CustomParameterIntent, type NativeParameters, type ParameterMode, type ParameterResolutionMetadata } from '@/features/inference/parameters'
import ParameterSlider from '@/components/ParameterSlider.vue'
import InferenceChunkPreview from '@/components/InferenceChunkPreview.vue'

const props = withDefaults(defineProps<{
  workflowFamilies?: Array<'mss' | 'vr'>
  metadata?: ParameterResolutionMetadata
  library?: Record<string, unknown>
  mode?: ParameterMode
  intent?: CustomParameterIntent
  node?: {
    parameters: NativeParameters
    overrides: NativeParameters
    modelDefaultKeys: ReadonlySet<string>
  }
  loading?: boolean
  disabled?: boolean
}>(), { mode: 'custom', intent: () => ({}) })
const emit = defineEmits<{
  'update:intent': [value: CustomParameterIntent]
  'update:node-field': [key: string, value: number | boolean | null]
}>()
const { t } = useI18n()
const vNumberA11y = useNumberInputAccessibility()
const fieldId = useId()
const resolved = computed(() => resolveParameters(props.metadata, props.library, props.mode, props.intent))
const parameters = computed(() => props.node?.parameters ?? resolved.value.parameters)
const readonly = computed(() => props.mode === 'model')
const has = (key: string) => Object.prototype.hasOwnProperty.call(parameters.value, key)
const supported = (key: string) => props.node && !props.metadata
  ? has(key) : ['free', 'lattice'].includes(props.metadata?.capabilities[key]?.kind || '')
const number = (key: string) => typeof parameters.value[key] === 'number' ? parameters.value[key] as number : 0
const sr = computed(() => props.metadata?.sampleRate)
const nodeOverrides = (key: string) => props.node && (Object.hasOwn(props.node.overrides, key) || props.node.modelDefaultKeys.has(key))
function nodeSuffix(key: string) {
  return props.node && (!nodeOverrides(key) || props.node.modelDefaultKeys.has(key)) ? t('workflows.followingSuffix') : ''
}
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

const durationFields = [
  { key: 'chunk_size', intent: 'chunkMilliseconds', label: 'parameterEditor.chunkSeconds', kind: 'number', min: 0.001, scale: 1000 },
  { key: 'overlap_size', intent: 'overlapBasisPoints', label: 'parameterEditor.overlapPercent', kind: 'number', min: 0.01, max: 99, scale: 100 },
] as const
type Field = (typeof fields)[number] | (typeof durationFields)[number]
const workflowFields = computed(() => [...durationFields, ...fields].filter(field => {
  if (field.key === 'batch_size' || field.key === 'normalize') return true
  return props.workflowFamilies?.includes('intent' in field ? 'mss' : 'vr')
}))
const booleanOptions = computed(() => [
  { value: 'inherit', label: t('parameterEditor.followModel') },
  { value: 'on', label: t('parameterEditor.modelLibrary.on') },
  { value: 'off', label: t('parameterEditor.modelLibrary.off') },
])
function workflowValue(field: Field) {
  const value = 'intent' in field ? props.intent[field.intent] : props.intent.vr?.[field.key]
  return typeof value === 'number' && 'scale' in field ? value / field.scale : value
}
function updateWorkflowField(field: Field, value: number | boolean | null) {
  if (value !== null) {
    if ('scale' in field) updateSize(field.intent, Number(value))
    else updateField(field, value)
    return
  }
  const next = { ...props.intent }
  if ('intent' in field) delete next[field.intent]
  else {
    const vr = { ...next.vr }
    delete vr[field.key]
    if (Object.keys(vr).length) next.vr = vr
    else delete next.vr
  }
  emit('update:intent', next)
}
function change(key: keyof CustomParameterIntent, value: number | boolean) {
  emit('update:intent', { ...props.intent, [key]: value })
}
function updateSize(key: 'chunkMilliseconds' | 'overlapBasisPoints', value: number | null) {
  if (value === null || !Number.isFinite(value)) return
  const intentValue = Math.round(value * (key === 'chunkMilliseconds' ? 1000 : 100))
  if (props.node) {
    const field = key === 'chunkMilliseconds' ? 'chunk_size' : 'overlap_size'
    const result = resolveParameters(props.metadata, parameters.value, 'custom', { [key]: intentValue })
    const samples = result.parameters[field]
    if (result.sources[field] === 'custom' && typeof samples === 'number'
      && !result.issues.some(issue => issue.key === field)) emit('update:node-field', field, samples)
    return
  }
  change(key, intentValue)
}
function updateField(field: typeof fields[number], value: number | boolean | null) {
  if (value === null) return
  if (props.node) {
    emit('update:node-field', field.key, value)
    return
  }
  if ('intent' in field) change(field.intent, value)
  else {
    emit('update:intent', { ...props.intent, vr: { ...props.intent.vr, [field.key]: value } })
  }
}
function markerValue(values: NativeParameters, key: string) {
  const value = values[key]
  if (typeof value !== 'number') return value
  if (key === 'chunk_size' && sr.value) return round(value / sr.value, 3)
  const chunk = values.chunk_size
  if (key === 'overlap_size' && typeof chunk === 'number' && chunk > 0) return round(value / chunk * 100, 2)
  return value
}

const markerSources = computed<Partial<Record<string, 'custom' | 'model-library'>>>(() => {
  const markers: Partial<Record<string, 'custom' | 'model-library'>> = {}
  const metadata = props.metadata
  if (props.node || !metadata) return markers
  const inherited = resolveParameters(metadata, props.library, 'model', {})
  const { parameters, sources } = resolved.value
  const { chunkMilliseconds } = props.intent
  // A ratio inherits the current chunk length even if the user customized that length.
  const inheritedOverlap = sources.overlap_size === 'custom' && chunkMilliseconds !== undefined
    ? resolveParameters(metadata, props.library, 'custom', { chunkMilliseconds }).parameters
    : inherited.parameters
  for (const [key, source] of Object.entries(sources)) {
    const baseline = key === 'overlap_size' ? inheritedOverlap : inherited.parameters
    if (source === 'custom' && markerValue(parameters, key) !== markerValue(baseline, key)) {
      markers[key] = 'custom'
    } else if (inherited.sources[key] === 'model-library'
      && markerValue(inherited.parameters, key) !== markerValue(metadata.defaults, key)) {
      markers[key] = 'model-library'
    }
  }
  return markers
})
function sourceLabel(key: string) { return t(`parameterEditor.source.${markerSources.value[key]}`) }
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
    <div v-else-if="workflowFamilies" class="parameter-secondary-grid">
      <div v-for="field in workflowFields" :key="field.key" class="parameter-tile">
        <label :for="fieldId + field.key">{{ t(field.label) }}{{ field.key === 'chunk_size' ? ' (' + t('parameterEditor.seconds') + ')' : field.key === 'overlap_size' ? ' (%)' : '' }}</label>
        <n-input-number v-if="field.kind === 'number'" v-number-a11y="t(field.label)" size="small" clearable
          :input-props="{ id: fieldId + field.key }" :value="typeof workflowValue(field) === 'number' ? Number(workflowValue(field)) : null"
          :min="field.min" :max="'max' in field ? field.max : undefined" :disabled="disabled" :placeholder="t('parameterEditor.followModel')"
          :precision="'scale' in field || field.key === 'post_process_threshold' ? undefined : 0"
          @update:value="(value: number | null) => updateWorkflowField(field, value)" />
        <AppSelect v-else :id="fieldId + field.key" :aria-label="t(field.label)" size="small" :disabled="disabled" :options="booleanOptions"
          :value="workflowValue(field) === undefined ? 'inherit' : workflowValue(field) ? 'on' : 'off'"
          @update:value="(value: string) => updateWorkflowField(field, value === 'inherit' ? null : value === 'on')" />
      </div>
    </div>
    <n-alert v-else-if="!metadata && !node" type="warning" :bordered="false">{{ t('parameterEditor.metadataUnavailable') }}</n-alert>
    <template v-else>
      <div v-if="has('chunk_size') || has('overlap_size')" class="parameter-panel">
      <div class="parameter-primary-grid">
        <section v-if="has('chunk_size')" class="parameter-primary" data-parameter-guide="chunk_size">
          <div class="parameter-heading">
            <label :for="`${fieldId}-chunk`">{{ t('parameterEditor.chunkSeconds') }}<small v-if="node">{{ nodeSuffix('chunk_size') }}</small></label>
            <span v-if="markerSources.chunk_size" class="parameter-source" :data-source="markerSources.chunk_size" role="img" :aria-label="sourceLabel('chunk_size')" :title="sourceLabel('chunk_size')" />
            <n-button v-if="nodeOverrides('chunk_size')" text size="tiny" :disabled="disabled"
              :aria-label="t('workflows.restoreField', { field: t('parameterEditor.chunkSeconds') })"
              @click="emit('update:node-field', 'chunk_size', null)">{{ t('workflows.restoreInheritance') }}</n-button>
          </div>
          <div class="parameter-value-row">
            <div class="parameter-number">
              <n-input-number v-number-a11y="t('parameterEditor.chunkSeconds') + ' (' + t('parameterEditor.seconds') + ')'" v-if="!readonly && supported('chunk_size') && sr" class="parameter-value-input" :style="inputWidth(chunkSeconds)" :value="chunkSeconds" :input-props="{ id: `${fieldId}-chunk` }" :show-button="false" :bordered="false" :disabled="disabled" :min="0.001" :max="metadata?.capabilities.chunk_size?.max && sr ? metadata.capabilities.chunk_size.max / sr : undefined" :step="0.5" @update:value="(v: number | null) => updateSize('chunkMilliseconds', v)" />
              <output v-else :id="`${fieldId}-chunk`" class="parameter-value">{{ compact(chunkSeconds) }}</output>
              <span class="parameter-unit">{{ t('parameterEditor.seconds') }}</span>
            </div>
            <span class="parameter-samples" :title="t('parameterEditor.actualChunk', { samples: format(number('chunk_size')), seconds: sr ? format(number('chunk_size') / sr) : '—' })">{{ format(number('chunk_size')) }} {{ t('parameterEditor.samplesUnit') }}</span>
          </div>
          <ParameterSlider v-if="!readonly && supported('chunk_size') && sr" :label="t('parameterEditor.chunkSeconds')" :value-text="compact(chunkSeconds) + ' ' + t('parameterEditor.seconds')" :value="chunkSeconds || 0" :min="chunkRange.min" :max="chunkRange.max" :step="0.5" :disabled="disabled" @update:value="v => updateSize('chunkMilliseconds', v)" />
          <p v-if="!node && resolved.adjustments.chunk_size" class="parameter-warning" :title="t('parameterEditor.snappedChunk', { seconds: chunkSeconds })">{{ t('parameterEditor.actualChunk', { samples: format(number('chunk_size')), seconds: sr ? format(number('chunk_size') / sr) : '—' }) }}</p>
          <small v-if="!supported('chunk_size') || !sr" class="parameter-note">{{ !sr ? t('parameterEditor.sampleRateMissing') : unavailable('chunk_size') }}</small>
        </section>
        <section v-if="has('overlap_size')" class="parameter-primary" data-parameter-guide="overlap_size">
          <div class="parameter-heading">
            <label :for="`${fieldId}-overlap`">{{ t('parameterEditor.overlapPercent') }}<small v-if="node">{{ nodeSuffix('overlap_size') }}</small></label>
            <span v-if="markerSources.overlap_size" class="parameter-source" :data-source="markerSources.overlap_size" role="img" :aria-label="sourceLabel('overlap_size')" :title="sourceLabel('overlap_size')" />
            <n-button v-if="nodeOverrides('overlap_size')" text size="tiny" :disabled="disabled"
              :aria-label="t('workflows.restoreField', { field: t('parameterEditor.overlapPercent') })"
              @click="emit('update:node-field', 'overlap_size', null)">{{ t('workflows.restoreInheritance') }}</n-button>
          </div>
          <div class="parameter-value-row">
            <div class="parameter-number">
              <n-input-number v-number-a11y="t('parameterEditor.overlapPercent') + ' (%)'" v-if="!readonly && supported('overlap_size') && (overlapPercent || intent.overlapBasisPoints !== undefined)" class="parameter-value-input" :style="inputWidth(overlapPercent)" :value="overlapPercent" :input-props="{ id: `${fieldId}-overlap` }" :show-button="false" :bordered="false" :disabled="disabled" :min="0.01" :max="99" :step="1" @update:value="(v: number | null) => updateSize('overlapBasisPoints', v)" />
              <output v-else :id="`${fieldId}-overlap`" class="parameter-value">{{ compact(overlapPercent) }}</output>
              <span class="parameter-unit">%</span>
            </div>
            <span class="parameter-samples" :title="t('parameterEditor.actualOverlap', { samples: format(number('overlap_size')), percent: overlapPercent === null ? '—' : format(number('overlap_size') / number('chunk_size') * 100) })">{{ format(number('overlap_size')) }} {{ t('parameterEditor.samplesUnit') }}</span>
          </div>
          <ParameterSlider v-if="!readonly && supported('overlap_size') && (overlapPercent || intent.overlapBasisPoints !== undefined)" :label="t('parameterEditor.overlapPercent')" :value-text="compact(overlapPercent) + '%'" :value="overlapPercent || 0" :min="overlapRange.min" :max="overlapRange.max" :step="1" :disabled="disabled" @update:value="v => updateSize('overlapBasisPoints', v)" />
          <n-button v-if="!readonly && supported('overlap_size') && overlapPercent === 0 && intent.overlapBasisPoints === undefined" text size="tiny" :disabled="disabled" @click="updateSize('overlapBasisPoints', 50)">{{ t('parameterEditor.customizeOverlap') }}</n-button>
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
              <label :for="`${fieldId}-${field.key}`">{{ t(field.label) }}<small v-if="node">{{ nodeSuffix(field.key) }}</small></label>
              <span v-if="markerSources[field.key]" class="parameter-source" :data-source="markerSources[field.key]" role="img" :aria-label="sourceLabel(field.key)" :title="sourceLabel(field.key)" />
              <n-button v-if="nodeOverrides(field.key)" text size="tiny" :disabled="disabled"
                :aria-label="t('workflows.restoreField', { field: t(field.label) })"
                @click="emit('update:node-field', field.key, null)">{{ t('workflows.restoreInheritance') }}</n-button>
            </div>
            <small v-if="!supported(field.key)" class="parameter-note">{{ unavailable(field.key) }}</small>
            <output v-if="readonly || !supported(field.key)" :id="`${fieldId}-${field.key}`" class="parameter-tile__value" :class="{ 'parameter-tile__value--on': field.kind === 'boolean' && parameters[field.key] }">{{ field.kind === 'boolean' ? t(parameters[field.key] ? 'parameterEditor.modelLibrary.on' : 'parameterEditor.modelLibrary.off') : format(number(field.key)) }}</output>
            <n-switch v-else-if="field.kind === 'boolean'" :id="`${fieldId}-${field.key}`" size="small" :aria-label="t(field.label)" :value="parameters[field.key] === true" :disabled="disabled" @update:value="(v: boolean) => updateField(field, v)" />
            <n-input-number v-number-a11y="t(field.label)" v-else size="small" class="parameter-tile__input" :value="number(field.key)" :input-props="{ id: `${fieldId}-${field.key}` }" :min="metadata?.capabilities[field.key]?.min ?? (node ? field.min : undefined)" :max="metadata?.capabilities[field.key]?.max ?? (node && 'max' in field ? field.max : undefined)" :precision="field.key === 'post_process_threshold' ? undefined : 0" :step="metadata?.capabilities[field.key]?.step || (field.key === 'post_process_threshold' ? 0.01 : 1)" :disabled="disabled" @update:value="(v: number | null) => updateField(field, v)" />
          </section>
        </template>
      </div>
      <template v-if="!node">
        <n-alert v-for="issue in resolved.issues" :key="`${issue.key}:${issue.code}`" type="error" :bordered="false">{{ t(`parameterEditor.error.${issue.code}`, { field: issue.key }) }}</n-alert>
        <n-alert v-if="resolved.skipped.length" type="info" :bordered="false">{{ t('parameterEditor.skipped', { fields: resolved.skipped.join(', ') }) }}</n-alert>
      </template>
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
.parameter-heading label small, .parameter-tile label small { display: inline-block; color: var(--on-surface-muted); font-size: 11px; white-space: nowrap; }
.parameter-heading:has(> button), .parameter-tile__copy:has(> button) { flex-wrap: wrap; }
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
.parameter-secondary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 170px), 1fr)); gap: 10px; }
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
