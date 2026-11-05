<script setup lang="ts">
import { createLogger } from '@/utils/logger'

import { computed, h, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NButton, useDialog, useMessage } from 'naive-ui'
import { getCurrentWindow } from '@tauri-apps/api/window'
import { invoke } from '@tauri-apps/api/core'
import AppBrandMark from '@/components/AppBrandMark.vue'
import { useSettingsStore } from '@/stores/settings'
import { useTaskStore } from '@/stores/task'
import { runWindowCloseGuards, runWindowCloseRecoveries, type WindowClosePersistenceError, type WindowCloseRecovery } from '@/utils/windowCloseGuards'

const studioLog = createLogger('ui.components.TitleBar')

const route = useRoute()
const { t } = useI18n()
const dialog = useDialog()
const message = useMessage()
const settings = useSettingsStore()
const task = useTaskStore()
const hasTauriWindow = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
const appWindow = hasTauriWindow ? getCurrentWindow() : null
const isMaximized = ref(false)
const closeInProgress = ref(false)
const allowForcedClose = ref(false)
const closeDialogOpen = ref(false)
let closeRecoveryCancelled = false

const pageTitle = computed(() => t(`nav.${String(route.name || 'home').startsWith('workflow-') ? 'workflows' : String(route.name || 'home')}`))
const isMainWindow = computed(() => !appWindow || appWindow.label === 'main')
const runningTaskCount = computed(() => task.runningTasks.length)
const closeDisabled = computed(() => settings.isModelDirMigrating || closeInProgress.value)
const isMacOS = typeof navigator !== 'undefined' && /Mac/i.test(navigator.platform)

async function refreshMaximized() {
  if (!appWindow) { isMaximized.value = false; return }
  try {
    isMaximized.value = await invoke<boolean>('is_current_window_maximized')
  } catch {
    try { isMaximized.value = await appWindow.isMaximized() } catch { isMaximized.value = false }
  }
}
async function minimize() {
  if (!appWindow) return
  try {
    await invoke('minimize_current_window')
  } catch (error) {
    studioLog.warn('[title-bar] minimize_current_window failed', error)
    appWindow.minimize().catch(innerError => studioLog.warn('[title-bar] appWindow.minimize failed', innerError))
  }
}
async function toggleMaximize() {
  if (!appWindow) return
  try {
    isMaximized.value = await invoke<boolean>('toggle_maximize_current_window')
  } catch (error) {
    studioLog.warn('[title-bar] toggle_maximize_current_window failed', error)
    appWindow.toggleMaximize().then(refreshMaximized).catch(innerError => studioLog.warn('[title-bar] appWindow.toggleMaximize failed', innerError))
  }
}

function confirmPersistenceFailure(error: WindowClosePersistenceError) {
  return new Promise<WindowCloseRecovery>(resolve => {
    let settled = false
    const finish = (action: WindowCloseRecovery) => {
      if (settled) return
      settled = true
      closeRecoveryCancelled = action === 'cancel'
      closeDialogOpen.value = false
      prompt.destroy()
      resolve(action)
    }
    closeDialogOpen.value = true
    const prompt = dialog.warning({
      title: t('tasks.closeSaveFailedTitle'),
      content: () => h('div', [
        h('p', t('tasks.closeSaveFailedContent')),
        h('p', { style: 'white-space: pre-wrap; overflow-wrap: anywhere' }, error.message),
      ]),
      action: () => h('div', { style: 'display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end', onKeydown: (event: KeyboardEvent) => {
        event.stopPropagation()
        if (event.key === 'Escape') { event.preventDefault(); finish('cancel') }
      } }, [
        h(NButton, { onClick: () => finish('cancel') }, { default: () => t('common.continueUsing') }),
        h(NButton, { type: 'error', secondary: true, onClick: () => finish('discard') }, { default: () => t('tasks.discardAndClose') }),
        h(NButton, { type: 'primary', onClick: () => finish('retry') }, { default: () => t('tasks.retrySaveAndClose') }),
      ]),
      onClose: () => finish('cancel'),
      onMaskClick: () => finish('cancel'),
      onEsc: () => finish('cancel'),
    })
  })
}

async function performWindowClose() {
  if (!appWindow) return false
  closeRecoveryCancelled = false
  try {
    await runWindowCloseGuards(confirmPersistenceFailure)
  } catch (error) {
    studioLog.warn('[title-bar] window close guard failed', error)
    return false
  }
  allowForcedClose.value = true
  try {
    await invoke('close_current_window')
    return true
  } catch {
    try {
      await appWindow.destroy()
      return true
    } catch {
      try {
        await appWindow.close()
        return true
      } catch {
        allowForcedClose.value = false
        await runWindowCloseRecoveries()
        return false
      }
    }
  }
}

function confirmForceClose(count: number) {
  return new Promise<boolean>((resolve) => {
    let settled = false
    const finish = (value: boolean) => {
      if (settled) return
      settled = true
      closeDialogOpen.value = false
      resolve(value)
    }
    closeDialogOpen.value = true
    dialog.warning({
      title: t('tasks.closeForceConfirmTitle'),
      content: t('tasks.closeForceConfirmContent', { count }),
      positiveText: t('tasks.forceCloseAction'),
      negativeText: t('common.continueUsing'),
      positiveButtonProps: { type: 'error' },
      negativeButtonProps: { secondary: true },
      onPositiveClick: () => finish(true),
      onNegativeClick: () => finish(false),
      onClose: () => finish(false),
    })
  })
}

