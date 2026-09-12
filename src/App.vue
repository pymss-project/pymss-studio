<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { darkTheme } from 'naive-ui'
import TitleBar from '@/components/TitleBar.vue'
import SideNav from '@/components/SideNav.vue'
import AppBrandMark from '@/components/AppBrandMark.vue'
import StartupOnboarding from '@/components/StartupOnboarding.vue'
import { useSettingsStore } from '@/stores/settings'
import { useAppStore } from '@/stores/app'
import { useUpdateStore } from '@/stores/update'
import { getResolvedThemeTokens, getThemeOverrides, themeIsDark } from '@/utils/theme'
import { useI18n } from 'vue-i18n'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import { open } from '@tauri-apps/plugin-shell'
import { useWorkflowStore } from '@/stores/workflow'
import { activeRuntimeEnvironment, runtimeBackendLabel, runtimeCoreSyncAvailable, runtimeCoreUpdateAvailable as hasRuntimeCoreUpdate } from '@/utils/runtime'
import { connectWorkerEvents } from '@/utils/events'

const settings = useSettingsStore()
const app = useAppStore()
const updates = useUpdateStore()
const workflow = useWorkflowStore()
const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const bootReady = ref(false)
const backgroundWarmupsStarted = ref(false)
const deferredPromptShown = ref(false)
const deferredUpdateModalVisible = ref(false)
const deferredUpdateInstalling = ref(false)
const deferredUpdateError = ref('')
const manualUpdatePromptShown = ref(false)
const manualUpdateModalVisible = ref(false)
const manualUpdateError = ref('')
const runtimeCorePromptVisible = ref(false)
const runtimeCorePromptShown = ref(false)
let unlistenNodeEditorClosed: UnlistenFn | undefined
let unlistenSimpleEditorClosed: UnlistenFn | undefined
let unlistenSimpleEditorAction: UnlistenFn | undefined

const isDark = computed(() => themeIsDark.value)
const isStandaloneRoute = computed(() => route.path === '/editor' || route.path === '/workflow-node-editor' || route.path === '/workflow-simple-editor')
const isWorkflowNodeEditorRoute = computed(() => route.path === '/workflow-node-editor')
const isWorkflowSimpleEditorRoute = computed(() => route.path === '/workflow-simple-editor')
const isWorkflowEditorRoute = computed(() => isWorkflowNodeEditorRoute.value || isWorkflowSimpleEditorRoute.value)
const isMacOS = typeof navigator !== 'undefined' && /Mac/i.test(navigator.platform)
const resolvedTheme = computed(() => {
  void isDark.value
  return getResolvedThemeTokens(settings.themeMode, settings.themeAccent)
})
const showStartupOnboarding = computed(() => bootReady.value && !isStandaloneRoute.value && settings.shouldShowStartupOnboarding)
const deferredUpdatePrompt = computed(() => updates.updateIsPrerelease
  ? t('settings.updatePrereleaseDeferredPrompt', { version: updates.latestVersion })
  : t('settings.updateDeferredPrompt', { version: updates.latestVersion }))
const manualUpdatePrompt = computed(() => updates.updateMessage || t('settings.updateManualInstallPrompt', { version: updates.latestVersion }))
const activeRuntime = computed(() => activeRuntimeEnvironment(app.runtimeInfo))
const runtimeVersionUpdateAvailable = computed(() => hasRuntimeCoreUpdate(
  activeRuntime.value,
  app.runtimeCoreVersions?.packages?.pymss?.latestVersion,
  app.runtimeCoreVersions?.packages?.['pymss-core']?.latestVersion,
  app.runtimeInfo?.manifestVersion,
))
const runtimeManifestSyncRequired = computed(() => runtimeCoreSyncAvailable(
  activeRuntime.value,
  app.runtimeInfo?.manifestVersion,
))
const runtimeCoreUpdateAvailable = computed(() => runtimeVersionUpdateAvailable.value || runtimeManifestSyncRequired.value)
const runtimeCorePromptContent = computed(() => t(
  runtimeManifestSyncRequired.value && !runtimeVersionUpdateAvailable.value
    ? 'settings.runtimeCoreStartupSyncPrompt'
    : 'settings.runtimeCoreStartupPrompt',
  {
    backend: runtimeBackendLabel(activeRuntime.value?.backend || t('settings.envNotChecked')),
    pymss: activeRuntime.value?.pymssVersion || activeRuntime.value?.packageVersions?.pymss || t('settings.runtimeCoreVersionUnknown'),
    core: activeRuntime.value?.pymssCoreVersion || activeRuntime.value?.packageVersions?.['pymss-core'] || t('settings.runtimeCoreVersionUnknown'),
    latest: app.runtimeCoreVersions?.packages?.pymss?.latestVersion || t('settings.runtimeCoreVersionUnknown'),
    coreLatest: app.runtimeCoreVersions?.packages?.['pymss-core']?.latestVersion || t('settings.runtimeCoreVersionUnknown'),
  },
))

