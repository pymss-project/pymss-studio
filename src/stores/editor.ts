import { defineStore } from 'pinia'
import { masterLimiterRenderRate, normalizeMasterLimiter } from '@/utils/masterLimiter'
import { createLogger } from '@/utils/logger'
import { clonePluginChain, hasEditorPlugins, neutralPluginRenderProject, normalizePluginChain, pluginProcessingProject, projectPluginSlots, pluginProjectVersion } from '@/utils/editorPlugins'
import { assertPluginProjectSupported, currentPluginSession, discardUnavailablePluginSession, observePluginSession, PluginHostDialogPendingError, pluginHostCapabilities, reloadPluginSession, snapshotPluginStates, unloadPluginSession, type PluginSessionState } from '@/utils/pluginHost'
import { computed, onScopeDispose, ref, watch } from 'vue'
import { convertFileSrc, invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import i18n, { getCurrentLocale } from '@/i18n'
import {
  overwriteClipsInRange,
  syncSingleClipFadeCompatibility,
  timelineDuration,
} from '@/utils/editorClips'
import { registerWindowCloseGuard, WindowClosePersistenceError } from '@/utils/windowCloseGuards'
import { isTauriRuntime } from '@/utils/appStore'
import { isWebRuntime } from '@/utils/webRuntime'
import type { SeparationTask, StemOutput } from '@/stores/task'
import type {
  EditorMasterLimiter,
  EditorPluginSlot,
  EditorExportFormat,
  EditorExportOptions,
  EditorProjectSummary,
  EditorSession,
  EditorSource,
  EditorSourceRole,
  EditorClip,
  EditorTrack,
  EditorAssetTreeNode,
} from '@/types/editor'

const studioLog = createLogger('ui.stores.editor')

export type {
  EditorExportFormat,
  EditorExportOptions,
  EditorProjectSummary,
  EditorSession,
  EditorSource,
  EditorSourceRole,
  EditorTrack,
  EditorAssetTreeNode,
} from '@/types/editor'

type HistorySnapshot = {
  tracks: EditorTrack[]
  masterVolume: number
  masterPan: number
  masterLimiter?: EditorMasterLimiter
  pluginChain?: EditorPluginSlot[]
  selectedTrackId: string | null
  selectedClipId: string | null
}

type ScanAudioPathsResult = {
  files: string[]
  warnings: string[]
}

type LinkedEditorAsset = {
  path: string
  name: string
  originKind: string
  originRoot?: string | null
  relativePath?: string | null
  missing?: boolean
}

type ImportEditorAssetsResult = {
  files: LinkedEditorAsset[]
  warnings: string[]
}

type EditorProjectCleanupResult = {
  deletedProjectIds: string[]
  missingProjectIds: string[]
  blockedProjectIds: string[]
  failedProjectIds: string[]
}

export type OrphanedEditorProject = {
  projectId: string
  sourceTaskId: string
  name: string
}

type RelinkEditorSourcesResult = {
  project: PersistedSession
  relinked: number
  unresolved: string[]
}

type PickFileResult = string | null

type AudioMetadata = {
  path: string
  name: string
  duration: number
  sampleRate: number
  channels: number
}

type WaveformPeaks = {
  path: string
  peaksPath: string
  peaks: number[]
  channelPeaks?: number[][]
  duration: number
  sampleRate: number
  channels: number
}

type LoadedWaveformPeaks = {
  peaks: number[]
  channelPeaks: number[][]
}

type ExportResult = {
  path: string
  duration: number
  sampleRate: number
  channels: number
  format: string
  peakProtectionApplied?: boolean
  peakAdjustmentDb?: number
  pluginTailLimited?: boolean
}

export type EditorExportProgress = {
  phase: 'preparing' | 'rendering' | 'finalizing' | 'writing' | 'completed'
  completed: number
  total: number
  current: string
}

type EditorExportWorkerEvent = {
  type: string
  taskId?: string | null
  requestId?: string | null
  payload?: Record<string, unknown>
}

type PendingEditorExport = {
  id: string
  projectId: string
  exportDir?: string
  progressOffset?: number
  resolve: (result: ExportResult) => void
  reject: (error: Error) => void
}

export class EditorExportCancelledError extends Error {
  constructor(message = 'Editor export cancelled') {
    super(message)
    this.name = 'EditorExportCancelledError'
  }
}

type PersistedSession = EditorSession & { version?: number }

const HISTORY_LIMIT = 80
const AUTO_SAVE_DELAY = 420
const NATIVE_RENDER_PROGRESS_WEIGHT = 75
const WAVEFORM_FULL_RESOLUTION = 2200
const COLOR_BY_STEM: Record<string, string> = {
  vocals: '#ff6b7c',
  vocal: '#ff6b7c',
  voice: '#ff6b7c',
  accompaniment: '#5d8dff',
  instrumental: '#5d8dff',
  instruments: '#5d8dff',
  drums: '#f2b45a',
  drum: '#f2b45a',
  bass: '#4fc58f',
  other: '#b08cff',
}

function makeId(prefix: string) {
  return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value))
}

function fileName(path: string) {
  return path.split(/[/\\]/).pop() || path
}

function stripExt(name: string) {
  return name.replace(/\.[^/.\\]+$/, '')
}

function safeProjectToken(value: string) {
  const normalized = String(value || '')
    .split('')
    .map((ch) => (/^[a-zA-Z0-9._-]$/.test(ch) ? ch : '_'))
    .join('')
    .replace(/^_+|_+$/g, '')
  const trimmed = normalized || 'project'
  return trimmed.slice(0, 96)
}

function projectIdForTaskId(taskId: string) {
  return `edit_${safeProjectToken(taskId)}`
}

function blankProjectName() {
  const locale = getCurrentLocale()
  return String(i18n.global.t('projects.blankProjectDefaultName', {}, { locale }))
}

export function isDefaultBlankProjectName(name: string) {
  const normalized = String(name || '').trim()
  if (!normalized) return true
  return normalized === '未命名工程'
    || normalized === '未命名空工程'
    || normalized === 'Untitled Project'
    || normalized === 'Untitled Blank Project'
}

function stemKeyFromOutput(output: StemOutput | { stem?: string; path?: string }) {
  const raw = String(output.stem || stripExt(fileName(output.path || ''))).trim().toLowerCase()
  if (!raw) return 'other'
  if (raw.includes('vocal') || raw.includes('voice')) return 'vocals'
  if (raw.includes('instrument') || raw.includes('accompaniment') || raw.includes('karaoke')) return 'accompaniment'
  if (raw.includes('drum')) return 'drums'
  if (raw.includes('bass')) return 'bass'
  if (raw.includes('other')) return 'other'
  return raw
}

const STEM_NAME_KEYS: Record<string, string> = {
  vocals: 'editor.stemVocals',
  accompaniment: 'editor.stemAccompaniment',
  drums: 'editor.stemDrums',
  bass: 'editor.stemBass',
  other: 'editor.stemOther',
}

const LEGACY_STEM_NAMES: Record<string, string[]> = {
  vocals: ['vocals', 'vocal', 'voice', '人声'],
  accompaniment: ['instrumental', 'instrument', 'accompaniment', 'karaoke', '伴奏'],
  drums: ['drums', 'drum', '鼓组'],
  bass: ['bass', '贝斯'],
  other: ['other', '其他'],
}

const EXACT_STEM_KEYS = new Map(
  Object.entries(LEGACY_STEM_NAMES).flatMap(([stemKey, names]) => (
    names
      .filter(name => /^[a-z]+$/i.test(name))
      .map(name => [normalizedDefaultName(name), stemKey] as const)
  )),
)

function normalizedDefaultName(value: string | null | undefined) {
  return String(value || '').trim().toLowerCase().replace(/[\s_-]+/g, ' ')
}

function localizedStemName(stemKey: string | null | undefined, fallback: string) {
  if (!stemKey) return fallback
  const exactStemKey = EXACT_STEM_KEYS.get(normalizedDefaultName(stemKey))
  const broadStemKey = stemKeyFromOutput({ stem: stemKey })
  const fallbackIsLegacyName = (LEGACY_STEM_NAMES[broadStemKey] || [])
    .some(name => normalizedDefaultName(name) === normalizedDefaultName(fallback))
  const canonicalKey = exactStemKey || (fallbackIsLegacyName ? broadStemKey : '')
  const translationKey = STEM_NAME_KEYS[canonicalKey]
  return translationKey ? String(i18n.global.t(translationKey)) : fallback
}

function recordingTrackIndex(name: string) {
  const match = name.trim().match(/^(?:recording|录音)\s*(\d+)$/i)
  return match ? Math.max(1, Number(match[1])) : null
}

function isLegacyAutoTrackName(name: string, role: EditorSourceRole, stemKey?: string | null) {
  if (role === 'recording') return recordingTrackIndex(name) !== null
  if (role !== 'stem' || !stemKey) return false
  const canonicalKey = stemKeyFromOutput({ stem: stemKey || '' })
  const candidates = [stemKey || '', ...(LEGACY_STEM_NAMES[canonicalKey] || [])]
  const normalizedName = normalizedDefaultName(name)
  return candidates.some((candidate) => normalizedDefaultName(candidate) === normalizedName)
}

function localizedAutoTrackName(
  name: string,
  role: EditorSourceRole,
  stemKey?: string | null,
) {
  if (role === 'recording') {
    const index = recordingTrackIndex(name)
    return index === null ? name : String(i18n.global.t('editor.recordingTrackName', { index }))
  }
  if (role === 'stem') return localizedStemName(stemKey, name)
  return name
}

function trackColor(role: EditorSourceRole, stemKey?: string | null) {
  if (role === 'recording') return '#e76f91'
  if (role === 'reference') return '#93a1b6'
  return COLOR_BY_STEM[String(stemKey || '').toLowerCase()] || '#7aa2ff'
}

function duplicatedTrackColor(source: EditorSource) {
  if (source.role === 'stem') return trackColor('stem', source.stemKey)
  return trackColor(source.role, source.stemKey)
}

function cloneLegacyTrackEffects(effects: unknown): EditorTrack['effects'] {
  return effects && typeof effects === 'object' && !Array.isArray(effects)
    ? JSON.parse(JSON.stringify(effects)) as Record<string, unknown>
    : undefined
}

