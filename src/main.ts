import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import i18n from './i18n'
import { initTheme } from './utils/theme'
import { connectWorkerEvents } from './utils/events'
import { useSettingsStore } from '@/stores/settings'
import { useAppStore, type RuntimeBackend } from '@/stores/app'
import { useUpdateStore } from '@/stores/update'
import { runtimeToActivate } from '@/utils/runtime'
import './styles/global.scss'

async function bootstrap() {
  const pinia = createPinia()
  const app = createApp(App)
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

  const settings = useSettingsStore(pinia)
  const appState = useAppStore(pinia)
  const updates = useUpdateStore(pinia)
  await settings.initialize().catch((error) => {
    console.warn('Failed to initialize settings', error)
  })
  await updates.initialize().catch((error) => {
    console.warn('Failed to initialize update store', error)
  })
  const tasks = await import('@/stores/task').then((mod) => mod.useTaskStore(pinia))
  await tasks.initialize().catch((error) => {
    console.warn('Failed to initialize tasks', error)
  })
  const models = await import('@/stores/model').then((mod) => mod.useModelStore(pinia))
  const workflows = await import('@/stores/workflow').then((mod) => mod.useWorkflowStore(pinia))
  await Promise.allSettled([models.initialize(), workflows.initialize()])
  // Register after the webview and dependent stores are ready. Tauri's event
  // bridge can still be initializing while the entry module is evaluated;
  // connecting here avoids a bootstrap race and lets early events update fully
  // initialized stores.
  const workerEventsReady = connectWorkerEvents(appState)
  await workerEventsReady.catch((error) => {
    console.warn('Failed to register worker events', error)
  })
  initTheme(settings.themeMode, settings.themeAccent)
  try {
    const { invoke } = await import('@tauri-apps/api/core')
    const buildInfo = await invoke<{ version?: string; variant?: string; updateSupported?: boolean }>('get_build_info')
    appState.buildInfoVersion = buildInfo.version || ''
    appState.buildInfoVariant = buildInfo.variant || ''
    appState.buildInfoUpdateSupported = buildInfo.updateSupported === true
  } catch (error: unknown) {
    console.warn('Failed to load build info version', error)
  }
  if (appState.buildInfoVersion && updates.hasPendingDeferredVersion(appState.buildInfoVersion)) {
    updates.status = 'ready'
  }
  appState.checkRuntimeInfo().then(async (runtime) => {
    // If the active pointer was lost (for example after an overwrite that preserved the
    // user data directory), activate the only usable environment before the first env check.
    // Multiple environments remain explicit-choice only.
    const candidate = runtimeToActivate(runtime)
    if (candidate?.backend && candidate.pythonPath) {
      try {
        await appState.activateRuntime(candidate.backend as RuntimeBackend, {
          pythonPath: candidate.pythonPath,
        }, { refreshCoreVersions: false, onlyIfNoActive: true })
        runtime = appState.runtimeInfo || runtime
      } catch (error) {
        console.warn('Failed to activate detected runtime', error)
        // The activation command may have succeeded before a follow-up probe failed.
        // Prefer the store's latest runtime snapshot over the stale callback value.
        runtime = appState.runtimeInfo || runtime
      }
    }
    if (runtime.ready) {
      void models.loadModels().catch((error) => {
        console.warn('Failed to preload model metadata', error)
      })
    }
    if (runtime.installedEnvironments?.some((env) => env.coreUpdateSupported !== false)) {
      void appState.loadRuntimeCoreVersions().catch((error) => {
        console.warn('Failed to check runtime core versions', error)
      })
    }
    return undefined
  }).catch((error) => {
    console.warn('Failed to load runtime info', error)
  })
  void updates.checkForUpdates().catch((error) => {
    console.warn('Failed to check for updates', error)
  })
}

bootstrap().catch((error) => {
  console.error('Failed to bootstrap application', error)
})
