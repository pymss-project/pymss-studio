<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { useMessage } from 'naive-ui'
import { useI18n } from 'vue-i18n'
import { useWorkflowStore } from '@/stores/workflow'
import { createLogger } from '@/utils/logger'

const workflow = useWorkflowStore()
const { t } = useI18n()
const message = useMessage()
const studioLog = createLogger('ui.workflowRecovery')
const opening = ref(false)
const dismissing = ref(false)
const dismissedPath = ref('')
const show = computed(() => Boolean(workflow.recoveryBackupPath && workflow.recoveryBackupPath !== dismissedPath.value))

watch(() => workflow.recoveryBackupPath, path => {
  if (path) studioLog.warn('Unreadable workflow data was backed up and the library was rebuilt')
}, { immediate: true })

async function openBackupDirectory() {
  opening.value = true
  try {
    const paths = await invoke<{ settingsDir: string }>('get_app_paths')
    await invoke('reveal_path', { path: paths.settingsDir })
  } catch (error) {
    message.error(String(error))
  } finally {
    opening.value = false
  }
}

async function dismiss() {
  if (dismissing.value) return
  const path = workflow.recoveryBackupPath
  dismissing.value = true
  try {
    await workflow.dismissRecoveryNotice()
    dismissedPath.value = path
  } catch (error) {
    message.error(String(error))
  } finally {
    dismissing.value = false
  }
}
</script>

<template>
  <n-modal :show="show" :mask-closable="!dismissing" :close-on-esc="!dismissing" @update:show="(visible: boolean) => { if (!visible) dismissedPath = workflow.recoveryBackupPath }">
    <n-card class="workflow-recovery-modal" :bordered="false" role="alertdialog" aria-modal="true" :title="t('workflows.recoveryTitle')">
      <p>{{ t('workflows.recoveryDescription') }}</p>
      <code class="workflow-recovery-path">{{ workflow.recoveryBackupPath }}</code>
      <template #footer>
        <div class="workflow-recovery-actions">
          <n-button secondary :loading="opening" @click="openBackupDirectory">{{ t('workflows.recoveryOpenDirectory') }}</n-button>
          <n-button type="primary" :loading="dismissing" @click="dismiss">{{ t('common.confirm') }}</n-button>
        </div>
      </template>
    </n-card>
  </n-modal>
</template>

<style scoped>
.workflow-recovery-modal { width: var(--modal-width-sm); }
.workflow-recovery-path { display: block; overflow-wrap: anywhere; }
.workflow-recovery-actions { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 8px; }
</style>