function formatBytes(bytes: number) {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${Math.round(bytes)} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let unit = units[0]
  for (let index = 1; index < units.length && value >= 1024; index += 1) {
    value /= 1024
    unit = units[index]
  }
  return `${value.toFixed(value >= 100 ? 0 : value >= 10 ? 1 : 2)} ${unit}`
}

function formatDuration(seconds: number | null) {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return '—'
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  return `${minutes}m ${remainder}s`
}

const downloadBytesLabel = computed(() => {
  const downloaded = formatBytes(updates.downloadDownloadedBytes)
  const total = updates.downloadTotalBytes > 0 ? formatBytes(updates.downloadTotalBytes) : '—'
  return `${downloaded} / ${total}`
})
const downloadSpeedLabel = computed(() => formatBytes(updates.downloadSpeedBytesPerSecond) + '/s')
const downloadEtaLabel = computed(() => formatDuration(updates.downloadEtaSeconds))

const routeWarmupLoaders = [
  () => import('@/views/SeparateView.vue'),
  () => import('@/views/ModelsView.vue'),
  () => import('@/views/WorkflowsView.vue'),
  () => import('@/views/WorkflowNodeEditorView.vue'),
  () => import('@/views/WorkflowSimpleEditorView.vue'),
  () => import('@/views/ResultsView.vue'),
  () => import('@/views/ToolsView.vue'),
  () => import('@/views/SettingsView.vue'),
  () => import('@/views/DebugView.vue'),
]

function scheduleIdleWork(task: () => void) {
  const idleWindow = window as Window & typeof globalThis & {
    requestIdleCallback?: (callback: IdleRequestCallback, options?: IdleRequestOptions) => number
  }
  if (typeof idleWindow.requestIdleCallback === 'function') {
    idleWindow.requestIdleCallback(() => task(), { timeout: 2000 })
    return
  }
  window.setTimeout(task, 400)
}

function startBackgroundWarmups() {
  if (backgroundWarmupsStarted.value || !bootReady.value || showStartupOnboarding.value) return
  backgroundWarmupsStarted.value = true
  scheduleIdleWork(() => {
    void Promise.allSettled(routeWarmupLoaders.map((load) => load()))
    if (!app.envInfo && !app.envLoading) {
      app.checkEnvInBackground().catch(() => {})
    }
  })
}

async function reconnectWorkerEvents() {
  await connectWorkerEvents(app).catch((error) => {
    console.warn('Failed to reconnect worker events', error)
  })
}

function showDeferredUpdatePrompt() {
  if (deferredPromptShown.value) return
  if (!updates.shouldShowDeferred || !updates.latestVersion) return
  if (updates.requiresManualInstall) return
  deferredPromptShown.value = true
  deferredUpdateError.value = ''
  deferredUpdateModalVisible.value = true
}

function showManualUpdatePrompt() {
  if (manualUpdatePromptShown.value || !bootReady.value) return
  if (!updates.requiresManualInstall || !updates.latestVersion) return
  manualUpdatePromptShown.value = true
  manualUpdateError.value = ''
  manualUpdateModalVisible.value = true
}

async function openManualUpdate() {
  const url = updates.manualInstallUrl || 'https://github.com/pymss-project/pymss-studio/releases/latest'
  try {
    await open(url)
    manualUpdateModalVisible.value = false
  } catch (error) {
    manualUpdateError.value = error instanceof Error ? error.message : String(error)
  }
}

function showRuntimeCorePrompt() {
  if (runtimeCorePromptShown.value || !bootReady.value || isStandaloneRoute.value || !runtimeCoreUpdateAvailable.value) return
  runtimeCorePromptShown.value = true
  runtimeCorePromptVisible.value = true
}

function openRuntimeSettings() {
  runtimeCorePromptVisible.value = false
  void router.push({ path: '/settings', query: { section: 'runtime' } })
}

async function installDeferredUpdate() {
  if (deferredUpdateInstalling.value) return
  deferredUpdateInstalling.value = true
  deferredUpdateError.value = ''
  deferredUpdateModalVisible.value = false
  try {
    await updates.downloadAndInstall()
  } catch (error) {
    deferredUpdateError.value = error instanceof Error ? error.message : String(error)
  } finally {
    deferredUpdateInstalling.value = false
  }
}

