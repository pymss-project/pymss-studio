<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { NetworkCheck, NetworkDiagnosticSnapshot } from '@/features/diagnostics/networkDiagnostics'
const props = defineProps<{ snapshot: NetworkDiagnosticSnapshot | null; busy: boolean; disabled: boolean }>()
const emit = defineEmits<{ check: [] }>()
const { t } = useI18n()
const checks = computed<NetworkCheck[]>(() => props.snapshot?.checks || (['models', 'pypi', 'plugins', 'updates'] as const).map(id => ({ id, status: 'not_checked' })))
</script>

<template>
  <n-card class="network-diagnostics" size="small" :title="t('debugSystem.networkTitle')">
    <template #header-extra><n-button size="small" secondary :loading="busy" :disabled="busy || disabled" @click="emit('check')">{{ t('debugSystem.checkNetwork') }}</n-button></template>
    <p>{{ t('debugSystem.networkHint') }}</p>
    <p v-if="busy && !snapshot">{{ t('debugSystem.finishingPreviousCheck') }}</p>
    <p v-if="snapshot">{{ t('debugPlugins.checkedAt') }}: {{ snapshot.checkedAt }} · {{ t('debugSystem.proxyMode') }}: {{ snapshot.configuration.proxyMode }} / {{ snapshot.observedProxyMode || t('debug.stateUnknown') }}</p>
    <div v-for="check in checks" :key="check.id" class="network-diagnostics-row">
      <div><strong>{{ t(`debugSystem.networkTargets.${check.id}`) }}</strong> <n-tag size="small" :bordered="false" :type="check.status === 'ok' ? 'success' : check.status === 'failed' || check.status === 'timeout' ? 'warning' : 'default'">{{ t(`debugSystem.networkStates.${check.status}`) }}</n-tag></div>
      <p v-if="check.source">{{ t('debugSystem.resolvedSource') }}: {{ check.source }}<span v-if="check.host"> · {{ check.host }}</span></p>
      <p v-if="check.sourcePreference">{{ t('debugSystem.sourcePreference') }}: {{ check.sourcePreference }}</p>
      <p v-if="check.elapsedMs !== undefined">{{ Math.round(check.elapsedMs) }} ms<span v-if="check.httpStatus"> · HTTP {{ check.httpStatus }}</span></p>
      <p v-if="check.gitAvailable !== undefined">{{ t('debugSystem.gitAvailable') }}: {{ check.gitAvailable }}</p>
      <p v-if="check.catalogCount !== undefined">{{ t('debugSystem.catalogCount') }}: {{ check.catalogCount }}</p>
      <p v-if="check.outcome">{{ t(`debugSystem.updateOutcome.${check.outcome}`) }}</p>
      <p v-if="check.error" class="network-diagnostics-error">{{ check.error }}</p>
    </div>
  </n-card>
</template>

<style scoped>
.network-diagnostics { margin-bottom: 16px; }
.network-diagnostics-row { padding: 10px 0; border-top: 1px solid var(--border); overflow-wrap: anywhere; }
.network-diagnostics-row p { margin: 6px 0; color: var(--on-surface-muted); }
.network-diagnostics-row .network-diagnostics-error { color: var(--danger); white-space: pre-wrap; }
</style>