function cloneTracks(tracks: EditorTrack[]) {
  return tracks.map((track) => ({
    ...track,
    effects: cloneLegacyTrackEffects(track.effects),
    pluginChain: clonePluginChain(track.pluginChain),
    clips: track.clips?.map((clip) => ({ ...clip })),
  }))
}

function normalizeSource(source: Partial<EditorSource>): EditorSource {
  const channelPeaks = Array.isArray(source.channelPeaks)
    ? source.channelPeaks
        .filter((channel) => Array.isArray(channel))
        .map((channel) => channel.map((value) => Number(value || 0)))
    : []
  const channelCount = Math.max(Number(source.channels || 0), channelPeaks.length)
  return {
    id: String(source.id || makeId('source')),
    role: source.role === 'reference' || source.role === 'recording' ? source.role : 'stem',
    stemKey: source.stemKey ? String(source.stemKey) : null,
    path: String(source.path || ''),
    name: String(source.name || fileName(String(source.path || '')) || 'Untitled'),
    duration: Number(source.duration || 0),
    sampleRate: Number(source.sampleRate || 0),
    channels: channelCount,
    peaksPath: source.peaksPath ? String(source.peaksPath) : null,
    peaks: Array.isArray(source.peaks) ? source.peaks.map((value) => Number(value || 0)) : [],
    channelPeaks,
    originKind: source.originKind ? String(source.originKind) : undefined,
    originRoot: source.originRoot ? String(source.originRoot) : null,
    relativePath: source.relativePath ? String(source.relativePath) : null,
    missing: Boolean(source.missing),
  }
}

function normalizePathKey(value: string) {
  return value.replace(/\\/g, '/').toLowerCase()
}

function sourceDisplayGroup(source: EditorSource) {
  if (source.role === 'recording') return String(i18n.global.t('editor.assetGroupRecordings'))
  if (source.originKind === 'task-result' || source.role === 'stem') {
    return String(i18n.global.t('editor.assetGroupSeparationResults'))
  }
  return String(i18n.global.t('editor.assetGroupExternal'))
}

function resolveAssetUrl(path: string) {
  try {
    return convertFileSrc(path)
  } catch {
    const normalized = path.replace(/\\/g, '/')
    if (/^[a-zA-Z]:\//.test(normalized)) return `file:///${normalized}`
    return path
  }
}

async function loadPeaksFromCache(path?: string | null, sourcePath?: string | null) {
  if (!path) return null
  try {
    const response = await fetch(resolveAssetUrl(path))
    if (!response.ok) return null
    const data = await response.json() as { path?: unknown, peaks?: unknown, channelPeaks?: unknown }
    if (sourcePath && typeof data.path === 'string' && normalizePathKey(data.path) !== normalizePathKey(sourcePath)) {
      return null
    }
    if (!Array.isArray(data.peaks)) return null
    return {
      peaks: data.peaks.map((value) => Number(value || 0)),
      channelPeaks: Array.isArray(data.channelPeaks)
        ? data.channelPeaks
            .filter((channel) => Array.isArray(channel))
            .map((channel) => channel.map((value) => Number(value || 0)))
        : [],
    }
  } catch {
    return null
  }
}

function normalizeClip(clip: Partial<EditorClip>, source?: EditorSource): EditorClip {
  const offset = clamp(Number(clip.offset || 0), 0, Math.max(0, Number(source?.duration || Infinity)))
  const availableDuration = Math.max(0, Number(source?.duration || 0) - offset)
  const requestedDuration = Math.max(0, Number(clip.duration ?? availableDuration))
  const duration = availableDuration > 0 ? Math.min(requestedDuration, availableDuration) : requestedDuration
  return {
    id: String(clip.id || makeId('clip')),
    assetId: String(clip.assetId || source?.id || ''),
    start: Math.max(0, Number(clip.start || 0)),
    offset,
    duration,
    volume: clamp(Number(clip.volume ?? 1), 0, 2),
    fadeIn: clamp(Number(clip.fadeIn || 0), 0, duration),
    fadeOut: clamp(Number(clip.fadeOut || 0), 0, duration),
    muted: Boolean(clip.muted),
    locked: Boolean(clip.locked),
  }
}

function normalizeTrack(
  track: Partial<EditorTrack>,
  sources: Map<string, EditorSource>,
  clipFadesCanonical: boolean,
): EditorTrack {
  const source = sources.get(String(track.sourceId || ''))
  const role = track.role === 'reference' || track.role === 'recording'
    ? track.role
    : (source?.role || 'stem')
  const stemKey = source?.stemKey || null
  const storedName = String(track.name || source?.name || 'Track')
  const autoName = typeof track.autoName === 'boolean'
    ? track.autoName
    : isLegacyAutoTrackName(storedName, role, stemKey)
  const clips = Array.isArray(track.clips)
    ? track.clips
        .map((clip) => normalizeClip(clip, sources.get(String(clip.assetId || track.sourceId || ''))))
        .filter((clip) => clip.assetId && clip.duration > 0)
    : []
  if (!clips.length && source && source.duration > 0) {
    clips.push(normalizeClip({
      id: `clip_${String(track.id || source.id)}`,
      assetId: source.id,
      duration: source.duration,
      fadeIn: Number(track.fadeIn || 0),
      fadeOut: Number(track.fadeOut || 0),
    }, source))
  }
  let fadeIn = Math.max(0, Number(track.fadeIn || 0))
  let fadeOut = Math.max(0, Number(track.fadeOut || 0))
  if (clips.length === 1) {
    const clip = clips[0]
    if (clipFadesCanonical) {
      fadeIn = clip.fadeIn
      fadeOut = clip.fadeOut
    } else {
      clip.fadeIn = clamp(fadeIn, 0, clip.duration)
      clip.fadeOut = clamp(fadeOut, 0, clip.duration)
      fadeIn = clip.fadeIn
      fadeOut = clip.fadeOut
    }
  }
  return {
    id: String(track.id || makeId('track')),
    sourceId: String(track.sourceId || source?.id || ''),
    role,
    name: autoName ? localizedAutoTrackName(storedName, role, stemKey) : storedName,
    autoName,
    color: String(track.color || trackColor(role, stemKey)),
    volume: clamp(Number(track.volume ?? 1), 0, 2),
    pan: clamp(Number(track.pan ?? 0), -1, 1),
    muted: Boolean(track.muted),
    solo: Boolean(track.solo),
    fadeIn,
    fadeOut,
    effects: cloneLegacyTrackEffects(track.effects),
    pluginChain: normalizePluginChain(track.pluginChain),
    type: track.type === 'recording' || role === 'recording'
      ? 'recording'
      : (track.type === 'audio' ? 'audio' : role),
    clips,
  }
}

function normalizeSession(session: PersistedSession): EditorSession {
  const sources = Array.isArray(session.sources)
    ? session.sources.map(normalizeSource)
    : []
  const sourceMap = new Map(sources.map((source) => [source.id, source]))
  const clipFadesCanonical = Number(session.version || 0) >= 3
  const tracks = Array.isArray(session.tracks)
    ? session.tracks.map((track) => normalizeTrack(track, sourceMap, clipFadesCanonical))
    : []

  return {
    id: String(session.id || makeId('session')),
    name: String(session.name || 'Untitled Session'),
    sourceTaskId: session.sourceTaskId ? String(session.sourceTaskId) : undefined,
    sourceResultDir: session.sourceResultDir ? String(session.sourceResultDir) : undefined,
    masterVolume: clamp(Number(session.masterVolume ?? 1), 0, 2),
    masterPan: clamp(Number(session.masterPan ?? 0), -1, 1),
    masterLimiter: session.masterLimiter ? normalizeMasterLimiter(session.masterLimiter) : undefined,
    pluginChain: normalizePluginChain(session.pluginChain),
    sources,
    tracks,
    createdAt: Number(session.createdAt || Date.now()),
    updatedAt: Number(session.updatedAt || Date.now()),
  }
}

function toPersistedSession(session: EditorSession) {
  return {
    ...session,
    version: pluginProjectVersion(session),
    pluginChain: clonePluginChain(session.pluginChain),
    tracks: cloneTracks(session.tracks),
    sources: session.sources.map((source) => ({
      ...source,
      peaks: [],
      channelPeaks: [],
    })),
  }
}

const hasPeaks = (p?: number[]) => p !== undefined && p.length > 0 && !(p.length === 1 && p[0] === -1)

function hasRenderablePeaks(source: EditorSource) {
  return hasPeaks(source.peaks) && (source.channels < 2 || (source.channelPeaks?.length || 0) >= 2)
}

function cachedPeaksMatchSource(source: EditorSource, peaks: LoadedWaveformPeaks) {
  return source.channels < 2 || peaks.channelPeaks.length >= 2
}

function sourceToExportAsset(source: EditorSource) {
  return {
    id: source.id,
    path: source.path,
    name: source.name,
    duration: source.duration,
    sampleRate: source.sampleRate,
    channels: source.channels,
    peaksPath: source.peaksPath || null,
    peaks: [],
  }
}

function runLimited<T>(items: T[], limit: number, runner: (item: T) => Promise<void>) {
  let index = 0
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (index < items.length) {
      const item = items[index]
      index += 1
      await runner(item)
    }
  })
  return Promise.all(workers)
}

function trackToExportTrack(track: EditorTrack, source?: EditorSource) {
  const clips = track.clips?.length
    ? track.clips.map((clip) => ({ ...clip }))
    : [{
        id: `clip_${track.id}`,
        assetId: track.sourceId,
        start: 0,
        offset: 0,
        duration: source?.duration || 0,
        volume: 1,
        fadeIn: track.fadeIn,
        fadeOut: track.fadeOut,
        muted: false,
        locked: false,
      }]
  return {
    id: track.id,
    sourceId: track.sourceId,
    name: track.name,
    type: track.role,
    volume: track.volume,
    pan: track.pan,
    muted: track.muted,
    solo: track.solo,
    clips,
  }
}

