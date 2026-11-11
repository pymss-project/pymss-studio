<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { CopyOutline } from '@vicons/ionicons5'
import { useWorkflowStore } from '@/stores/workflow'
import { useWorkflowDescriptionDraft } from '@/composables/useWorkflowDescriptionDraft'
import { legacyWorkflowDescription, WORKFLOW_NOTES_MAX_LENGTH } from '@/workflows/metadata'

const props = defineProps<{ workflowId: string; disabled?: boolean }>()
const emit = defineEmits<{ 'keep-draft': [] }>()
const { t } = useI18n()
const message = useMessage()
const store = useWorkflowStore()
const model = useWorkflowDescriptionDraft(props.workflowId)
const editor = ref<{ focus: () => void; textareaElRef?: HTMLTextAreaElement } | null>(null)
const copying = ref(false)
const dirty = computed(model.dirty)
const tooLong = computed(model.tooLong)
const legacy = computed(() => legacyWorkflowDescription(store.workflows.find(item => item.id === props.workflowId)))
const status = computed(() => model.state.error ? t('workflows.descriptionDraftRetained')
  : model.state.saving ? t('workflows.autoSaveSaving') : dirty.value ? t('workflows.autoSavePending') : t('workflows.autoSaveSaved'))
async function focus() {
  await nextTick()
  editor.value?.focus()
  const input = editor.value?.textareaElRef
  input?.setSelectionRange(input.value.length, input.value.length)
}
async function copy() {
  copying.value = true
  try { await navigator.clipboard.writeText(model.state.text); message.success(t('common.copied')) }
  catch { message.error(t('common.copyFailed')) }
  finally { copying.value = false }
}
function appendLegacy() {
  model.update([model.state.text.trimEnd(), legacy.value].filter(Boolean).join('\n\n'))
}
function handleBlur() {
  if (!model.state.error) void model.flush()
}
defineExpose({ flush: model.flush, focus })
</script>

<template>
  <div class="workflow-description-editor">
    <div class="workflow-description-editor__label">
      <label :for="`workflow-description-${workflowId}`">{{ t('workflows.workflowDescription') }}</label>
      <n-button quaternary size="tiny" :loading="copying" :aria-label="t('common.copy')" :title="t('common.copy')" @click="copy">
        <template #icon><n-icon :component="CopyOutline" /></template>
      </n-button>
    </div>
    <n-input ref="editor" :value="model.state.text" type="textarea" :autosize="{ minRows: 6, maxRows: 10 }"
      :disabled="disabled" :status="tooLong || model.state.error ? 'error' : undefined" :placeholder="t('workflows.descriptionPlaceholder')"
      :input-props="{ id: `workflow-description-${workflowId}`, 'aria-label': t('workflows.workflowDescription') }"
      @update:value="model.update" @blur="handleBlur" />
    <div class="workflow-description-editor__status" :class="{ 'workflow-description-editor__status--error': model.state.error || tooLong }">
      <span role="status" aria-live="polite">{{ status }}</span>
      <span :class="{ 'workflow-description-editor__count--near': model.state.text.length > 3600 && !tooLong }">{{ model.state.text.length }} / {{ WORKFLOW_NOTES_MAX_LENGTH }}</span>
    </div>
    <p v-if="tooLong" class="workflow-description-editor__error" role="alert">{{ t('workflows.descriptionTooLong', { max: WORKFLOW_NOTES_MAX_LENGTH }) }}</p>
    <p v-else-if="model.state.error" class="workflow-description-editor__error" role="alert">{{ model.state.error }}</p>
    <div v-if="dirty && (model.state.error || tooLong)" class="workflow-description-editor__recovery">
      <n-button v-if="!tooLong" text type="primary" size="small" @click="model.flush">{{ t('common.retry') }}</n-button>
      <n-button text size="small" @click="emit('keep-draft')">{{ t('workflows.descriptionKeepDraftClose') }}</n-button>
    </div>
    <details v-if="legacy" class="workflow-description-editor__legacy">
      <summary>{{ t('workflows.legacyDescription') }}</summary>
      <p>{{ legacy }}</p>
      <n-button secondary size="small" :disabled="disabled || model.state.text.includes(legacy)" @click="appendLegacy">{{ t('workflows.appendLegacyDescription') }}</n-button>
    </details>
  </div>
</template>

<style scoped>
.workflow-description-editor { min-width: 0; display: grid; gap: 6px; }
.workflow-description-editor__label, .workflow-description-editor__status { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.workflow-description-editor__label label { color: var(--on-surface-muted); font-size: 12px; font-weight: 600; }
.workflow-description-editor__status { flex-wrap: wrap; color: var(--on-surface-muted); font-size: 12px; }
.workflow-description-editor__status--error, .workflow-description-editor__error { color: var(--danger); }
.workflow-description-editor__count--near { color: var(--warning); }
.workflow-description-editor__error { margin: 0; font-size: 12px; overflow-wrap: anywhere; }
.workflow-description-editor__recovery { display: flex; flex-wrap: wrap; gap: 12px; }
.workflow-description-editor__legacy { margin-top: 6px; color: var(--on-surface-muted); font-size: 12px; }
.workflow-description-editor__legacy summary { cursor: pointer; }
.workflow-description-editor__legacy p { white-space: pre-wrap; overflow-wrap: anywhere; user-select: text; }
</style>
