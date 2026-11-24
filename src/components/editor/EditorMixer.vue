<script setup lang="ts">
import { computed, h, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { EllipsisHorizontal, LockClosedOutline, Magnet, Navigate } from '@vicons/ionicons5'
import type { DropdownOption } from 'naive-ui'
import type { EditorClip, EditorClipDrop, EditorSource, EditorTimelineTool, EditorTrack } from '@/types/editor'
import EditorWaveform from '@/components/editor/EditorWaveform.vue'
import { editorClipShortcutLabel, type EditorClipCommand } from '@/utils/editorClipShortcuts'
import { formatTime, formatTimecode } from '@/utils/editorTime'
import {
  clipColor,
  clipTrimBounds,
  clipsForTrack as resolveTrackClips,
  hasLockedClipInRange,
  overlappingClipRanges,
  resolveClipTimingEdit,
  resolveSnappedClipTimingEdit,
  resolveSnappedSplitTime,
  timelineClipEdges,
  type ClipTimingEditMode,
  type ClipTrimBounds,
} from '@/utils/editorClips'

const props = defineProps<{
  tracks: EditorTrack[]
  sourceMap: Map<string, EditorSource>
  selectedTrackId: string | null
  selectedClipId: string | null
  currentTime: number
  duration: number
  pixelsPerSecond: number
  trackLevels: Record<string, [number, number]>
  clipCommandState: Record<EditorClipCommand, boolean>
  timelineTool: EditorTimelineTool
  snappingEnabled: boolean
  editingDisabled?: boolean
  externalDragging?: boolean
  externalImporting?: boolean
  recordingPreview?: {
    trackId: string
    start: number
    duration: number
  } | null
}>()

const emit = defineEmits<{
  selectTrack: [trackId: string]
  selectClip: [payload: { trackId: string; clipId: string }]
  toggleMute: [trackId: string]
  toggleSolo: [trackId: string]
  seek: [time: number]
  removeTrack: [trackId: string]
  clipCommand: [command: EditorClipCommand]
  setClipLocked: [trackId: string, clipId: string, locked: boolean]
  setTool: [tool: EditorTimelineTool]
  toggleSnapping: []
  splitAt: [payload: { trackId: string; clipId: string; time: number }]
  dropClip: [drop: EditorClipDrop]
  revealTrack: [trackId: string]
  showInspector: []
  zoomAt: [payload: { direction?: 'in' | 'out'; anchorRatio: number; deltaY?: number }]
  'scroll-ready': [element: HTMLElement]
  addTrackFromAsset: [sourceId: string]
  beginClipEdit: []
  updateClipTiming: [payload: {
    trackId: string
    clipId: string
    patch: Partial<Pick<EditorClip, 'start' | 'offset' | 'duration'>>
  }]
  commitClipEdit: [cancelled: boolean]
}>()

const { t } = useI18n()
const HEADER_WIDTH = 180
const SNAP_DISTANCE_PX = 7
const EDGE_SCROLL_ZONE_PX = 40
const EDGE_SCROLL_SPEED_PX = 750
const CLIP_LABEL_HEIGHT = 15
const scrollEl = ref<HTMLElement | null>(null)
const scrollViewportWidth = ref(0)
let resizeObserver: ResizeObserver | null = null

watch(
  () => scrollEl.value,
  (value) => {
    if (resizeObserver) {
      resizeObserver.disconnect()
      resizeObserver = null
    }
    if (value) {
      const syncViewportWidth = () => {
        scrollViewportWidth.value = value.clientWidth
      }
      syncViewportWidth()
      if (typeof ResizeObserver !== 'undefined') {
        resizeObserver = new ResizeObserver(() => {
          syncViewportWidth()
        })
        resizeObserver.observe(value)
      }
      emit('scroll-ready', value)
    }
  },
  { immediate: true },
)

const laneWidth = computed(() => {
  const viewportLaneWidth = Math.max(0, scrollViewportWidth.value - HEADER_WIDTH)
  const contentWidth = props.duration > 0 ? props.duration * props.pixelsPerSecond : 0
  // Always fill at least the visible lane area so the track/timeline reaches the
  // inspector edge instead of being cut off where the audio content ends.
  return Math.max(1, Math.ceil(Math.max(contentWidth, viewportLaneWidth)))
})
const playheadLeft = computed(() => HEADER_WIDTH + Math.max(0, props.currentTime * props.pixelsPerSecond))
const hasSolo = computed(() => props.tracks.some((track) => track.solo))
const dropTargetTrackId = ref<string | null>(null)
const fallbackDropSourceId = ref<string | null>(null)
const showTrackMenu = ref(false)
const menuX = ref(0)
const menuY = ref(0)
const contextTrack = ref<EditorTrack | null>(null)
const contextClip = ref<{ track: EditorTrack; clip: EditorClip } | null>(null)
const mixerRootEl = ref<HTMLElement | null>(null)
const activeClipEditId = ref<string | null>(null)
const clipSnapTime = ref<number | null>(null)
const clipDropPreview = ref<{ drop: EditorClipDrop; clip: EditorClip; color: string; blocked: boolean } | null>(null)
const clipDropPreviewSource = computed(() => clipDropPreview.value && sourceForClip(clipDropPreview.value.clip))
type ClipEditState = {
  pointerId: number
  captureTarget: HTMLElement
  trackId: string
  mode: ClipTimingEditMode
  clip: EditorClip
  color: string
  copy: boolean
  canEdit: boolean
  clientX: number
  clientY: number
  scrollLeft: number
  start: number
  offset: number
  duration: number
  sourceDuration: number
  trimBounds?: ClipTrimBounds
  snapTargets: number[]
  begun: boolean
}
let clipEditState: ClipEditState | null = null
const clipMoveActive = ref(false)
let latestClipPointer: PointerEvent | null = null
let clipScrollFrame: number | null = null
let lastClipScrollTime = 0

const isMac = /Mac/i.test(navigator.platform)
const timelineTools = [
  { id: 'selection', label: 'editor.selectionTool', shortcut: 'V' },
  { id: 'razor', label: 'editor.razorTool', shortcut: 'C' },
] as const
const clipCommandLabels = {
  cut: 'editor.cutClip', copy: 'editor.copyClip', paste: 'editor.pasteClip',
  split: 'editor.splitClip', splitAll: 'editor.splitAllClips', delete: 'editor.removeClip',
} as const

function clipMenuItem(command: EditorClipCommand): DropdownOption {
  return {
    key: command,
    disabled: !props.clipCommandState[command],
    label: () => h('span', { class: 'editor-clip-command' }, [
      t(clipCommandLabels[command]),
      h('kbd', editorClipShortcutLabel(command, isMac)),
    ]),
  }
}

const trackMenuOptions = computed<DropdownOption[]>(() => {
  const track = contextTrack.value
  if (!track) return []
  return [
    clipMenuItem('paste'),
    clipMenuItem('splitAll'),
    { type: 'divider', key: 'track-divider' },
    {
      key: 'inspect',
      label: t('editor.menuTrackParams'),
    },
    {
      key: 'reveal',
      label: t('editor.menuRevealAsset'),
    },
    {
      key: 'remove',
      label: t('editor.removeTrack'),
      disabled: Boolean(props.editingDisabled),
    },
  ]
})

const clipMenuOptions = computed<DropdownOption[]>(() => [
  clipMenuItem('cut'),
  clipMenuItem('copy'),
  clipMenuItem('paste'),
  clipMenuItem('split'),
  clipMenuItem('splitAll'),
  clipMenuItem('delete'),
  {
    key: 'toggle-lock',
    label: t(contextClip.value?.clip.locked ? 'editor.unlockClip' : 'editor.lockClip'),
    disabled: Boolean(props.editingDisabled),
  },
  { type: 'divider', key: 'clip-divider' },
  { key: 'inspect', label: t('editor.menuClipParams') },
])

const rulerStep = computed(() => {
  const targetPx = 100
  const rawStep = targetPx / props.pixelsPerSecond
  const niceSteps = [1, 2, 5, 10, 15, 30, 60, 120, 300]
  return niceSteps.find((item) => item >= rawStep) || niceSteps[niceSteps.length - 1]
})

const laneStepPx = computed(() => Math.max(48, rulerStep.value * props.pixelsPerSecond))

const RULER_END_LABEL_CLEARANCE = 80
const rulerTicks = computed(() => {
  const total = props.duration
  if (total <= 0) return []
  const step = rulerStep.value
  const end = total * props.pixelsPerSecond
  const ticks = [{ time: 0, left: 0, end: false, showLabel: true }]
  // Regular labels sit after their ticks, and the end label sits before its tick.
  for (let time = step; time < total; time += step) {
    const left = time * props.pixelsPerSecond
    ticks.push({ time, left, end: false, showLabel: end - left >= RULER_END_LABEL_CLEARANCE })
  }
  ticks.push({ time: total, left: end, end: true, showLabel: end >= RULER_END_LABEL_CLEARANCE })
  return ticks
})

function sourceFor(track: EditorTrack) {
  return props.sourceMap.get(track.sourceId)
    || props.sourceMap.get(track.clips?.[track.clips.length - 1]?.assetId || '')
    || null
}

function clipsForTrack(track: EditorTrack) {
  return resolveTrackClips(track, props.sourceMap)
}

const selectedClip = computed(() => {
  const track = props.tracks.find(item => item.id === props.selectedTrackId)
  return track && clipsForTrack(track).find(clip => clip.id === props.selectedClipId)
})

const overlapRangesByTrack = computed(() => {
  const preview = clipDropPreview.value
  return new Map(props.tracks.map((track) => {
    const trackClips = clipsForTrack(track)
    const clips = preview && !preview.blocked && !preview.drop.copy && preview.drop.fromTrackId === track.id
      ? trackClips.filter(item => item.id !== preview.drop.clipId)
      : trackClips
    return [track.id, overlappingClipRanges(clips)] as const
  }))
})

function sourceForClip(clip: EditorClip) {
  return props.sourceMap.get(clip.assetId) || null
}

function clipAccessibleName(clip: EditorClip, track: EditorTrack) {
  const name = sourceForClip(clip)?.name || track.name
  return clip.locked ? t('editor.lockedClipName', { name }) : name
}

function sourceMissing(track: EditorTrack) {
  return clipsForTrack(track).some((clip) => Boolean(sourceForClip(clip)?.missing))
}

function trackChannels(track: EditorTrack) {
  return clipsForTrack(track).reduce((max, clip) => {
    const source = sourceForClip(clip)
    const peakChannels = source?.channelPeaks?.filter(channel => channel.length).length || 0
    return Math.max(max, Number(source?.channels || 0), peakChannels)
  }, 0)
}

function trackWaveHeight(track: EditorTrack) {
  return (trackChannels(track) >= 2 ? 61 : 41) - CLIP_LABEL_HEIGHT
}

function clipWaveWidth(clip: EditorClip) {
  return Math.max(1, Math.ceil(clip.duration * props.pixelsPerSecond))
}

function clipStyle(clip: EditorClip, start = clip.start) {
  return {
    width: `${Math.max(1, Math.min(laneWidth.value, clip.duration * props.pixelsPerSecond))}px`,
    transform: `translateX(${Math.max(0, start * props.pixelsPerSecond)}px)`,
  }
}

function recordingPreviewStyle() {
  const preview = props.recordingPreview
  if (!preview) return undefined
  return {
    width: `${Math.max(2, Math.min(laneWidth.value, preview.duration * props.pixelsPerSecond))}px`,
    transform: `translateX(${Math.max(0, preview.start * props.pixelsPerSecond)}px)`,
  }
}

function beginClipPointer(event: PointerEvent, track: EditorTrack, clip: EditorClip, mode: ClipTimingEditMode) {
  if (event.button !== 0) return
  event.preventDefault()
  event.stopPropagation()
  if (clipEditState) return
  focusTimeline()
  emit('selectClip', { trackId: track.id, clipId: clip.id })
  if (props.editingDisabled) return

  if (props.timelineTool === 'razor') {
    if (mode !== 'move') return
    emit('splitAt', { trackId: track.id, clipId: clip.id, time: razorPointerTime(event, clip).time })
    return
  }

  const copy = mode === 'move' && event.altKey
  const canEdit = !clip.locked || copy
  if (!canEdit && mode !== 'move') return
  const captureTarget = event.currentTarget as HTMLElement
  captureTarget.setPointerCapture(event.pointerId)
  clipEditState = {
    pointerId: event.pointerId,
    captureTarget,
    trackId: track.id,
    mode,
    clip,
    color: clipColor(clip, track),
    copy,
    canEdit,
    clientX: event.clientX,
    clientY: event.clientY,
    scrollLeft: scrollEl.value?.scrollLeft ?? 0,
    start: clip.start,
    offset: clip.offset,
    duration: clip.duration,
    sourceDuration: Math.max(0, Number(sourceForClip(clip)?.duration || 0)),
    trimBounds: mode === 'move' ? undefined : clipTrimBounds(clip, clipsForTrack(track)),
    snapTargets: [props.currentTime, ...timelineClipEdges(props.tracks, props.sourceMap, copy ? undefined : clip.id)],
    begun: false,
  }
  activeClipEditId.value = clip.id
  window.addEventListener('pointermove', handleClipPointerMove)
  window.addEventListener('pointerup', finishClipPointer)
  window.addEventListener('pointercancel', finishClipPointer)
  window.addEventListener('blur', cancelClipPointer)
}

const razorSnapTargets = computed(() => timelineClipEdges(props.tracks, props.sourceMap))

function clipPointerTime(clip: EditorClip, clientX: number, element: HTMLElement) {
  return clip.start + (clientX - element.getBoundingClientRect().left) / props.pixelsPerSecond
}

function razorPointerTime(event: PointerEvent, clip: EditorClip) {
  const time = clipPointerTime(clip, event.clientX, event.currentTarget as HTMLElement)
  const snapped = props.snappingEnabled
    ? resolveSnappedSplitTime(clip, time, 1 / (sourceForClip(clip)?.sampleRate || 48000),
      [props.currentTime, ...razorSnapTargets.value], SNAP_DISTANCE_PX / props.pixelsPerSecond)
    : time
  return { time: snapped, snapTime: snapped === time ? null : snapped }
}

function updateRazorGuide(event: PointerEvent, clip: EditorClip) {
  if (props.timelineTool !== 'razor' || props.editingDisabled || clip.locked) return
  clipSnapTime.value = razorPointerTime(event, clip).snapTime
}

function clearRazorGuide() {
  if (!clipEditState) clipSnapTime.value = null
}

function timingAtPointer(event: PointerEvent, active: ClipEditState) {
  const scrollDelta = (scrollEl.value?.scrollLeft ?? 0) - active.scrollLeft
  const delta = (event.clientX - active.clientX + scrollDelta) / props.pixelsPerSecond
  const useSnap = props.snappingEnabled && (active.mode !== 'move' || delta !== 0)
  const { patch, snapTime } = resolveSnappedClipTimingEdit(
    active, active.mode, delta, active.sourceDuration, useSnap ? active.snapTargets : [],
    SNAP_DISTANCE_PX / props.pixelsPerSecond,
    active.trimBounds,
  )
  clipSnapTime.value = snapTime
  return patch
}

function clipDropAt(event: PointerEvent, active: ClipEditState): EditorClipDrop | null {
  const lane = document.elementFromPoint(event.clientX, event.clientY)
    ?.closest<HTMLElement>('[data-clip-lane], [data-clip-new-track]')
  if (!lane || !mixerRootEl.value?.contains(lane)) {
    clipSnapTime.value = null
    return null
  }
  const toTrackId = lane.dataset.clipLane ?? null
  const { start = active.start } = timingAtPointer(event, active)
  if (!active.copy && toTrackId === active.trackId && start === active.start) return null
  return {
    clipId: active.clip.id,
    fromTrackId: active.trackId,
    toTrackId,
    start,
    copy: active.copy,
  }
}

async function handleClipPointerMove(event: PointerEvent) {
  const active = clipEditState
  if (!active || event.pointerId !== active.pointerId) return
  const deltaPx = event.clientX - active.clientX
  const starting = !active.begun
  if (starting) {
    const distance = active.mode === 'move' ? Math.hypot(deltaPx, event.clientY - active.clientY) : Math.abs(deltaPx)
    if (distance < 3) return
    active.begun = true
  }
  if (!active.canEdit) return
  const startedMove = starting && active.mode === 'move'
  if (startedMove) clipMoveActive.value = true
  else if (starting) emit('beginClipEdit')
  latestClipPointer = event
  if (startedMove) {
    await nextTick()
    if (clipEditState !== active || latestClipPointer !== event) return
  }
  updateClipPreview(event, active)
  if (clipScrollFrame === null) {
    lastClipScrollTime = performance.now()
    clipScrollFrame = requestAnimationFrame(scrollAtClipPointer)
  }
}

function updateClipPreview(event: PointerEvent, active: ClipEditState) {
  if (active.mode === 'move') {
    const drop = clipDropAt(event, active)
    const track = props.tracks.find(item => item.id === drop?.toTrackId)
    clipDropPreview.value = drop ? {
      drop,
      clip: active.clip,
      color: active.color,
      blocked: track ? hasLockedClipInRange(clipsForTrack(track), drop.start, active.clip.duration) : false,
    } : null
    return
  }
  emit('updateClipTiming', { trackId: active.trackId, clipId: active.clip.id, patch: timingAtPointer(event, active) })
}

function nudgeTrimEdge(event: KeyboardEvent, track: EditorTrack, clip: EditorClip, mode: 'trim-start' | 'trim-end') {
  if (props.editingDisabled || clip.locked || clipEditState || showTrackMenu.value
    || event.ctrlKey || event.metaKey || event.altKey) return
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
  event.preventDefault()
  event.stopPropagation()
  const step = event.shiftKey ? 0.1 : 0.01
  const delta = event.key === 'ArrowLeft' ? -step : step
  const patch = resolveClipTimingEdit(
    clip, mode, delta, sourceForClip(clip)?.duration || 0, clipTrimBounds(clip, clipsForTrack(track)),
  )
  emit('updateClipTiming', { trackId: track.id, clipId: clip.id, patch })
}

function edgeScrollRate(position: number, start: number, end: number) {
  const zone = Math.min(EDGE_SCROLL_ZONE_PX, (end - start) / 2)
  if (zone <= 0 || position < start || position > end) return 0
  return (Math.max(0, zone - (end - position)) - Math.max(0, zone - (position - start))) / zone
}

function scrollAtClipPointer(now: number) {
  clipScrollFrame = null
  const pointer = latestClipPointer
  const active = clipEditState
  const element = scrollEl.value
  if (!active || !pointer || !element) return
  const bounds = element.getBoundingClientRect()
  const left = bounds.left + HEADER_WIDTH
  const right = bounds.left + element.clientWidth
  const bottom = bounds.top + element.clientHeight
  if (pointer.clientX < left || pointer.clientX > right
    || pointer.clientY < bounds.top || pointer.clientY > bottom) return

  const speed = EDGE_SCROLL_SPEED_PX * Math.min(now - lastClipScrollTime, 50) / 1000
  const dx = edgeScrollRate(pointer.clientX, left, right)
  const dy = active.mode === 'move' ? edgeScrollRate(pointer.clientY, bounds.top, bottom) : 0
  const previousX = element.scrollLeft
  const previousY = element.scrollTop
  if (dx) element.scrollLeft += Math.sign(dx) * Math.max(1, Math.round(Math.abs(dx) * speed))
  if (dy) element.scrollTop += Math.sign(dy) * Math.max(1, Math.round(Math.abs(dy) * speed))
  if (element.scrollLeft === previousX && element.scrollTop === previousY) return

  updateClipPreview(pointer, active)
  lastClipScrollTime = now
  clipScrollFrame = requestAnimationFrame(scrollAtClipPointer)
}

function cancelClipPointer() {
  clearClipPointer(true)
}

function clearClipPointer(cancelled: boolean) {
  const active = clipEditState
  if (!active) return
  window.removeEventListener('pointermove', handleClipPointerMove)
  window.removeEventListener('pointerup', finishClipPointer)
  window.removeEventListener('pointercancel', finishClipPointer)
  window.removeEventListener('blur', cancelClipPointer)
  clipEditState = null
  latestClipPointer = null
  if (clipScrollFrame !== null) cancelAnimationFrame(clipScrollFrame)
  clipScrollFrame = null
  if (active.captureTarget.hasPointerCapture(active.pointerId)) {
    active.captureTarget.releasePointerCapture(active.pointerId)
  }
  activeClipEditId.value = null
  clipMoveActive.value = false
  clipDropPreview.value = null
  clipSnapTime.value = null
  if (active.begun && active.mode !== 'move') emit('commitClipEdit', cancelled)
}

function finishClipPointer(event: PointerEvent) {
  const active = clipEditState
  if (!active || event.pointerId !== active.pointerId) return
  const canCommit = event.type === 'pointerup' && !props.editingDisabled
  const clickedClip = canCommit && active.mode === 'move' && !active.begun
  const clickTime = clickedClip ? clipPointerTime(active.clip, event.clientX, active.captureTarget) : null
  const drop = canCommit && clipMoveActive.value ? clipDropAt(event, active) : null
  clearClipPointer(!canCommit)
  if (clickTime !== null) emit('seek', clickTime)
  if (drop) emit('dropClip', drop)
}

function trackLevel(trackId: string) {
  return props.trackLevels[trackId] || [0, 0]
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value))
}

