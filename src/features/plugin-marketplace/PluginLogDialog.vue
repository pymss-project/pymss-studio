<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { CopyOutline, SearchOutline } from '@vicons/ionicons5'
import TaskLogConsole from '@/components/TaskLogConsole.vue'
import PluginTaskStatus from './PluginTaskStatus.vue'
import { logClock, type LogRecord } from '@/utils/logRecord'
import { getLogLineLevels } from '@/utils/taskLog'
import { pluginPhases, type PluginActivity, type PluginPhase } from './composable'

const props = defineProps<{ show: boolean; records: readonly LogRecord[]; task: PluginActivity | null; tasks?: readonly PluginActivity[] }>()
const emit = defineEmits<{ 'update:show': [value: boolean]; cancel: [id?: string] }>()
const { t } = useI18n()
const message = useMessage()
const search = ref('')
const selectedId = ref<string>()
const selectedTask = computed(() => props.tasks?.find(task => task.id === selectedId.value) || props.task)
const taskOptions = computed(() => [...(props.tasks || [])].reverse().map(task => ({ value: task.id, label: `${t(`plugins.actions.${task.action}`)} · ${task.name}` })))
const taskRecords = computed(() => props.records.filter(record => !selectedTask.value?.id || record.context.pluginTaskId === selectedTask.value.id))
const localized = computed(() => {
  const levels = getLogLineLevels(taskRecords.value.map(record => record.message))
  return taskRecords.value.map((record, index) => {
    const phase = record.context.pluginPhase
    const name = record.context.pluginName
    const outcome = record.context.pluginOutcome
    const text = pluginPhases.includes(phase as PluginPhase)
      ? `${t(`plugins.phases.${phase}`)}${typeof name === 'string' ? ` · ${name}` : ''}`
      : outcome === 'cancelled' || outcome === 'succeeded' ? t(`plugins.taskStates.${outcome}`) : record.message
    return { ...record, message: text, level: record.level === 'error' ? 'error' as const : levels[index] }
  })
})
const visible = computed(() => localized.value.filter(record => `${record.message} ${record.detail || ''}`.toLowerCase().includes(search.value.trim().toLowerCase())))
async function copy() {
  try {
    await navigator.clipboard.writeText(visible.value.map(record => `${logClock(record.timestamp)} ${record.message}${record.detail ? `\n${record.detail}` : ''}`).join('\n'))
    message.success(t('plugins.logCopied'))
  } catch (error) { message.error(t('plugins.logCopyFailed', { error: String(error) })) }
}
</script>

<template>
  <n-modal :show="show" preset="card" :title="t('plugins.operationLog')" style="width: min(920px, calc(100vw - 32px)); max-height: calc(100dvh - 48px)" @update:show="emit('update:show', $event)">
    <div class="plugin-log-body">
      <n-select v-if="tasks && tasks.length > 1" :value="selectedTask?.id" :options="taskOptions" :aria-label="t('plugins.logTask')" @update:value="selectedId = $event" />
      <PluginTaskStatus v-if="selectedTask" :task="selectedTask" @cancel="emit('cancel', selectedTask?.id)" />
      <div class="plugin-log-toolbar">
        <n-input v-model:value="search" clearable :placeholder="t('plugins.logSearch')" size="small"><template #prefix><n-icon :component="SearchOutline" /></template></n-input>
        <n-button size="small" :disabled="!visible.length" @click="copy"><template #icon><n-icon :component="CopyOutline" /></template>{{ t('plugins.copyLog') }}</n-button>
      </div>
      <TaskLogConsole :lines="visible" compact />
      <div class="plugin-log-footer"><span>{{ t('plugins.logCount', { visible: visible.length, total: taskRecords.length }) }}</span><span>{{ t('plugins.logOriginalOutput') }}</span></div>
    </div>
  </n-modal>
</template>

<style scoped>
.plugin-log-body { display: grid; gap: 14px; min-width: 0; }
.plugin-log-toolbar, .plugin-log-footer { display: flex; align-items: center; gap: 12px; }
.plugin-log-toolbar > .n-input { flex: 1; min-width: 0; }
.plugin-log-footer { justify-content: space-between; flex-wrap: wrap; color: var(--on-surface-muted); font-size: 11px; }
:deep(.log-console) { display: flex; flex-direction: column; gap: 3px; min-height: 200px; max-height: min(46dvh, 400px); }
@media (max-width: 560px) { .plugin-log-toolbar { flex-wrap: wrap; }.plugin-log-toolbar > .n-input { flex-basis: 100%; } }
</style>
