<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { InformationCircleOutline } from '@vicons/ionicons5'
import InferenceParameterFields from '@/components/InferenceParameterFields.vue'
import InferenceSettingsCard from '@/components/InferenceSettingsCard.vue'
import WorkflowNumberInput from './WorkflowNumberInput.vue'
import { resolveParameters, type NativeParameters } from '@/features/inference/parameters'
import { INFERENCE_PRESETS, supportsInferencePresets, type InferencePreset } from '@/features/inference/presets'
import { inferenceChunkSizeIssue, workflowNumberStep } from '@/features/inference/sampleStep'
import { useModelStore } from '@/stores/model'
import { useTaskStore } from '@/stores/task'
import { useSettingsStore } from '@/stores/settings'
import { hasInvalidSimpleInferenceNumbers } from '@/workflows/formats'
import { findWorkflowModel, resolveWorkflowNode, type WorkflowParameterTarget } from '@/workflows/inferencePolicy'

const props = defineProps<{ target: WorkflowParameterTarget }>()
const emit = defineEmits<{ close: []; save: [overrides: NativeParameters, modelDefaultKeys: string[]] }>()
const { t } = useI18n({ useScope: 'global' })
const models = useModelStore()
const task = useTaskStore()
const settings = useSettingsStore()
const overrides = ref<NativeParameters>({ ...props.target.overrides })
const modelDefaultKeys = ref(new Set(props.target.modelDefaultKeys))
const entry = computed(() => findWorkflowModel(models.models, props.target))
const fetched = ref<Awaited<ReturnType<typeof models.getWorkflowParameterMetadata>>>()
const loading = ref(false)
const configError = ref('')
const preferencesLoading = ref(true)
const busy = computed(() => loading.value || preferencesLoading.value)
const sameDirectory = computed(() => !props.target.modelPath && (!props.target.modelDir || props.target.modelDir === settings.modelDir))
const missingCatalogConfig = computed(() => fetched.value?.missingCatalogConfig === true)
const metadata = computed(() => {
  if (missingCatalogConfig.value) return undefined
  if (fetched.value) return fetched.value.resolution
  return sameDirectory.value ? entry.value?.inferenceParamMeta?.resolution : undefined
})
const library = computed(() => models.getModelInferenceOverrides(entry.value?.name ?? props.target.model))
const sampleMeta = computed(() => {
  const info = entry.value?.inferenceParamMeta
  if (!metadata.value) return props.target.modelPath || props.target.modelDir ? undefined : info
  return info?.resolution?.fingerprint === metadata.value.fingerprint ? info : undefined
})

let loadId = 0
async function loadMetadata(fetchConfig = false) {
  const id = ++loadId
  if (!fetchConfig) fetched.value = undefined
  configError.value = ''
  loading.value = false
  if (!props.target.model && !props.target.modelPath) return
  loading.value = true
  try {
    const result = await models.getWorkflowParameterMetadata(props.target, undefined, fetchConfig)
    if (id === loadId) {
      fetched.value = result
      if (fetchConfig && (!result.resolution || result.missingCatalogConfig)) configError.value = t('parameterEditor.metadataUnavailable')
    }
  } catch (error) {
    if (id === loadId && fetchConfig) configError.value = error instanceof Error ? error.message : String(error)
  } finally {
    if (id === loadId) loading.value = false
  }
}
watch([() => props.target.model, () => props.target.modelPath, () => props.target.configPath,
  () => props.target.modelType, () => props.target.modelDir, () => settings.modelDir], () => loadMetadata(), { immediate: true })
onBeforeUnmount(() => { loadId++ })
void task.refreshParameterPreferences().catch(() => undefined).finally(() => { preferencesLoading.value = false })

