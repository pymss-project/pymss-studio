<script setup lang="ts">
/**
 * Generic plugin-operation tool page (round 5 vertical slice).
 *
 * Renders every registered operation discovered from the Python runtime:
 * file inputs, number/enum/boolean/string params generated from the
 * OperationSpec. One shared implementation — no per-plugin Vue code.
 */
import { computed, onActivated, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { pluginRevision } from '../plugin-marketplace/composable'
import { usePluginOps, type OperationParamSchema, type OperationSpec } from '../plugin-ops/composable'

const { t } = useI18n()
const message = useMessage()
const { state, loading, error, refresh, run } = usePluginOps()
watch(pluginRevision, refresh)

const selectedId = ref('')
const selected = computed<OperationSpec | null>(
  () => state.specs.find((s: OperationSpec) => s.id === selectedId.value) || null,
)

const fileInputs = ref<Record<string, string>>({})
const paramValues = ref<Record<string, unknown>>({})
const running = ref(false)
const runError = ref('')
const runResult = ref<{ kind: string; path?: string; value?: unknown }[] | null>(null)

watch(selected, (spec) => {
  fileInputs.value = {}
  paramValues.value = {}
  runError.value = ''
  runResult.value = null
  if (!spec) return
  for (const [name, schema] of Object.entries(spec.params?.properties || {}) as [string, OperationParamSchema][]) {
    paramValues.value[name] = schema.default !== undefined ? schema.default : null
  }
})

onActivated(refresh)

async function execute() {
  if (!selected.value || running.value) return
  running.value = true
  runError.value = ''
  runResult.value = null
  try {
    const inputs: Record<string, { kind: 'file'; path: string }> = {}
    for (const [name, path] of Object.entries(fileInputs.value)) {
      if (path) inputs[name] = { kind: 'file', path }
    }
    const params: Record<string, unknown> = {}
    for (const [name, value] of Object.entries(paramValues.value)) {
      if (value !== null && value !== undefined && value !== '') params[name] = value
    }
    const result = await run({ operation: selected.value.id, params, inputs })
    runResult.value = Object.values(result.outputs || {})
    message.success(t('tools.completed'))
  } catch (err) {
    if (err instanceof Error && err.message === 'cancelled') message.info(t('tasks.cancelSuccess'))
    else runError.value = err instanceof Error ? err.message : String(err)
  } finally {
    running.value = false
  }
}

async function reveal(path?: string) {
  if (!path) return
  try {
    const { invoke } = await import('@tauri-apps/api/core')
    await invoke('reveal_path', { path })
  } catch {
    // reveal is best-effort
  }
}
</script>

<template>
  <div class="plugin-ops-tool">
    <n-empty v-if="!loading && !state.available" :description="state.unavailableReason || t('tools.pluginOps.unavailable')" />
    <n-spin v-else-if="loading" class="plugin-ops-loading" />
    <template v-else-if="state.specs.length">
      <n-select
        v-model:value="selectedId"
        :options="state.specs.map((s: OperationSpec) => ({ label: s.title || s.id, value: s.id }))"
        :placeholder="t('tools.pluginOps.selectOperation')"
        filterable
        class="plugin-ops-select"
      />
      <template v-if="selected">
        <p v-if="selected.description" class="plugin-ops-description">{{ selected.description }}</p>

        <div v-for="(schema, name) in selected.inputs" :key="`in-${name}`" class="plugin-ops-field">
          <label v-if="schema.type === 'audio'">{{ t('tools.pluginOps.audioInput', { name }) }}</label>
          <n-input
            v-model:value="fileInputs[name]"
            :placeholder="t('tools.pluginOps.filePath')"
            clearable
          />
        </div>

        <div v-for="(schema, name) in (selected.params?.properties as Record<string, OperationParamSchema> || {})" :key="`p-${name}`" class="plugin-ops-field">
          <label>{{ name }}</label>
          <n-select
            v-if="schema.enum"
            v-model:value="paramValues[name]"
            :options="schema.enum.map((v: string | number) => ({ label: String(v), value: v }))"
          />
          <n-switch v-else-if="schema.type === 'boolean'" v-model:value="paramValues[name]" />
          <n-input-number
            v-else-if="schema.type === 'number' || schema.type === 'integer'"
            v-model:value="paramValues[name]"
            :min="schema.minimum"
            :max="schema.maximum"
          />
          <n-input v-else v-model:value="paramValues[name]" />

        </div>

        <n-button type="primary" :loading="running" :disabled="!selectedId" @click="execute">
          {{ t('tools.pluginOps.run') }}
        </n-button>

        <n-alert v-if="runError" type="error" class="plugin-ops-error">{{ runError }}</n-alert>
        <div v-if="runResult" class="plugin-ops-results">
          <div v-for="(out, i) in runResult" :key="i" class="plugin-ops-result">
            <template v-if="out.kind === 'audio_file' && out.path">
              <n-button text type="primary" @click="reveal(out.path)">{{ out.path }}</n-button>
            </template>
            <template v-else-if="out.kind === 'scalar'">{{ String(out.value) }}</template>
          </div>
        </div>
      </template>
    </template>
    <n-empty v-else :description="t('tools.pluginOps.noOperations')" />
    <n-alert v-if="error" type="warning" class="plugin-ops-error">{{ error }}</n-alert>
  </div>
</template>

<style scoped>
.plugin-ops-tool { display: flex; flex-direction: column; gap: 14px; }
.plugin-ops-select { max-width: 420px; }
.plugin-ops-description { margin: 0; opacity: 0.75; }
.plugin-ops-field { display: flex; flex-direction: column; gap: 4px; max-width: 420px; }
.plugin-ops-field > label { font-size: 13px; opacity: 0.8; }
.plugin-ops-error { margin-top: 8px; }
.plugin-ops-results { display: flex; flex-direction: column; gap: 4px; }
.plugin-ops-loading { align-self: center; margin-top: 40px; }
</style>