function trackMeterPeak(trackId: string) {
  const [left, right] = trackLevel(trackId)
  return Math.max(left, right)
}

function trackMeterStyle(trackId: string, channel: 0 | 1) {
  const raw = trackLevel(trackId)[channel]
  const normalized = clamp(raw > 0.012 ? Math.max(raw, 0.08) : 0, 0, 1)
  return {
    transform: `scaleY(${normalized})`,
    opacity: normalized > 0 ? 1 : 0,
  }
}

function rowClass(track: EditorTrack) {
  return {
    'track-row--selected': track.id === props.selectedTrackId,
    'track-row--dimmed': track.muted || (hasSolo.value && !track.solo),
    'track-row--offline': sourceMissing(track),
    'track-row--stereo': trackChannels(track) >= 2,
  }
}

function trackMetaLabel(track: EditorTrack) {
  if (sourceMissing(track)) return t('editor.laneAudioOffline')
  if (track.role === 'recording' && !clipsForTrack(track).length) return t('editor.recordingTrackReady')
  return `${sourceFor(track)?.channels || 0}ch`
}

function seekFromEvent(event: MouseEvent) {
  focusTimeline()
  const element = event.currentTarget as HTMLElement
  const rect = element.getBoundingClientRect()
  const x = event.clientX - rect.left + element.scrollLeft
  emit('seek', Math.max(0, Math.min(props.duration, x / props.pixelsPerSecond)))
}

