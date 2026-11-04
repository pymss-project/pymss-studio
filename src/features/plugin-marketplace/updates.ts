import type { InstalledPlugin, PluginInventory, PluginUpdate } from './composable'

export type PluginUpdateState = 'unchecked' | 'update' | 'content' | 'source_changed' | 'older' | 'incomparable' | 'latest' | 'error'

export function pluginUpdateState(update?: PluginUpdate): PluginUpdateState {
  if (!update) return 'unchecked'
  if (update.error) return 'error'
  if (update.status) return update.status
  // Older cores cannot distinguish a newer version from a changed source.
  if (update.up_to_date === true) return 'latest'
  return update.up_to_date === false ? 'source_changed' : 'unchecked'
}

export function canApplyPluginUpdate(update?: PluginUpdate): boolean {
  const state = pluginUpdateState(update)
  return (state === 'update' || state === 'content') && update?.target?.schemaVersion === 1
}

export function canCheckPluginUpdate(plugin: InstalledPlugin): boolean {
  return plugin.managed && plugin.prepared !== false && !plugin.removalPending && !!(plugin.url || plugin.preinstalled)
}

export function pluginUpdateKey(plugin: InstalledPlugin, runtime: PluginInventory['runtime']): string {
  return JSON.stringify([runtime.identity, runtime.pythonPath, runtime.bindingDir, runtime.updateSourceIdentity,
    plugin.name, plugin.managed, plugin.prepared, plugin.removalPending, plugin.version, plugin.source,
    plugin.url, plugin.sourceRef, plugin.sourceSubpath, plugin.resolvedCommit, plugin.contentDigest, plugin.installedAt])
}
