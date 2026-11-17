<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useDialog, useMessage } from 'naive-ui'
import { DocumentTextOutline, TrashOutline } from '@vicons/ionicons5'
import TaskLogConsole from '@/components/TaskLogConsole.vue'
import WorkflowNodeLog from '@/components/workflow/WorkflowNodeLog.vue'
import WorkflowExecutionProgress from '@/components/workflow/WorkflowExecutionProgress.vue'
import WorkflowResultPreview from '@/components/workflow/WorkflowResultPreview.vue'
import { workflowBatchExecution, workflowTaskLabel, selectedWorkflowJob } from '@/workflows/batchExecution'
import { useTaskStore, type SeparationTask } from '@/stores/task'
import { useWorkbenchStore } from '@/stores/workbench'
import { isMainTaskOwner } from '@/utils/taskOwner'
import { requestWorkbenchAction } from '@/utils/workbenchBridge'
import { isTerminalTaskStatus, resolveJobStatus, type TaskStatus } from '@/features/tasks/lifecycle'

const props = withDefaults(defineProps<{
  workflowKey: string; submitting?: boolean; canRun?: boolean; size?: 'small' | 'medium'; runPrimary?: boolean; showStatus?: boolean
}>(), { canRun: true, size: 'medium', runPrimary: false, showStatus: true })
const jobId = defineModel<string>('jobId', { default: '' })
const emit = defineEmits<{ run: []; cancel: [] }>()
const task = useTaskStore()
const workbench = useWorkbenchStore()
const { t } = useI18n()
const message = useMessage()
const dialog = useDialog()
const showDetails = ref(false)
const detailsTab = ref('logs')
const session = computed(() => workbench.session(props.workflowKey))
const owner = isMainTaskOwner()
const jobs = computed(() => owner
  ? task.allJobs.filter(job => job.primary.runConfig?.workbench && job.primary.runConfig.workflowId === props.workflowKey)
  : workbench.remoteJobs[props.workflowKey] || [])
const currentJob = computed(() => selectedWorkflowJob(jobs.value, jobId.value))
watch(() => currentJob.value?.id, id => { if (!id) showDetails.value = false })
const batchTasks = computed(() => currentJob.value?.tasks || [])
const active = computed(() => batchTasks.value.some(item => !isTerminalTaskStatus(item.status)))
const canRemoveRun = computed(() => Boolean(currentJob.value) && !active.value
  && !batchTasks.value.some(item => item.status === 'done' && !item.resultHidden && (item.outputs.length || item.files.length)))
const removingRun = ref(false)
const batchStatus = computed(() => resolveJobStatus(batchTasks.value))
const batchLogs = computed(() => batchTasks.value.flatMap((item, index) => [
  ...(batchTasks.value.length > 1 ? [`[info] ── ${index + 1}. ${workflowTaskLabel(item)} ──`] : []),
  ...item.logs,
]))
const execution = computed(() => workflowBatchExecution(batchTasks.value))
const outputGroups = computed(() => batchTasks.value.filter(item => item.outputs.length).map(item => ({
  id: item.id, label: batchTasks.value.length > 1 ? workflowTaskLabel(item) : '', outputs: item.outputs,
})))
const outputCount = computed(() => outputGroups.value.reduce((total, group) => total + group.outputs.length, 0))
const outputDirectories = computed(() => [...new Set(batchTasks.value.filter(item => item.outputs.length)
  .flatMap(item => item.outputDirectories?.length ? item.outputDirectories : [item.output]).filter(Boolean))])