function handleTimelineWheel(event: WheelEvent) {
  const element = scrollEl.value
  if (!element) return

  if (event.ctrlKey || event.metaKey) {
    if (event.cancelable) event.preventDefault()
    const rect = element.getBoundingClientRect()
    const anchorRatio = Math.max(0, Math.min(1, (event.clientX - rect.left) / Math.max(1, element.clientWidth)))
    emit('zoomAt', { direction: event.deltaY < 0 ? 'in' : 'out', anchorRatio, deltaY: event.deltaY })
    return
  }

  const canScrollVertically = element.scrollHeight > element.clientHeight + 1
  const horizontalIntent = event.shiftKey || Math.abs(event.deltaX) > Math.abs(event.deltaY) || !canScrollVertically
  if (!horizontalIntent) return

  const delta = event.shiftKey
    ? (Math.abs(event.deltaY) > Math.abs(event.deltaX) ? event.deltaY : event.deltaX)
    : (Math.abs(event.deltaX) > 0 ? event.deltaX : event.deltaY)
  if (!delta) return

  if (event.cancelable) event.preventDefault()
  element.scrollLeft += delta
}

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  cancelClipPointer()
})

watch(() => props.editingDisabled, (disabled) => {
  if (disabled) cancelClipPointer()
})
watch(() => [props.timelineTool, props.snappingEnabled], clearRazorGuide)

