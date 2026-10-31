<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { OptionsOutline, FolderOpenOutline } from '@vicons/ionicons5'
import { call, toolRef } from '@/sdk'
import AudioToolHeader from '@/features/audio-tools/shared/AudioToolHeader.vue'
import ToolPanelShell from './ToolPanelShell.vue'
import ToolActivityDialog from './ToolActivityDialog.vue'
import { schemaDefaults, supportsOperationSchema, validateParameters, type OperationSchema } from './operationSchema'
import { operationDescriptions } from './operationDescriptions'

type Port = { type?: string }
type Description = { id: string; inputs: Record<string, Port>; params: OperationSchema; outputs: Record<string, Port>; specDigest?: string }
type Output = { kind?: string; path?: string; value?: unknown }
const props = defineProps<{ config: { titleKey: string; descriptionKey?: string; operations: Array<{ id: string; titleKey: string; inputs?: string[] }>; layout?: 'split' | 'stacked' } }>()
const { t, te } = useI18n()
const savedOperation = toolRef('operation', props.config.operations[0]?.id || '')
const operationId = ref(props.config.operations.some(item => item.id === savedOperation.value) ? savedOperation.value : props.config.operations[0]?.id || '')
const paths = toolRef<Record<string, string>>('audioPaths', {})
const spec = ref<Description | null>(null)
const params = ref<Record<string, unknown>>({})
const error = ref('')
const results = ref<Record<string, Output> | null>(null)
const loading = ref(false), running = ref(false), cancelling = ref(false), cancelled = ref(false)
const progress = ref<number | null>(null)
const progressMessage = ref('')
const activity = ref<Array<{ time: string; message: string }>>([])
function recordActivity(message: string) {
  if (!message || activity.value.at(-1)?.message === message) return
  activity.value.push({ time: new Date().toLocaleTimeString([], { hour12: false }), message })
  if (activity.value.length > 200) activity.value.shift()
}
const elapsed = ref(0)
const picking = ref(false)
let generation = 0, disposed = false, activeId: number | null = null
let elapsedTimer: ReturnType<typeof setInterval> | undefined
const elapsedLabel = computed(() => `${String(Math.floor(elapsed.value / 60)).padStart(2, '0')}:${String(elapsed.value % 60).padStart(2, '0')}`)
function stopElapsed() { clearInterval(elapsedTimer); elapsedTimer = undefined }
const fields = computed(() => Object.entries(spec.value?.params.properties || {}))
const audioInputs = computed(() => spec.value ? Object.keys(spec.value.inputs) : props.config.operations.find(item => item.id === operationId.value)?.inputs || [])
const errors = computed(() => validateParameters(spec.value?.params, params.value))
const canRun = computed(() => !!spec.value && !loading.value && !running.value && !picking.value && audioInputs.value.every(name => typeof paths.value[name] === 'string' && paths.value[name].trim()) && !Object.keys(errors.value).length)
const stateLabel = computed(() => t(`pluginTools.${error.value ? 'failed' : running.value ? 'running' : cancelled.value ? 'cancelled' : results.value ? 'completed' : loading.value ? 'loading' : 'ready'}`))
function parameterLabel(name: string, field: Record<string, unknown>) {
  const key = `parameters.${name}`
  return te(key) || te(key, 'en') ? t(key) : typeof field.title === 'string' ? field.title : name
}
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const forcedDescriptions = new Set<string>()
const descriptions = operationDescriptions(async (operation: string) => {
  const value = await call<Description>('operations.describe', { operation, ...(forcedDescriptions.has(operation) ? { refresh: true } : {}) }).promise
  if (value?.specDigest !== undefined && (typeof value.specDigest !== 'string' || !/^[0-9a-f]{64}$/.test(value.specDigest))) throw new Error(t('pluginTools.invalidSpec'))
  if (!value || value.id !== operation || !record(value.inputs) || !record(value.outputs) || !record(value.params) || !record(value.params.properties) || !Object.values(value.inputs).every(port => record(port) && port.type === 'audio') || !supportsOperationSchema(value.params)) throw new Error(t('pluginTools.invalidSpec'))
  forcedDescriptions.delete(operation)
  return value
})
const parametersByOperation = new Map<string, Record<string, unknown>>()
async function describe() {
  if (spec.value) parametersByOperation.set(spec.value.id, { ...params.value })
  const current = ++generation, operation = operationId.value
  spec.value = descriptions.peek(operation) || null; error.value = ''; results.value = null; progress.value = null; cancelled.value = false; loading.value = !spec.value
  savedOperation.value = operation
  if (spec.value) { params.value = parametersByOperation.get(operation) || schemaDefaults(spec.value.params); return }
  try {
    const value = await descriptions.get(operation)
    if (disposed || current !== generation) return
    spec.value = value
    params.value = parametersByOperation.get(operation) || schemaDefaults(value.params)
  } catch (failure) { if (!disposed && current === generation) error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { if (!disposed && current === generation) loading.value = false }
}
watch(operationId, describe, { immediate: true })
async function pickAudio(name: string) {
  const current = generation
  picking.value = true
  try {
    const selected = await call<{ path?: string } | null>('files.pickAudio').promise
    if (!disposed && current === generation && selected?.path) paths.value = { ...paths.value, [name]: selected.path }
  } catch (failure) { if (!disposed && current === generation) error.value = failure instanceof Error ? failure.message : String(failure) }
  finally { picking.value = false }
}
const enumValue = (name: string, values: unknown[]) => { const index = values.findIndex(value => Object.is(value, params.value[name])); return index < 0 ? null : index }
function chooseEnum(name: string, values: unknown[], index: number | null) { if (index === null) delete params.value[name]; else params.value[name] = values[index] }
async function run() {
  if (!canRun.value || !spec.value) return
  const snapshot = { operation: spec.value.id, inputs: Object.fromEntries(audioInputs.value.map(name => [name, { kind: 'file', path: paths.value[name].trim() }])), params: Object.fromEntries(Object.entries(params.value).filter(([, value]) => value !== undefined && value !== null && value !== '')), ...(spec.value.specDigest ? { expectedSpecDigest: spec.value.specDigest } : {}) }
  error.value = ''; results.value = null; cancelled.value = false; progress.value = null; running.value = true
  progressMessage.value = ''; elapsed.value = 0
  activity.value = []; recordActivity(t('pluginTools.running'))
  const startedAt = Date.now()
  elapsedTimer = setInterval(() => { elapsed.value = Math.floor((Date.now() - startedAt) / 1000) }, 1000)
  const job = call<{ operation: string; outputs: Record<string, Output> }>('operations.start', snapshot, update => {
    if (disposed || update.id !== activeId || (update.operation && update.operation !== snapshot.operation) || typeof update.fraction !== 'number' || !Number.isFinite(update.fraction)) return
    progress.value = Math.max(progress.value || 0, Math.min(1, Math.max(0, update.fraction)))
    if (typeof update.message === 'string') { progressMessage.value = update.message; recordActivity(update.message) }
  })
  activeId = job.id
  try {
    const response = await job.promise
    if (disposed) return
    if (response?.operation !== snapshot.operation || !record(response.outputs)) throw new Error(t('pluginTools.operationMismatch'))
    results.value = response.outputs; error.value = ''; progress.value = 1
    recordActivity(t('pluginTools.completed'))
  } catch (failure) {
    if (disposed) return
    const item = failure as Error & { code?: string }
    cancelled.value = item.code === 'TASK_CANCELLED'; error.value = cancelled.value ? '' : item.message
    if (item.code === 'OPERATION_SPEC_CHANGED') {
      descriptions.invalidate(snapshot.operation)
      forcedDescriptions.add(snapshot.operation)
      parametersByOperation.delete(snapshot.operation)
      spec.value = null
      await describe()
      if (disposed) return
      if (!error.value) error.value = t('pluginTools.definitionChanged')
    }
    recordActivity(cancelled.value ? t('pluginTools.cancelled') : error.value)
  } finally { stopElapsed(); elapsed.value = Math.floor((Date.now() - startedAt) / 1000); activeId = null; running.value = false; cancelling.value = false }
}
async function cancel() {
  if (activeId === null || cancelling.value) return
  cancelling.value = true
  try { if (!await call<boolean>('operations.cancel', { id: activeId }).promise) cancelling.value = false }
  catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure); cancelling.value = false }
}
async function reveal(path: string) { try { await call('files.reveal', { path }).promise } catch (failure) { error.value = failure instanceof Error ? failure.message : String(failure) } }
const outputText = (value: Output) => typeof value.value === 'string' ? value.value : JSON.stringify(value.value ?? value, null, 2)
onBeforeUnmount(() => { disposed = true; generation += 1; stopElapsed() })
</script>
<template>
  <ToolPanelShell layout="stacked" :show-status="Boolean(results)" :status-title="t('pluginTools.statusTitle')" :status-hint="t('pluginTools.statusHint')">
    <template #actions>
      <ToolActivityDialog :busy="running" :percentage="progress === null ? null : Math.round(progress * 100)">
        <div class="operation-status"><strong id="status" role="status" aria-live="polite">{{ stateLabel }}</strong><span>{{ elapsedLabel }}</span></div>
        <n-progress v-if="progress !== null" id="progress" type="line" :percentage="Math.round(progress * 100)" :show-indicator="false" />
        <small v-if="progress !== null" class="operation-detail">{{ Math.round(progress * 100) }}% <span v-if="running">{{ progressMessage }}</span></small>
        <div class="operation-activity" role="log" aria-live="polite">
          <p v-if="!activity.length">{{ t('tools.noActivityLog') }}</p>
          <div v-for="(entry, index) in activity" :key="index"><time>{{ entry.time }}</time><span>{{ entry.message }}</span></div>
        </div>
        <n-button v-if="running" id="cancel" secondary :loading="cancelling" :disabled="cancelling" @click="cancel">{{ t('common.cancel') }}</n-button>
      </ToolActivityDialog>
    </template>
    <AudioToolHeader :icon="OptionsOutline" :title="t(config.titleKey)" :description="t(config.descriptionKey || 'pluginTools.description')" />
    <div v-if="config.operations.length > 1" class="audio-tool-field"><label>{{ t('pluginTools.operation') }}</label>
      <n-select id="operation" v-model:value="operationId" :disabled="running" :options="config.operations.map(item => ({ label: t(item.titleKey), value: item.id }))" />
    </div>
    <div v-for="name in audioInputs" :key="name" class="audio-tool-field"><label :for="`input-${name}`">{{ t('pluginTools.audio') }}<span v-if="audioInputs.length > 1"> · {{ name }}</span></label>
      <div class="audio-tool-path"><n-input :id="`input-${name}`" v-model:value="paths[name]" :disabled="running || picking" :placeholder="t('tools.chooseAudio')" />
        <n-button secondary :disabled="running || picking" @click="pickAudio(name)"><template #icon><n-icon :component="FolderOpenOutline" /></template>{{ t('common.browse') }}</n-button>
      </div>
    </div>
    <div class="audio-tool-grid">
      <div v-for="[name, field] in fields" :key="name" class="audio-tool-field"><label :for="`param-${name}`">{{ parameterLabel(name, field) }}</label>
        <n-select v-if="Array.isArray(field.enum)" :id="`param-${name}`" :value="enumValue(name, field.enum)" :disabled="running" :options="field.enum.map((value, index) => ({ label: String(value), value: index }))" clearable @update:value="chooseEnum(name, field.enum as unknown[], $event)" />
        <n-checkbox v-else-if="field.type === 'boolean'" :id="`param-${name}`" v-model:checked="params[name] as boolean" :disabled="running">{{ parameterLabel(name, field) }}</n-checkbox>
        <n-input-number v-else-if="field.type === 'number' || field.type === 'integer'" :id="`param-${name}`" v-model:value="params[name] as number | null" :disabled="running" :min="field.minimum as number" :max="field.maximum as number" :step="field.type === 'integer' ? 1 : undefined" />
        <n-input v-else :id="`param-${name}`" v-model:value="params[name] as string" :disabled="running" />
        <small v-if="errors[name]" class="operation-error">{{ t('pluginTools.invalid') }}</small>
      </div>
    </div>
    <p v-if="loading" role="status" class="operation-detail">{{ t('pluginTools.loading') }}</p>
    <div class="audio-tool-actions"><n-button id="run" type="primary" :disabled="!canRun" :loading="running" @click="run">{{ t('pluginTools.run') }}</n-button></div>
    <n-alert v-if="error" id="error" type="error" :bordered="false">{{ error }}</n-alert>
    <n-alert v-else-if="cancelled" type="info" :bordered="false">{{ t('pluginTools.cancelled') }}</n-alert>
    <n-button v-if="!spec && !loading" id="retry" secondary @click="describe">{{ t('pluginTools.retry') }}</n-button>
    <template #status>
      <section v-if="results" id="results" class="tool-result-stack">
        <div v-for="(value, name) in results" :key="name" class="tool-result-section"><slot name="result" :port="name" :output="value" :operation="operationId"><strong>{{ name }}</strong>
          <template v-if="value.kind === 'audio_file' && value.path"><span class="operation-path">{{ value.path }}</span><n-button text size="small" @click="reveal(value.path)">{{ t('tools.openOutputLocation') }}</n-button></template>
          <pre v-else class="tool-result-json">{{ outputText(value) }}</pre></slot>
        </div>
      </section>
    </template>
  </ToolPanelShell>
</template>
<style scoped>
.operation-status { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:16px; color:var(--on-surface-muted); font-size:12px; }
.operation-status > span { font:11px var(--font-mono); font-variant-numeric:tabular-nums; }
.operation-detail { display:block; margin:8px 0 12px; color:var(--on-surface-muted); font-size:11px; overflow-wrap:anywhere; }
.operation-error { color:var(--danger); font-size:11px; }
.operation-path { overflow-wrap:anywhere; }
#progress,#error,#cancel,#retry { margin-bottom:12px; }
.operation-activity { max-height: 300px; overflow: auto; margin: 16px 0; border: 1px solid var(--outline); border-radius: 9px; padding: 8px 12px; }
.operation-activity > div { display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 12px; padding: 7px 0; font-size: 12px; border-bottom: 1px solid var(--outline); }
.operation-activity > div:last-child { border-bottom: 0; }
.operation-activity time { color: var(--on-surface-muted); font-family: var(--font-mono); }
.operation-activity span { white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