const unsuccessfulTasks = computed(() => batchTasks.value.filter(item => ['failed', 'cancelled'].includes(item.status)))
const retrying = ref('')
const retriedJobs = ref<Record<string, string>>({})
const statusKeys: Record<TaskStatus, string> = {
  queued: 'tasks.statusQueued', preparing: 'tasks.statusPreparing', separating: 'tasks.statusSeparating',
  done: 'tasks.statusDone', failed: 'tasks.statusFailed', cancelled: 'tasks.statusCancelled',
  validating_input: 'tasks.statusValidatingInput', downloading_model: 'tasks.statusCheckingModel',
  ensuring_model: 'tasks.statusCheckingModel', loading_model: 'tasks.statusLoadingModel', writing_output: 'tasks.statusWritingOutput',
}
const jobOptions = computed(() => [...jobs.value].sort((a, b) => b.createdAt - a.createdAt).map(job => ({
  value: job.id, label: new Date(job.primary.createdAt).toLocaleString() + ' · ' + job.inputCount + ' · ' + t(statusKeys[resolveJobStatus(job.tasks)]),
})))
async function revealOutput(path: string) {
  try { await task.revealPath(path) }
  catch (error) { message.error(String(error)) }
}
async function retry(item: SeparationTask) {
  if (retrying.value || retriedJobs.value[item.id]) return
  retrying.value = item.id
  try {
    const next = owner
      ? await task.retryTask(item.id).then(value => value ? { jobId: value.jobId || value.id } : null)
      : await requestWorkbenchAction({ action: 'retry', workflowKey: props.workflowKey, taskId: item.id })
    if (next?.jobId) retriedJobs.value[item.id] = next.jobId
  } catch (error) { message.error(String(error)) }
  finally { retrying.value = '' }
}

function removeCurrentRun() {
  const target = currentJob.value
  if (!target || !canRemoveRun.value || removingRun.value) return
  dialog.warning({
    title: t('workbench.removeRun'), content: t('workbench.removeRunContent'),
    positiveText: t('results.removeAction'), negativeText: t('common.cancel'), positiveButtonProps: { type: 'error' },
    onPositiveClick: async () => {
      removingRun.value = true
      try {
        if (owner) await task.removeWorkflowRun(props.workflowKey, target.id)
        else await requestWorkbenchAction({ action: 'remove-run', workflowKey: props.workflowKey, jobId: target.id })
        if (jobId.value === target.id) { jobId.value = ''; session.value.selectedTaskId = '' }
      } catch (error) { message.error(String(error)); return false }
      finally { removingRun.value = false }
    },
  })
}
</script>

