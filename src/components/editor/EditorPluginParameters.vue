<script setup lang="ts">
import { computed, onScopeDispose, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { EditorSession } from '@/types/editor'
import { getPluginParameters, observePluginSession, PluginHostDialogPendingError, setPluginParameter, type PluginParameter } from '@/utils/pluginHost'

const props = defineProps<{ session: EditorSession; slotId: string; disabled?: boolean }>()
const { t } = useI18n()
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const search = ref('')
const page = ref(1)
const params = ref<PluginParameter[]>([])
const values = ref<Record<string, number>>({})
const drafts = ref<Record<string, number | null>>({})
const pageSize = 12
let generation = 0
let waitingForDialog = false
const filtered = computed(() => params.value.filter(param => `${param.name} ${param.id}`.toLocaleLowerCase().includes(search.value.toLocaleLowerCase())))
const visible = computed(() => filtered.value.slice((page.value - 1) * pageSize, page.value * pageSize))
watch(search, () => { page.value = 1 })
watch(() => [props.session, props.slotId, props.disabled] as const, () => { void refresh() }, { immediate: true })
const stopObservingSession = observePluginSession(host => {
  if (waitingForDialog && host?.project === props.session && !host.interactionRequired && !host.unavailable) void refresh()
})
onScopeDispose(() => { generation++; waitingForDialog = false; stopObservingSession() })

async function refresh() {
  const current = ++generation
  waitingForDialog = false
  params.value = []; values.value = {}; drafts.value = {}; error.value = ''; page.value = 1
  saving.value = false
  if (props.disabled) { loading.value = false; return }
  loading.value = true
  try {
    const response = await getPluginParameters(props.session, props.slotId)
    if (current !== generation) return
    const seen = new Set<string>()
    params.value = response.params.filter(param => {
      if (!param || typeof param.id !== 'string' || typeof param.name !== 'string' || seen.has(param.id)
        || ![param.min, param.max, param.default].every(Number.isFinite) || param.min > param.max) return false
      seen.add(param.id)
      return true
    }).slice(0, 4096).map(param => ({ ...param, choices: Array.isArray(param.choices) ? param.choices : [] }))
    for (const param of params.value) {
      const value = response.values[param.id]
      values.value[param.id] = Math.min(param.max, Math.max(param.min, Number.isFinite(value) ? value : param.default))
    }
    drafts.value = { ...values.value }
  } catch (cause) { if (current === generation) { error.value = cause instanceof Error ? cause.message : String(cause); waitingForDialog = cause instanceof PluginHostDialogPendingError } }
  finally { if (current === generation) loading.value = false }
}
function choiceOptions(param: PluginParameter) {
  return param.choices.map((label, value) => ({ label: String(label), value })).filter(option => option.value >= param.min && option.value <= param.max)
}
function hasChoices(param: PluginParameter) { return param.unit === 'Choice' && param.choices.length > 0 && param.choices.length <= 256 }
function unitLabel(param: PluginParameter) {
  return ({ Db: 'dB', Hz: 'Hz', Ms: 'ms', Percent: '%', Ratio: ':1', Semitones: 'st', Cents: 'ct', Seconds: 's' } as Record<string, string>)[param.unit] || ''
}
function formatValue(value: number | null) {
  return value === null || !Number.isFinite(value) ? '' : Number(value.toPrecision(6)).toString()
}
function parseValue(param: PluginParameter, input: string) {
  const value = drafts.value[param.id] ?? null
  // Formatting must not turn focusing and blurring an unchanged field into a parameter edit.
  if (input === formatValue(value)) return value
  const parsed = input.trim() ? Number(input) : null
  return parsed !== null && Number.isFinite(parsed) ? parsed : null
}
async function commit(param: PluginParameter, raw: number | null) {
  if (saving.value || loading.value || props.disabled) return
  if (raw === null || !Number.isFinite(raw)) { drafts.value[param.id] = values.value[param.id]; return }
  const bounded = Math.min(param.max, Math.max(param.min, raw))
  const value = param.unit === 'Choice' || param.unit === 'Toggle' ? Math.min(param.max, Math.max(param.min, Math.round(bounded))) : bounded
  drafts.value[param.id] = value
  if (value === values.value[param.id]) return
  const current = generation
  saving.value = true; error.value = ''
  try {
    const response = await setPluginParameter(props.session, props.slotId, param.id, value)
    if (current === generation) {
      const applied = Number.isFinite(response.value) ? response.value! : value
      values.value[param.id] = applied; drafts.value[param.id] = applied
    }
  } catch (cause) {
    if (current === generation) { error.value = cause instanceof Error ? cause.message : String(cause); waitingForDialog = cause instanceof PluginHostDialogPendingError; drafts.value[param.id] = values.value[param.id] }
  } finally { if (current === generation) saving.value = false }
}
</script>

<template>
  <div class="plugin-parameters" :aria-busy="loading || saving">
    <div class="plugin-parameters__toolbar">
      <n-input v-model:value="search" size="small" clearable :placeholder="t('editor.plugins.searchParameters')" :disabled="loading || disabled" />
      <n-button size="small" :loading="loading" :disabled="saving || disabled" @click="refresh">{{ t('editor.plugins.refreshParameters') }}</n-button>
    </div>
    <p v-if="error" class="plugin-parameters__message" role="alert">{{ error }}</p>
    <p v-else-if="!loading && !params.length" class="plugin-parameters__message">{{ t('editor.plugins.noParameters') }}</p>
    <p v-else-if="!loading && !filtered.length" class="plugin-parameters__message">{{ t('editor.plugins.noParameterMatches') }}</p>
    <div class="plugin-parameters__list">
    <div v-for="param in visible" :key="param.id" class="plugin-parameters__row">
      <span :title="`${param.name} (${param.id})`">{{ param.name }}<small v-if="unitLabel(param)"> · {{ unitLabel(param) }}</small></span>
      <n-switch v-if="param.unit === 'Toggle'" size="small" :value="values[param.id] >= param.min + (param.max - param.min) / 2" :disabled="saving || disabled || param.min === param.max" :aria-label="param.name" @update:value="(on: boolean) => commit(param, on ? param.max : param.min)" />
      <AppSelect v-else-if="hasChoices(param)" size="small" :value="values[param.id]" :options="choiceOptions(param)" :disabled="saving || disabled || param.min === param.max" :aria-label="param.name" @update:value="(value: number) => commit(param, value)" />
      <n-input-number v-else v-model:value="drafts[param.id]" size="small" :min="param.min" :max="param.max" :format="formatValue" :parse="(input: string) => parseValue(param, input)" :step="param.unit === 'Choice' ? 1 : undefined" :disabled="saving || disabled || param.min === param.max" :input-props="{ 'aria-label': param.name }" @blur="commit(param, drafts[param.id])" @keydown.enter="commit(param, drafts[param.id])" />
    </div>
    </div>
    <n-pagination v-if="filtered.length > pageSize" v-model:page="page" size="small" :page-size="pageSize" :item-count="filtered.length" :page-slot="5" />
  </div>
</template>

<style scoped>
.plugin-parameters { display: flex; flex-direction: column; gap: 8px; min-width: 0; min-height: 0; max-height: 280px; }
.plugin-parameters__list { display: grid; gap: 7px; min-height: 0; overflow: auto; }
.plugin-parameters__toolbar { display: flex; gap: 6px; min-width: 0; }
.plugin-parameters__toolbar, .plugin-parameters > .n-pagination { flex-shrink: 0; }
.plugin-parameters__toolbar > :first-child { min-width: 0; flex: 1; }
.plugin-parameters__row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(100px, 45%); align-items: center; gap: 8px; }
.plugin-parameters__row > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.plugin-parameters__row small, .plugin-parameters__message { color: var(--on-surface-muted); font-size: 12px; }
.plugin-parameters__row > :last-child { min-width: 0; }
.plugin-parameters__row :deep(input) { font-variant-numeric: tabular-nums; }
.plugin-parameters__message { margin: 0; overflow-wrap: anywhere; }
</style>