function allowAssetDrop(trackId?: string | null) {
  if (!fallbackDropSourceId.value) return
  dropTargetTrackId.value = trackId || null
}

function clearAssetDrop() {
  dropTargetTrackId.value = null
}

function commitAssetDrop() {
  const sourceId = fallbackDropSourceId.value
  clearAssetDrop()
  if (!sourceId) return
  emit('addTrackFromAsset', sourceId)
}

function setDraggingAssetSourceId(sourceId: string | null) {
  fallbackDropSourceId.value = sourceId
}

function setDropTargetTrackId(trackId: string | null) {
  if (!fallbackDropSourceId.value) {
    dropTargetTrackId.value = null
    return
  }
  dropTargetTrackId.value = trackId
}

function containsPoint(x: number, y: number) {
  const root = mixerRootEl.value
  if (!root) return false
  const rect = root.getBoundingClientRect()
  return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom
}

function toggleTimelineSnapping() {
  focusTimeline()
  emit('toggleSnapping')
}

function useTimelineTool(tool: EditorTimelineTool) {
  focusTimeline()
  emit('setTool', tool)
}

function focusTimeline() {
  mixerRootEl.value?.focus({ preventScroll: true })
}

function selectTrackRow(event: MouseEvent, trackId: string) {
  if (event.target instanceof Element && !event.target.closest('button')) focusTimeline()
  emit('selectTrack', trackId)
}

function openTrackContextMenu(event: MouseEvent, track: EditorTrack) {
  focusTimeline()
  emit('selectTrack', track.id)
  contextTrack.value = track
  contextClip.value = null
  showTrackMenu.value = false
  menuX.value = event.clientX
  menuY.value = event.clientY
  window.requestAnimationFrame(() => {
    showTrackMenu.value = true
  })
}

function openClipContextMenu(event: MouseEvent, track: EditorTrack, clip: EditorClip) {
  focusTimeline()
  emit('selectClip', { trackId: track.id, clipId: clip.id })
  contextTrack.value = null
  contextClip.value = { track, clip }
  showTrackMenu.value = false
  menuX.value = event.clientX
  menuY.value = event.clientY
  window.requestAnimationFrame(() => {
    showTrackMenu.value = true
  })
}

function handleContextMenuSelect(key: string | number) {
  showTrackMenu.value = false
  focusTimeline()
  if (Object.hasOwn(clipCommandLabels, key)) {
    const command = key as EditorClipCommand
    if (props.clipCommandState[command]) emit('clipCommand', command)
    return
  }
  if (key === 'inspect') emit('showInspector')
  if (key === 'toggle-lock' && contextClip.value && !props.editingDisabled) {
    const { track, clip } = contextClip.value
    emit('setClipLocked', track.id, clip.id, !clip.locked)
  }
  const track = contextTrack.value
  if (!track) return
  if (key === 'reveal') emit('revealTrack', track.id)
  if (key === 'remove' && !props.editingDisabled) emit('removeTrack', track.id)
}

function interceptKeydown(event: KeyboardEvent) {
  if (clipEditState) {
    if (event.key === 'Escape') cancelClipPointer()
    event.preventDefault()
    return true
  }
  if (!showTrackMenu.value) return false
  if (event.key === 'Escape' || event.key === 'Tab') {
    showTrackMenu.value = false
    if (event.key === 'Escape') focusTimeline()
  }
  if (event.key === 'Escape' || event.key === 'Delete' || event.key === 'Backspace') event.preventDefault()
  return true
}

defineExpose({
  focusTimeline,
  interceptKeydown,
  setDraggingAssetSourceId,
  setDropTargetTrackId,
  containsPoint,
  commitAssetDrop,
})
</script>