async function keepDeferredUpdateForNextLaunch() {
  deferredUpdateError.value = ''
  try {
    await updates.deferUntilNextLaunch()
    deferredUpdateModalVisible.value = false
  } catch (error) {
    deferredUpdateError.value = error instanceof Error ? error.message : String(error)
  }
}

onMounted(async () => {
  window.setTimeout(() => {
    bootReady.value = true
  }, 120)
  if (typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window) {
    unlistenNodeEditorClosed = await listen('pymss://workflow-node-editor-closed', () => {
      workflow.markNodeEditorClosed()
      void workflow.reload()
    })
    unlistenSimpleEditorClosed = await listen('pymss://workflow-simple-editor-closed', () => {
      workflow.markSimpleEditorClosed()
      void workflow.reload()
    })
    unlistenSimpleEditorAction = await listen<{ action?: string; workflowId?: string }>('pymss://workflow-simple-editor-action', async (event) => {
      // `WebviewWindow.emit` broadcasts to every webview.  Only the main
      // window should react to an editor action; handling it inside the
      // standalone editor would replace that window with the main route just
      // before it is closed.
      if (isStandaloneRoute.value) return
      const payload = event.payload || {}
      if (payload.action === 'run') {
        // The editor has its own Pinia instance, so a newly-created workflow
        // is not present in this window until the shared store is reloaded.
        await workflow.reload()
        if (payload.workflowId) workflow.selectWorkflow(payload.workflowId)
        void router.push({ path: '/', query: { mode: 'workflow' } })
      }
    })
  }
})

onUnmounted(() => {
  unlistenNodeEditorClosed?.()
  unlistenSimpleEditorClosed?.()
  unlistenSimpleEditorAction?.()
})

watch([bootReady, showStartupOnboarding], () => {
  startBackgroundWarmups()
}, { immediate: true })

watch([bootReady, () => updates.shouldShowDeferred, () => updates.latestVersion], () => {
  if (bootReady.value) showDeferredUpdatePrompt()
}, { immediate: true })

watch([bootReady, () => updates.requiresManualInstall, () => updates.latestVersion], () => {
  showManualUpdatePrompt()
}, { immediate: true })

watch([bootReady, isStandaloneRoute, runtimeCoreUpdateAvailable], () => {
  if (isStandaloneRoute.value) {
    runtimeCorePromptVisible.value = false
    return
  }
  showRuntimeCorePrompt()
}, { immediate: true })

const themeOverrides = computed(() => {
  void isDark.value
  return getThemeOverrides(settings.themeMode, settings.themeAccent)
})
</script>

