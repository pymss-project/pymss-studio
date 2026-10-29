import { createLogger } from '../../utils/logger.ts'
import { loadAppStore, saveAppStore } from '../../utils/appStore.ts'

const studioLog = createLogger('ui.features.plugin-marketplace.toolState')

// Keep the established audio-tools store intact while plugin pages gradually
// take ownership of their own settings. Only selection is owned by Studio.
export type StoredPluginToolState = Record<string, unknown> & {
  activePluginTool?: string
}

let queue: Promise<void> = Promise.resolve()

export function loadPluginToolState() {
  return loadAppStore<StoredPluginToolState>('audio-tools')
}

export function updatePluginToolState(patch: Partial<StoredPluginToolState>) {
  const run = queue.then(async () => {
    const current = await loadPluginToolState() || {}
    await saveAppStore('audio-tools', { ...current, ...patch })
  })
  queue = run.catch(error => studioLog.warn('[plugin-tools] state save failed', error))
  return run
}