<template>
  <section
    ref="mixerRootEl"
    class="editor-mixer"
    tabindex="0"
    :aria-label="t('editor.timeline')"
    :class="{
      'editor-mixer--external-drop': externalDragging,
      'editor-mixer--razor': timelineTool === 'razor' && !editingDisabled,
    }"
  >
    <div v-if="externalDragging || externalImporting" class="external-drop-hint" role="status">
      <n-spin v-if="externalImporting" size="small" />
      <span>{{ t(externalImporting ? 'editor.importingAudio' : 'editor.dropImportsTracks') }}</span>
    </div>
    <div
      ref="scrollEl"
      class="editor-mixer__scroll"
      :style="{
        '--lane-width': `${laneWidth}px`,
        '--lane-step': `${laneStepPx}px`,
        '--clip-label-height': `${CLIP_LABEL_HEIGHT}px`,
      }"
      @wheel="handleTimelineWheel"
    >
      <div class="ruler">
        <div class="ruler__head">
          <span>{{ t('editor.tracks') }}</span>
          <div class="ruler__tools">
            <button
              v-for="tool in timelineTools"
              :key="tool.id"
              type="button"
              class="ruler__tool"
              :class="{ 'ruler__tool--selection': tool.id === 'selection' }"
              :aria-pressed="tool.id === timelineTool"
              :aria-label="t(tool.label)"
              :aria-keyshortcuts="tool.shortcut"
              :title="t(tool.label) + ' (' + tool.shortcut + ')'"
              @keydown.space.stop
              @click="useTimelineTool(tool.id)"
            >
              <n-icon v-if="tool.id === 'selection'" :component="Navigate" aria-hidden="true" />
              <span v-else class="ruler__razor-icon" aria-hidden="true" />
            </button>
            <button
              type="button"
              class="ruler__tool ruler__tool--snap"
              :aria-pressed="snappingEnabled"
              :aria-label="t('editor.snapToEdges')"
              aria-keyshortcuts="S"
              :title="t('editor.snapToEdges') + ' (S)'"
              @keydown.space.stop
              @click="toggleTimelineSnapping"
            >
              <n-icon :component="Magnet" aria-hidden="true" />
            </button>
          </div>
          <small>{{ tracks.length }}</small>
        </div>
        <div class="ruler__track" :style="{ width: `${laneWidth}px` }" @click="seekFromEvent">
          <div class="ruler__grid">
            <span v-for="tick in rulerTicks" :key="tick.time" class="ruler-tick" :class="{ 'ruler-tick--end': tick.end }" :style="{ transform: `translateX(${tick.left}px)` }">
              <span v-if="tick.showLabel" class="ruler-tick__label">{{ formatTimecode(tick.time) }}</span>
            </span>
          </div>
        </div>
      </div>

      <div v-if="tracks.length" class="tracks">
        <div
          v-for="track in tracks"
          :key="track.id"
          class="track-row"
          :data-track-id="track.id"
          :class="rowClass(track)"
          @mousedown.left="selectTrackRow($event, track.id)"
          @contextmenu.stop.prevent="openTrackContextMenu($event, track)"
          @mouseenter="allowAssetDrop(track.id)"
        >
          <div class="track-row__head">
            <div class="track-row__body">
              <div class="track-row__title">
                <span class="track-dot" :style="{ background: track.color || '#7aa2ff' }" />
                <strong :title="track.name">{{ track.name }}</strong>
              </div>
              <div class="track-row__subline">
                <div class="track-row__buttons">
                  <button
                    class="chip"
                    type="button"
                    :title="track.muted ? t('editor.unmuteTrack') : t('editor.muteTrack')"
                    :aria-label="track.muted ? t('editor.unmuteTrack') : t('editor.muteTrack')"
                    :class="{ 'chip--active chip--warning': track.muted }"
                    @click.stop="emit('toggleMute', track.id)"
                  >M</button>
                  <button
                    class="chip"
                    type="button"
                    :title="track.solo ? t('editor.unsoloTrack') : t('editor.soloTrack')"
                    :aria-label="track.solo ? t('editor.unsoloTrack') : t('editor.soloTrack')"
                    :class="{ 'chip--active': track.solo }"
                    @click.stop="emit('toggleSolo', track.id)"
                  >S</button>
                  <button
                    class="chip chip--icon"
                    type="button"
                    :title="t('editor.trackActions')"
                    :aria-label="t('editor.trackActions')"
                    @click.stop="openTrackContextMenu($event, track)"
                  ><n-icon :component="EllipsisHorizontal" /></button>
                </div>
                <span class="track-row__meta" :class="{ 'track-row__meta--offline': sourceMissing(track) }">
                  {{ trackMetaLabel(track) }}
                </span>
              </div>
            </div>
            <div
              class="track-meter"
              :class="{ 'track-meter--active': trackMeterPeak(track.id) > 0.02 }"
              :title="`${Math.round(trackLevel(track.id)[0] * 100)} / ${Math.round(trackLevel(track.id)[1] * 100)}`"
            >
              <span class="track-meter__bar"><i :style="trackMeterStyle(track.id, 0)" /></span>
              <span class="track-meter__bar"><i :style="trackMeterStyle(track.id, 1)" /></span>
            </div>
          </div>

          <div class="track-row__lane" :data-clip-lane="track.id" @click="seekFromEvent">
            <div class="track-wave" :style="{ width: `${laneWidth}px`, '--track-color': track.color || '#7aa2ff' }">
              <div
                v-for="clip in clipsForTrack(track)"
                :key="clip.id"
                class="track-wave__clip"
                :class="{
                  'track-wave__clip--offline': sourceForClip(clip)?.missing,
                  'track-wave__clip--muted': clip.muted,
                  'track-wave__clip--selected': clip.id === selectedClipId,
                  'track-wave__clip--editing': clip.id === activeClipEditId,
                  'track-wave__clip--locked': clip.locked,
                  'track-wave__clip--moving': clipDropPreview?.drop.clipId === clip.id
                    && !clipDropPreview.drop.copy && !clipDropPreview.blocked,
                }"
                :style="{ ...clipStyle(clip), '--clip-color': clip.color }"
                tabindex="0"
                role="group"
                :aria-label="clipAccessibleName(clip, track)"
                :title="clipAccessibleName(clip, track)"
                @focusin="emit('selectClip', { trackId: track.id, clipId: clip.id })"
                @pointerdown="beginClipPointer($event, track, clip, 'move')"
                @pointermove="updateRazorGuide($event, clip)"
                @pointerleave="clearRazorGuide"
                @lostpointercapture="finishClipPointer"
                @click.stop
                @contextmenu.stop.prevent="openClipContextMenu($event, track, clip)"
              >
                <button
                  v-if="timelineTool === 'selection'"
                  class="track-wave__trim track-wave__trim--start"
                  type="button"
                  :disabled="clip.locked || editingDisabled"
                  :aria-label="t('editor.clipTrimStart')"
                  :title="t('editor.clipTrimStart')"
                  aria-keyshortcuts="ArrowLeft ArrowRight Shift+ArrowLeft Shift+ArrowRight"
                  @keydown="nudgeTrimEdge($event, track, clip, 'trim-start')"
                  @pointerdown.stop="beginClipPointer($event, track, clip, 'trim-start')"
                  @click.stop
                />
                <div v-if="!sourceForClip(clip)?.missing" class="track-wave__label" aria-hidden="true">
                  <n-icon v-if="clip.locked" :component="LockClosedOutline" class="track-wave__lock" />
                  <span v-if="clipWaveWidth(clip) >= 96">{{ sourceForClip(clip)?.name || track.name }}</span>
                </div>
                <div class="track-wave__content">
                  <EditorWaveform
                    v-if="sourceForClip(clip) && !sourceForClip(clip)?.missing"
                    :peaks="sourceForClip(clip)?.peaks || []"
                    :channel-peaks="sourceForClip(clip)?.channelPeaks || []"
                    :channels="sourceForClip(clip)?.channels || 1"
                    :asset-duration="sourceForClip(clip)?.duration || clip.duration"
                    :offset="clip.offset"
                    :duration="clip.duration"
                    :width="clipWaveWidth(clip)"
                    :height="trackWaveHeight(track)"
                    :fade-in="clip.fadeIn"
                    :fade-out="clip.fadeOut"
                    :color="clipColor(clip, track)"
                  />
                  <div v-else-if="sourceForClip(clip)?.missing" class="track-wave__offline">
                    <strong>{{ t('editor.laneAudioOffline') }}</strong>
                    <span>{{ t('editor.assetMissingHint') }}</span>
                  </div>
                  <div v-else class="track-wave__empty">{{ t('editor.laneNoAudio') }}</div>
                </div>
                <button
                  v-if="timelineTool === 'selection'"
                  class="track-wave__trim track-wave__trim--end"
                  type="button"
                  :disabled="clip.locked || editingDisabled"
                  :aria-label="t('editor.clipTrimEnd')"
                  :title="t('editor.clipTrimEnd')"
                  aria-keyshortcuts="ArrowLeft ArrowRight Shift+ArrowLeft Shift+ArrowRight"
                  @keydown="nudgeTrimEdge($event, track, clip, 'trim-end')"
                  @pointerdown.stop="beginClipPointer($event, track, clip, 'trim-end')"
                  @click.stop
                />
              </div>
              <div
                v-if="clipDropPreview?.drop.toTrackId === track.id"
                class="track-wave__clip track-wave__drop-preview"
                :class="{
                  'track-wave__clip--muted': clipDropPreview.clip.muted,
                  'track-wave__drop-preview--blocked': clipDropPreview.blocked,
                }"
                :style="{ ...clipStyle(clipDropPreview.clip, clipDropPreview.drop.start), '--clip-color': clipDropPreview.color }"
                aria-hidden="true"
              >
                <template v-if="!clipDropPreview.blocked">
                  <div class="track-wave__label">
                    <span v-if="clipDropPreview.drop.copy || clipWaveWidth(clipDropPreview.clip) >= 96">
                      {{ clipDropPreview.drop.copy ? '+ ' : '' }}{{ clipWaveWidth(clipDropPreview.clip) >= 96 ? clipDropPreviewSource?.name || track.name : '' }}
                    </span>
                  </div>
                  <div v-if="clipDropPreviewSource && !clipDropPreviewSource.missing" class="track-wave__content">
                    <EditorWaveform
                      :peaks="clipDropPreviewSource.peaks || []"
                      :channel-peaks="clipDropPreviewSource.channelPeaks || []"
                      :channels="clipDropPreviewSource.channels"
                      :asset-duration="clipDropPreviewSource.duration"
                      :offset="clipDropPreview.clip.offset"
                      :duration="clipDropPreview.clip.duration"
                      :width="clipWaveWidth(clipDropPreview.clip)"
                      :height="trackWaveHeight(track)"
                      :fade-in="clipDropPreview.clip.fadeIn"
                      :fade-out="clipDropPreview.clip.fadeOut"
                      :color="clipDropPreview.color"
                    />
                  </div>
                </template>
              </div>
              <div
                v-for="range in overlapRangesByTrack.get(track.id)"
                :key="range.start"
                class="track-wave__overlap"
                :style="{
                  left: `${range.start * pixelsPerSecond}px`,
                  width: `${(range.end - range.start) * pixelsPerSecond}px`,
                }"
                aria-hidden="true"
              />
              <div
                v-if="track.id === selectedTrackId && selectedClip && !clipDropPreview"
                class="track-wave__selection"
                :style="clipStyle(selectedClip)"
                aria-hidden="true"
              />
              <div
                v-if="recordingPreview?.trackId === track.id"
                class="track-wave__recording"
                :style="recordingPreviewStyle()"
                :aria-label="t('editor.recordingTimelinePreview', { time: formatTime(recordingPreview.duration) })"
              >
                <span>{{ t('editor.recordingTimelinePreview', { time: formatTime(recordingPreview.duration) }) }}</span>
              </div>
              <div
                v-if="!clipsForTrack(track).length && recordingPreview?.trackId !== track.id"
                class="track-wave__empty"
              >
                {{ track.role === 'recording' ? t('editor.recordingTrackReadyHint') : t('editor.laneNoAudio') }}
              </div>
            </div>
          </div>
        </div>
        <div v-if="clipMoveActive" class="new-clip-track-lane" data-clip-new-track>
          <div v-if="clipDropPreview?.drop.toTrackId === null" class="new-clip-track-preview" role="status">
            <div class="track-wave__clip track-wave__drop-preview"
              :style="{ ...clipStyle(clipDropPreview.clip, clipDropPreview.drop.start), '--clip-color': clipDropPreview.color }"
              aria-hidden="true" />
            <span>{{ t('editor.clipDropCreatesTrack') }}</span>
          </div>
        </div>
        <span
          v-if="snappingEnabled && clipSnapTime !== null"
          class="clip-snap-line"
          aria-hidden="true"
          :style="{ transform: `translateX(${HEADER_WIDTH + clipSnapTime * pixelsPerSecond}px)` }"
        />
      </div>

      <div
        v-else
        class="empty-state"
        :class="{ 'empty-state--drop-target': externalDragging || dropTargetTrackId === '__empty__' || Boolean(fallbackDropSourceId) }"
        @mouseenter="allowAssetDrop('__empty__')"
      >
        <strong>{{ t('editor.emptyTimeline') }}</strong>
        <span>{{ t('editor.emptyTimelineHint') }}</span>
      </div>

      <div v-if="fallbackDropSourceId && tracks.length" class="drop-new-track">
        {{ t('editor.dropCreatesTrack') }}
      </div>

      <span class="global-playhead" :style="{ transform: `translateX(${playheadLeft}px)` }" />
    </div>

    <n-dropdown
      placement="bottom-start"
      trigger="manual"
      :x="menuX"
      :y="menuY"
      :options="contextClip ? clipMenuOptions : trackMenuOptions"
      :show="showTrackMenu"
      @clickoutside="showTrackMenu = false"
      @select="handleContextMenuSelect"
    />
  </section>