async function cancelTasksAndClose() {
  if (closeInProgress.value) return
  closeInProgress.value = true
  const loading = message.loading(t('tasks.closingAfterCancel'), { duration: 0 })
  try {
    await task.cancelAllTasks()
    if (runningTaskCount.value > 0) {
      loading.destroy()
      const forceClose = await confirmForceClose(runningTaskCount.value)
      if (!forceClose) {
        closeInProgress.value = false
        return
      }
      const forced = await performWindowClose()
      if (!forced) {
        if (!closeRecoveryCancelled) message.error(t('tasks.closeForceFailed'))
        closeInProgress.value = false
      }
      return
    }
    const closed = await performWindowClose()
    if (!closed) {
      if (!closeRecoveryCancelled) message.error(t('tasks.closeFailed'))
      closeInProgress.value = false
    }
  } catch {
    message.error(t('tasks.closeCancelFailed'))
    closeInProgress.value = false
  } finally {
    loading.destroy()
  }
}

async function requestClose() {
  if (settings.isModelDirMigrating) {
    message.warning(t('settings.modelDirMigrationCloseBlocked'))
    return
  }
  if (!appWindow || closeInProgress.value) return
  if (!isMainWindow.value || runningTaskCount.value <= 0) {
    closeInProgress.value = true
    const closed = await performWindowClose()
    if (!closed) {
      if (!closeRecoveryCancelled) message.error(t('tasks.closeFailed'))
      closeInProgress.value = false
    }
    return
  }
  if (closeDialogOpen.value) return
  closeDialogOpen.value = true
  dialog.warning({
    title: t('tasks.closeRunningTasksTitle'),
    content: t('tasks.closeRunningTasksContent', { count: runningTaskCount.value }),
    positiveText: t('tasks.closeAndCancelAllAction'),
    negativeText: t('common.continueUsing'),
    positiveButtonProps: { type: 'error' },
    negativeButtonProps: { secondary: true },
    onPositiveClick: async () => {
      closeDialogOpen.value = false
      await cancelTasksAndClose()
    },
    onNegativeClick: () => {
      closeDialogOpen.value = false
    },
    onClose: () => {
      closeDialogOpen.value = false
    },
  })
}

async function close() {
  await requestClose()
}
function startDrag(event: MouseEvent) {
  if (event.detail > 1) { void toggleMaximize(); return }
  if (!appWindow) return
  invoke('start_drag_current_window').catch((error) => {
    studioLog.warn('[title-bar] start_drag_current_window failed', error)
    appWindow.startDragging().catch(innerError => studioLog.warn('[title-bar] appWindow.startDragging failed', innerError))
  })
}

let unlistenResize: (() => void) | undefined
let unlistenCloseRequested: (() => void) | undefined
onMounted(async () => {
  await refreshMaximized()
  if (!appWindow) return
  try { unlistenResize = await appWindow.onResized(refreshMaximized) } catch {}
  try {
    unlistenCloseRequested = await appWindow.onCloseRequested((event) => {
      if (allowForcedClose.value) return
      event.preventDefault()
      if (settings.isModelDirMigrating) {
        message.warning(t('settings.modelDirMigrationCloseBlocked'))
        return
      }
      void requestClose()
    })
  } catch {}
})
onUnmounted(() => {
  unlistenResize?.()
  unlistenCloseRequested?.()
})
</script>

<template>
  <header v-if="!isMacOS" class="title-bar">
    <div class="title-brand" data-tauri-drag-region @mousedown.left="startDrag">
      <AppBrandMark :size="22" variant="compact" />
      <div class="brand-copy">
        <strong>{{ t('app.name') }}</strong>
        <span>{{ pageTitle }}</span>
      </div>
    </div>
    <div class="title-drag-space" data-tauri-drag-region @mousedown.left="startDrag" />
    <div class="window-actions" @mousedown.stop>
      <button type="button" :aria-label="t('common.minimize')" @click="minimize">
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M2 6h8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
      </button>
      <button type="button" :aria-label="t('common.maximize')" @click="toggleMaximize">
        <svg v-if="isMaximized" width="12" height="12" viewBox="0 0 12 12" fill="none"><rect x="1.5" y="3.5" width="7" height="7" rx="1" stroke="currentColor" stroke-width="1.2"/><path d="M3.5 3.5V2a.5.5 0 01.5-.5h6a.5.5 0 01.5.5v6a.5.5 0 01-.5.5h-1.5" stroke="currentColor" stroke-width="1.2"/></svg>
        <svg v-else width="12" height="12" viewBox="0 0 12 12" fill="none"><rect x="2" y="2" width="8" height="8" rx="1.5" stroke="currentColor" stroke-width="1.2"/></svg>
      </button>
      <button type="button" :aria-label="t('common.close')" class="danger" :disabled="closeDisabled" @click="close">
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M3 3l6 6M9 3l-6 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
      </button>
    </div>
  </header>
</template>
