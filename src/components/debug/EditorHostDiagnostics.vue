<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type { BusinessDiagnosticSnapshot } from '@/features/diagnostics/businessDiagnostics'
defineProps<{ snapshot: BusinessDiagnosticSnapshot | null; loading: boolean; error: string }>()
const emit = defineEmits<{ refresh: [] }>()
const { t, te } = useI18n()
function state(value: unknown) { const key = `debugBusiness.hostState.${String(value)}`; return te(key) ? t(key) : t('debug.stateUnknown') }
</script>

<template>
  <n-card class="editor-host-diagnostics" size="small" :title="t('debugBusiness.editorTitle')">
    <template #header-extra><n-button size="small" secondary :loading="loading" :disabled="loading" @click="emit('refresh')">{{ t('common.refresh') }}</n-button></template>
    <p class="editor-host-diagnostics-hint">{{ t('debugBusiness.editorHint') }}</p>
    <n-alert v-if="error" type="error">{{ error }}</n-alert>
    <template v-if="snapshot">
      <p>{{ t('debugPlugins.checkedAt') }}: {{ snapshot.checkedAt }}</p>
      <n-alert v-if="snapshot.windows.status !== 'observed'" type="warning">{{ state(snapshot.windows.status) }}</n-alert>
      <n-alert v-if="snapshot.windows.missing.length" type="warning">{{ t('debugBusiness.missingWindows') }} <code>{{ snapshot.windows.missing.join(', ') }}</code></n-alert>
      <p v-if="!snapshot.windows.expected.length">{{ t('debugBusiness.noEditorWindows') }}</p>
      <section v-for="window in snapshot.windows.observed" :key="window.windowLabel" class="editor-host-diagnostics-window">
        <strong>{{ window.windowLabel }}</strong><code>{{ window.projectId || window.workflowId }}</code>
        <p>{{ window.observedAt }}</p>
        <n-tag v-if="window.saving" size="small">{{ t('debugBusiness.pendingSave') }}</n-tag>
        <n-tag v-if="window.dirty" size="small" type="warning">{{ t('debugBusiness.unsaved') }}</n-tag>
        <n-tag v-if="window.revisionConflict" size="small" type="error">{{ t('debugBusiness.revisionConflict') }}</n-tag>
        <p v-if="window.kind === 'editor'">{{ t('debugBusiness.playback') }}: {{ window.playbackStatus }} · {{ t('debugBusiness.recording') }}: {{ window.recordingState }}</p>
        <p v-if="window.exporting">{{ t('debugBusiness.exporting') }}: {{ window.exportPhase }} {{ window.exportCompleted }} / {{ window.exportTotal }}</p>
        <p v-if="window.lastExportPath">{{ t('debugBusiness.lastExport') }}: <code>{{ window.lastExportPath }}</code></p>
        <p v-if="window.pluginStateDirty">{{ t('debugBusiness.pluginStateDirty') }}</p>
        <p v-if="window.pluginStateUnavailable">{{ t('debugBusiness.pluginStateUnavailable') }}</p>
        <p v-for="(detail, index) in [window.error, window.playbackError, window.recordingError, window.pluginStateError].filter(Boolean)" :key="index" class="editor-host-diagnostics-error">{{ detail }}</p>
      </section>
      <h3>{{ t('debugBusiness.nativeHost') }}</h3>
      <p>{{ t('debugBusiness.hostEnabled') }}: {{ snapshot.nativeHost.capabilities.enabled ? t('debug.stateOk') : t('debugUpdates.unsupported') }} · {{ snapshot.nativeHost.capabilities.formats.join(', ') || '—' }}</p>
      <n-alert v-if="snapshot.nativeHost.stopping" type="warning">{{ t('debugBusiness.hostStopping') }}</n-alert>
      <dl class="editor-host-diagnostics-rows">
        <div><dt>{{ t('debugBusiness.hostSession') }}</dt><dd>{{ state(snapshot.nativeHost.session.status) }}</dd></div>
        <div><dt>{{ t('debugBusiness.hostScan') }}</dt><dd>{{ state(snapshot.nativeHost.scan.status) }}<span v-if="snapshot.nativeHost.scan.total !== undefined"> · {{ snapshot.nativeHost.scan.completed }} / {{ snapshot.nativeHost.scan.total }}</span><code v-if="snapshot.nativeHost.scan.currentName"> · {{ snapshot.nativeHost.scan.currentName }}</code></dd></div>
        <div><dt>{{ t('debugBusiness.hostRenders') }}</dt><dd>{{ state(snapshot.nativeHost.renders.status) }}<span v-if="snapshot.nativeHost.renders.activeCount !== undefined"> · {{ snapshot.nativeHost.renders.activeCount }}</span></dd></div>
        <div><dt>{{ t('debugBusiness.hostCache') }}</dt><dd>{{ state(snapshot.nativeHost.storage.status) }}</dd></div>
      </dl>
      <details><summary>{{ t('debugBusiness.hostDetails') }}</summary><pre>{{ JSON.stringify(snapshot.nativeHost.storage, null, 2) }}</pre></details>
    </template>
    <p v-else-if="!loading && !error">{{ t('debugUpdates.notChecked') }}</p>
  </n-card>
</template>

<style scoped>
.editor-host-diagnostics { margin-bottom: 16px; }
.editor-host-diagnostics-hint, .editor-host-diagnostics-rows dt { color: var(--on-surface-muted); }
.editor-host-diagnostics-window { border-top: 1px solid var(--border); padding: 12px 0; }
.editor-host-diagnostics-window > code { display: block; margin-top: 6px; }
.editor-host-diagnostics code, .editor-host-diagnostics pre { overflow-wrap: anywhere; white-space: pre-wrap; }
.editor-host-diagnostics-rows > div { display: grid; grid-template-columns: 150px minmax(0, 1fr); gap: 12px; padding: 5px 0; }
.editor-host-diagnostics-rows dd { margin: 0; }
.editor-host-diagnostics-error { color: var(--danger); white-space: pre-wrap; }
@media (max-width: 600px) { .editor-host-diagnostics-rows > div { grid-template-columns: 1fr; gap: 4px; } }
</style>
