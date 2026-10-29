import { normalizePluginCategory, type PluginLocales } from './presentation.ts'

export type PluginToolView = {
  id: string
  title: string
  entry: string
  entryPath?: string
  placements: string[]
  operation?: string | null
  permissions: string[]
  sdkVersion?: number
  requiredCapabilities?: string[]
  category?: string | null
  icon?: string | null
  revision?: string
}

export type PluginToolPlugin = {
  name: string
  managed: boolean
  loaded: boolean | null
  enabled?: boolean
  toolCategory?: string | null
  toolIcon?: string | null
  locales?: PluginLocales
  contributions?: { views?: PluginToolView[] }
}

export type PluginToolEntry = {
  key: string
  routeId: string
  pluginName: string
  view: PluginToolView
  category?: string | null
  icon?: string | null
  locales?: PluginLocales
}

export function pluginToolRouteId(pluginName: string, viewId: string) {
  return `${pluginName}/${viewId}`
}

export function pluginToolKey(pluginName: string, viewId: string) {
  return `plugin:${pluginToolRouteId(pluginName, viewId)}`
}

export function pluginToolCategories(plugin: Pick<PluginToolPlugin, 'toolCategory' | 'contributions'>) {
  const fallback = normalizePluginCategory(plugin.toolCategory)
  const views = (plugin.contributions?.views || []).filter(view => Array.isArray(view.placements) && view.placements.includes('tool'))
  return [...new Set(views.length ? views.map(view => normalizePluginCategory(view.category) || fallback) : [fallback])]
}

export function pluginToolEntries(plugins: readonly PluginToolPlugin[]): PluginToolEntry[] {
  return plugins
    .filter(plugin => plugin.managed && plugin.loaded && plugin.enabled !== false)
    .flatMap(plugin => (plugin.contributions?.views || [])
      .filter(view => Array.isArray(view.placements) && view.placements.includes('tool'))
      .map(view => {
        const category = normalizePluginCategory(view.category) || normalizePluginCategory(plugin.toolCategory)
        return {
          key: pluginToolKey(plugin.name, view.id),
          routeId: pluginToolRouteId(plugin.name, view.id),
          pluginName: plugin.name,
          view,
          category,
          icon: view.icon || plugin.toolIcon,
          locales: view.id === 'tool' ? plugin.locales : undefined,
        }
      }))
}
