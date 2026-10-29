export const pluginCategories = ['convert', 'analyze', 'recognize', 'edit'] as const
export type PluginCategory = typeof pluginCategories[number]
export type PluginLocale = {
  title?: string
  description?: string
  tags?: string[]
  operations?: Record<string, string>
}
export type PluginLocales = Record<string, PluginLocale>

export function normalizePluginCategory(value: unknown): PluginCategory | null {
  return pluginCategories.includes(value as PluginCategory) ? value as PluginCategory : null
}

export function pluginLocale(locales: PluginLocales | undefined, locale: string): PluginLocale {
  const entries = Object.entries(locales || {})
  const language = locale.toLowerCase().split('-')[0]
  const fallback = entries.find(([key]) => key.toLowerCase() === 'en')?.[1] || {}
  const selected = entries.find(([key]) => key.toLowerCase() === locale.toLowerCase())?.[1]
    || entries.find(([key]) => key.toLowerCase() === language)?.[1]
    || entries.find(([key]) => key.toLowerCase().split('-')[0] === language)?.[1]
    || fallback
  return { ...fallback, ...selected, operations: { ...fallback.operations, ...selected.operations } }
}

export function pluginSearchText(plugin: { name: string; description?: string; tags?: string[]; locales?: PluginLocales }) {
  return [plugin.name, plugin.description, ...(plugin.tags || []),
    ...Object.values(plugin.locales || {}).flatMap(value => [value.title, value.description, ...(value.tags || []), ...Object.values(value.operations || {})])]
    .filter(Boolean).join(' ').toLowerCase()
}