<template>
  <span class="workflow-run-controls" data-workbench-run-panel>
    <span v-if="showStatus && currentJob" class="run-status" :data-run-status="batchStatus">
      {{ t(statusKeys[batchStatus]) }} · {{ Math.round(currentJob.progress) }}%
    </span>
    <n-button v-if="jobs.length" quaternary :size="size" class="run-details-trigger" aria-haspopup="dialog" :aria-expanded="showDetails" @click="showDetails = true">
      <template #icon><n-icon :component="DocumentTextOutline" /></template>
      {{ t('workbench.details') }}
    </n-button>
    <span class="run-actions">
      <n-button v-if="active" secondary type="error" :size="size" @click="emit('cancel')">{{ t('tasks.cancelAction') }}</n-button>
      <n-button :size="size" :type="runPrimary ? 'primary' : 'default'" :disabled="!canRun || submitting" :loading="submitting" @click="emit('run')">{{ t('workflows.runWorkflowAction') }}</n-button>
    </span>
    <n-modal :aria-label="t('workbench.details')" v-model:show="showDetails" preset="card" :title="t('workbench.details')" class="workflow-run-details-modal" style="width: min(960px, calc(100vw - 48px))">
      <div class="run-details" data-workbench-details>
        <div v-if="jobs.length > 1 || canRemoveRun" class="run-detail-selection">
          <label v-if="jobs.length > 1"><span>{{ t('workbench.chooseRun') }}</span><AppSelect :aria-label="t('workbench.chooseRun')" :value="currentJob?.id" :options="jobOptions" size="small" @update:value="jobId = $event; session.selectedTaskId = ''" /></label>
          <n-button v-if="canRemoveRun" size="small" secondary type="error" :loading="removingRun" :disabled="removingRun" @click="removeCurrentRun"><template #icon><n-icon :component="TrashOutline" /></template>{{ t('workbench.removeRun') }}</n-button>
        </div>
        <template v-if="currentJob">
          <WorkflowExecutionProgress :job="currentJob" />
          <div v-if="unsuccessfulTasks.length" class="run-failures">
            <section v-for="item in unsuccessfulTasks" :key="item.id" class="run-failure">
              <div><strong :title="item.input">{{ workflowTaskLabel(item) }}</strong><span>{{ item.error || t(statusKeys[item.status]) }}</span></div>
              <n-button v-if="retriedJobs[item.id]" size="small" secondary @click="jobId = retriedJobs[item.id]">{{ t('workbench.viewRetry') }}</n-button>
              <n-button v-else size="small" secondary :loading="retrying === item.id" :disabled="Boolean(retrying)" @click="retry(item)">{{ t('common.retry') }}</n-button>
            </section>
          </div>
          <n-tabs v-model:value="detailsTab" type="line" size="small">
            <n-tab-pane name="logs" :tab="t('tasks.logs')"><TaskLogConsole :lines="batchLogs" /></n-tab-pane>
            <n-tab-pane name="nodes" :tab="t('workbench.nodeExecution')">
              <div class="run-node-list">
                <section v-for="node in execution.nodes" :key="node.id" class="run-node-detail"><strong :title="node.label">{{ node.kind === 'input' ? t('workflows.originalInput') : node.kind === 'save' ? t('workflows.saveNode') : node.label }}</strong><WorkflowNodeLog :node-id="node.id" :node-label="node.kind === 'input' ? t('workflows.originalInput') : node.kind === 'save' ? t('workflows.saveNode') : node.label" :compact="false" :state="execution.states[node.id]" :logs="execution.logs[node.id]" pending /></section>
              </div>
            </n-tab-pane>
            <n-tab-pane name="results" :tab="t('nav.results') + ' · ' + outputCount">
              <WorkflowResultPreview v-if="showDetails && detailsTab === 'results'" :key="currentJob.id" :groups="outputGroups" :directories="outputDirectories" @reveal="revealOutput" />
            </n-tab-pane>
          </n-tabs>
        </template>
      </div>
    </n-modal>
  </span>
</template>

<style scoped>
.workflow-run-controls { display: inline-flex; align-items: center; gap: 8px; min-width: 0; }
.run-status { white-space: nowrap; color: inherit; font-size: 12px; }
.run-status[data-run-status="failed"] { color: var(--danger); }
.run-details-trigger { border-radius: 8px; font-weight: 500; }
.run-actions { display: inline-flex; align-items: center; gap: 12px; }
.run-details-trigger + .run-actions { padding-left: 10px; border-left: 1px solid var(--outline); }
.run-details { display: grid; gap: 16px; max-height: calc(100vh - 180px); overflow-y: auto; }
.run-detail-selection { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 12px; }
.run-detail-selection label { display: grid; gap: 6px; min-width: 0; flex: 1; max-width: 480px; }
.run-detail-selection label > span { color: var(--on-surface-muted); font-size: 11px; }
.run-node-list { display: grid; gap: 10px; }
.run-node-detail { padding: 12px; border: 1px solid var(--outline); border-radius: 10px; }
.run-node-detail > strong { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.run-node-detail :deep(.workflow-node-log) { margin-top: 6px; }
.run-node-detail :deep(.log-console) { max-height: 260px; }
.run-failures { display: grid; gap: 8px; }
.run-failure { display: flex; align-items: center; gap: 16px; padding: 10px 12px; border-radius: 8px; background: color-mix(in srgb, var(--danger) 7%, var(--surface)); }
.run-failure > div { flex: 1; min-width: 0; display: grid; gap: 4px; }
.run-failure strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.run-failure span { font-size: 11px; color: var(--danger); overflow-wrap: anywhere; }
@media (max-width: 640px) { .run-detail-selection { grid-template-columns: 1fr; } }
</style>
