import type { EditorPluginSlot, EditorSession } from '../types/editor'

export function clonePluginChain(chain?: EditorPluginSlot[]) {
  return chain?.map(slot => JSON.parse(JSON.stringify(slot)) as EditorPluginSlot)
}

/** AU component codes may contain colons, so consume fixed-width or #8hex fields. */
export function pluginAudioUnitVersion(id: string): 2 | 3 | null {
  const version = id.startsWith('au3:') ? 3 : id.startsWith('au:') ? 2 : null
  if (!version) return null
  let rest = id.slice(version === 3 ? 4 : 3)
  const codes: number[] = []
  for (let index = 0; index < 3; index++) {
    const encoded = /^#[0-9a-fA-F]{8}(?=:|$)/.test(rest)
    const field = rest.slice(0, encoded ? 9 : 4)
    if (!encoded && !/^[\x20-\x7e]{4}$/.test(field)) return null
    codes.push(encoded ? Number.parseInt(field.slice(1), 16) : [...field].reduce((value, character) => value * 256 + character.charCodeAt(0), 0))
    rest = rest.slice(field.length)
    if (index < 2) {
      if (!rest.startsWith(':')) return null
      rest = rest.slice(1)
    }
  }
  return !rest && [0x61756678, 0x61756d66, 0x61756d75].includes(codes[0]) ? version : null
}

type PluginDescriptorIdentity = { id: string; format?: string; auVersion?: 2 | 3; isShellChild?: boolean }
export function pluginDescriptorFormat(descriptor: PluginDescriptorIdentity) {
  return descriptor.format || (descriptor.id.startsWith('vst2:') ? 'vst2' : pluginAudioUnitVersion(descriptor.id) ? 'au' : 'vst3')
}

/** Favorites follow plugin identity across relocation; project slots still retain their paths. */
export function pluginFavoriteKey(descriptor: PluginDescriptorIdentity) {
  return JSON.stringify([pluginDescriptorFormat(descriptor), descriptor.id])
}

export function pluginDescriptorVariant(descriptor: PluginDescriptorIdentity) {
  const format = pluginDescriptorFormat(descriptor)
  if (format === 'au') {
    const version = pluginAudioUnitVersion(descriptor.id) || descriptor.auVersion
    return version ? `AUv${version}` : 'AU'
  }
  return format === 'vst2' && descriptor.isShellChild ? 'VST2 Shell' : format.toUpperCase()
}

/** Group only unambiguous VST2/VST3 pairs for display; their states remain format-specific. */
export function pluginDescriptorFamilies<T extends PluginDescriptorIdentity & { name: string; vendor?: string; isInstrument?: boolean | null }>(descriptors: readonly T[]): T[][] {
  const normalize = (value: string) => value.trim().replace(/\s+/g, ' ').toLowerCase()
  const familyKey = (plugin: T) => {
    const format = pluginDescriptorFormat(plugin)
    const vendor = normalize(plugin.vendor || '')
    const name = normalize(plugin.name)
    return vendor && name && (format === 'vst2' || format === 'vst3')
      ? JSON.stringify([vendor, name, plugin.isInstrument === true]) : null
  }
  const buckets = new Map<string, T[]>()
  for (const plugin of descriptors) {
    const key = familyKey(plugin)
    if (key) {
      if (!buckets.has(key)) buckets.set(key, [])
      buckets.get(key)!.push(plugin)
    }
  }
  const pairs = new Map<string, T[]>()
  for (const [key, plugins] of buckets) {
    if (plugins.length === 2 && pluginDescriptorFormat(plugins[0]) !== pluginDescriptorFormat(plugins[1])) {
      pairs.set(key, pluginDescriptorFormat(plugins[0]) === 'vst3' ? [...plugins] : [plugins[1], plugins[0]])
    }
  }
  const emitted = new Set<string>()
  const families: T[][] = []
  for (const plugin of descriptors) {
    const key = familyKey(plugin)
    const pair = key && pairs.get(key)
    if (pair) {
      if (!emitted.has(key!)) { emitted.add(key!); families.push(pair) }
    } else families.push([plugin])
  }
  return families
}

export type PluginCatalogCategory = 'eq' | 'dynamics' | 'pitch' | 'reverb' | 'delay' | 'modulation'
  | 'distortion' | 'dither' | 'instrument' | 'analyzer' | 'mastering' | 'spatial' | 'ambience'
  | 'restoration' | 'offline' | 'generator' | 'tools' | 'other'

/** Only vendor-declared metadata is used; names and paths do not establish effect types. */
export function pluginDescriptorCategory(plugin: PluginDescriptorIdentity & {
  isInstrument?: boolean | null; category?: string; subCategories?: string[]
  rawCategory?: number | null; componentType?: number
}): PluginCatalogCategory {
  if (plugin.isInstrument === true || plugin.componentType === 0x61756d75) return 'instrument'
  const format = pluginDescriptorFormat(plugin)
  if (format === 'vst3') {
    const categories: Record<string, PluginCatalogCategory> = {
      eq: 'eq', filter: 'eq', dynamics: 'dynamics', pitchshift: 'pitch', 'pitch shift': 'pitch',
      reverb: 'reverb', delay: 'delay', modulation: 'modulation', harmonic: 'distortion',
      distortion: 'distortion', dither: 'dither', analyzer: 'analyzer', mastering: 'mastering',
      spatial: 'spatial', surround: 'spatial', restoration: 'restoration', generator: 'generator', tools: 'tools',
    }
    const categoryKey = typeof plugin.category === 'string' ? plugin.category.trim().toLowerCase() : ''
    const category = Object.hasOwn(categories, categoryKey) ? categories[categoryKey] : undefined
    if (category) return category
    const tokens = Array.isArray(plugin.subCategories) ? plugin.subCategories.filter(token => typeof token === 'string')
      .flatMap(token => token.split('|')).slice(0, 32).map(token => token.trim().toLowerCase()) : []
    if (tokens[0] === 'instrument') return 'instrument'
    for (const token of tokens) if (Object.hasOwn(categories, token)) return categories[token]
  }
  if (format === 'vst2' && !plugin.isShellChild && typeof plugin.rawCategory === 'number') {
    // VST2 RoomFx declares both delays and reverbs; keep that coarse distinction.
    const categories: Record<number, PluginCatalogCategory> = {
      2: 'instrument', 3: 'analyzer', 4: 'mastering', 5: 'spatial', 6: 'ambience',
      7: 'spatial', 8: 'restoration', 9: 'offline', 11: 'generator',
    }
    return categories[plugin.rawCategory] || 'other'
  }
  return 'other'
}