<template>
  <n-config-provider :theme="isDark ? darkTheme : null" :theme-overrides="themeOverrides">
    <n-notification-provider>
      <n-message-provider>
        <n-dialog-provider>
        <div class="app-shell" :class="{ 'app-shell--editor': isStandaloneRoute, 'app-shell--workflow-node-editor': isWorkflowEditorRoute, 'app-shell--native-titlebar': isMacOS, 'no-animations': !settings.animationsEnabled }">
          <div class="app-backdrop" />
          <TitleBar v-if="!isWorkflowEditorRoute" />
          <div v-if="app.workerEventConnectionStatus === 'error'" class="worker-event-alert">
            <n-alert type="error" :show-icon="true">
              <template #header>{{ t('app.workerConnectionFailed') }}</template>
              <div class="worker-event-alert__content">
                <span>{{ t('app.workerConnectionFailedDetail') }}</span>
                <n-button
                  size="small"
                  secondary
                  type="error"
                  @click="reconnectWorkerEvents"
                >
                  {{ t('app.workerConnectionRetry') }}
                </n-button>
              </div>
            </n-alert>
          </div>
          <div class="app-body">
            <SideNav v-if="!isStandaloneRoute" />
            <main class="app-content">
              <router-view v-slot="{ Component, route }">
                <component v-if="isWorkflowEditorRoute" :is="Component" :key="route.fullPath" />
                <transition v-else name="page" mode="out-in">
                  <keep-alive include="ToolsView">
                    <component :is="Component" :key="route.path" />
                  </keep-alive>
                </transition>
              </router-view>
            </main>
          </div>
          <transition name="boot-fade">
            <div v-if="!bootReady && !isStandaloneRoute" class="boot-splash">
              <AppBrandMark class="boot-splash__mark" :size="58" shadow />
            <div class="boot-splash__copy">
              <strong>Pymss Studio</strong>
                <span>{{ t('app.bootPreparing') }}</span>
              </div>
            </div>
          </transition>
          <StartupOnboarding v-if="showStartupOnboarding" />
        </div>
        <n-modal v-model:show="deferredUpdateModalVisible" preset="dialog" type="warning" :mask-closable="false" :closable="false">
          <template #header>
            {{ t('settings.updateDeferred') }}
          </template>
          <div>{{ deferredUpdatePrompt }}</div>
          <n-alert v-if="deferredUpdateError" type="error" :bordered="false" style="margin-top: 12px">
            {{ deferredUpdateError }}
          </n-alert>
          <template #action>
            <n-button secondary :disabled="deferredUpdateInstalling" @click="keepDeferredUpdateForNextLaunch">
              {{ t('settings.updateRemindLater') }}
            </n-button>
            <n-button type="primary" :loading="deferredUpdateInstalling" @click="installDeferredUpdate">
              {{ t('settings.installUpdate') }}
            </n-button>
          </template>
        </n-modal>
        <n-modal v-model:show="manualUpdateModalVisible" preset="dialog" type="warning" :mask-closable="false" :closable="false">
          <template #header>
            {{ t('settings.updateManualInstallTitle') }}
          </template>
          <div>{{ manualUpdatePrompt }}</div>
          <n-alert v-if="manualUpdateError" type="error" :bordered="false" style="margin-top: 12px">
            {{ manualUpdateError }}
          </n-alert>
          <template #action>
            <n-button secondary @click="manualUpdateModalVisible = false">
              {{ t('common.close') }}
            </n-button>
            <n-button type="primary" @click="openManualUpdate">
              {{ t('settings.updateOpenGitHub') }}
            </n-button>
          </template>
        </n-modal>
        <n-modal v-model:show="runtimeCorePromptVisible" preset="dialog" type="warning" :mask-closable="false" :closable="false">
          <template #header>
            {{ t('settings.runtimeCoreStartupTitle') }}
          </template>
          <div>{{ runtimeCorePromptContent }}</div>
          <template #action>
            <n-button secondary @click="runtimeCorePromptVisible = false">
              {{ t('settings.runtimeCoreStartupLater') }}
            </n-button>
            <n-button type="warning" @click="openRuntimeSettings">
              {{ t('settings.runtimeCoreStartupOpenSettings') }}
            </n-button>
          </template>
        </n-modal>
        <n-modal
          :show="updates.isInstallOverlayVisible"
          preset="card"
          :mask-closable="false"
          :closable="false"
          class="update-install-modal"
          :bordered="false"
        >
          <div class="update-install-panel">
            <div class="update-install-panel__head">
              <strong>
                {{ updates.status === 'failed' ? t('settings.updateInstallFailed') : updates.status === 'installing' ? t('settings.updateInstalling') : t('settings.updateDownloading') }}
              </strong>
              <span>{{ updates.status === 'failed' ? t('settings.updateInstallFailedHint') : t('settings.updateInstallOverlayHint') }}</span>
            </div>
            <n-progress
              v-if="updates.status !== 'failed'"
              type="line"
              :percentage="updates.downloadProgressPercent"
              :processing="updates.status === 'downloading'"
              :show-indicator="updates.downloadTotalBytes > 0"
              status="success"
            />
            <div v-if="updates.status === 'downloading'" class="update-install-panel__stats">
              <span>{{ t('settings.updateDownloadProgress', { percent: updates.downloadProgressPercent }) }}</span>
              <span>{{ downloadBytesLabel }}</span>
              <span>{{ t('settings.updateDownloadSpeed', { speed: downloadSpeedLabel }) }}</span>
              <span>{{ t('settings.updateDownloadEta', { eta: downloadEtaLabel }) }}</span>
            </div>
            <p v-else-if="updates.status === 'installing'" class="update-install-panel__meta">
              {{ t('settings.updateInstallRestarting') }}
            </p>
            <n-alert v-if="updates.status === 'failed' && updates.error" type="error" :bordered="false">
              {{ updates.error }}
            </n-alert>
            <section v-if="updates.downloadLogs.length" class="update-install-panel__logs" aria-live="polite">
              <div class="update-install-panel__logs-head">
                <strong>{{ t('settings.updateDownloadLogs') }}</strong>
                <span>{{ t('settings.updateDownloadLogHint') }}</span>
              </div>
              <div class="update-install-panel__log-list">
                <div v-for="(entry, index) in updates.downloadLogs" :key="`${entry.at}-${index}`" class="update-install-panel__log-line">
                  <time>{{ entry.at }}</time>
                  <span>{{ entry.message }}</span>
                </div>
              </div>
            </section>
            <div v-if="updates.status === 'failed'" class="update-install-panel__actions">
              <n-button secondary @click="updates.dismissInstallError()">{{ t('common.close') }}</n-button>
            </div>
          </div>
        </n-modal>
        </n-dialog-provider>
      </n-message-provider>
    </n-notification-provider>
  </n-config-provider>