</template>

<style scoped>
:global(.editor-clip-command) {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 20px;
}

:global(.editor-clip-command kbd) {
  color: var(--on-surface-muted);
  font-family: inherit;
  font-size: 0.85em;
}

.editor-mixer:focus-visible,
.track-wave__clip:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: -2px;
}


.editor-mixer {
  --ruler-height: 34px;
  position: relative;
  min-width: 0;
  min-height: 0;
  display: block;
  background: color-mix(in srgb, var(--surface-1) 82%, var(--surface-2));
}

.editor-mixer--external-drop {
  outline: 2px dashed var(--primary);
  outline-offset: -2px;
}

.external-drop-hint {
  position: absolute;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 8;
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: calc(100% - 32px);
  padding: 10px 14px;
  border: 1px solid var(--primary-border);
  border-radius: 8px;
  background: var(--surface-1);
  color: var(--primary-strong);
  font-size: 12px;
  pointer-events: none;
}

.editor-mixer__scroll {
  position: relative;
  min-height: 0;
  height: 100%;
  overflow: auto;
  background:
    linear-gradient(
      180deg,
      color-mix(in srgb, var(--surface-1) 88%, var(--surface-2)),
      color-mix(in srgb, var(--surface-1) 68%, var(--surface-2))
    );
  scrollbar-width: thin;
  scrollbar-color: color-mix(in srgb, var(--surface-4, var(--surface-3)) 82%, rgba(255,255,255,0.14))
    transparent;
}

.editor-mixer__scroll::-webkit-scrollbar {
  width: 12px;
  height: 12px;
}

.editor-mixer__scroll::-webkit-scrollbar-track {
  background: transparent;
  border-radius: 999px;
}

.editor-mixer__scroll::-webkit-scrollbar-thumb {
  border: 3px solid transparent;
  border-radius: 999px;
  background: color-mix(in srgb, var(--on-surface-muted) 48%, transparent) padding-box;
}

.editor-mixer__scroll::-webkit-scrollbar-thumb:hover {
  background: color-mix(in srgb, var(--primary) 64%, transparent) padding-box;
}

.editor-mixer__scroll::-webkit-scrollbar-corner {
  background: transparent;
}

.ruler {
  position: sticky;
  top: 0;
  z-index: 5;
  display: grid;
  grid-template-columns: 180px auto;
  width: max-content;
  min-width: 100%;
  height: var(--ruler-height);
  border-bottom: 1px solid var(--outline);
  background: color-mix(in srgb, var(--surface) 84%, var(--surface-1));
}

