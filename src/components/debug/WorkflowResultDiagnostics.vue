<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type { WorkflowResultSnapshot } from '@/utils/workflowResultDiagnostics'
import type { OutputDiagnosticSnapshot } from '@/features/diagnostics/businessDiagnostics'
defineProps<{ snapshot: WorkflowResultSnapshot; outputs: OutputDiagnosticSnapshot | null; loading: boolean; error: string }>()
const emit = defineEmits<{ inspect: [] }>()
const { t } = useI18n()
</script>

<template>
  <n-card class="workflow-result-diagnostics" size="small" :title="t('debugBusiness.workflowTitle')">
    <p class="workflow-result-diagnostics-hint">{{ t('debugBusiness.workflowHint') }}</p>
    <n-alert v-if="snapshot.workflow.lastMutationError" type="error">{{ snapshot.workflow.lastMutationError.code }} · {{ snapshot.workflow.lastMutationError.message }} · {{ snapshot.workflow.lastMutationError.at }}</n-alert>
    <n-alert v-if="snapshot.workflow.recoveryBackupPath" type="warning">{{ t('debugBusiness.recoveryBackup') }} <code>{{ snapshot.workflow.recoveryBackupPath }}</code></n-alert>
    <dl class="workflow-result-diagnostics-rows">
      <div><dt>{{ t('debugBusiness.library') }}</dt><dd>{{ snapshot.workflow.initialized ? snapshot.workflow.count : t('debug.stateUnknown') }}</dd></div>
      <div><dt>{{ t('debugBusiness.saving') }}</dt><dd>{{ t(snapshot.workflow.isSaving ? 'debugBusiness.pendingSave' : 'debugBusiness.noPendingSave') }}</dd></div>
      <div><dt>{{ t('debugBusiness.completed') }}</dt><dd>{{ snapshot.results.completedCount }}</dd></div>
      <div><dt>{{ t('debugBusiness.visible') }}</dt><dd>{{ snapshot.results.visibleCount }}</dd></div>
      <div><dt>{{ t('debugBusiness.hidden') }}</dt><dd>{{ snapshot.results.hiddenCount }}</dd></div>
    </dl>
    <details v-if="snapshot.results.tasks.length">
      <summary>{{ t('debugBusiness.recentResults') }}</summary>
      <div v-for="task in snapshot.results.tasks" :key="task.id" class="workflow-result-diagnostics-item">
        <code>{{ task.id }}</code> · {{ t(`debugBusiness.resultReason.${task.reason}`) }}
        <div>{{ t('debugBusiness.artifactCounts', { outputs: task.outputCount, files: task.fileCount, values: task.nodeValueCount }) }}</div>
      </div>
      <p v-if="snapshot.results.omittedTasks">{{ t('debugBusiness.omittedTasks', { count: snapshot.results.omittedTasks }) }}</p>
    </details>
    <div class="workflow-result-diagnostics-actions">
      <n-button size="small" secondary :disabled="loading || !snapshot.results.paths.length" :loading="loading" @click="emit('inspect')">{{ t('debugBusiness.inspectFiles') }}</n-button>
      <router-link to="/results">{{ t('debugBusiness.openResults') }}</router-link>
      <router-link to="/workflows">{{ t('debugBusiness.openWorkflows') }}</router-link>
    </div>
    <p class="workflow-result-diagnostics-hint">{{ t('debugBusiness.fileCheckHint') }}</p>
    <p v-if="snapshot.results.omittedPaths">{{ t('debugBusiness.omittedPaths', { count: snapshot.results.omittedPaths }) }}</p>
    <n-alert v-if="error" type="error">{{ error }}</n-alert>
    <template v-if="outputs">
      <p>{{ t('debugPlugins.checkedAt') }}: {{ outputs.checkedAt }}</p>
      <div v-for="file in outputs.files" :key="file.path" class="workflow-result-diagnostics-item">
        <n-tag size="small" :bordered="false" :type="file.state === 'present' ? 'success' : 'warning'">{{ t(`debugBusiness.fileState.${file.state}`) }}</n-tag>
        <code>{{ file.path }}</code><p v-if="file.error">{{ file.error }}</p>
      </div>
    </template>
    <p v-else>{{ t('debugBusiness.filesNotChecked') }}</p>
  </n-card>
</template>

<style scoped>
.workflow-result-diagnostics { margin-bottom: 16px; }
.workflow-result-diagnostics-rows > div { display: grid; grid-template-columns: 160px minmax(0, 1fr); gap: 12px; padding: 5px 0; }
.workflow-result-diagnostics-rows dt, .workflow-result-diagnostics-hint { color: var(--on-surface-muted); }
.workflow-result-diagnostics-rows dd { margin: 0; }
.workflow-result-diagnostics-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 14px; margin-top: 12px; }
.workflow-result-diagnostics-item { padding: 8px 0; border-bottom: 1px solid var(--border); overflow-wrap: anywhere; }
.workflow-result-diagnostics code { overflow-wrap: anywhere; }
@media (max-width: 600px) { .workflow-result-diagnostics-rows > div { grid-template-columns: 1fr; gap: 4px; } }
</style>
