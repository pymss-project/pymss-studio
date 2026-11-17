<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { ChevronDownOutline, CopyOutline, ExpandOutline } from '@vicons/ionicons5'
import TaskLogConsole from '@/components/TaskLogConsole.vue'
import { formatLogRecord, normalizeLogEntries, type LogEntry } from '@/utils/logRecord'
import { WORKFLOW_NODE_STATUS_KEYS, type WorkflowNodeState } from '@/workflows/workbench'

const props = withDefaults(defineProps<{ nodeId: string; nodeLabel?: string; state?: WorkflowNodeState; logs?: readonly LogEntry[]; pending?: boolean; compact?: boolean }>(), { compact: true })
const { t } = useI18n()
const message = useMessage()
const expanded = ref(false)
const copying = ref(false)
async function copyLogs() {
  if (copying.value || !props.logs?.length) return
  copying.value = true
  try {
    await navigator.clipboard.writeText(normalizeLogEntries(props.logs).map(formatLogRecord).join('\n'))
    message.success(t('workbench.logsCopied'))
  } catch {
    message.error(t('workbench.logsCopyFailed'))
  } finally {
    copying.value = false
  }
}
function guardLogKeys(event: KeyboardEvent) {
  if (event.key !== 'Escape' && event.key !== 'Tab') event.stopPropagation()
}
</script>

<template>
  <details v-if="state || logs?.length || pending" class="workflow-node-log" :data-node-log="nodeId" :data-node-status="state?.status || 'queued'" @pointerdown.stop @click.stop @dblclick.stop @contextmenu.stop @keydown="guardLogKeys" @wheel.stop>
    <summary>
      <span class="workflow-node-log__status">
        <template v-if="state?.summary">{{ t('workbench.completedFiles', { done: state.summary.completed, total: state.summary.total }) }}<template v-if="state.summary.failed"> · {{ t('workbench.failedFiles', { count: state.summary.failed }) }}</template><template v-if="state.summary.cancelled"> · {{ t('workbench.cancelledFiles', { count: state.summary.cancelled }) }}</template><template v-if="state.summary.skipped"> · {{ t('workbench.skippedFiles', { count: state.summary.skipped }) }}</template><template v-if="state.summary.unknown"> · {{ t('workbench.missingNodeRecords', { count: state.summary.unknown }) }}</template></template>
        <template v-else>{{ t(state ? WORKFLOW_NODE_STATUS_KEYS[state.status] : 'tasks.statusQueued') }}<span v-if="state"> · {{ Math.round(state.progress) }}%</span></template>
      </span>
      <span class="workflow-node-log__label"><span>{{ t('tasks.logs') }}</span><small v-if="logs?.length">{{ logs.length }}</small><ChevronDownOutline class="workflow-node-log__chevron" aria-hidden="true" /></span>
    </summary>
    <div v-if="state" class="workflow-node-log__progress" role="progressbar" :aria-label="t('workbench.nodeProgress') + ' · ' + (nodeLabel || nodeId)" :aria-valuetext="`${Math.round(state.progress)}% · ${t(WORKFLOW_NODE_STATUS_KEYS[state.status])}`" :aria-valuenow="Math.round(state.progress)" :aria-valuemin="0" :aria-valuemax="100"><i :style="{ width: Math.min(100, Math.max(0, state.progress)) + '%' }" /></div>
    <div class="workflow-node-log__actions">
      <n-button size="tiny" quaternary :disabled="!logs?.length" :loading="copying" @click="copyLogs"><template #icon><n-icon :component="CopyOutline" /></template>{{ t('workbench.copyLogs') }}</n-button>
      <n-button size="tiny" quaternary :disabled="!logs?.length" aria-haspopup="dialog" :aria-expanded="expanded" @click="expanded = true"><template #icon><n-icon :component="ExpandOutline" /></template>{{ t('workbench.expandLogs') }}</n-button>
    </div>
    <TaskLogConsole :lines="logs || []" :compact="compact" />
  </details>
  <n-modal :aria-label="t('workbench.nodeLogsTitle', { node: nodeLabel || nodeId })" v-model:show="expanded" preset="card" :title="t('workbench.nodeLogsTitle', { node: nodeLabel || nodeId })" class="workflow-node-log-modal" style="width: min(1040px, calc(100vw - 48px))" @pointerdown.stop @click.stop @dblclick.stop @contextmenu.stop @keydown="guardLogKeys" @wheel.stop>
    <div class="workflow-node-log__actions">
      <n-button size="small" secondary :disabled="!logs?.length" :loading="copying" @click="copyLogs"><template #icon><n-icon :component="CopyOutline" /></template>{{ t('workbench.copyLogs') }}</n-button>
    </div>
    <TaskLogConsole :lines="logs || []" />
  </n-modal>
</template>

<style scoped>
.workflow-node-log { margin-top: 10px; border-top: 1px solid var(--outline); min-width: 0; color: var(--on-surface-muted); }
.workflow-node-log summary { display: flex; align-items: center; justify-content: space-between; gap: 6px; min-height: 28px; cursor: pointer; font-size: 10px; list-style: none; }
.workflow-node-log summary::-webkit-details-marker { display: none; }
.workflow-node-log__actions { display: flex; align-items: center; justify-content: flex-end; gap: 6px; margin-bottom: 6px; }
.workflow-node-log__status { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.workflow-node-log__label { display: inline-flex; align-items: center; gap: 5px; flex-shrink: 0; line-height: 1; white-space: nowrap; }
.workflow-node-log__label small { display: inline-flex; align-items: center; justify-content: center; min-width: 14px; height: 16px; box-sizing: border-box; font-size: 9px; padding: 0 4px; background: var(--surface-2); border-radius: 4px; }
.workflow-node-log__chevron { display: block; flex: 0 0 12px; width: 12px; height: 12px; transition: transform 120ms; }
.workflow-node-log[open] .workflow-node-log__chevron { transform: rotate(180deg); }
.workflow-node-log__progress { height: 3px; border-radius: 3px; background: var(--surface-2); overflow: hidden; margin-bottom: 6px; }
.workflow-node-log__progress i { display: block; height: 100%; background: var(--primary); }
.workflow-node-log:not([open]) .workflow-node-log__progress { margin-bottom: 0; }
.workflow-node-log[data-node-status="done"] .workflow-node-log__status { color: var(--success, #44936b); }
.workflow-node-log[data-node-status="running"] .workflow-node-log__status { color: var(--primary-strong); font-weight: 600; }
.workflow-node-log[data-node-status="failed"] .workflow-node-log__status { color: var(--danger); }
.workflow-node-log[data-node-status="failed"] .workflow-node-log__progress i { background: var(--danger); }
</style>