.ruler__head {
  position: sticky;
  left: 0;
  z-index: 6;
  width: 180px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 10px;
  border-right: 1px solid var(--outline);
  background: color-mix(in srgb, var(--surface) 84%, var(--surface-1));
  color: var(--on-surface-muted);
}

.ruler__head span,
.ruler__head small {
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.ruler__tools {
  display: flex;
  gap: 3px;
  margin-left: auto;
  margin-right: 7px;
}

.ruler__tool {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid var(--outline-strong);
  border-radius: 5px;
  background: var(--surface-1);
  color: var(--on-surface);
  font-size: 19px;
  line-height: 1;
  cursor: pointer;
}

.ruler__tool:hover {
  background: var(--surface-2);
}

.ruler__tool[aria-pressed='true'] {
  border-color: var(--primary);
  background: color-mix(in srgb, var(--primary) 19%, var(--surface-1));
  color: var(--primary-strong);
}

.ruler__tool:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: -2px;
}

.ruler__tool--selection :deep(svg) {
  transform: rotate(-90deg);
}

.ruler__razor-icon {
  width: 1em;
  height: 1em;
  background: currentColor;
  mask: url('@/assets/editor-razor.svg') center / contain no-repeat;
}

.ruler__tool--snap {
  margin-inline-start: 4px;
}

.ruler__grid {
  position: relative;
  width: 100%;
  height: 100%;
}

.ruler__track {
  position: relative;
  width: var(--lane-width);
}

.ruler-tick {
  position: absolute;
  top: 0;
  left: 0;
  padding: 10px 0 0 4px;
  border-left: 1px solid var(--outline);
  color: var(--on-surface-muted);
  font-size: 10px;
  min-height: 27px;
  white-space: nowrap;
}

.ruler-tick--end { padding-inline: 0; }

.ruler-tick--end .ruler-tick__label {
  position: absolute;
  top: 10px;
  inset-inline-end: 4px;
}

.track-row {
  display: grid;
  grid-template-columns: 180px auto;
  width: max-content;
  min-width: 100%;
  min-height: 50px;
  border-bottom: 1px solid color-mix(in srgb, var(--outline) 64%, transparent);
  background: transparent;
}

.track-row--stereo {
  min-height: 70px;
}

.tracks {
  position: relative;
  display: flex;
  flex-direction: column;
  width: max-content;
  min-width: 100%;
  min-height: calc(100% - var(--ruler-height));
  background-image:
    linear-gradient(90deg, transparent 179px, color-mix(in srgb, var(--outline) 82%, transparent) 179px 180px, transparent 180px),
    linear-gradient(90deg, transparent 0 calc(var(--lane-step) - 1px), color-mix(in srgb, var(--outline) 18%, transparent) calc(var(--lane-step) - 1px) var(--lane-step));
  background-size: 100% 100%, var(--lane-step) 100%;
  background-position: 0 0, 180px 0;
  background-repeat: no-repeat, repeat;
}

.new-clip-track-lane {
  flex: 1 0 70px;
  width: var(--lane-width);
  margin-inline-start: 180px;
}

.new-clip-track-preview {
  position: relative;
  height: 70px;
  border-bottom: 1px dashed var(--primary-border);
  pointer-events: none;
}

.new-clip-track-preview span {
  position: sticky;
  z-index: 7;
  left: 190px;
  display: inline-block;
  margin: 20px 10px 0;
  padding: 5px 9px;
  border-radius: 5px;
  background: var(--surface-1);
  color: var(--primary-strong);
  font-size: 12px;
}

.track-row--selected {
  background: color-mix(in srgb, var(--primary-soft) 28%, transparent);
}

.track-row--offline {
  background: color-mix(in srgb, var(--warning) 5%, transparent);
}

.track-row--dimmed {
  --track-content-opacity: 0.46;
}

.track-row__head {
  position: sticky;
  left: 0;
  z-index: 3;
  isolation: isolate;
  width: 180px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 8px;
  align-items: stretch;
  gap: 6px;
  padding: 5px 8px;
  border-right: 1px solid var(--outline);
  background: color-mix(in srgb, var(--surface) 74%, var(--surface-2));
}

.track-row--selected .track-row__head {
  background: color-mix(in srgb, var(--primary-soft) 40%, color-mix(in srgb, var(--surface) 82%, var(--surface-2)));
}

.track-row--selected .track-row__head::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 2px;
  background: var(--primary);
}

.track-row--offline .track-row__head {
  background: color-mix(in srgb, var(--warning) 7%, var(--surface));
}

.track-row--offline .track-row__head::after {
  content: '';
  position: absolute;
  right: 0;
  top: 8px;
  bottom: 8px;
  width: 2px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--warning) 76%, transparent);
}

.track-row__body {
  min-width: 0;
  display: grid;
  align-content: center;
  gap: 4px;
}

.track-row__title {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.track-row__title strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  font-weight: 600;
  line-height: 1.15;
}

.track-dot {
  width: 11px;
  height: 11px;
  border-radius: 50%;
  flex-shrink: 0;
}

.track-row__subline {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 6px;
}

.track-row__buttons {
  display: flex;
  align-items: center;
  gap: 4px;
  justify-content: flex-start;
  flex-shrink: 0;
}

.track-row__meta {
  color: var(--on-surface-muted);
  font-size: 10px;
  white-space: nowrap;
  opacity: 0.82;
}

.track-row__meta--offline {
  color: color-mix(in srgb, var(--warning) 82%, var(--on-surface-muted));
  font-weight: 600;
}

.chip {
  width: 23px;
  height: 23px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 5px;
  color: var(--on-surface-muted);
  background: color-mix(in srgb, var(--surface-1) 70%, var(--surface-2));
  cursor: pointer;
  font-size: 10px;
  transition: background 140ms ease, color 140ms ease, transform 120ms ease;
}

.chip:hover {
  color: var(--on-surface);
  background: color-mix(in srgb, var(--surface) 72%, var(--surface-2));
}

.chip:active {
  transform: scale(0.96);
}

.chip--active {
  color: #fff;
  background: var(--primary);
}

.chip--warning {
  background: var(--warning);
}

.track-meter {
  display: grid;
  grid-template-columns: repeat(2, 3px);
  gap: 2px;
  align-items: end;
  justify-content: center;
  min-height: 100%;
  flex-shrink: 0;
  align-self: stretch;
  opacity: 1;
}

.track-meter__bar {
  position: relative;
  width: 3px;
  height: 100%;
  overflow: hidden;
  border-radius: 999px;
  background: color-mix(in srgb, var(--on-surface-muted) 10%, transparent);
}

.track-meter--active {
  opacity: 1;
}

.track-meter--active .track-meter__bar {
  background: color-mix(in srgb, var(--on-surface-muted) 12%, transparent);
}

.track-meter__bar i {
  position: absolute;
  inset: 0;
  transform-origin: center bottom;
  border-radius: inherit;
  opacity: 0;
  background: linear-gradient(180deg, rgba(255, 106, 106, 0.95), rgba(255, 170, 102, 0.92) 52%, rgba(92, 214, 132, 0.9));
  transition: transform 70ms linear, opacity 90ms linear;
}

.track-row__lane {
  position: relative;
  z-index: 0;
  width: var(--lane-width);
  overflow: hidden;
  background: color-mix(in srgb, var(--surface) 84%, var(--surface-3));
}

.track-wave {
  position: relative;
  height: 100%;
  min-width: 100%;
}