/** Distinguish variants and same-name shell entries without hiding their full identities. */
export function pluginDescriptorLabels(descriptors: readonly (PluginDescriptorIdentity & { name: string; vendor?: string; path: string })[], includePath = false) {
  const count = (labels: readonly string[]) => {
    const counts = new Map<string, number>()
    for (const label of labels) counts.set(label, (counts.get(label) || 0) + 1)
    return counts
  }
  const base = descriptors.map(plugin => `${plugin.name} · ${pluginDescriptorVariant(plugin)}${plugin.vendor ? ` · ${plugin.vendor}` : ''}`)
  const baseCounts = count(base)
  const identified = base.map((label, index) => baseCounts.get(label)! > 1 ? `${label} · ${descriptors[index].id}` : label)
  const identityCounts = count(identified)
  return identified.map((label, index) => includePath || identityCounts.get(label)! > 1 ? `${label} · ${descriptors[index].path}` : label)
}

/** A saved opaque state may only be restored into the same format and plugin class. */
export function pluginSlotMatchesDescriptor(slot: EditorPluginSlot, descriptor: { id: string; classId: string; isInstrument: boolean | null; format?: string }) {
  const format = pluginDescriptorFormat(descriptor)
  return slot.format === format && descriptor.isInstrument !== true
    && (slot.pluginId === descriptor.id || (format !== 'au' && slot.pluginId === descriptor.classId))
    && (typeof slot.classId !== 'string' || slot.classId === descriptor.classId)
}

export function normalizePluginChain(value: unknown): EditorPluginSlot[] | undefined {
  if (!Array.isArray(value)) return undefined
  return value.filter(slot => slot && typeof slot === 'object' && !Array.isArray(slot))
    .map(slot => JSON.parse(JSON.stringify(slot)) as EditorPluginSlot)
}

export function projectPluginSlots(project: EditorSession) {
  return [...(project.pluginChain || []), ...project.tracks.flatMap(track => track.pluginChain || [])]
}

export function pluginChainSignature(project: EditorSession) {
  return JSON.stringify([
    project.pluginChain || [],
    project.tracks.filter(track => track.pluginChain?.length).map(track => [track.id, track.pluginChain]),
  ])
}

export function hasEditorPlugins(project: EditorSession | null | undefined) {
  return Boolean(project && projectPluginSlots(project).length)
}

export function pluginProjectVersion(project: EditorSession) {
  return hasEditorPlugins(project) || project.masterLimiter ? 4 : 3
}

export function pluginProjectBlockReason(project: EditorSession, formats: readonly string[] = ['vst3', 'vst2', 'au']): 'format' | null {
  if (projectPluginSlots(project).some(slot => !slot.bypassed && !formats.includes(slot.format))) return 'format'
  return null
}

export function applyPluginStates(project: EditorSession, states: Record<string, string>) {
  let changed = false
  for (const slot of projectPluginSlots(project)) {
    if (typeof states[slot.id] === 'string' && slot.state !== states[slot.id]) {
      slot.state = states[slot.id]
      changed = true
    }
  }
  return changed
}

/** Legacy settings remain in saved projects but are not part of the audio graph. */
export function pluginProcessingProject(project: EditorSession): EditorSession {
  return {
    ...project,
    // Row height is timeline layout only and must not reach the audio host.
    tracks: project.tracks.map(({ effects: _effects, height: _height, ...track }) => track),
  }
}

/** Captured host state updates the host signature atomically; explicit history restores remain observable. */
export function pluginProjectSignature(project: EditorSession) {
  return JSON.stringify(pluginProcessingProject(project), (key, value) => ['peaks', 'channelPeaks', 'updatedAt'].includes(key) ? undefined : value)
}

export function neutralPluginRenderProject(project: EditorSession, render: { path: string; duration: number; sampleRate: number; channels: number }): EditorSession {
  const source = { id: 'plugin-render', role: 'stem' as const, name: project.name, ...render }
  return {
    ...project, masterVolume: 1, masterPan: 0, masterLimiter: undefined, pluginChain: undefined, sources: [source],
    tracks: [{ id: 'plugin-render', sourceId: source.id, role: 'stem', name: project.name,
      volume: 1, pan: 0, muted: false, solo: false, fadeIn: 0, fadeOut: 0,
      clips: [{ id: 'plugin-render', assetId: source.id, start: 0, offset: 0, duration: render.duration,
        volume: 1, fadeIn: 0, fadeOut: 0, muted: false, locked: false }] }],
  }
}
