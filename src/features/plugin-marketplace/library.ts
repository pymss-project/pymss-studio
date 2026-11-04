import type { CatalogPlugin, InstalledPlugin, PluginUpdate } from './composable'
import { canApplyPluginUpdate, pluginUpdateState } from './updates.ts'

export type PluginLibraryEntry = {
  key: string
  name: string
  installed?: InstalledPlugin
  catalog?: CatalogPlugin
  otherSources: CatalogPlugin[]
}

function normalizedPath(value: string, windowsSource = false): { path: string; windows: boolean } {
  let path = value.trim()
  const windows = windowsSource || /^[a-z]:[\\/]/i.test(path) || path.startsWith('\\\\') || path.startsWith('//')
  if (windows) path = path.replace(/\\/g, '/')
  if (/^\/\/\?\/unc\//i.test(path)) path = `//${path.slice(8)}`
  else if (path.startsWith('//?/')) path = path.slice(4)
  const prefix = /^[a-z]:\//i.test(path) ? path.slice(0, 3) : path.startsWith('//') ? '//' : path.startsWith('/') ? '/' : ''
  const parts: string[] = []
  for (const part of path.slice(prefix.length).split('/')) {
    if (!part || part === '.') continue
    if (part === '..' && parts.length > (prefix === '//' ? 2 : 0) && parts.at(-1) !== '..') parts.pop()
    else if (part !== '..' || !prefix) parts.push(part)
  }
  path = prefix + parts.join('/')
  return { path: windows ? path.toLowerCase() : path, windows }
}

function acquisitionSource(value: string | null | undefined, subpath?: string | null): { key: string; official: boolean } | undefined {
  if (!value?.trim()) return undefined
  const source = value.trim()
  let root: string
  let windows = false
  let official = false
  if (/^[a-z][a-z\d+.-]*:\/\//i.test(source)) {
    let url: URL
    try { url = new URL(source) } catch { return undefined }
    official = url.protocol === 'https:' && ['github.com', 'www.github.com', 'gitcode.com'].includes(url.hostname)
      && !url.port && !url.username && !url.password && !url.search && !url.hash
      && /^\/pymss-project\/pymss-plugins(?:\.git)?\/?$/i.test(url.pathname)
    root = official ? 'official:pymss-plugins' : url.href.replace(/\/$/, '')
  } else {
    const local = normalizedPath(source)
    root = `path:${local.path}`
    windows = local.windows
  }
  const selection = normalizedPath(subpath || '', windows).path
  return { key: JSON.stringify([root, windows ? selection.toLowerCase() : selection]), official }
}

export function installedPluginKey(plugin: Pick<InstalledPlugin, 'name' | 'managed' | 'path'>): string {
  const name = encodeURIComponent(plugin.name)
  return plugin.managed ? `managed:${name}` : `readonly:${name}:${encodeURIComponent(normalizedPath(plugin.path).path)}`
}

export function buildPluginLibrary(installed: InstalledPlugin[], catalog: CatalogPlugin[]): PluginLibraryEntry[] {
  const entries: PluginLibraryEntry[] = []
  const managed = new Map<string, PluginLibraryEntry>()
  const managedSources = new Map<string, string | undefined>()
  for (const plugin of installed) {
    const entry: PluginLibraryEntry = { key: installedPluginKey(plugin), name: plugin.name, installed: plugin, otherSources: [] }
    entries.push(entry)
    if (plugin.managed) {
      managed.set(plugin.name, entry)
      managedSources.set(plugin.name, acquisitionSource(plugin.url, plugin.sourceSubpath)?.key)
    }
  }
  const catalogKeys = new Set<string>()
  for (const plugin of catalog) {
    const entry = managed.get(plugin.name)
    const source = acquisitionSource(plugin.url, plugin.subpath)
    if (entry && source && (source.key === managedSources.get(plugin.name) || (entry.installed?.preinstalled && source.official))) {
      entry.catalog = plugin
      continue
    }
    if (entry) {
      entry.otherSources.push(plugin)
      continue
    }
    const key = installedPluginKey({ name: plugin.name, managed: true, path: '' })
    if (catalogKeys.has(key)) continue
    catalogKeys.add(key)
    entries.push({ key, name: plugin.name, catalog: plugin, otherSources: [] })
  }
  return entries
}

export function pluginNeedsAttention(plugin: InstalledPlugin, update?: PluginUpdate): boolean {
  if (plugin.removalPending || (plugin.managed && (canApplyPluginUpdate(update) || pluginUpdateState(update) === 'error'))) return true
  if (plugin.enabled === false) return false
  return plugin.prepared === false || plugin.loaded === false || plugin.availability === 'unavailable' || plugin.compatibility?.compatible === false
    || !!(plugin.error || plugin.dependencyError || plugin.viewError)
}
