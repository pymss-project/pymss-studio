<script setup lang="ts">
import { computed, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { useI18n } from 'vue-i18n'
import { useWorkflowStore } from '@/stores/workflow'
import { legacyWorkflowDescription, resolveWorkflowDescription } from '@/workflows/metadata'

const props = defineProps<{ show: boolean; workflowId: string }>()
const emit = defineEmits<{ 'update:show': [value: boolean]; edit: []; 'after-leave': [] }>()
const { t } = useI18n()
const message = useMessage()
const workflow = useWorkflowStore()
const current = computed(() => workflow.workflows.find(item => item.id === props.workflowId))
const text = computed(() => resolveWorkflowDescription(current.value))
const legacy = computed(() => legacyWorkflowDescription(current.value))
const copying = ref(false)
function close() { emit('update:show', false) }
async function copy() {
  if (copying.value) return
  copying.value = true
  try { await navigator.clipboard.writeText(text.value); message.success(t('common.copied')) }
  catch { message.error(t('common.copyFailed')) }
  finally { copying.value = false }
}
</script>

<template>
  <n-modal :show="show" preset="card" class="workflow-description-dialog"
    style="width: min(560px, calc(100vw - 48px)); max-height: calc(100dvh - 96px)"
    :title="t('workflows.workflowDescription')" :bordered="false"
    :content-style="{ minHeight: 0, overflow: 'auto' }" role="dialog" aria-modal="true"
    :aria-label="t('workflows.workflowDescription')" @update:show="(value: boolean) => { if (!value) close() }" @after-leave="emit('after-leave')">
    <p class="workflow-description-dialog__name">{{ current?.name }}</p>
    <p class="workflow-description-dialog__text">{{ text || t('workflows.descriptionEmpty') }}</p>
    <details v-if="legacy" class="workflow-description-dialog__legacy">
      <summary>{{ t('workflows.legacyDescription') }}</summary>
      <p class="workflow-description-dialog__hint">{{ t('workflows.legacyDescriptionHint') }}</p>
      <p class="workflow-description-dialog__text">{{ legacy }}</p>
    </details>
    <template #footer>
      <div class="workflow-description-dialog__footer">
        <n-button secondary :loading="copying" @click="copy">{{ t('common.copy') }}</n-button>
        <span class="workflow-description-dialog__spacer" />
        <n-button secondary @click="close">{{ t('common.close') }}</n-button>
        <n-button type="primary" :disabled="!current" @click="emit('edit')">{{ t('workflows.editDescription') }}</n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.workflow-description-dialog__name { margin: 0 0 16px; color: var(--on-surface-muted); font-size: 12px; overflow-wrap: anywhere; }
.workflow-description-dialog__text { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; user-select: text; line-height: 1.7; }
.workflow-description-dialog__hint { margin: 8px 0; color: var(--on-surface-muted); font-size: 12px; line-height: 1.6; }
.workflow-description-dialog__legacy { margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--outline); }
.workflow-description-dialog__legacy summary { cursor: pointer; color: var(--on-surface-muted); font-size: 12px; }
.workflow-description-dialog__footer { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.workflow-description-dialog__spacer { flex: 1; }
</style>
