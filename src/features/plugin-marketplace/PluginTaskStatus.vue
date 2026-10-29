<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { CheckmarkCircleOutline, AlertCircleOutline, StopCircleOutline } from '@vicons/ionicons5'
import { isPluginCommitPhase, type PluginActivity } from './composable'

const props = defineProps<{ task: PluginActivity }>()
const emit = defineEmits<{ cancel: [] }>()
const { t } = useI18n()
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | undefined
const active = computed(() => props.task.status === 'running' || props.task.status === 'cancelling')
const committing = computed(() => active.value && isPluginCommitPhase(props.task.phase))
const elapsed = computed(() => Math.max(0, Math.floor(((props.task.finishedAt ?? now.value) - props.task.startedAt) / 1000)))
const elapsedLabel = computed(() => `${String(Math.floor(elapsed.value / 60)).padStart(2, '0')}:${String(elapsed.value % 60).padStart(2, '0')}`)
const idle = computed(() => Math.max(0, Math.floor((now.value - props.task.updatedAt) / 1000)))
const phaseLabel = computed(() => t(active.value && props.task.status !== 'cancelling' ? `plugins.phases.${props.task.phase}` : `plugins.taskStates.${props.task.status}`))
const icon = computed(() => props.task.status === 'failed' ? AlertCircleOutline : props.task.status === 'cancelled' ? StopCircleOutline : CheckmarkCircleOutline)

watch([active, () => props.task.startedAt], () => {
  if (timer) clearInterval(timer)
  timer = undefined
  now.value = Date.now()
  if (active.value) timer = setInterval(() => { now.value = Date.now() }, 1000)
}, { immediate: true })
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <section class="plugin-activity" :class="`plugin-activity--${task.status}`" :aria-label="t('plugins.taskStatus')">
    <n-spin v-if="active" :size="18" class="plugin-activity-icon" />
    <n-icon v-else :component="icon" :size="22" class="plugin-activity-icon" />
    <div class="plugin-activity-body">
      <div class="plugin-activity-title"><strong>{{ t(`plugins.actions.${task.action}`) }}</strong><span v-if="task.name" :title="task.name">{{ task.name }}</span></div>
      <span role="status" aria-live="polite" :title="task.error || undefined">{{ phaseLabel }}</span>
      <p v-if="active && idle >= 15" class="plugin-activity-idle">{{ t('plugins.taskQuiet', { seconds: idle }) }}</p>
    </div>
    <span class="plugin-activity-time">{{ t('plugins.taskElapsed', { time: elapsedLabel }) }}</span>
    <n-button v-if="active" size="small" :loading="task.status === 'cancelling'" :disabled="task.status === 'cancelling' || committing" @click="emit('cancel')">{{ t(committing ? 'plugins.completingChanges' : task.status === 'cancelling' ? 'plugins.taskStates.cancelling' : 'common.cancel') }}</n-button>
  </section>
</template>

<style scoped>
.plugin-activity { display: flex; align-items: center; gap: 14px; padding: 16px 18px; border: 1px solid var(--outline); border-radius: 12px; background: var(--surface-1); font-size: 13px; }
.plugin-activity-icon { flex-shrink: 0; color: var(--primary); }
.plugin-activity-body { flex: 1; min-width: 0; line-height: 1.65; }
.plugin-activity-title { display: flex; align-items: baseline; gap: 10px; }
.plugin-activity-title span { color: var(--on-surface-muted); overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.plugin-activity-time { flex-shrink: 0; color: var(--on-surface-muted); font-variant-numeric: tabular-nums; }
.plugin-activity-idle { margin: 4px 0 0; color: var(--on-surface-muted); font-size: 12px; }
.plugin-activity--failed .plugin-activity-icon { color: var(--error-color, #d86262); }
.plugin-activity--cancelled .plugin-activity-icon { color: var(--on-surface-muted); }
@media (max-width: 640px) { .plugin-activity { flex-wrap: wrap; gap: 10px; }.plugin-activity-body { flex-basis: calc(100% - 34px); }.plugin-activity-time { margin-left: 28px; }.plugin-activity > .n-button { margin-left: auto; } }
</style>
