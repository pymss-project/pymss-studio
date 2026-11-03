<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { formatBytes } from '@/utils/format'
import type { StorageDiagnosticSnapshot } from '@/features/diagnostics/storageDiagnostics'
defineProps<{ snapshot: StorageDiagnosticSnapshot | null; busy: boolean; error: string; disabled: boolean }>()
const emit = defineEmits<{ inspect: [writeProbe: boolean] }>()
const { t } = useI18n()
</script>

<template>
  <n-card class="storage-diagnostics" size="small" :title="t('debugSystem.storageTitle')">
    <template #header-extra><div class="storage-diagnostics-actions">
      <n-button size="small" secondary :loading="busy" :disabled="busy || disabled" @click="emit('inspect', false)">{{ t('debugSystem.inspectDirectories') }}</n-button>
      <n-button size="small" secondary :disabled="busy || disabled" @click="emit('inspect', true)">{{ t('debugSystem.verifyWrite') }}</n-button>
    </div></template>
    <p>{{ t('debugSystem.storageHint') }}</p>
    <n-alert v-if="error" type="error">{{ error }}</n-alert>
    <p v-if="!snapshot && !busy">{{ t('debugUpdates.notChecked') }}</p>
    <template v-if="snapshot">
      <p>{{ t('debugPlugins.checkedAt') }}: {{ snapshot.checkedAt }}</p>
      <div v-for="directory in snapshot.directories" :key="directory.id" class="storage-diagnostics-row">
        <strong>{{ t(`debugSystem.directoryIds.${directory.id}`) }}</strong> · {{ t(`debugSystem.directoryStates.${directory.state}`) }}
        <code>{{ directory.path }}</code>
        <p>{{ t('debugSystem.freeSpace') }}: {{ directory.availableBytes === null ? t('debug.stateUnknown') : formatBytes(directory.availableBytes) }}</p>
        <p>{{ t('debugSystem.writeStatus') }}: {{ t(`debugSystem.writeStates.${directory.writeStatus}`) }}</p>
        <p v-for="(detail, index) in [directory.error, directory.writeError, directory.cleanupError].filter(Boolean)" :key="index" class="storage-diagnostics-error">{{ detail }}</p>
        <n-alert v-if="directory.probePath" type="warning">{{ t('debugSystem.cleanupFailed') }} <code>{{ directory.probePath }}</code></n-alert>
      </div>
    </template>
  </n-card>
</template>

<style scoped>
.storage-diagnostics { margin-bottom: 16px; }
.storage-diagnostics-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.storage-diagnostics-row { border-top: 1px solid var(--border); padding: 10px 0; }
.storage-diagnostics-row > code { display: block; margin: 6px 0; }
.storage-diagnostics code { overflow-wrap: anywhere; }
.storage-diagnostics-row p { margin: 6px 0; color: var(--on-surface-muted); }
.storage-diagnostics-row .storage-diagnostics-error { color: var(--danger); white-space: pre-wrap; }
</style>