const fieldDefinitions: Record<string, { label: string; kind: 'boolean' | 'number'; min?: number; max?: number; step?: number; help?: string }> = {
  batch_size: { label: 'inference.batchSize', kind: 'number', min: 1, help: 'parameterHelp.batchDescription' },
  chunk_size: { label: 'inference.chunkSize', kind: 'number', min: 1 },
  overlap_size: { label: 'inference.overlapSize', kind: 'number', min: 0 },
  standardize: { label: 'inference.standardize', kind: 'boolean', help: 'parameterHelp.standardizeDescription' },
  normalize: { label: 'inference.normalize', kind: 'boolean', help: 'parameterHelp.normalizeDescription' },
  window_size: { label: 'inference.vrWindowSize', kind: 'number', min: 1, help: 'parameterHelp.vrWindowDescription' },
  aggression: { label: 'inference.vrAggression', kind: 'number', min: 0, max: 100, help: 'parameterHelp.vrAggressionDescription' },
  enable_post_process: { label: 'inference.vrEnablePostProcess', kind: 'boolean' },
  post_process_threshold: { label: 'inference.vrPostProcessThreshold', kind: 'number', min: 0, max: 1, step: 0.01 },
  high_end_process: { label: 'inference.vrHighEndProcess', kind: 'boolean' },
  enable_tta: { label: 'workflows.inferenceEnableTta', kind: 'boolean', help: 'parameterHelp.ttaDescription' },
}
const mssFields = ['chunk_size', 'overlap_size', 'standardize']
const vrFields = ['window_size', 'aggression', 'enable_post_process', 'post_process_threshold', 'high_end_process']
function resolve(values: NativeParameters, defaultKeys: Iterable<string> = modelDefaultKeys.value) {
  const legacy = [...defaultKeys]
  const explicit = Object.fromEntries(Object.entries(values).filter(([key]) => !legacy.includes(key)))
  return resolveWorkflowNode({ ...props.target, overrides: explicit, modelDefaultKeys: legacy }, metadata.value, library.value, task.parameterContext)
}

