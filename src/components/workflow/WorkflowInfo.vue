<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { CloseOutline, CopyOutline } from '@vicons/ionicons5'
import type { WorkflowMetadata } from '@/workflows/metadata'
import WorkflowDescriptionPreview from './WorkflowDescriptionPreview.vue'

const props = defineProps<{ workflowName: string; metadata: WorkflowMetadata; description: string }>()
const emit = defineEmits<{ close: []; 'edit-description': []; 'view-description': [] }>()
const { t } = useI18n()
const message = useMessage()
const panel = ref<HTMLElement | null>(null)
const copying = ref(false)

onMounted(() => { void nextTick(() => panel.value?.focus({ preventScroll: true })) })

async function copyGuid() {
  if (copying.value) return
  copying.value = true
  try {
    await navigator.clipboard.writeText(props.metadata.guid)
    message.success(t('common.copied'))
  } catch {
    message.error(t('common.copyFailed'))
  } finally {
    copying.value = false
  }
}
</script>

<template>
  <section ref="panel" class="workflow-info" role="dialog" :aria-label="t('workflows.workflowInfo')"
    tabindex="-1" @keydown.esc.stop.prevent="emit('close')">
    <header class="workflow-info__heading">
      <strong>{{ t('workflows.workflowInfo') }}</strong>
      <n-button size="tiny" quaternary circle :aria-label="t('common.close')" @click="emit('close')">
        <template #icon><n-icon :component="CloseOutline" /></template>
      </n-button>
    </header>
    <p class="workflow-info__name">{{ workflowName }}</p>
    <dl class="workflow-info__fields">
      <div v-if="metadata.author">
        <dt>{{ t('workflows.workflowAuthor') }}</dt>
        <dd>{{ metadata.author }}</dd>
      </div>
      <div v-if="metadata.version">
        <dt>{{ t('workflows.workflowReleaseVersion') }}</dt>
        <dd>{{ metadata.version }}</dd>
      </div>
      <div>
        <dt>GUID</dt>
        <dd class="workflow-info__guid">
          <code>{{ metadata.guid }}</code>
          <n-button size="tiny" quaternary circle :loading="copying" :aria-label="`${t('common.copy')} GUID`"
            :title="`${t('common.copy')} GUID`" @click="copyGuid">
            <template #icon><n-icon :component="CopyOutline" /></template>
          </n-button>
        </dd>
      </div>
      <div v-if="metadata.studioVersion">
        <dt>{{ t('workflows.workflowStudioVersion') }}</dt>
        <dd>{{ metadata.studioVersion }}</dd>
      </div>
      <div v-if="description" class="workflow-info__notes">
        <dt>{{ t('workflows.workflowDescription') }}</dt>
        <dd><WorkflowDescriptionPreview :text="description" @view="emit('view-description')" /></dd>
      </div>
    </dl>
    <n-button class="workflow-info__edit-notes" text type="primary" size="small" @click="emit('edit-description')">{{ t('workflows.editDescription') }}</n-button>
  </section>
</template>

<style scoped>
.workflow-info { width: min(400px, calc(100vw - 64px)); max-height: min(480px, calc(100dvh - 80px)); overflow-y: auto; overscroll-behavior: contain; outline: none; }
.workflow-info__heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.workflow-info__heading strong { color: var(--on-surface); font-size: 14px; font-weight: 600; }
.workflow-info__name { margin: 4px 0 16px; color: var(--on-surface-muted); font-size: 12px; overflow-wrap: anywhere; }
.workflow-info__fields { display: grid; gap: 12px; margin: 0; }
.workflow-info__fields > div { display: grid; grid-template-columns: 116px minmax(0, 1fr); align-items: baseline; gap: 12px; }
.workflow-info__fields dt { color: var(--on-surface-muted); font-size: 12px; }
.workflow-info__fields dd { margin: 0; color: var(--on-surface); font-size: 13px; overflow-wrap: anywhere; user-select: text; }
.workflow-info__guid { display: flex; align-items: center; gap: 8px; }
.workflow-info__guid code { flex: 1; min-width: 0; font-family: ui-monospace, Consolas, monospace; font-size: 11px; }
.workflow-info__guid .n-button { flex-shrink: 0; }
.workflow-info__fields > .workflow-info__notes { grid-template-columns: 1fr; gap: 6px; }
.workflow-info__notes dd { white-space: pre-wrap; line-height: 1.6; }
.workflow-info__edit-notes { margin-top: 16px; }
</style>
