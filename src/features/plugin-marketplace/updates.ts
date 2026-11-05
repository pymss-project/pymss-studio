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

export type PluginUpdateSelection = {
  name: string
  version: string | null
  locales?: InstalledPlugin['locales']
  installationKey: string
  update: PluginUpdate
}

export function selectPluginUpdates(inventory: PluginInventory | null, updates: Record<string, PluginUpdate>): PluginUpdateSelection[] {
  if (!inventory) return []
  const names = new Set<string>()
  return inventory.installed.flatMap(plugin => {
    const update = updates[plugin.name]
    if (names.has(plugin.name) || !canCheckPluginUpdate(plugin) || !canApplyPluginUpdate(update)) return []
    names.add(plugin.name)
    return [JSON.parse(JSON.stringify({ name: plugin.name, version: plugin.version, locales: plugin.locales,
      installationKey: pluginUpdateKey(plugin, inventory.runtime), update })) as PluginUpdateSelection]
  })
}