export const useEditorStore = defineStore('editor', () => {
  const session = ref<EditorSession | null>(null)
  const loading = ref(false)
  const saving = ref(false)
  const exporting = ref(false)
  const exportCancelling = ref(false)
  const exportProgress = ref<EditorExportProgress | null>(null)
  const lastExport = ref<ExportResult | null>(null)
  const lastError = ref<string | null>(null)
  const pluginStateDirty = ref(false)
  const pluginStateError = ref<string | null>(null)
  const pluginStateUnavailable = ref(false)
  const selectedTrackId = ref<string | null>(null)
  const selectedClipId = ref<string | null>(null)
  const pixelsPerSecond = ref(96)
  const exportFormat = ref<EditorExportFormat>('wav')
  const projectSummaries = ref<EditorProjectSummary[]>([])

  const undoStack = ref<HistorySnapshot[]>([])
  const redoStack = ref<HistorySnapshot[]>([])
  const interactionDepth = ref(0)
  const pendingPeaks = new Map<string, Promise<EditorSource | null>>()
  let pendingExport: PendingEditorExport | null = null
  let exportUnlisten: UnlistenFn | null = null
  let exportListenerPromise: Promise<void> | null = null
  let exportStartPromise: Promise<void> | null = null
  let exportCancelRequested = false
  let exportRenderController: AbortController | null = null
  let exportRequestSequence = 0
  let nativeRenderTaskId: string | null = null

  const canUndo = computed(() => undoStack.value.length > 0)
  const canRedo = computed(() => redoStack.value.length > 0)
  const masterVolume = computed(() => session.value?.masterVolume ?? 1)
  const masterPan = computed(() => session.value?.masterPan ?? 0)

  const sourceMap = computed(() => {
    const map = new Map<string, EditorSource>()
    session.value?.sources.forEach((source) => map.set(source.id, source))
    return map
  })

  const selectedTrack = computed(() => {
    if (!session.value || !selectedTrackId.value) return null
    return session.value.tracks.find((track) => track.id === selectedTrackId.value) || null
  })

  const selectedClip = computed(() => {
    const track = selectedTrack.value
    if (!track || !selectedClipId.value) return null
    return track.clips?.find((clip) => clip.id === selectedClipId.value) || null
  })

  const selectedSource = computed(() => {
    const track = selectedTrack.value
    if (!track) return null
    return sourceMap.value.get(selectedClip.value?.assetId || '')
      || sourceMap.value.get(track.sourceId)
      || sourceMap.value.get(track.clips?.[track.clips.length - 1]?.assetId || '')
      || null
  })

  const stemTracks = computed(() => session.value?.tracks.filter((track) => track.role === 'stem') || [])
  const referenceTracks = computed(() => session.value?.tracks.filter((track) => track.role === 'reference') || [])
  const assetTree = computed<EditorAssetTreeNode[]>(() => {
    if (!session.value) return []
    const rootName = String(i18n.global.t('editor.assets'))
    const root: EditorAssetTreeNode = {
      key: '__root__',
      name: rootName,
      path: '',
      expanded: true,
      children: [],
      assets: [],
    }
    const folderMap = new Map<string, EditorAssetTreeNode>([[root.key, root]])

    const ensureFolder = (parts: string[]) => {
      let current = root
      let currentPath = ''
      for (const part of parts) {
        if (!part) continue
        currentPath = currentPath ? `${currentPath}/${part}` : part
        const key = `folder:${currentPath.toLowerCase()}`
        let next = folderMap.get(key)
        if (!next) {
          next = {
            key,
            name: part,
            path: currentPath,
            expanded: currentPath.split('/').length <= 1,
            children: [],
            assets: [],
          }
          folderMap.set(key, next)
          current.children.push(next)
        }
        current = next
      }
      return current
    }

    for (const source of session.value.sources) {
      const groupName = sourceDisplayGroup(source)
      const relative = String(source.relativePath || '').replace(/\\/g, '/')
      const parts = [groupName, ...relative.split('/').slice(0, -1).filter(Boolean)]
      ensureFolder(parts).assets.push(source)
    }

    const sortTree = (node: EditorAssetTreeNode) => {
      node.children.sort((a, b) => a.name.localeCompare(b.name))
      node.assets.sort((a, b) => a.name.localeCompare(b.name))
      node.children.forEach(sortTree)
    }
    sortTree(root)
    return root.children.length ? root.children : [{ ...root, name: rootName }]
  })

  const duration = computed(() => {
    return session.value ? timelineDuration(session.value.tracks, sourceMap.value) : 0
  })

  function snapshot(): HistorySnapshot | null {
    if (!session.value) return null
    return {
      tracks: cloneTracks(session.value.tracks),
      masterVolume: session.value.masterVolume,
      masterPan: session.value.masterPan,
      masterLimiter: session.value.masterLimiter ? { ...session.value.masterLimiter } : undefined,
      pluginChain: clonePluginChain(session.value.pluginChain),
      selectedTrackId: selectedTrackId.value,
      selectedClipId: selectedClipId.value,
    }
  }

  function applySnapshot(next: HistorySnapshot) {
    if (!session.value) return
    session.value.tracks = cloneTracks(next.tracks)
    session.value.masterVolume = next.masterVolume
    session.value.masterPan = next.masterPan
    session.value.masterLimiter = next.masterLimiter ? { ...next.masterLimiter } : undefined
    session.value.pluginChain = clonePluginChain(next.pluginChain)
    selectedTrackId.value = next.selectedTrackId
    selectedClipId.value = next.selectedClipId
  }

  function pushHistory() {
    if (interactionDepth.value > 0) return
    const snap = snapshot()
    if (!snap) return
    undoStack.value.push(snap)
    if (undoStack.value.length > HISTORY_LIMIT) undoStack.value.shift()
    redoStack.value = []
  }

  function undo() {
    if (!session.value || !undoStack.value.length) return
    const current = snapshot()
    const previous = undoStack.value.pop()!
    if (current) redoStack.value.push(current)
    applySnapshot(previous)
    scheduleSave()
  }

  function redo() {
    if (!session.value || !redoStack.value.length) return
    const current = snapshot()
    const next = redoStack.value.pop()!
    if (current) undoStack.value.push(current)
    applySnapshot(next)
    scheduleSave()
  }

  function beginInteraction() {
    if (interactionDepth.value === 0) pushHistory()
    interactionDepth.value += 1
  }

  function commitInteraction() {
    if (interactionDepth.value > 0) interactionDepth.value -= 1
    if (interactionDepth.value === 0) scheduleSave()
  }

  function clearHistory() {
    undoStack.value = []
    redoStack.value = []
    interactionDepth.value = 0
  }

  let pluginSaveTimer: ReturnType<typeof setTimeout> | null = null
  let saveTimer: ReturnType<typeof setTimeout> | null = null
  let saveDrainTask: Promise<EditorSession | null> | null = null
  const pendingProjectSaves = new Map<string, PersistedSession>()
  const suspendedProjectSaves = new Set<string>()
  let discardingWindowChanges = false

  function queueSessionSave() {
    if (!session.value || discardingWindowChanges) return null
    const project = toPersistedSession(session.value)
    if (suspendedProjectSaves.has(project.id)) return null
    pendingProjectSaves.set(project.id, project)
    return project.id
  }

  async function capturePluginState(project: EditorSession, force = false, recovery = false) {
    try {
      return await snapshotPluginStates(project, { force, recovery })
    } catch (cause) {
      const detail = cause instanceof Error ? cause.message : String(cause)
      const error = cause instanceof PluginHostDialogPendingError ? detail : String(i18n.global.t('editor.plugins.stateCaptureFailed', { detail }))
      if (session.value?.id === project.id || currentPluginSession()?.project === project) {
        pluginStateDirty.value = true
        pluginStateError.value = error
        if (!(cause instanceof PluginHostDialogPendingError)) lastError.value = error
      }
      if (cause instanceof PluginHostDialogPendingError) throw cause
      throw new Error(error)
    }
  }

  function schedulePluginStateSave(project: EditorSession, attempt = 0) {
    if (discardingWindowChanges) return
    if (pluginSaveTimer) clearTimeout(pluginSaveTimer)
    pluginSaveTimer = setTimeout(async () => {
      pluginSaveTimer = null
      if (session.value !== project) return
      try {
        await capturePluginState(project)
        if (discardingWindowChanges || session.value !== project) return
        queueSessionSave()
        await drainPendingSaves()
      } catch {
        // One automatic retry handles transient capture/storage failures; further retries are explicit.
        if (attempt === 0 && session.value === project) schedulePluginStateSave(project, 1)
      }
    }, attempt === 0 ? 900 : 2000)
  }

  let pendingDialogMessage: string | null = null
  const stopObservingPlugins = observePluginSession(host => {
    if (!host || host.project !== session.value) return
    pluginStateUnavailable.value = host.unavailable
    if (host.error) {
      pluginStateDirty.value = true
      pluginStateError.value = host.interactionRequired ? host.error : String(i18n.global.t('editor.plugins.stateCaptureFailed', { detail: host.error }))
      pendingDialogMessage = host.interactionRequired ? pluginStateError.value : null
      if (!host.interactionRequired) lastError.value = pluginStateError.value
      return
    }
    if (pendingDialogMessage) {
      if (pluginStateError.value === pendingDialogMessage) pluginStateError.value = null
      if (lastError.value === pendingDialogMessage) lastError.value = null
      pendingDialogMessage = null
    }
    if (host.dirty) {
      pluginStateDirty.value = true
      schedulePluginStateSave(host.project)
    }
  })
  watch(() => session.value?.id, () => {
    pendingDialogMessage = null
    if (pluginSaveTimer) clearTimeout(pluginSaveTimer)
    pluginSaveTimer = null
    pluginStateDirty.value = false
    pluginStateError.value = null
    pluginStateUnavailable.value = false
  })
  onScopeDispose(() => {
    stopObservingPlugins()
    if (pluginSaveTimer) clearTimeout(pluginSaveTimer)
  })

  async function retryPluginStateSave() { return saveProject() }

  async function restoreSavedPluginSession() {
    const project = session.value
    if (!project) return
    const persisted = await invoke<PersistedSession>('load_editor_project', { projectId: project.id })
    if (session.value !== project) throw new Error(String(i18n.global.t('editor.plugins.stateCaptureSuperseded')))
    const savedSlots = projectPluginSlots(persisted)
    for (const slot of projectPluginSlots(project)) {
      const saved = savedSlots.find(item => item.id === slot.id && item.path === slot.path && item.pluginId === slot.pluginId)
      if (saved?.state !== undefined) slot.state = saved.state
      else delete slot.state
    }
    await reloadPluginSession(project)
    await saveProject()
  }


  async function releasePluginSession(project: EditorSession) {
    if (currentPluginSession()?.project.id !== project.id) return
    if (pluginSaveTimer) clearTimeout(pluginSaveTimer)
    pluginSaveTimer = null
    await capturePluginState(project, true)
    pendingProjectSaves.set(project.id, toPersistedSession(project))
    await drainPendingSaves()
    await unloadPluginSession(project.id)
  }

  async function drainPendingSaves() {
    if (discardingWindowChanges) return null
    if (saveDrainTask) return saveDrainTask
    const task = (async () => {
      saving.value = true
      try {
        while (pendingProjectSaves.size) {
          if (discardingWindowChanges) return null
          const next = pendingProjectSaves.entries().next().value as [string, PersistedSession] | undefined
          if (!next) break
          const [projectId, project] = next
          pendingProjectSaves.delete(projectId)
          try {
            const target = session.value?.id === projectId ? session.value : project
            if (hasEditorPlugins(target)) await capturePluginState(target)
            if (discardingWindowChanges) return null
            const captured = toPersistedSession(target)
            const result = await invoke<PersistedSession>('save_editor_project', { project: captured })
            if (session.value?.id === projectId) {
              if (result.updatedAt) session.value.updatedAt = Number(result.updatedAt)
              const host = currentPluginSession()
              pluginStateDirty.value = Boolean(host?.project === session.value && host.dirty)
              pluginStateError.value = null
              lastError.value = null
              if (!pluginStateDirty.value && pluginSaveTimer) { clearTimeout(pluginSaveTimer); pluginSaveTimer = null }
            }
          } catch (error) {
            // Keep a newer snapshot if edits arrived while this save was in flight.
            if (!discardingWindowChanges && !pendingProjectSaves.has(projectId)) pendingProjectSaves.set(projectId, toPersistedSession(session.value?.id === projectId ? session.value : project))
            const detail = error instanceof Error ? error.message : String(error)
            if (!(error instanceof PluginHostDialogPendingError)) lastError.value = detail
            if (session.value?.id === projectId && hasEditorPlugins(project)) {
              pluginStateDirty.value = true
              pluginStateError.value = detail
            }
            throw error
          }
        }
        return session.value
      } finally {
        saving.value = false
      }
    })()
    saveDrainTask = task
    try {
      return await task
    } finally {
      if (saveDrainTask === task) saveDrainTask = null
    }
  }

  function scheduleSave() {
    if (!queueSessionSave()) return
    if (saveTimer) clearTimeout(saveTimer)
    saveTimer = setTimeout(() => {
      saveTimer = null
      void drainPendingSaves().catch(() => undefined)
    }, AUTO_SAVE_DELAY)
  }

  async function flushSave() {
    queueSessionSave()
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = null }
    if (pluginSaveTimer) { clearTimeout(pluginSaveTimer); pluginSaveTimer = null }
    const host = currentPluginSession()
    if (host && host.project !== session.value && (host.dirty || host.error)) await releasePluginSession(host.project)
    if (session.value && hasEditorPlugins(session.value)) await capturePluginState(session.value, true)
    return drainPendingSaves()
  }

  // The store outlives the editor route. Keeping persistence here ensures a
  // delayed autosave is still flushed when the user closes from another page.
  onScopeDispose(registerWindowCloseGuard(async () => {
    const closingProject = session.value
    try {
      await flushSave()
      const host = currentPluginSession()
      if (host?.project === closingProject) await unloadPluginSession(host.project.id)
      else if (host) await releasePluginSession(host.project)
    } catch (cause) {
      throw new WindowClosePersistenceError(cause, async () => {
        if (session.value !== closingProject) throw new Error(String(i18n.global.t('editor.plugins.stateCaptureSuperseded')))
        discardingWindowChanges = true
        try {
          if (saveTimer) { clearTimeout(saveTimer); saveTimer = null }
          if (pluginSaveTimer) { clearTimeout(pluginSaveTimer); pluginSaveTimer = null }
          await saveDrainTask?.catch(() => undefined)
          if (session.value !== closingProject) throw new Error(String(i18n.global.t('editor.plugins.stateCaptureSuperseded')))
          const host = currentPluginSession()
          if (host && host.project !== closingProject) throw new Error(String(i18n.global.t('editor.plugins.stateCaptureSuperseded')))
          if (host) await unloadPluginSession(host.project.id, { discardUnsaved: true })
          pendingProjectSaves.clear()
        } finally {
          discardingWindowChanges = false
        }
      })
    }
  }, -100))

  onScopeDispose(registerWindowCloseGuard(async () => {
    if (exporting.value) await cancelExport()
  }, 100))

  function selectTrack(trackId: string | null) {
    selectedTrackId.value = trackId
    const track = session.value?.tracks.find((item) => item.id === trackId)
    if (!track?.clips?.some((clip) => clip.id === selectedClipId.value)) {
      selectedClipId.value = track?.clips?.[0]?.id || null
    }
  }

  function selectClip(trackId: string, clipId: string) {
    const track = session.value?.tracks.find((item) => item.id === trackId)
    if (!track?.clips?.some((clip) => clip.id === clipId)) return
    selectedTrackId.value = trackId
    selectedClipId.value = clipId
  }

  async function requestProjectFromTask(task: SeparationTask) {
    return invoke<PersistedSession>('create_editor_project_from_task', {
      payload: {
        taskId: task.id,
        input: task.input,
        outputDir: task.output,
        outputs: task.outputs as StemOutput[],
      },
    })
  }

  async function createFromTask(task: SeparationTask) {
    loading.value = true
    lastError.value = null
    try {
      const result = await requestProjectFromTask(task)
      session.value = normalizeSession(result)
      selectTrack(session.value.tracks[0]?.id || null)
      await hydratePeaksFromCache(session.value.id)
      const metadataChanged = await hydrateSourceMetadataState(session.value.id)
      clearHistory()
      const currentProjectId = session.value.id
      if (metadataChanged) await saveProject()
      hydrateSessionSourcesInBackground(currentProjectId)
      return session.value
    } catch (error) {
      lastError.value = error instanceof Error ? error.message : String(error)
      throw error
    } finally {
      loading.value = false
    }
  }

  async function openProjectWindow(projectId: string) {
    const result = await invoke<{ url?: string }>('open_editor_window', { payload: { projectId } })
    if (result?.url && isWebRuntime()) window.open(`#${result.url.replace(/^#/, '')}`, '_blank')
    return result
  }

  async function projectExists(projectId: string) {
    return invoke<boolean>('editor_project_exists', { projectId })
  }

  async function ensureProjectForTask(task: SeparationTask, options?: { loadIntoSession?: boolean }) {
    const loadIntoSession = options?.loadIntoSession ?? true
    const projectId = projectIdForTaskId(task.id)
    const exists = await projectExists(projectId)
    if (exists) {
      return { id: projectId }
    }
    if (!loadIntoSession) {
      const result = await requestProjectFromTask(task)
      const normalized = normalizeSession(result)
      return { id: normalized.id }
    }
    return createFromTask(task)
  }

  async function listProjects() {
    const result = await invoke<EditorProjectSummary[]>('list_editor_projects')
    projectSummaries.value = [...(result || [])].sort((a, b) => b.updatedAt - a.updatedAt)
    return projectSummaries.value
  }

  async function refreshProjects() {
    return listProjects()
  }

  // Keep this interface for the future import-first editor flow. The visible
  // blank-project entry is hidden until external audio import has clear UI.
  async function createBlankProject(name?: string) {
    const locale = getCurrentLocale()
    const result = await invoke<PersistedSession>('create_blank_editor_project', {
      payload: {
        name: name?.trim() || blankProjectName(),
        locale,
      },
    })
    const normalized = normalizeSession(result)
    const summary: EditorProjectSummary = {
      id: normalized.id,
      name: normalized.name,
      sourceTaskId: normalized.sourceTaskId,
      sourceResultDir: normalized.sourceResultDir,
      createdAt: normalized.createdAt,
      updatedAt: normalized.updatedAt,
      type: normalized.sourceTaskId ? 'task' : 'blank',
    }
    projectSummaries.value = [summary, ...projectSummaries.value.filter((item) => item.id !== summary.id)]
      .sort((a, b) => b.updatedAt - a.updatedAt)
    return normalized
  }

  async function deleteProject(projectId: string) {
    // Serialize deletion after queued writes so a delayed autosave cannot
    // recreate the project after the backend removes it.
    await drainPendingSaves()
    suspendedProjectSaves.add(projectId)
    pendingProjectSaves.delete(projectId)
    let result: boolean
    try {
      result = await invoke<boolean>('delete_editor_project', { projectId })
    } catch (error) {
      suspendedProjectSaves.delete(projectId)
      if (session.value?.id === projectId) scheduleSave()
      throw error
    }
    projectSummaries.value = projectSummaries.value.filter((item) => item.id !== projectId)
    if (session.value?.id === projectId) {
      session.value = null
      selectedTrackId.value = null
      selectedClipId.value = null
      clearHistory()
    }
    suspendedProjectSaves.delete(projectId)
    return result
  }

  async function deleteProjectsForTasks(taskIds: string[]) {
    const uniqueTaskIds = [...new Set(taskIds.map(id => id.trim()).filter(Boolean))]
    const projectIds = uniqueTaskIds.map(projectIdForTaskId)
    const emptyResult = (): EditorProjectCleanupResult => ({
      deletedProjectIds: [],
      missingProjectIds: [],
      blockedProjectIds: [],
      failedProjectIds: [],
    })
    if (!projectIds.length) return emptyResult()
    if (!isTauriRuntime()) {
      return { ...emptyResult(), missingProjectIds: projectIds }
    }

    await drainPendingSaves()
    projectIds.forEach((projectId) => {
      suspendedProjectSaves.add(projectId)
      pendingProjectSaves.delete(projectId)
    })
    try {
      const result = await invoke<EditorProjectCleanupResult>('delete_editor_projects_for_tasks', {
        taskIds: uniqueTaskIds,
      })
      const removedProjectIds = new Set([
        ...(result.deletedProjectIds || []),
        ...(result.missingProjectIds || []),
      ])
      projectSummaries.value = projectSummaries.value.filter(item => !removedProjectIds.has(item.id))
      if (session.value && removedProjectIds.has(session.value.id)) clearSession()
      return {
        deletedProjectIds: result.deletedProjectIds || [],
        missingProjectIds: result.missingProjectIds || [],
        blockedProjectIds: result.blockedProjectIds || [],
        failedProjectIds: result.failedProjectIds || [],
      }
    } finally {
      projectIds.forEach(projectId => suspendedProjectSaves.delete(projectId))
    }
  }

  async function listOpenProjectsForTasks(taskIds: string[]) {
    const uniqueTaskIds = [...new Set(taskIds.map(id => id.trim()).filter(Boolean))]
    if (!uniqueTaskIds.length || !isTauriRuntime()) return []
    const backendOpenProjectIds = await invoke<string[]>('list_open_editor_projects_for_tasks', {
      taskIds: uniqueTaskIds,
    })
    return [...new Set(backendOpenProjectIds || [])]
  }

  async function listOrphanedProjects(activeTaskIds: string[]) {
    if (!isTauriRuntime()) return [] as OrphanedEditorProject[]
    return invoke<OrphanedEditorProject[]>('list_orphaned_editor_projects', {
      activeTaskIds: [...new Set(activeTaskIds.map(id => id.trim()).filter(Boolean))],
    })
  }

  async function deleteOrphanedProjects(activeTaskIds: string[]) {
    const emptyResult = (): EditorProjectCleanupResult => ({
      deletedProjectIds: [],
      missingProjectIds: [],
      blockedProjectIds: [],
      failedProjectIds: [],
    })
    if (!isTauriRuntime()) return emptyResult()
    const orphanedProjects = await listOrphanedProjects(activeTaskIds)
    const projectIds = orphanedProjects.map(project => project.projectId)
    if (!projectIds.length) return emptyResult()
    await drainPendingSaves()
    projectIds.forEach((projectId) => {
      suspendedProjectSaves.add(projectId)
      pendingProjectSaves.delete(projectId)
    })
    try {
      const result = await invoke<EditorProjectCleanupResult>('delete_orphaned_editor_projects', {
        activeTaskIds: [...new Set(activeTaskIds.map(id => id.trim()).filter(Boolean))],
      })
      const removedProjectIds = new Set([
        ...(result.deletedProjectIds || []),
        ...(result.missingProjectIds || []),
      ])
      projectSummaries.value = projectSummaries.value.filter(item => !removedProjectIds.has(item.id))
      if (session.value && removedProjectIds.has(session.value.id)) clearSession()
      return {
        deletedProjectIds: result.deletedProjectIds || [],
        missingProjectIds: result.missingProjectIds || [],
        blockedProjectIds: result.blockedProjectIds || [],
        failedProjectIds: result.failedProjectIds || [],
      }
    } finally {
      projectIds.forEach(projectId => suspendedProjectSaves.delete(projectId))
    }
  }

  function clearSession() {
    session.value = null
    selectedTrackId.value = null
    selectedClipId.value = null
    clearHistory()
  }

  async function loadProject(projectId: string) {
    loading.value = true
    lastError.value = null
    try {
      const result = await invoke<PersistedSession>('load_editor_project', { projectId })
      session.value = normalizeSession(result)
      await hydratePeaksFromCache(session.value.id)
      const metadataChanged = await hydrateSourceMetadataState(session.value.id)
      selectTrack(session.value.tracks[0]?.id || null)
      clearHistory()
      const currentProjectId = session.value.id
      if (metadataChanged) await saveProject()
      hydrateSessionSourcesInBackground(currentProjectId)
      return session.value
    } catch (error) {
      lastError.value = error instanceof Error ? error.message : String(error)
      throw error
    } finally {
      loading.value = false
    }
  }

  async function saveProject() {
    if (!queueSessionSave() && !pendingProjectSaves.size) return null
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = null }
    if (pluginSaveTimer) { clearTimeout(pluginSaveTimer); pluginSaveTimer = null }
    const host = currentPluginSession()
    if (host && host.project !== session.value && (host.dirty || host.error)) await releasePluginSession(host.project)
    if (session.value && hasEditorPlugins(session.value)) await capturePluginState(session.value, true)
    return drainPendingSaves()
  }

  async function getMetadata(path: string) {
    return invoke<AudioMetadata>('get_audio_metadata', { payload: { path } })
  }

  function hasUsableAudioMetadata(metadata: AudioMetadata | EditorSource | null | undefined) {
    return Boolean(metadata && [metadata.duration, metadata.sampleRate, metadata.channels]
      .every((value) => Number.isFinite(value) && value > 0))
  }

  async function ensurePeaks(sourceId: string) {
    if (!session.value) return null
    const source = session.value.sources.find((item) => item.id === sourceId)
    if (!source || source.missing || hasRenderablePeaks(source)) return source || null

    const cachedPeaks = await loadPeaksFromCache(source.peaksPath, source.path)
    if (cachedPeaks?.peaks.length && cachedPeaksMatchSource(source, cachedPeaks)) {
      source.peaks = cachedPeaks.peaks
      source.channelPeaks = cachedPeaks.channelPeaks
      return source
    }

    const pending = pendingPeaks.get(sourceId)
    if (pending) return pending
    const projectId = session.value.id
    const task = (async () => {
      try {
        const result = await invoke<WaveformPeaks>('generate_waveform_peaks', {
          payload: {
            projectId,
            path: source.path,
            resolution: WAVEFORM_FULL_RESOLUTION,
          },
        })
        if (!session.value || session.value.id !== projectId) return source
        const current = session.value.sources.find((item) => item.id === sourceId)
        if (!current) return source
        current.peaks = result.peaks?.length ? result.peaks : [-1]
        current.channelPeaks = result.channelPeaks || []
        current.peaksPath = result.peaksPath
        current.duration = Number(result.duration || current.duration)
        current.sampleRate = Number(result.sampleRate || current.sampleRate)
        current.channels = Number(result.channels || current.channels)
        return current
      } catch {
        return source
      } finally {
        pendingPeaks.delete(sourceId)
      }
    })()
    pendingPeaks.set(sourceId, task)
    return task
  }

  function ensurePeaksInBackground(sourceId: string) {
    void ensurePeaks(sourceId)
      .then(() => {
        if (!session.value) return
        scheduleSave()
      })
      .catch(() => {})
  }

  async function ensurePeaksForAllSources(projectId = session.value?.id || '') {
    if (!session.value || !projectId || session.value.id !== projectId) return
    const sources = [...session.value.sources]
    await runLimited(sources, 2, async (source) => {
      if (!session.value || session.value.id !== projectId) return
      if (hasPeaks(source.peaks)) return
      await ensurePeaks(source.id)
    })
  }

  function hydrateSessionSourcesInBackground(projectId: string) {
    void hydrateSessionSources(projectId)
      .then((changed) => {
        if (!changed) return
        if (session.value?.id !== projectId) return
        void saveProject()
      })
      .catch(() => {})
  }

  async function hydrateSourceMetadataState(projectId = session.value?.id || '') {
    if (!session.value || !projectId || session.value.id !== projectId) return false

    let changed = false
    const sources = [...session.value.sources]

    await runLimited(sources, 4, async (source) => {
      if (!session.value || session.value.id !== projectId) return
      const before = {
        name: source.name,
        duration: source.duration,
        sampleRate: source.sampleRate,
        channels: source.channels,
        missing: Boolean(source.missing),
      }
      const metadata = await getMetadata(source.path).catch(() => null)
      if (!session.value || session.value.id !== projectId) return
      source.missing = !metadata
      if (metadata) {
        source.name = metadata.name || source.name
        source.duration = Number(metadata.duration || source.duration)
        source.sampleRate = Number(metadata.sampleRate || source.sampleRate)
        const peakChannels = source.channelPeaks?.filter(channel => channel.length).length || 0
        source.channels = Math.max(Number(metadata.channels || 0), peakChannels, Number(source.channels || 0))
      }
      if (
        source.name !== before.name
        || source.duration !== before.duration
        || source.sampleRate !== before.sampleRate
        || source.channels !== before.channels
        || Boolean(source.missing) !== before.missing
      ) {
        changed = true
      }
    })

    return changed
  }

  async function hydrateSessionSources(projectId = session.value?.id || '') {
    if (!session.value || !projectId || session.value.id !== projectId) return false

    let changed = false
    const sources = [...session.value.sources]

    await runLimited(sources, 2, async (source) => {
      if (!session.value || session.value.id !== projectId) return

      const before = {
        name: source.name,
        duration: source.duration,
        sampleRate: source.sampleRate,
        channels: source.channels,
        peaksPath: source.peaksPath || null,
        peaksCount: source.peaks?.length || 0,
        missing: Boolean(source.missing),
      }

      const metadata = await getMetadata(source.path).catch(() => null)
      if (!session.value || session.value.id !== projectId) return

      source.missing = !metadata
      if (source.missing) {
        source.duration = Number(source.duration || 0)
        source.sampleRate = Number(source.sampleRate || 0)
        source.channels = Number(source.channels || 0)
        source.peaks = Array.isArray(source.peaks) ? source.peaks : []
      } else if (!hasPeaks(source.peaks)) {
        await ensurePeaks(source.id)
      }

      if (metadata) {
        source.name = metadata.name || source.name
        source.duration = Number(metadata.duration || source.duration)
        source.sampleRate = Number(metadata.sampleRate || source.sampleRate)
        const peakChannels = source.channelPeaks?.filter(channel => channel.length).length || 0
        source.channels = Math.max(Number(metadata.channels || 0), peakChannels, Number(source.channels || 0))
      }

      if (
        source.name !== before.name
        || source.duration !== before.duration
        || source.sampleRate !== before.sampleRate
        || source.channels !== before.channels
        || (source.peaksPath || null) !== before.peaksPath
        || (source.peaks?.length || 0) !== before.peaksCount
        || Boolean(source.missing) !== before.missing
      ) {
        changed = true
      }
    })

    return changed
  }

  async function hydratePeaksFromCache(projectId = session.value?.id || '') {
    if (!session.value || !projectId || session.value.id !== projectId) return
    const sources = [...session.value.sources]
    await Promise.allSettled(sources.map(async (source) => {
      if (hasRenderablePeaks(source)) return
      const cachedPeaks = await loadPeaksFromCache(source.peaksPath, source.path)
      if (cachedPeaks?.peaks.length && cachedPeaksMatchSource(source, cachedPeaks) && session.value?.id === projectId) {
        source.peaks = cachedPeaks.peaks
        source.channelPeaks = cachedPeaks.channelPeaks
      }
    }))
  }

  async function addReferenceSourceByPath(path: string) {
    if (!session.value) throw new Error('Editor session is not loaded')
    const source = await ensureSourceByPath(path, {
      originKind: 'external',
      originRoot: path.replace(/[/\\][^/\\]+$/, '') || null,
      relativePath: fileName(path),
    })
    addTrackFromSourceId(source.id)
    return source
  }

  async function ensureSourceByPath(path: string, options?: Partial<EditorSource>) {
    if (!session.value) throw new Error('Editor session is not loaded')
    const targetSession = session.value
    const normalized = normalizePathKey(path)
    const existing = session.value.sources.find((source) => normalizePathKey(source.path) === normalized)
    if (existing) return existing

    const metadata = await getMetadata(path)
    if (session.value !== targetSession) throw new Error('Editor project changed during asset import')
    if (!hasUsableAudioMetadata(metadata)) throw new Error(String(i18n.global.t('editor.assetMetadataInvalid')))
    const concurrentSource = targetSession.sources.find((source) => normalizePathKey(source.path) === normalized)
    if (concurrentSource) return concurrentSource
    const source: EditorSource = {
      id: makeId('source'),
      role: 'reference',
      stemKey: null,
      path,
      name: metadata.name || fileName(path),
      duration: Number(metadata.duration || 0),
      sampleRate: Number(metadata.sampleRate || 0),
      channels: Number(metadata.channels || 0),
      peaksPath: null,
      peaks: [],
      originKind: options?.originKind || 'external',
      originRoot: options?.originRoot ? String(options.originRoot) : null,
      relativePath: options?.relativePath ? String(options.relativePath) : fileName(path),
      missing: !metadata || Boolean(options?.missing),
    }
    session.value.sources.push(source)
    if (!source.missing) ensurePeaksInBackground(source.id)
    scheduleSave()
    return source
  }

  function addTrackFromSourceId(sourceId: string) {
    if (!session.value) throw new Error('Editor session is not loaded')
    const source = session.value.sources.find((item) => item.id === sourceId)
    if (!source) throw new Error('Source asset not found')

    pushHistory()
    const track: EditorTrack = {
      id: makeId('track'),
      sourceId: source.id,
      role: 'reference',
      name: stripExt(source.name),
      color: duplicatedTrackColor(source),
      volume: 1,
      pan: 0,
      muted: false,
      solo: false,
      fadeIn: 0,
      fadeOut: 0,
      type: 'reference',
      clips: [normalizeClip({
        id: makeId('clip'),
        assetId: source.id,
        duration: source.duration,
      }, source)],
    }
    session.value.tracks.push(track)
    selectTrack(track.id)
    scheduleSave()
    return track
  }

  function addRecordingTrack() {
    if (!session.value) throw new Error('Editor session is not loaded')
    pushHistory()
    const index = session.value.tracks.filter((track) => track.role === 'recording').length + 1
    const track: EditorTrack = {
      id: makeId('track'),
      sourceId: '',
      role: 'recording',
      type: 'recording',
      name: String(i18n.global.t('editor.recordingTrackName', { index })),
      autoName: true,
      color: trackColor('recording'),
      volume: 1,
      pan: 0,
      muted: false,
      solo: false,
      fadeIn: 0,
      fadeOut: 0,
      clips: [],
    }
    session.value.tracks.push(track)
    selectTrack(track.id)
    scheduleSave()
    return track
  }

  async function addRecordingClip(
    trackId: string,
    recording: AudioMetadata,
    start: number,
  ) {
    if (!session.value) throw new Error('Editor session is not loaded')
    pushHistory()
    let track = session.value.tracks.find((item) => item.id === trackId)
    if (!track) {
      const index = session.value.tracks.filter((item) => item.role === 'recording').length + 1
      track = {
        id: makeId('track'),
        sourceId: '',
        role: 'recording',
        type: 'recording',
        name: String(i18n.global.t('editor.recordingTrackName', { index })),
        autoName: true,
        color: trackColor('recording'),
        volume: 1,
        pan: 0,
        muted: false,
        solo: false,
        fadeIn: 0,
        fadeOut: 0,
        clips: [],
      }
      session.value.tracks.push(track)
    }
    const source: EditorSource = {
      id: makeId('source'),
      role: 'recording',
      stemKey: null,
      path: recording.path,
      name: recording.name || fileName(recording.path),
      duration: Math.max(0, Number(recording.duration || 0)),
      sampleRate: Math.max(0, Number(recording.sampleRate || 0)),
      channels: Math.max(1, Number(recording.channels || 1)),
      peaksPath: null,
      peaks: [],
      channelPeaks: [],
      originKind: 'recording',
      relativePath: `recordings/${recording.name || fileName(recording.path)}`,
      missing: false,
    }
    session.value.sources.push(source)
    track.role = 'recording'
    track.type = 'recording'
    track.sourceId = source.id
    const clip = normalizeClip({
      id: makeId('clip'),
      assetId: source.id,
      start: Math.max(0, Number(start || 0)),
      duration: source.duration,
    }, source)
    const currentClips = track.clips || []
    track.clips = [
      ...overwriteClipsInRange(currentClips, clip.start, clip.duration, () => makeId('clip')),
      clip,
    ].sort((left, right) => left.start - right.start)
    syncSingleClipFadeCompatibility(track)
    selectClip(track.id, clip.id)
    scheduleSave()
    ensurePeaksInBackground(source.id)
    return source
  }

  async function scanAssets(paths: string[], options?: { addTracks?: boolean }) {
    if (!session.value) throw new Error('Editor session is not loaded')
    const targetSession = session.value
    const imported = await invoke<ImportEditorAssetsResult>('import_editor_assets', {
      projectId: targetSession.id,
      paths,
    })
    if (session.value !== targetSession) throw new Error('Editor project changed during asset import')
    const sourcePaths = new Map<string, string>()
    const warnings = [...(imported.warnings || [])]
    for (const asset of imported.files || []) {
      if (session.value !== targetSession) throw new Error('Editor project changed during asset import')
      try {
        const source = await ensureSourceByPath(asset.path, {
          originKind: asset.originKind,
          originRoot: asset.originRoot || null,
          relativePath: asset.relativePath || fileName(asset.path),
          missing: asset.missing,
        })
        if (source.missing || !hasUsableAudioMetadata(source)) {
          throw new Error(String(i18n.global.t('editor.assetMetadataInvalid')))
        }
        if (!sourcePaths.has(source.id)) sourcePaths.set(source.id, asset.path)
      } catch (error) {
        if (session.value !== targetSession) throw error
        const detail = error instanceof Error ? error.message : String(error)
        warnings.push(String(i18n.global.t('editor.assetImportFailed', { name: fileName(asset.path), detail })))
      }
    }
    // Metadata can finish while a slider or clip is being dragged. Keep the
    // imported tracks in their own undo action after that interaction ends.
    while (options?.addTracks && sourcePaths.size && interactionDepth.value > 0 && session.value === targetSession) {
      await new Promise<void>((resolve) => {
        const stop = watch([interactionDepth, session], () => {
          if (interactionDepth.value > 0 && session.value === targetSession) return
          stop()
          resolve()
        }, { flush: 'sync' })
      })
    }
    if (session.value !== targetSession) throw new Error('Editor project changed during asset import')
    const currentSources = new Map(targetSession.sources.map((source) => [source.id, source]))
    for (const [sourceId, path] of sourcePaths) {
      const source = currentSources.get(sourceId)
      if (source && !source.missing && hasUsableAudioMetadata(source)) continue
      sourcePaths.delete(sourceId)
      const detail = String(i18n.global.t(source ? 'editor.assetMetadataInvalid' : 'editor.assetRemovedDuringImport'))
      warnings.push(String(i18n.global.t('editor.assetImportFailed', { name: fileName(path), detail })))
    }
    if (options?.addTracks && sourcePaths.size) {
      beginInteraction()
      try {
        for (const sourceId of sourcePaths.keys()) addTrackFromSourceId(sourceId)
      } finally {
        commitInteraction()
      }
    }
    await saveProject()
    return {
      files: [...sourcePaths.values()],
      warnings,
    } satisfies ScanAudioPathsResult
  }

  function sourcesInUse() {
    if (!session.value) return []
    const sourceIds = new Set(session.value.tracks.flatMap((track) => [
      track.sourceId,
      ...(track.clips || []).map((clip) => clip.assetId),
    ]).filter(Boolean))
    return session.value.sources.filter((source) => sourceIds.has(source.id))
  }

  function missingSources() {
    return session.value?.sources.filter((source) => source.missing) || []
  }

  function missingSourcesInUse() {
    return sourcesInUse().filter((source) => source.missing)
  }

  function assertNoMissingSourcesInUse(message: string) {
    const missing = missingSourcesInUse()
    if (!missing.length) return
    throw new Error(message)
  }

  async function pickRelinkFile() {
    return invoke<PickFileResult>('pick_single_audio_file')
  }

  async function relinkSource(sourceId: string, pickedPath?: string | null) {
    if (!session.value) throw new Error('Editor session is not loaded')
    const source = session.value.sources.find((item) => item.id === sourceId)
    if (!source) throw new Error('Source asset not found')
    const resolvedPath = pickedPath ?? await pickRelinkFile()
    if (!resolvedPath) return null
    const result = await invoke<RelinkEditorSourcesResult>('relink_editor_sources', {
      payload: {
        projectId: session.value.id,
        sourceId,
        pickedPath: resolvedPath,
      },
    })
    session.value = normalizeSession(result.project)
    await hydratePeaksFromCache(session.value.id)
    hydrateSessionSourcesInBackground(session.value.id)
    return result
  }

  async function relinkMissingSources(pickedPath?: string | null) {
    const firstMissing = missingSources()[0]
    if (!firstMissing) return null
    return relinkSource(firstMissing.id, pickedPath)
  }

  function renameTrack(trackId: string, name: string, commit = true) {
    const track = session.value?.tracks.find((item) => item.id === trackId)
    if (!track) return
    const currentName = track.name
    const trimmedName = name.trim()
    const resolvedName = trimmedName || currentName

    if (!commit) {
      if (resolvedName === currentName) return
      track.name = resolvedName
      return
    }

    if (resolvedName === currentName) {
      scheduleSave()
      return
    }

    pushHistory()
    track.name = resolvedName
    track.autoName = false
    scheduleSave()
  }

  function localizeDefaultTrackNames() {
    if (!session.value) return
    for (const track of session.value.tracks) {
      if (!track.autoName) continue
      const source = sourceMap.value.get(track.sourceId)
      track.name = localizedAutoTrackName(track.name, track.role, source?.stemKey)
    }
  }

  function toggleTrackFlag(trackId: string, flag: 'muted' | 'solo') {
    const track = session.value?.tracks.find((item) => item.id === trackId)
    if (!track) return
    pushHistory()
    track[flag] = !track[flag]
    scheduleSave()
  }

  function setTrackVolume(trackId: string, value: number) {
    const track = session.value?.tracks.find((item) => item.id === trackId)
    if (!track) return
    track.volume = clamp(Number(value), 0, 2)
    if (interactionDepth.value === 0) scheduleSave()
  }

  function setTrackPan(trackId: string, value: number) {
    const track = session.value?.tracks.find((item) => item.id === trackId)
    if (!track) return
    track.pan = clamp(Number(value), -1, 1)
    if (interactionDepth.value === 0) scheduleSave()
  }

  function setClipFades(
    trackId: string,
    clipId: string,
    patch: { fadeIn?: number; fadeOut?: number },
  ) {
    const track = session.value?.tracks.find((item) => item.id === trackId)
    const clip = track?.clips?.find((item) => item.id === clipId)
    if (!track || !clip) return
    const maxDuration = Math.max(0, Number(clip.duration || 0))
    pushHistory()
    if (patch.fadeIn !== undefined) clip.fadeIn = clamp(Number(patch.fadeIn || 0), 0, maxDuration)
    if (patch.fadeOut !== undefined) clip.fadeOut = clamp(Number(patch.fadeOut || 0), 0, maxDuration)
    syncSingleClipFadeCompatibility(track)
    scheduleSave()
  }

  function setClipTiming(
    trackId: string,
    clipId: string,
    patch: Partial<Pick<EditorClip, 'start' | 'offset' | 'duration'>>,
  ) {
    const track = session.value?.tracks.find((item) => item.id === trackId)
    const clip = track?.clips?.find((item) => item.id === clipId)
    if (!track || !clip) return
    const source = sourceMap.value.get(clip.assetId)
    const sourceDuration = Math.max(0, Number(source?.duration || 0))
    const maxOffset = sourceDuration > 0 ? Math.max(0, sourceDuration - 0.01) : Number.MAX_SAFE_INTEGER
    const nextOffset = clamp(Number(patch.offset ?? clip.offset), 0, maxOffset)
    const availableDuration = sourceDuration > 0
      ? Math.max(0.01, sourceDuration - nextOffset)
      : Number.MAX_SAFE_INTEGER
    clip.start = Math.max(0, Number(patch.start ?? clip.start))
    clip.offset = nextOffset
    clip.duration = clamp(Number(patch.duration ?? clip.duration), 0.01, availableDuration)
    clip.fadeIn = clamp(clip.fadeIn, 0, clip.duration)
    clip.fadeOut = clamp(clip.fadeOut, 0, clip.duration)
    syncSingleClipFadeCompatibility(track)
    if (interactionDepth.value === 0) scheduleSave()
  }

  function setMasterVolume(value: number) {
    if (!session.value) return
    session.value.masterVolume = clamp(Number(value), 0, 2)
    if (interactionDepth.value === 0) scheduleSave()
  }

  function setMasterLimiter(patch: Partial<EditorMasterLimiter>) {
    if (!session.value) return
    const previous = normalizeMasterLimiter(session.value.masterLimiter)
    const next = normalizeMasterLimiter({ ...previous, ...patch })
    if (JSON.stringify(previous) === JSON.stringify(next)) return
    pushHistory()
    session.value.masterLimiter = next
    if (interactionDepth.value === 0) scheduleSave()
  }

  function setMasterPan(value: number) {
    if (!session.value) return
    session.value.masterPan = clamp(Number(value), -1, 1)
    if (interactionDepth.value === 0) scheduleSave()
  }

  function removeTrack(trackId: string) {
    if (!session.value) return
    const track = session.value.tracks.find((item) => item.id === trackId)
    if (!track) return
    pushHistory()
    session.value.tracks = session.value.tracks.filter((item) => item.id !== trackId)
    if (selectedTrackId.value === trackId) {
      selectTrack(session.value.tracks[0]?.id || null)
    }
    scheduleSave()
  }

  function removeClip(trackId: string, clipId: string) {
    const track = session.value?.tracks.find((item) => item.id === trackId)
    const clipIndex = track?.clips?.findIndex((item) => item.id === clipId) ?? -1
    if (!track || clipIndex < 0) return false

    pushHistory()
    const removedClip = track.clips![clipIndex]
    const nextClips = track.clips!.filter((item) => item.id !== clipId)
    track.clips = nextClips
    syncSingleClipFadeCompatibility(track)

    if (track.sourceId === removedClip.assetId && !nextClips.some((item) => item.assetId === removedClip.assetId)) {
      track.sourceId = nextClips[nextClips.length - 1]?.assetId || ''
    }

    if (selectedTrackId.value === trackId && selectedClipId.value === clipId) {
      selectedClipId.value = nextClips[Math.min(clipIndex, nextClips.length - 1)]?.id || null
    }

    scheduleSave()
    return true
  }

  function removeSource(sourceId: string) {
    if (!session.value) return { removedSource: false, removedTracks: 0 }
    const source = session.value.sources.find((item) => item.id === sourceId)
    if (!source) return { removedSource: false, removedTracks: 0 }
    if (source.role === 'stem') return { removedSource: false, removedTracks: 0 }

    pushHistory()
    const removedTrackIds: string[] = []
    session.value.tracks = session.value.tracks.flatMap((track) => {
      const nextClips = (track.clips || []).filter((clip) => clip.assetId !== sourceId)
      const usedByTrack = track.sourceId === sourceId || nextClips.length !== (track.clips || []).length
      if (!usedByTrack) return [track]
      if (!nextClips.length) {
        removedTrackIds.push(track.id)
        return []
      }
      const nextTrack = { ...track, sourceId: nextClips[nextClips.length - 1].assetId, clips: nextClips }
      syncSingleClipFadeCompatibility(nextTrack)
      return [nextTrack]
    })
    session.value.sources = session.value.sources.filter((item) => item.id !== sourceId)

    if (selectedTrackId.value && removedTrackIds.includes(selectedTrackId.value)) {
      selectTrack(session.value.tracks[0]?.id || null)
    } else if (selectedClipId.value && !selectedTrack.value?.clips?.some((clip) => clip.id === selectedClipId.value)) {
      selectTrack(selectedTrackId.value)
    }

    scheduleSave()
    return { removedSource: true, removedTracks: removedTrackIds.length }
  }

  async function ensureEditorExportListener() {
    if (!isTauriRuntime() || exportUnlisten) return
    if (!exportListenerPromise) {
      exportListenerPromise = listen<EditorExportWorkerEvent>('pymss://worker-event', (event) => {
        const workerEvent = event.payload
        const pending = pendingExport
        const eventId = workerEvent.taskId || workerEvent.requestId
        if (workerEvent.type === 'editor_plugin_render_progress') {
          if (!nativeRenderTaskId || eventId !== nativeRenderTaskId || exportCancelRequested) return
          const payload = workerEvent.payload || {}
          const total = Number(payload.total)
          const completed = Number(payload.completed)
          if (!Number.isFinite(total) || total <= 0 || !Number.isFinite(completed) || completed < 0 || completed > total) return
          const next = completed / total * NATIVE_RENDER_PROGRESS_WEIGHT
          const previous = exportProgress.value
          if (previous && next < previous.completed / Math.max(1, previous.total) * 100) return
          const phase = payload.phase === 'preparing' ? 'preparing'
            : payload.phase === 'rendering' ? 'rendering' : 'finalizing'
          exportProgress.value = { phase, completed: next, total: 100, current: '' }
          return
        }
        if (!pending || eventId !== pending.id) return

        if (workerEvent.type === 'editor_export_progress') {
          const payload = workerEvent.payload || {}
          const total = Number(payload.total ?? 100)
          const completed = Number(payload.completed ?? 0)
          if (!Number.isFinite(total) || total <= 0 || !Number.isFinite(completed) || completed < 0 || completed > total) return
          const offset = pending.progressOffset || 0
          const nextCompleted = offset ? offset + completed / total * (100 - offset) : completed
          const nextTotal = offset ? 100 : total
          const previous = exportProgress.value
          if (previous && nextCompleted / nextTotal < previous.completed / Math.max(1, previous.total)) return
          exportProgress.value = {
            phase: String(payload.phase || 'preparing') as EditorExportProgress['phase'],
            completed: nextCompleted,
            total: nextTotal,
            current: String(payload.current || ''),
          }
          return
        }

        if (workerEvent.type === 'editor_mix_exported') {
          pendingExport = null
          pending.resolve(workerEvent.payload as ExportResult)
          return
        }

        if (workerEvent.type === 'task_cancelled') {
          pendingExport = null
          pending.reject(new EditorExportCancelledError())
          return
        }

        if (workerEvent.type === 'error') {
          pendingExport = null
          const message = workerEvent.payload?.code === 'EDITOR_EXPORT_PEAK_EXCEEDED'
            ? String(i18n.global.t('editor.exportPeakExceeded'))
            : String(workerEvent.payload?.message || 'Editor export failed')
          pending.reject(new Error(message))
        }
      }).then((unlisten) => {
        exportUnlisten = unlisten
      }).finally(() => {
        exportListenerPromise = null
      })
    }
    await exportListenerPromise
  }

  async function exportMix(options?: EditorExportFormat | EditorExportOptions) {
    if (!session.value) throw new Error('Editor session is not loaded')
    if (exporting.value) throw new Error('Editor export is already running')
    assertNoMissingSourcesInUse(String(i18n.global.t('editor.assetOfflineBlocked')))
    const normalized = typeof options === 'string'
      ? { format: options, audioParams: undefined }
      : (options || {})
    const fmt = normalized.format || exportFormat.value
    const taskId = `editor_export_${Date.now().toString(36)}_${++exportRequestSequence}_${Math.random().toString(36).slice(2, 8)}`
    const exportProject = session.value
    let snapshot = JSON.parse(JSON.stringify(exportProject)) as EditorSession
    const renderController = new AbortController()
    exportRenderController = renderController
    const pluginRenderRequired = projectPluginSlots(snapshot).some(slot => !slot.bypassed) || normalizeMasterLimiter(snapshot.masterLimiter).enabled
    let pluginTailLimited = false
    exporting.value = true
    exportCancelling.value = false
    exportCancelRequested = false
    exportProgress.value = { phase: 'preparing', completed: 0, total: 100, current: '' }
    lastError.value = null
    try {
      await ensureEditorExportListener()
      if (exportCancelRequested) throw new EditorExportCancelledError()
      const prepared = pluginRenderRequired
        ? await (async () => {
            const hostCapabilities = await pluginHostCapabilities()
            if (!hostCapabilities.enabled) throw new Error(String(i18n.global.t('editor.plugins.unavailable')))
            assertPluginProjectSupported(exportProject, hostCapabilities.formats || ['vst3'])
            if (projectPluginSlots(exportProject).some(slot => !slot.bypassed)) await capturePluginState(exportProject, true)
            if (exportCancelRequested) throw new EditorExportCancelledError()
            // Capture can wait while the project is edited. Freeze its complete
            // post-capture version instead of mixing fresh plugin states into an older mix.
            snapshot = JSON.parse(JSON.stringify(exportProject)) as EditorSession
            assertPluginProjectSupported(snapshot, hostCapabilities.formats || ['vst3'])
            nativeRenderTaskId = taskId
            exportProgress.value = { phase: 'preparing', completed: 0, total: 100, current: '' }
            try {
              const rendered = await invoke<{ path: string; duration: number; sampleRate: number; channels: number; tailLimited?: boolean }>('plugin_host_render', {
                project: pluginProcessingProject(snapshot), taskId,
                sampleRate: normalizeMasterLimiter(snapshot.masterLimiter).enabled
                  ? masterLimiterRenderRate(snapshot, normalized.audioParams?.sampleRate) : undefined,
              })
              exportProgress.value = { phase: 'finalizing', completed: NATIVE_RENDER_PROGRESS_WEIGHT, total: 100, current: '' }
              pluginTailLimited = rendered.tailLimited === true
              return { session: neutralPluginRenderProject(snapshot, rendered), sourceSampleRate: rendered.sampleRate }
            } finally {
              nativeRenderTaskId = null
            }
          })()
        : { session: snapshot, sourceSampleRate: undefined }
      exportRenderController = null
      if (exportCancelRequested) throw new EditorExportCancelledError()
      const project = prepared.session
      const completion = new Promise<ExportResult>((resolve, reject) => {
        pendingExport = {
          id: taskId,
          projectId: snapshot.id,
          exportDir: normalized.exportDir,
          progressOffset: prepared.session !== snapshot ? NATIVE_RENDER_PROGRESS_WEIGHT : 0,
          resolve,
          reject,
        }
      })
      exportStartPromise = invoke<{ taskId: string; started: boolean }>('start_editor_mix_export', {
        payload: {
          taskId,
          format: fmt,
          exportDir: normalized.exportDir,
          fileName: normalized.fileName,
          audioParams: normalized.audioParams || {},
          project: {
            id: project.id,
            name: project.name,
            masterVolume: project.masterVolume,
            masterPan: project.masterPan,
            sourceSampleRate: prepared.sourceSampleRate,
            assets: project.sources.map(sourceToExportAsset),
            tracks: project.tracks.map((track) => trackToExportTrack(track, project.sources.find(source => source.id === track.sourceId))),
          },
        },
      }).then(() => undefined)
      await exportStartPromise
      exportStartPromise = null
      const result = await completion
      if (pluginTailLimited) result.pluginTailLimited = true
      lastExport.value = result
      return result
    } catch (error) {
      if (pendingExport?.id === taskId) pendingExport = null
      const failure = renderController.signal.aborted ? new EditorExportCancelledError() : error
      lastError.value = failure instanceof EditorExportCancelledError
        ? null
        : failure instanceof Error ? failure.message : String(failure)
      throw failure
    } finally {
      if (pluginRenderRequired) {
        await invoke('plugin_host_cleanup_render', { taskId }).catch(error => {
          studioLog.warn('Failed to clean up plugin render files', error)
        })
      }
      exporting.value = false
      exportCancelling.value = false
      exportProgress.value = null
      exportStartPromise = null
      exportCancelRequested = false
      exportRenderController = null
    }
  }

  async function cancelPendingExportTask(pending: PendingEditorExport | null) {
    if (!pending) return false
    const accepted = await invoke<boolean>('cancel_editor_mix_export', {
      taskId: pending.id,
      projectId: pending.projectId,
      exportDir: pending.exportDir,
    })
    return accepted
  }

  async function cancelExport() {
    if (!exporting.value || exportCancelling.value) return false
    exportCancelling.value = true
    exportCancelRequested = true
    try {
      if (exportRenderController) {
        exportRenderController.abort()
        if (nativeRenderTaskId) await invoke('plugin_host_cancel_render', { taskId: nativeRenderTaskId })
        return true
      }
      if (exportStartPromise) await exportStartPromise
      if (!pendingExport) return true
      return cancelPendingExportTask(pendingExport)
    } catch (error) {
      exportCancelling.value = false
      throw error
    }
  }

  function setPluginChain(trackId: string | null, chain: EditorPluginSlot[]) {
    if (!session.value) return
    const target = trackId ? session.value.tracks.find(track => track.id === trackId) : session.value
    if (!target) return
    pushHistory()
    target.pluginChain = clonePluginChain(chain)
    scheduleSave()
  }

  async function recoverPluginChain(options: {
    project: EditorSession; trackId: string | null; chain: EditorPluginSlot[];
    host: PluginSessionState; sessionId: string; signature: string;
  }) {
    const { project, trackId, host, sessionId, signature } = options
    const validate = () => {
      const target = trackId === null ? project : project.tracks.find(track => track.id === trackId)
      if (session.value !== project || !target || JSON.stringify(target.pluginChain || []) !== signature) {
        throw new Error(String(i18n.global.t('editor.plugins.stateCaptureSuperseded')))
      }
    }
    const clearTimers = () => {
      if (saveTimer) { clearTimeout(saveTimer); saveTimer = null }
      if (pluginSaveTimer) { clearTimeout(pluginSaveTimer); pluginSaveTimer = null }
    }
    validate()
    const resumeProjectSave = saveTimer !== null || pendingProjectSaves.has(project.id)
    const resumePluginSave = pluginSaveTimer !== null
    clearTimers()
    try {
      if (saveDrainTask) await saveDrainTask.catch(() => undefined)
      validate()
      await discardUnavailablePluginSession(project, host, sessionId)
      validate()
      clearTimers()
      const previousError = pluginStateError.value
      setPluginChain(trackId, options.chain)
      pluginStateDirty.value = false
      pluginStateError.value = null
      pluginStateUnavailable.value = false
      if (lastError.value === previousError) lastError.value = null
      await flushSave()
    } catch (cause) {
      // A rejected recovery must not cancel edits that were already waiting for autosave.
      if (!discardingWindowChanges && session.value === project) {
        if (saveTimer === null && (resumeProjectSave || pendingProjectSaves.has(project.id))) scheduleSave()
        if (pluginSaveTimer === null && resumePluginSave) schedulePluginStateSave(project)
      }
      throw cause
    }
  }

  function setZoom(value: number) {
    pixelsPerSecond.value = clamp(Number(value), 4, 240)
  }

  function zoomIn() {
    setZoom(pixelsPerSecond.value + 18)
  }

  function zoomOut() {
    setZoom(pixelsPerSecond.value - 18)
  }

  return {
    session,
    setPluginChain,
    recoverPluginChain,
    pluginStateDirty,
    pluginStateError,
    pluginStateUnavailable,
    restoreSavedPluginSession,
    capturePluginState,
    retryPluginStateSave,
    releasePluginSession,
    loading,
    saving,
    exporting,
    exportCancelling,
    exportProgress,
    lastExport,
    lastError,
    projectSummaries,
    selectedTrackId,
    selectedClipId,
    exportFormat,
    selectedTrack,
    selectedClip,
    selectedSource,
    sourceMap,
    stemTracks,
    referenceTracks,
    assetTree,
    pixelsPerSecond,
    masterVolume,
    masterPan,
    canUndo,
    canRedo,
    duration,
    selectTrack,
    selectClip,
    undo,
    redo,
    beginInteraction,
    commitInteraction,
    clearHistory,
    scheduleSave,
    flushSave,
    createFromTask,
    ensureProjectForTask,
    listProjects,
    refreshProjects,
    createBlankProject,
    deleteProject,
    deleteProjectsForTasks,
    listOpenProjectsForTasks,
    listOrphanedProjects,
    deleteOrphanedProjects,
    clearSession,
    openProjectWindow,
    projectExists,
    loadProject,
    saveProject,
    getMetadata,
    ensurePeaks,
    ensurePeaksInBackground,
    hydratePeaksFromCache,
    hydrateSessionSources,
    hydrateSessionSourcesInBackground,
    ensureSourceByPath,
    addReferenceSourceByPath,
    addTrackFromSourceId,
    addRecordingTrack,
    addRecordingClip,
    scanAssets,
    relinkSource,
    relinkMissingSources,
    pickRelinkFile,
    missingSources,
    missingSourcesInUse,
    assertNoMissingSourcesInUse,
    renameTrack,
    localizeDefaultTrackNames,
    toggleTrackFlag,
    setTrackVolume,
    setTrackPan,
    setClipFades,
    setClipTiming,
    setMasterVolume,
    setMasterPan,
    setMasterLimiter,
    removeTrack,
    removeClip,
    removeSource,
    exportMix,
    cancelExport,
    setZoom,
    zoomIn,
    zoomOut,
  }
})
