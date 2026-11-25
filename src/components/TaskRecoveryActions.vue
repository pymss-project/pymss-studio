<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import type { SeparationTask } from '@/stores/task'
import { taskRecoveryAction } from '@/features/tasks/recovery'

const props = defineProps<{ task: SeparationTask }>()
const emit = defineEmits<{ 'select-input': [] }>()
const { t } = useI18n()
const router = useRouter()
const action = computed(() => taskRecoveryAction(props.task))
const labels = {
  runtime: { hint: 'taskRecovery.runtimeHint', action: 'taskRecovery.runtimeAction' },
  parameters: { hint: 'taskRecovery.parametersHint', action: 'taskRecovery.parametersAction' },
  models: { hint: 'taskRecovery.modelsHint', action: 'taskRecovery.modelsAction' },
  input: { hint: 'taskRecovery.inputHint', action: 'taskRecovery.inputAction' },
}

function openRecovery() {
  if (action.value === 'input') emit('select-input')
  else if (action.value === 'runtime') void router.push({ path: '/settings', query: { section: 'runtime' } })
  else if (action.value === 'parameters') void router.push({ path: '/models', query: { parameters: props.task.model } })
  else if (action.value === 'models') void router.push('/models')
}
</script>

<template>
  <div v-if="action" class="task-recovery" role="note">
    <p>{{ t(labels[action].hint) }}</p>
    <n-button secondary @click="openRecovery">{{ t(labels[action].action) }}</n-button>
  </div>
</template>

<style scoped>
.task-recovery { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; padding: 12px; border-radius: 10px; background: var(--surface-2); }
.task-recovery p { flex: 1 1 240px; margin: 0; color: var(--on-surface-muted); font-size: 13px; }
</style>
