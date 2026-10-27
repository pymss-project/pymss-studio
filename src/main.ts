import { pluginHostCapabilities } from '@/utils/pluginHost'
import { createLogger } from '@/utils/logger'
import { createApp, nextTick } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import i18n from './i18n'
import { initTheme } from './utils/theme'
import { connectWorkerEvents } from './utils/events'
import { isMainTaskOwner } from './utils/taskOwner'
import { initializeWorkbenchBridge } from './utils/workbenchBridge'
import { isTauriRuntime } from './utils/appStore'
import { installWebRuntime, isWebRuntime } from './utils/webRuntime'
import { listenForEditorExportPreferences } from './utils/editorExportPreferences'
import { useSettingsStore } from '@/stores/settings'
import { useAppStore, type RuntimeBackend } from '@/stores/app'
import { useUpdateStore } from '@/stores/update'
import { activeRuntimeEnvironment, runtimeCoreSyncAvailable, runtimeToActivate } from '@/utils/runtime'
import './styles/global.scss'
import { isTutorialFrame } from './features/onboarding/demoContext'

const studioLog = createLogger('ui.main')

async function bootstrap() {
  const pinia = createPinia()
  const app = createApp(App)
  app.config.errorHandler = (error, _instance, info) => {
    studioLog.error('Vue application error', error, info)
  }
  window.addEventListener('unhandledrejection', event => studioLog.error('Unhandled promise rejection', event.reason))
  window.addEventListener('error', event => studioLog.error(event.message || 'Unhandled application error', event.error))
  app.use(pinia)
  app.use(router)
  app.use(i18n)

  const mounted = () => {
    if (!document.querySelector('#app')?.hasChildNodes()) app.mount('#app')
  }

  // Mount the shell before optional persistence/runtime probes. A standalone
  // workflow editor must not leave the whole window behind the boot splash if
  // one of those IPC calls is delayed or unavailable; its stores can hydrate
  // in the background while the route remains usable.
  mounted()

  // Web mode installs a browser-side Tauri IPC emulation before any store
  // touches @tauri-apps/api; failures must not block the desktop path.
  if (isWebRuntime() || (!isTauriRuntime() && typeof window !== 'undefined' && !isTutorialFrame())) {
    try {
      await installWebRuntime('')
    } catch (error) {
      studioLog.warn('Failed to install web runtime bridge', error)
    }
  }

  const settings = useSettingsStore(pinia)
  const appState = useAppStore(pinia)
  const updates = useUpdateStore(pinia)
  await settings.initialize().catch((error) => {
    studioLog.warn('Failed to initialize settings', error)
  })
  if (isTauriRuntime() && isMainTaskOwner()) {
    try {
      const unlisten = await listenForEditorExportPreferences(settings.saveEditorExportPreference)
      app.onUnmount(unlisten)
    } catch (error) {
      studioLog.warn('Failed to connect editor export preferences', error)
    }
  }
  const pluginHost = await pluginHostCapabilities()
  if (pluginHost.labBuild !== true) await updates.initialize().catch((error) => {
    studioLog.warn('Failed to initialize update store', error)
  })
  const tasks = await import('@/stores/task').then((mod) => mod.useTaskStore(pinia))
  if (isMainTaskOwner()) await tasks.initialize().catch((error) => {
    studioLog.warn('Failed to initialize tasks', error)
  })
  const models = await import('@/stores/model').then((mod) => mod.useModelStore(pinia))
  const workflows = await import('@/stores/workflow').then((mod) => mod.useWorkflowStore(pinia))
  await Promise.allSettled([models.initialize(), workflows.initialize()])
  await initializeWorkbenchBridge().catch(error => {
    studioLog.warn('Failed to connect workflow editor windows', error)
  })
  // Register after the webview and dependent stores are ready. Tauri's event
  // bridge can still be initializing while the entry module is evaluated;
  // connecting here avoids a bootstrap race and lets early events update fully
  // initialized stores.
  const workerEventsReady = connectWorkerEvents(appState)
  await workerEventsReady.catch((error) => {
    studioLog.warn('Failed to register worker events', error)
  })
  initTheme(settings.themeMode, settings.themeAccent)
  try {
    const { invoke } = await import('@tauri-apps/api/core')
    const buildInfo = await invoke<{ version?: string; variant?: string; updateSupported?: boolean }>('get_build_info')
    appState.buildInfoVersion = buildInfo.version || ''
    appState.buildInfoVariant = buildInfo.variant || ''
    appState.buildInfoUpdateSupported = buildInfo.updateSupported === true
  } catch (error: unknown) {
    studioLog.warn('Failed to load build info version', error)
  }
  if (!updates.restartRequired && appState.buildInfoVersion && updates.hasPendingDeferredVersion(appState.buildInfoVersion)) {
    updates.status = 'ready'
  }
  if (isTauriRuntime() && isMainTaskOwner()) {
    await router.isReady()
    await nextTick()
    const { invoke } = await import('@tauri-apps/api/core')
    await invoke('confirm_managed_update_startup')
  }
  appState.checkRuntimeInfo().then(async (runtime) => {
    // If the active pointer was lost (for example after an overwrite that preserved the
    // user data directory), activate the only usable environment before the first env check.
    // Multiple environments remain explicit-choice only.
    const candidate = appState.runtimeManagementSupported ? runtimeToActivate(runtime) : undefined
    if (candidate?.backend && candidate.pythonPath) {
      try {
        await appState.activateRuntime(candidate.backend as RuntimeBackend, {
          pythonPath: candidate.pythonPath,
        }, { refreshCoreVersions: false, onlyIfNoActive: true })
        runtime = appState.runtimeInfo || runtime
      } catch (error) {
        studioLog.warn('Failed to activate detected runtime', error)
        // The activation command may have succeeded before a follow-up probe failed.
        // Prefer the store's latest runtime snapshot over the stale callback value.
        runtime = appState.runtimeInfo || runtime
      }
    }
    const activeRuntime = activeRuntimeEnvironment(runtime)
    if (runtime.ready && (!appState.runtimeCoreUpdateSupported || !runtimeCoreSyncAvailable(activeRuntime, runtime.manifestVersion))) {
      void models.loadModels().catch((error) => {
        studioLog.warn('Failed to preload model metadata', error)
      })
    }
    if (appState.runtimeCoreUpdateSupported && runtime.installedEnvironments?.some((env) => env.coreUpdateSupported !== false)) {
      void appState.loadRuntimeCoreVersions().catch((error) => {
        studioLog.warn('Failed to check runtime core versions', error)
      })
    }
    return undefined
  }).catch((error) => {
    studioLog.warn('Failed to load runtime info', error)
  })
  if (pluginHost.labBuild !== true) void updates.checkForUpdates().catch((error) => {
    studioLog.warn('Failed to check for updates', error)
  })
}

;(isTutorialFrame() ? import('./features/onboarding/demoBootstrap').then(module => module.bootstrapTutorial()) : bootstrap()).catch((error) => {
  if (isTutorialFrame()) {
    ;(window as any).__pymssTutorialDispose?.()
    window.parent.postMessage({ type: 'pymss-tutorial-error', message: error instanceof Error ? error.message : String(error) }, '*')
  }
  studioLog.error('Failed to bootstrap application', error)
})
