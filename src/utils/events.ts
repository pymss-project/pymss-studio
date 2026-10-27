import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import { useAppStore } from '@/stores/app'
import { useSettingsStore } from '@/stores/settings'
import { useTaskStore } from '@/stores/task'
import { useModelStore, type WorkerEvent } from '@/stores/model'
import { retryWithDelays } from '@/utils/async'
import { isMainTaskOwner } from '@/utils/taskOwner'
import { isWebRuntime } from '@/utils/webRuntime'
import type { WorkerEventConnectionStatus } from '@/stores/app'

let unlistenWorkerEvents: UnlistenFn | undefined
let registrationInFlight: Promise<void> | null = null

// Tauri injects its internals before normal application startup, but keeping
// this check dynamic also covers delayed webview initialization and reconnects.
// Web mode installs the same internals surface over SSE.
function hasTauriEventApi() {
  return typeof window !== 'undefined' && ('__TAURI_INTERNALS__' in window)
}

export async function registerWorkerEvents() {
  if ((!hasTauriEventApi() && !isWebRuntime()) || unlistenWorkerEvents) return
  if (!registrationInFlight) {
    registrationInFlight = listen('pymss://worker-event', (event) => {
      const app = useAppStore()
      const settings = useSettingsStore()
      const tasks = useTaskStore()
      const models = useModelStore()
      const workerEvent = event.payload as WorkerEvent
      app.handleWorkerEvent(workerEvent)
      if (settings.developerMode) app.recordWorkerEvent(workerEvent)
      void settings.handleWorkerEvent(workerEvent)
      app.handleRuntimeEvent(workerEvent)
      if (isMainTaskOwner()) tasks.handleWorkerEvent(workerEvent)
      models.handleWorkerEvent(workerEvent)
    }).then((unlisten) => {
      unlistenWorkerEvents = unlisten
    }).finally(() => {
      registrationInFlight = null
    })
  }
  return registrationInFlight
}

type WorkerEventConnectionTarget = {
  setWorkerEventConnection: (status: WorkerEventConnectionStatus, error?: string) => void
}

export async function connectWorkerEvents(target: WorkerEventConnectionTarget) {
  target.setWorkerEventConnection('connecting')
  try {
    await retryWithDelays(registerWorkerEvents, [250, 750])
    target.setWorkerEventConnection('connected')
    // Resume persisted queue items only after the listener is installed so the
    // first worker events cannot be lost during application bootstrap.
    if (isMainTaskOwner()) useTaskStore().scheduleQueue()
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    target.setWorkerEventConnection('error', detail)
    throw error
  }
}