const current = computed(() => resolve(overrides.value))
const original = computed(() => resolve(props.target.overrides, props.target.modelDefaultKeys ?? []))
// Older catalog entries may carry native defaults even when full capabilities are unavailable.
// A zero MSS size in the runtime fallback means "read from config", not a sample count.
const cachedDefaults = computed<NativeParameters>(() => {
  if (missingCatalogConfig.value || metadata.value || !sameDirectory.value || !entry.value) return {}
  const base = models.getModelBaseInferenceDefaults(entry.value.name) ?? entry.value.defaultInferenceParams
  if (!base) return {}
  const values: NativeParameters = { ...base }
  if (!(Number(values.chunk_size) > 0)) {
    delete values.chunk_size
    if (values.overlap_size === 0) delete values.overlap_size
  }
  if (entry.value.defaultInferenceParamsSource === 'config' && entry.value.modelType !== 'vr' && base.standardize === undefined) {
    if (typeof base.normalize === 'boolean') values.standardize = base.normalize
    values.normalize = false
  }
  const { mode, intent } = task.parameterContext
  if (mode === 'custom') {
    // Conversions and capability checks need the actual model config.
    if (intent.chunkMilliseconds !== undefined) delete values.chunk_size
    if (intent.chunkMilliseconds !== undefined || intent.overlapBasisPoints !== undefined) delete values.overlap_size
    if (intent.batchSize !== undefined) delete values.batch_size
    if (intent.standardize !== undefined) delete values.standardize
    if (intent.normalize !== undefined) delete values.normalize
    for (const key of Object.keys(intent.vr || {})) delete values[key]
  }
  return values
})
function effectiveValue(result: ReturnType<typeof resolve>, key: string) {
  if (result.dynamic && result.sources[key] !== 'node') return undefined
  return key === 'enable_tta' ? result.useTta : result.parameters[key] ?? cachedDefaults.value[key]
}
const fields = computed(() => {
  const modelType = (props.target.modelType || entry.value?.modelType)?.toLowerCase()
  const kindFields = props.target.kind === 'vr' || modelType === 'vr' ? vrFields : mssFields
  const applicable = new Set([
    'batch_size', 'normalize', 'enable_tta', ...kindFields,
    ...Object.keys(current.value.parameters),
    ...Object.keys(overrides.value),
  ])
  return [...new Set([...Object.keys(fieldDefinitions), ...Object.keys(overrides.value)])]
    .filter(key => applicable.has(key))
    .map(key => {
      const def = fieldDefinitions[key]
      const capability = metadata.value?.capabilities[key]
      const editable = Boolean(def) && (key === 'enable_tta' || !metadata.value || capability?.kind === 'free' || capability?.kind === 'lattice')
      const name = def ? t(def.label) : key
      const label = key === 'chunk_size' || key === 'overlap_size' ? name + ' (' + t('parameterEditor.samplesUnit') + ')' : name
      const help = key === 'enable_tta' && kindFields === vrFields ? 'parameterHelp.vrTtaDescription' : def?.help
      const effective = effectiveValue(current.value, key)
      return { ...def, key, editable, label, help, effective, increment: workflowNumberStep(key, capability, sampleMeta.value),
        min: capability?.min ?? def?.min, max: capability?.max ?? def?.max }
    })
    .filter(field => field.editable || Object.hasOwn(overrides.value, field.key))
})
const nodeParameters = computed<NativeParameters>(() => {
  const known: NativeParameters = {}
  for (const key of Object.keys(fieldDefinitions)) {
    if ((key === 'chunk_size' || key === 'overlap_size') && !metadata.value?.sampleRate) continue
    const value = effectiveValue(current.value, key)
    if (value !== undefined) known[key] = value
  }
  return known
})
const presets = computed(() => {
  if (props.target.dynamic || !supportsInferencePresets(metadata.value)) return []
  return INFERENCE_PRESETS.flatMap(preset => {
    const { parameters, issues } = resolveParameters(metadata.value, {}, 'custom', preset)
    const { chunk_size: chunk, overlap_size: overlap } = parameters
    if (issues.length || typeof chunk !== 'number' || typeof overlap !== 'number') return []
    return [{ ...preset, chunk, overlap, selected: overrides.value.chunk_size === chunk && overrides.value.overlap_size === overlap
      && !modelDefaultKeys.value.has('chunk_size') && !modelDefaultKeys.value.has('overlap_size') }]
  })
})
const activePreset = computed(() => presets.value.find(preset => preset.selected)?.id)
function applyPreset(value: InferencePreset) {
  const preset = presets.value.find(item => item.id === value.id)
  if (!preset) return
  updateField('chunk_size', preset.chunk)
  updateField('overlap_size', preset.overlap)
}
function updateField(key: string, value: number | boolean | null) {
  const next = { ...overrides.value }
  if (value === null) delete next[key]
  else next[key] = value
  overrides.value = next
  modelDefaultKeys.value.delete(key)
}

