<script setup lang="ts">
import { useI18n } from 'vue-i18n'

defineProps<{
  show: boolean
  workflowName: string
}>()

const emit = defineEmits<{
  'update:show': [value: boolean]
  reload: []
  'save-copy': []
  overwrite: []
}>()

const { t } = useI18n()

function resolve(action: 'reload' | 'save-copy' | 'overwrite') {
  emit('update:show', false)
  if (action === 'reload') emit('reload')
  else if (action === 'save-copy') emit('save-copy')
  else emit('overwrite')
}
</script>

<template>
  <n-modal :show="show" :mask-closable="false" @update:show="emit('update:show', $event)">
    <n-card :aria-label="t('workflows.revisionConflictTitle')"
      class="workflow-conflict-modal"
      :title="t('workflows.revisionConflictTitle')"
      :bordered="false"
      role="dialog"
      aria-modal="true"
    >
      <p>{{ t('workflows.revisionConflictHint', { name: workflowName }) }}</p>
      <template #footer>
        <div class="workflow-conflict-modal__actions">
          <n-button secondary @click="resolve('reload')">{{ t('workflows.reloadLatest') }}</n-button>
          <n-button secondary @click="resolve('save-copy')">{{ t('workflows.saveAsCopy') }}</n-button>
          <n-button type="warning" @click="resolve('overwrite')">{{ t('workflows.overwrite') }}</n-button>
        </div>
      </template>
    </n-card>
  </n-modal>
</template>

<style scoped>
.workflow-conflict-modal {
  width: min(520px, calc(100vw - 32px));
}

.workflow-conflict-modal p {
  margin: 0;
  color: var(--on-surface-muted);
  line-height: 1.65;
}

.workflow-conflict-modal__actions {
  display: flex;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 10px;
}
</style>