</template>

<style scoped>
.boot-splash {
  position: absolute;
  inset: 0;
  z-index: 120;
  display: grid;
  place-items: center;
  gap: 14px;
  background:
    radial-gradient(circle at 20% 16%, color-mix(in srgb, v-bind('resolvedTheme.primarySoft') 90%, transparent), transparent 28%),
    linear-gradient(180deg, rgba(255,255,255,0.03), transparent 32%),
    var(--surface);
}

.boot-splash__mark {
  flex: 0 0 auto;
}

.boot-splash__copy {
  display: grid;
  gap: 6px;
  text-align: center;
}

.boot-splash__copy strong {
  font-size: 18px;
  letter-spacing: 0.01em;
}

.boot-splash__copy span {
  font-size: 12px;
  color: var(--on-surface-muted);
}

.boot-fade-enter-active,
.boot-fade-leave-active {
  transition: opacity 240ms ease;
}

.boot-fade-enter-from,
.boot-fade-leave-to {
  opacity: 0;
}

.worker-event-alert {
  position: absolute;
  z-index: 110;
  top: 52px;
  left: 50%;
  width: min(620px, calc(100vw - 32px));
  transform: translateX(-50%);
}

.worker-event-alert__content {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

@media (max-width: 640px) {
  .worker-event-alert__content {
    align-items: stretch;
    flex-direction: column;
    gap: 10px;
  }
}

:global(.update-install-modal) {
  /* Keep the progress dialog compact on desktop while fitting narrow windows. */
  width: clamp(320px, 44vw, 520px);
  max-width: calc(100vw - 24px);
  box-sizing: border-box;
}

.update-install-panel {
  display: grid;
  gap: 16px;
  min-width: 0;
  max-height: min(640px, calc(100vh - 96px));
  overflow-y: auto;
}

.update-install-panel__head {
  display: grid;
  gap: 6px;
}

.update-install-panel__head strong {
  color: var(--on-surface);
  font-size: 17px;
}

.update-install-panel__head span,
.update-install-panel__meta {
  color: var(--on-surface-muted);
  font-size: 12px;
}

.update-install-panel__meta {
  margin: -6px 0 0;
}

.update-install-panel__stats {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 16px;
  padding: 10px 12px;
  border: 1px solid color-mix(in srgb, var(--outline) 68%, transparent);
  border-radius: 10px;
  background: color-mix(in srgb, var(--surface-2) 74%, transparent);
  color: var(--on-surface-muted);
  font-size: 12px;
}

.update-install-panel__stats span:nth-child(2n) {
  color: var(--on-surface);
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.update-install-panel__logs {
  display: grid;
  gap: 8px;
  min-width: 0;
}

.update-install-panel__logs-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

.update-install-panel__logs-head strong {
  color: var(--on-surface);
  font-size: 12px;
}

.update-install-panel__logs-head span {
  color: var(--on-surface-muted);
  font-size: 11px;
}

.update-install-panel__log-list {
  display: grid;
  gap: 4px;
  max-height: 148px;
  overflow-y: auto;
  padding: 9px 10px;
  border: 1px solid color-mix(in srgb, var(--outline) 58%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, var(--surface-3) 58%, transparent);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 11px;
  line-height: 1.45;
}

.update-install-panel__log-line {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 8px;
  min-width: 0;
  color: var(--on-surface-muted);
}

.update-install-panel__log-line time {
  color: var(--primary-strong);
  font-variant-numeric: tabular-nums;
}

.update-install-panel__log-line span {
  overflow-wrap: anywhere;
}

.update-install-panel__actions {
  display: flex;
  justify-content: flex-end;
}

@media (max-width: 520px) {
  .update-install-panel__stats {
    grid-template-columns: 1fr;
  }

  .update-install-panel__stats span:nth-child(2n) {
    text-align: left;
  }

  .update-install-panel__logs-head {
    align-items: flex-start;
    flex-direction: column;
    gap: 3px;
  }
}
</style>
