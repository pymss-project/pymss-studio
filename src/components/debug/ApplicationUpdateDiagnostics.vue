<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { UpdateDiagnosticSnapshot } from '@/utils/updateDiagnostics'

const props = defineProps<{ snapshot: UpdateDiagnosticSnapshot }>()
const { t } = useI18n()
const integrityState = computed(() => {
  const integrity = props.snapshot.integrity
  if (props.snapshot.integrityChecking) return t('debugUpdates.status.checking')
  if (props.snapshot.integrityError) return t('debug.stateError')
  if (!integrity) return t('debugUpdates.notChecked')
  if (!integrity.supported) return t('debugUpdates.unsupported')
  return t(integrity.valid === true ? 'debug.stateOk' : integrity.valid === false ? 'debug.stateError' : 'debug.stateUnknown')
})
const rows = computed(() => [
  { label: t('debugUpdates.currentVersion'), value: props.snapshot.currentVersion },
  { label: t('debugUpdates.latestVersion'), value: props.snapshot.latestVersion },
  { label: t('debugUpdates.selectedSource'), value: props.snapshot.selectedSource ? { github: 'GitHub', modelscope: 'ModelScope', custom: t('debugUpdates.customSource') }[props.snapshot.selectedSource] : null },
  { label: t('debugUpdates.lastCheckedAt'), value: props.snapshot.lastCheckedAt },
  { label: t('debugUpdates.integrity'), value: integrityState.value },
  { label: t('debugUpdates.downloadBytes'), value: `${props.snapshot.downloadDownloadedBytes} / ${props.snapshot.downloadTotalBytes || '—'}` },
])
</script>

<template>
  <n-card class="update-diagnostics-panel" size="small" :title="t('debugUpdates.title')">
    <p>{{ t(`debugUpdates.status.${snapshot.status}`) }}</p>
    <p class="update-diagnostics-hint">{{ t('debugUpdates.hint') }}</p>
    <n-alert v-if="snapshot.error || snapshot.installFailed" type="error">{{ snapshot.error || t('debugUpdates.installFailed') }}</n-alert>
    <n-alert v-if="snapshot.restartRequired" type="warning">{{ t('debugUpdates.restartRequired') }}</n-alert>
    <n-alert v-if="snapshot.repairing" type="info">{{ t('debugUpdates.repairing') }}</n-alert>
    <n-alert v-if="snapshot.integrityError || snapshot.integrity?.detail" :type="snapshot.integrityError || snapshot.integrity?.valid === false ? 'warning' : 'info'">{{ snapshot.integrityError || snapshot.integrity?.detail }}</n-alert>
    <dl class="update-diagnostics-rows">
      <div v-for="row in rows" :key="row.label"><dt>{{ row.label }}</dt><dd>{{ row.value || t('debugUpdates.notChecked') }}</dd></div>
    </dl>
    <n-progress v-if="snapshot.status === 'downloading' && snapshot.downloadTotalBytes > 0 && snapshot.downloadProgressPercent !== null" type="line" :percentage="snapshot.downloadProgressPercent" />
    <details v-if="snapshot.downloadLogs.length" class="update-diagnostics-logs">
      <summary>{{ t('debugUpdates.recentLogs') }}</summary>
      <p v-for="(entry, index) in snapshot.downloadLogs" :key="index"><time>{{ entry.at }}</time> {{ entry.message }}</p>
    </details>
    <router-link to="/settings">{{ t('debugUpdates.openSettings') }}</router-link>
  </n-card>
</template>

<style scoped>
.update-diagnostics-panel { margin-bottom: 16px; }
.update-diagnostics-hint, .update-diagnostics-rows dt { color: var(--on-surface-muted); }
.update-diagnostics-rows > div { display: grid; grid-template-columns: minmax(110px, 150px) minmax(0, 1fr); gap: 12px; padding: 6px 0; }
.update-diagnostics-rows dd { margin: 0; overflow-wrap: anywhere; }
.update-diagnostics-logs { margin: 12px 0; }
.update-diagnostics-logs p { font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
.update-diagnostics-logs time { color: var(--on-surface-muted); }
@media (max-width: 600px) { .update-diagnostics-rows > div { grid-template-columns: 1fr; gap: 4px; } }
</style>