const unknownValue = computed(() => t('workflows.valueAtRuntime'))
function displayBoolean(value: unknown) {
  return typeof value === 'boolean' ? t(value ? 'parameterEditor.modelLibrary.on' : 'parameterEditor.modelLibrary.off') : unknownValue.value
}
function booleanOptions(key: string) {
  const inherited = { ...overrides.value }
  delete inherited[key]
  const result = Object.hasOwn(overrides.value, key) ? resolve(inherited) : current.value
  return [
    { value: 'inherit', label: t('workflows.inferenceFollowGlobalShort') + ' (' + displayBoolean(effectiveValue(result, key)) + ')' },
    { value: 'on', label: t('parameterEditor.modelLibrary.on') },
    { value: 'off', label: t('parameterEditor.modelLibrary.off') },
  ]
}
function resetAll() {
  overrides.value = {}
  modelDefaultKeys.value.clear()
}
const changed = computed(() => {
  const stored = props.target.overrides
  return Object.keys(overrides.value).length !== Object.keys(stored).length
    || Object.entries(overrides.value).some(([key, value]) => !Object.hasOwn(stored, key) || stored[key] !== value)
    || modelDefaultKeys.value.size !== (props.target.modelDefaultKeys?.length ?? 0)
})
const invalid = computed(() => Object.entries(overrides.value).find(([key, value]) => (
  value !== props.target.overrides[key] && (
    hasInvalidSimpleInferenceNumbers({ [key]: value })
    || (typeof value === 'number' && key !== 'post_process_threshold' && !Number.isSafeInteger(value))
    || (key === 'chunk_size' && value === 0)
    || (key === 'aggression' && Number(value) > 100)
  )
)))
const chunkIssue = computed(() => {
  const chunk = overrides.value.chunk_size
  return typeof chunk === 'number' && inferenceChunkSizeIssue(chunk, sampleMeta.value)
})
const blocksSaving = computed(() => {
  if (invalid.value || (chunkIssue.value && overrides.value.chunk_size !== props.target.overrides.chunk_size)) return true
  const updated = Object.entries(overrides.value).some(([key, value]) =>
    value !== props.target.overrides[key] || (props.target.modelDefaultKeys?.includes(key) && !modelDefaultKeys.value.has(key)),
  )
  if (!updated) return false
  return current.value.issues.some(issue => (
    (issue.code === 'invalidValue' || issue.code === 'noLegalChunk')
      && (overrides.value[issue.key] !== props.target.overrides[issue.key]
        || !original.value.issues.some(item => item.key === issue.key && item.code === issue.code))
  ))
})
</script>