.track-wave__clip {
  --clip-opacity: calc(var(--track-content-opacity, 1) * var(--clip-content-opacity, 1));
  --clip-tint: color-mix(in srgb, var(--clip-color, var(--track-color)) calc(100% * var(--clip-opacity)), var(--surface-2));
  --clip-border: color-mix(in srgb, var(--on-surface) 32%, var(--clip-tint));
  position: absolute;
  top: 4px;
  left: 0;
  bottom: 4px;
  height: calc(100% - 8px);
  max-width: 100%;
  min-width: 1px;
  overflow: hidden;
  border-radius: 2px;
  background: color-mix(in srgb, var(--clip-tint) 32%, var(--surface-1));
  cursor: grab;
  touch-action: none;
}

.track-wave__clip::after {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: inherit;
  box-shadow: inset 0 0 0 1px var(--clip-border);
  pointer-events: none;
}

.track-wave__label {
  height: var(--clip-label-height);
  display: flex;
  align-items: center;
  overflow: hidden;
  background: color-mix(in srgb, var(--clip-tint) 46%, var(--surface-2));
  color: color-mix(in srgb, var(--primary-strong) 52%, var(--clip-label-contrast, var(--on-surface)));
  font-size: 10px;
  font-weight: 600;
  line-height: var(--clip-label-height);
}

.track-wave__lock {
  flex: none;
  margin-inline-start: 5px;
}

:global(.dark-theme) .editor-mixer {
  --clip-label-contrast: #fff;
}

.track-wave__label span {
  display: block;
  min-width: 0;
  overflow: hidden;
  padding-inline: 7px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.track-wave__content {
  height: calc(100% - var(--clip-label-height));
  opacity: var(--clip-opacity);
}

.editor-mixer--razor .track-wave__clip {
  cursor: url('@/assets/editor-razor.svg') 12 12, pointer;
}

.editor-mixer--razor .track-wave__clip--locked {
  cursor: not-allowed;
}

.track-wave__clip--moving {
  background: transparent;
}

.track-wave__clip--moving::after {
  border: 1px dashed var(--clip-border);
  box-shadow: none;
}

.track-wave__clip--moving > * {
  opacity: 0;
}

.track-wave__drop-preview {
  z-index: 6;
  pointer-events: none;
}

.track-wave__drop-preview::after {
  box-shadow: inset 0 0 0 1px var(--primary-strong);
}

.track-wave__drop-preview--blocked {
  background: color-mix(in srgb, var(--danger) 16%, transparent);
}

.track-wave__drop-preview--blocked::after {
  box-shadow: inset 0 0 0 1px var(--danger);
}

.track-wave__overlap {
  --overlap-edge: color-mix(in srgb, var(--track-color) 52%, var(--on-surface));
  --overlap-shade: color-mix(in srgb, var(--track-color) 22%, black);
  position: absolute;
  z-index: 5;
  top: 4px;
  bottom: 4px;
  min-width: 2px;
  box-shadow: inset 1px 0 var(--overlap-edge), inset -1px 0 var(--overlap-edge);
  background: color-mix(in srgb, var(--overlap-shade) 26%, transparent);
  pointer-events: none;
}

.track-wave__clip--muted {
  --clip-content-opacity: 0.46;
}

.track-wave__clip--editing {
  cursor: grabbing;
}

.track-wave__selection {
  position: absolute;
  z-index: 6;
  inset: 4px auto 4px 0;
  border-radius: 2px;
  box-shadow: inset 0 0 0 2px var(--primary-strong);
  pointer-events: none;
}

.track-wave__recording {
  position: absolute;
  z-index: 7;
  top: 4px;
  left: 0;
  bottom: 4px;
  min-width: 2px;
  overflow: hidden;
  display: flex;
  align-items: center;
  padding: 0 8px;
  border: 1px solid color-mix(in srgb, var(--danger) 64%, var(--outline));
  border-radius: 2px;
  color: color-mix(in srgb, var(--danger) 72%, var(--on-surface));
  background: color-mix(in srgb, var(--danger) 18%, var(--surface));
  pointer-events: none;
}

.track-wave__recording span {
  overflow: hidden;
  color: inherit;
  font-size: 10px;
  font-weight: 600;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.track-wave__trim {
  position: absolute;
  top: 0;
  bottom: 0;
  z-index: 4;
  width: min(7px, 25%);
  padding: 0;
  border: 0;
  background: transparent;
  cursor: ew-resize;
  opacity: 0;
  touch-action: none;
}

.track-wave__trim--start {
  left: 0;
}

.track-wave__trim--end {
  right: 0;
}

.track-wave__trim:hover,
.track-wave__trim:focus-visible {
  opacity: 1;
}

.track-wave__trim::after {
  content: '';
  position: absolute;
  top: 28%;
  bottom: 28%;
  left: 50%;
  width: 1px;
  transform: translateX(-50%);
  border-radius: 999px;
  background: color-mix(in srgb, var(--on-surface) 68%, transparent);
}

.track-wave__clip--offline {
  --clip-border: color-mix(in srgb, var(--on-surface) 40%, var(--warning));
}

.track-wave__clip--offline .track-wave__content {
  height: 100%;
  background:
    repeating-linear-gradient(
      135deg,
      color-mix(in srgb, var(--warning) 12%, transparent) 0 10px,
      color-mix(in srgb, var(--warning) 4%, transparent) 10px 20px
    ),
    linear-gradient(
      180deg,
      color-mix(in srgb, var(--warning) 10%, color-mix(in srgb, var(--surface-1) 52%, var(--surface-2))),
      color-mix(in srgb, var(--warning) 4%, color-mix(in srgb, var(--surface-1) 52%, var(--surface-2)))
    );
}

.track-wave__empty {
  height: 100%;
  display: grid;
  place-items: center;
  color: var(--on-surface-muted);
  font-size: 12px;
}

.track-wave__offline {
  height: 100%;
  display: grid;
  align-content: center;
  justify-items: center;
  gap: 3px;
  padding: 0 14px;
  text-align: center;
}

.track-wave__offline strong {
  font-size: 12px;
  color: color-mix(in srgb, var(--warning) 84%, var(--on-surface));
}

.track-wave__offline span {
  font-size: 10px;
  line-height: 1.35;
  color: var(--on-surface-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
}

.empty-state {
  display: grid;
  gap: 6px;
  margin: 0;
  padding: 48px 28px;
  background: transparent;
  text-align: center;
}

.empty-state strong {
  font-size: 14px;
}

.empty-state--drop-target {
  outline: 1px dashed var(--primary);
  outline-offset: -12px;
  background: color-mix(in srgb, var(--primary-soft) 44%, transparent);
}

.empty-state span {
  color: var(--on-surface-muted);
}

.drop-new-track {
  position: sticky;
  left: 200px;
  bottom: 16px;
  z-index: 7;
  width: fit-content;
  margin: 12px 24px 20px 200px;
  padding: 10px 14px;
  border: 1px dashed color-mix(in srgb, var(--primary) 68%, transparent);
  border-radius: 999px;
  color: var(--primary-strong);
  background: color-mix(in srgb, var(--primary-soft) 44%, var(--surface-1));
  font-size: 12px;
}

.clip-snap-line {
  position: absolute;
  inset: 0 auto 0 0;
  z-index: 2;
  border-left: 1px solid var(--primary-strong);
  pointer-events: none;
}

.global-playhead {
  position: absolute;
  top: 30px;
  bottom: 0;
  left: 0;
  z-index: 2;
  width: 2px;
  background: var(--primary);
  pointer-events: none;
}
</style>
