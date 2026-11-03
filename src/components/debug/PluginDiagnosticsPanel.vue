<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { PluginDiagnosticSnapshot } from '@/features/diagnostics/pluginDiagnostics'

const props = defineProps<{
  snapshot: PluginDiagnosticSnapshot | null
  loading: boolean
  verifying: boolean
  error: string
}>()
const emit = defineEmits<{ refresh: []; verify: [] }>()
const { t } = useI18n()
const rows = computed(() => props.snapshot ? [
  { label: t('debugPlugins.configuredPython'), value: props.snapshot.runtime.configuredPythonPath },
  { label: t('debugPlugins.workerPython'), value: props.snapshot.runtime.pythonPath },
  { label: t('debugPlugins.bindingDirectory'), value: props.snapshot.runtime.bindingDir },
  { label: t('debugPlugins.bindingIdentity'), value: props.snapshot.runtime.identity },
  { label: 'pymss', value: props.snapshot.runtime.pymssVersion },
  { label: t('debugPlugins.checkedAt'), value: props.snapshot.checkedAt },
] : [])
const compatible = computed(() => props.snapshot?.runtime.managementSupported && props.snapshot.runtime.pluginApisSupported)
const canVerify = computed(() => compatible.value && props.snapshot?.runtime.interpreterMatches !== false && !props.snapshot?.runtime.bindingError)
function state(loaded: boolean | null) {
  return t(loaded === true ? 'debugPlugins.verified' : loaded === false ? 'debugPlugins.failed' : 'debugPlugins.notVerified')
}
</script>

<template>
  <n-card class="plugin-diagnostics-panel" size="small" :title="t('debugPlugins.healthTitle')">
    <template #header-extra>
      <div class="plugin-diagnostics-actions">
        <n-button size="small" secondary :loading="loading && !verifying" :disabled="loading" @click="emit('refresh')">{{ t('common.refresh') }}</n-button>
        <n-button size="small" secondary :loading="verifying" :disabled="loading || !canVerify" @click="emit('verify')">{{ t('debugPlugins.verifyLoading') }}</n-button>
      </div>
    </template>
    <p class="plugin-diagnostics-hint">{{ t('debugPlugins.healthHint') }}</p>
    <n-alert v-if="error" type="error">{{ error }}</n-alert>
    <template v-if="snapshot">
      <n-alert v-if="snapshot.runtime.interpreterMatches === false" type="error">{{ t('debugPlugins.interpreterMismatch') }}</n-alert>
      <n-alert v-if="snapshot.runtime.bindingError" type="error">{{ snapshot.runtime.bindingError }}</n-alert>
      <n-alert v-if="!compatible" type="warning">{{ t('plugins.runtimeIncompatible') }} {{ snapshot.runtime.apiError }}</n-alert>
      <n-alert v-if="snapshot.verification.error" type="warning">{{ snapshot.verification.error }}</n-alert>
      <dl class="plugin-diagnostics-paths">
        <div v-for="row in rows" :key="row.label"><dt>{{ row.label }}</dt><dd><code>{{ row.value || t('debug.stateUnknown') }}</code></dd></div>
      </dl>
      <div v-if="snapshot.plugins.length" class="plugin-diagnostics-list">
        <div v-for="plugin in snapshot.plugins" :key="`${plugin.name}:${plugin.path}`" class="plugin-diagnostics-entry">
          <div class="plugin-diagnostics-entry-heading">
            <strong>{{ plugin.name }}</strong><span>{{ plugin.version || '—' }}</span>
            <n-tag size="small" :bordered="false" :type="!plugin.enabled ? 'default' : plugin.loaded === true ? 'success' : plugin.loaded === false ? 'error' : 'default'">{{ plugin.enabled ? state(plugin.loaded) : t('plugins.disabled') }}</n-tag>
          </div>
          <code>{{ plugin.path }}</code>
          <p v-if="plugin.error" class="plugin-diagnostics-error">{{ plugin.error }}</p>
        </div>
      </div>
      <n-empty v-else :description="t('debugPlugins.noInstalledPlugins')" />
    </template>
    <n-empty v-else-if="!loading && !error" :description="t('debug.stateUnknown')" />
    <router-link to="/plugins">{{ t('debugPlugins.openManager') }}</router-link>
  </n-card>
</template>

<style scoped>
.plugin-diagnostics-panel { margin-bottom: 16px; }
.plugin-diagnostics-actions, .plugin-diagnostics-entry-heading { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.plugin-diagnostics-hint { color: var(--on-surface-muted); }
.plugin-diagnostics-paths { margin: 16px 0; }
.plugin-diagnostics-paths > div { display: grid; grid-template-columns: minmax(110px, 150px) minmax(0, 1fr); gap: 12px; padding: 6px 0; }
.plugin-diagnostics-paths dt { color: var(--on-surface-muted); }
.plugin-diagnostics-paths dd { margin: 0; }
.plugin-diagnostics-panel code { overflow-wrap: anywhere; }
.plugin-diagnostics-list { display: grid; gap: 12px; margin-bottom: 12px; }
.plugin-diagnostics-entry { padding: 10px 0; border-top: 1px solid var(--border); }
.plugin-diagnostics-entry > code { display: block; margin-top: 6px; color: var(--on-surface-muted); }
.plugin-diagnostics-error { white-space: pre-wrap; color: var(--danger); }
@media (max-width: 600px) { .plugin-diagnostics-paths > div { grid-template-columns: 1fr; gap: 4px; } }
</style>