<template>
  <n-modal :show="true" @update:show="(show: boolean) => !show && emit('close')">
    <InferenceSettingsCard class="workflow-inference-dialog" :aria-busy="busy"
      :presets="presets" :active-preset="activePreset" :presets-disabled="busy"
      :tta="target.dynamic ? undefined : current.useTta" :tta-disabled="busy"
      :reset-label="t('workflows.inferenceRestoreAll')" :reset-disabled="busy || !Object.keys(overrides).length && !modelDefaultKeys.size"
      :apply-disabled="busy || !changed || blocksSaving"
      @close="emit('close')" @reset="resetAll" @apply="emit('save', overrides, [...modelDefaultKeys])"
      @preset="applyPreset" @update:tta="(value: boolean) => updateField('enable_tta', value)">
      <template #source>
        <span class="workflow-parameter-model">
          {{ t('workflows.stepInferenceContext', { id: target.id }) }} · <span>{{ target.model || t('workflows.stepModelPlaceholder') }}</span>
        </span>
      </template>
      <template #context>
        <p>{{ t(target.linked || Object.keys(target.defaults).length ? 'workflows.inferenceLinkedParams' : 'workflows.inferenceInheritParams') }}</p>
      </template>
      <template #tta-label>
        <small v-if="!Object.hasOwn(overrides, 'enable_tta')" class="workflow-parameter-follow">{{ t('workflows.followingSuffix') }}</small>
      </template>
      <template #tta-actions>
        <n-button v-if="Object.hasOwn(overrides, 'enable_tta')" text size="tiny" :disabled="busy"
          :aria-label="t('workflows.restoreField', { field: t('separate.tta') })"
          @click="updateField('enable_tta', null)">{{ t('workflows.restoreInheritance') }}</n-button>
      </template>
      <div v-if="busy" role="status"><n-spin :size="14" /> {{ t('common.loading') }}</div>
      <div v-else-if="missingCatalogConfig" class="workflow-config-action">
        <span>{{ t('workflows.inheritedConfigHint') }}</span>
        <n-button text size="small" type="primary" @click="loadMetadata(true)">{{ t('workflows.fetchParameterConfig') }}</n-button>
      </div>
      <n-alert v-if="configError" type="error" :bordered="false">{{ configError }}</n-alert>
      <InferenceParameterFields v-if="!target.dynamic && !missingCatalogConfig" :metadata="metadata" :node="{ parameters: nodeParameters, overrides, modelDefaultKeys }"
        :disabled="busy" @update:node-field="updateField" />
      <div v-else class="workflow-inference-grid">
        <div v-for="field in fields" :key="field.key" class="workflow-inference-field">
          <div class="workflow-field-title">
            <label :for="'step-param-' + field.key">{{ field.label }}<small v-if="!Object.hasOwn(overrides, field.key)">{{ t('workflows.followingSuffix') }}</small></label>
            <n-popover v-if="field.help" trigger="click" placement="top" style="max-width: min(260px, 80vw)">
              <template #trigger>
                <n-button text size="tiny" :aria-label="field.label + ' · ' + t('parameterHelp.label')"><n-icon :component="InformationCircleOutline" /></n-button>
              </template>
              {{ t(field.help) }}
            </n-popover>
          </div>
          <WorkflowNumberInput v-if="field.kind === 'number'" :label="field.label" clearable
            :id="'step-param-' + field.key" :placeholder="unknownValue" :inherited="typeof field.effective === 'number' ? field.effective : undefined" v-bind="field.increment"
            :value="typeof overrides[field.key] === 'number' && !modelDefaultKeys.has(field.key) ? Number(overrides[field.key]) : null"
            :min="field.min" :max="field.max" :precision="field.key === 'post_process_threshold' ? undefined : 0"
            :disabled="busy || !field.editable" @update:value="(value: number | null) => updateField(field.key, value)" />
          <AppSelect v-else :id="'step-param-' + field.key" size="small" :aria-label="field.label"
            :disabled="busy || !field.editable" :options="booleanOptions(field.key)"
            :value="overrides[field.key] === undefined ? 'inherit' : overrides[field.key] ? 'on' : 'off'"
            @update:value="(value: string) => updateField(field.key, value === 'inherit' ? null : value === 'on')">
            <template #selected-value>{{ displayBoolean(field.effective) }}</template>
          </AppSelect>
          <n-button v-if="Object.hasOwn(overrides, field.key) && (!field.editable || modelDefaultKeys.has(field.key))"
            text size="tiny" @click="updateField(field.key, null)">{{ t('workflows.inferenceFollowGlobalShort') }}</n-button>
        </div>
      </div>
      <n-alert v-if="invalid" type="error" :bordered="false">{{ t('workflows.invalidInferenceNumber', { parameter: fields.find(field => field.key === invalid?.[0])?.label, value: String(invalid[1]) }) }}</n-alert>
      <n-alert v-if="chunkIssue" type="warning" :bordered="false">{{ t('inference.invalidChunkSize', chunkIssue) }}</n-alert>
      <n-alert v-for="issue in current.issues.filter(issue => issue.code !== 'metadataUnavailable' && !(chunkIssue && issue.key === 'chunk_size'))"
        :key="issue.key + issue.code" type="warning" :bordered="false">{{ t(`parameterEditor.error.${issue.code}`, { field: fields.find(field => field.key === issue.key)?.label ?? issue.key }) }}</n-alert>
    </InferenceSettingsCard>
  </n-modal>
</template>

<style scoped>
.workflow-parameter-model { font-size: 12px; color: var(--on-surface-muted); overflow-wrap: anywhere; }
.workflow-parameter-follow { color: var(--on-surface-muted); font-size: 11px; }
.workflow-inference-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.workflow-inference-field { display: grid; align-content: start; gap: 6px; min-width: 0; padding: 8px 10px; background: var(--surface-2); border-radius: 8px; }
.workflow-field-title { display: flex; justify-content: space-between; align-items: center; gap: 4px; font-weight: 500; }
.workflow-field-title button { color: var(--on-surface-muted); }
.workflow-field-title small, .workflow-config-action { color: var(--on-surface-muted); font-size: 12px; font-weight: 400; }
.workflow-config-action { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 10px; }
.workflow-inference-field > button { justify-self: start; }
</style>
