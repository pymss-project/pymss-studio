import { onBeforeUnmount, onMounted } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import { isTauriRuntime } from '@/utils/appStore'
import { createLogger } from '@/utils/logger'

const log = createLogger('ui.diagnosticWindows')

/** Observe the view that owns this window, only when diagnostics requests it. */
export function useDiagnosticWindowState(kind: 'workflow' | 'editor' | 'renderer', snapshot: () => Record<string, unknown>) {
  let disposed = false
  let unlisten: UnlistenFn | undefined
  onMounted(async () => {
    if (!isTauriRuntime()) return
    try {
      const eventName = kind === 'renderer' ? 'pymss://renderer-state-request' : 'pymss://diagnostic-state-request'
      const stop = await listen<{ requestId: string }>(eventName, event => {
        if (disposed) return
        let observation: Record<string, unknown>
        try { observation = { ...snapshot(), kind } }
        catch (error) { observation = { kind, error: error instanceof Error ? error.message : String(error) } }
        void invoke('publish_debug_window_state', { requestId: event.payload.requestId, snapshot: observation })
          .catch(error => { if (!disposed) log.warn('Failed to publish window diagnostics', error) })
      })
      if (disposed) stop()
      else unlisten = stop
    } catch (error) { log.warn('Failed to connect window diagnostics', error) }
  })
  onBeforeUnmount(() => { disposed = true; unlisten?.() })
}
