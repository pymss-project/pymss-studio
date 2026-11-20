<script setup lang="ts">
import { computed, h, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { CutOutline, EllipsisHorizontal, MagnetOutline, NavigateOutline } from '@vicons/ionicons5'
import type { DropdownOption } from 'naive-ui'
import type { EditorClip, EditorClipDrop, EditorSource, EditorTimelineTool, EditorTrack } from '@/types/editor'
import EditorWaveform from '@/components/editor/EditorWaveform.vue'
import { editorClipShortcutLabel, type EditorClipCommand } from '@/utils/editorClipShortcuts'
import { formatTime, formatTimecode } from '@/utils/editorTime'
import {
  clipsForTrack as resolveTrackClips,
  resolveSnappedClipTimingEdit,
  timelineClipEdges,
  type ClipTimingEditMode,
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
  setTool: [tool: EditorTimelineTool]
  toggleSnapping: []
  splitAt: [time: number]
  dropClip: [drop: EditorClipDrop]
  revealTrack: [trackId: string]
  showInspector: []
  contextMute: [trackId: string]
  contextSolo: [trackId: string]
  zoomAt: [payload: { direction?: 'in' | 'out'; anchorRatio: number; deltaY?: number }]
  'scroll-ready': [element: HTMLElement]
  addTrackFromAsset: [sourceId: string]
  beginClipEdit: []
  updateClipTiming: [payload: {
    trackId: string
    clipId: string
    patch: Partial<Pick<EditorClip, 'start' | 'offset' | 'duration'>>
  }]
  commitClipEdit: []
}>()

const { t } = useI18n()
const HEADER_WIDTH = 180
const SNAP_DISTANCE_PX = 7
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
const clipDropPreview = ref<{ drop: EditorClipDrop; clip: EditorClip } | null>(null)
type ClipEditState = {
  pointerId: number
  trackId: string
  mode: ClipTimingEditMode
  clip: EditorClip
  copy: boolean
  clientX: number
  clientY: number
  scrollLeft: number
  start: number
  offset: number
  duration: number
  sourceDuration: number
  snapTargets: number[]
  begun: boolean
}
let clipEditState: ClipEditState | null = null

const isMac = /Mac/i.test(navigator.platform)
const timelineTools = [
  { id: 'selection', label: 'editor.selectionTool', icon: NavigateOutline, shortcut: 'V' },
  { id: 'razor', label: 'editor.razorTool', icon: CutOutline, shortcut: 'C' },
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

const rulerTicks = computed(() => {
  const total = props.duration
  if (total <= 0) return []
  const step = rulerStep.value
  const ticks: { time: number; left: number }[] = []
  for (let time = 0; time <= total; time += step) {
    ticks.push({ time, left: time * props.pixelsPerSecond })
  }
  if (!ticks.length || ticks[ticks.length - 1].time < total) {
    ticks.push({ time: total, left: total * props.pixelsPerSecond })
  }
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

function sourceForClip(clip: EditorClip) {
  return props.sourceMap.get(clip.assetId) || null
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
  return trackChannels(track) >= 2 ? 64 : 42
}

function clipWaveWidth(clip: EditorClip) {
  return Math.max(1, Math.ceil(clip.duration * props.pixelsPerSecond))
}

function clipStyle(clip: EditorClip, start = clip.start) {
  return {
    width: `${Math.max(1, Math.min(laneWidth.value, clipWaveWidth(clip)))}px`,
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
    if (clip.locked || mode !== 'move') return
    const bounds = (event.currentTarget as HTMLElement).getBoundingClientRect()
    emit('splitAt', clip.start + (event.clientX - bounds.left) / props.pixelsPerSecond)
    return
  }

  const copy = mode === 'move' && event.altKey
  if (clip.locked && !copy) return
  if (mode === 'move') (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
  clipEditState = {
    pointerId: event.pointerId,
    trackId: track.id,
    mode,
    clip,
    copy,
    clientX: event.clientX,
    clientY: event.clientY,
    scrollLeft: scrollEl.value?.scrollLeft ?? 0,
    start: clip.start,
    offset: clip.offset,
    duration: clip.duration,
    sourceDuration: Math.max(0, Number(sourceForClip(clip)?.duration || 0)),
    snapTargets: [props.currentTime, ...timelineClipEdges(props.tracks, props.sourceMap, clip.id)],
    begun: false,
  }
  activeClipEditId.value = clip.id
  window.addEventListener('pointermove', handleClipPointerMove)
  window.addEventListener('pointerup', finishClipPointer)
  window.addEventListener('pointercancel', finishClipPointer)
}

function timingAtPointer(event: PointerEvent, active: ClipEditState) {
  const scrollDelta = (scrollEl.value?.scrollLeft ?? 0) - active.scrollLeft
  const delta = (event.clientX - active.clientX + scrollDelta) / props.pixelsPerSecond
  const useSnap = props.snappingEnabled && (active.mode !== 'move' || delta !== 0)
  const { patch, snapTime } = resolveSnappedClipTimingEdit(
    active, active.mode, delta, active.sourceDuration, useSnap ? active.snapTargets : [],
    SNAP_DISTANCE_PX / props.pixelsPerSecond,
  )
  clipSnapTime.value = snapTime
  return patch
}

function clipDropAt(event: PointerEvent, active: ClipEditState): EditorClipDrop | null {
  const lane = document.elementFromPoint(event.clientX, event.clientY)?.closest<HTMLElement>('[data-clip-lane]')
  if (!lane || !mixerRootEl.value?.contains(lane) || !lane.dataset.clipLane) {
    clipSnapTime.value = null
    return null
  }
  const { start = active.start } = timingAtPointer(event, active)
  return {
    clipId: active.clip.id,
    fromTrackId: active.trackId,
    toTrackId: lane.dataset.clipLane,
    start,
    copy: active.copy,
  }
}

function handleClipPointerMove(event: PointerEvent) {
  const active = clipEditState
  if (!active || event.pointerId !== active.pointerId) return
  const deltaPx = event.clientX - active.clientX
  if (!active.begun) {
    const distance = active.mode === 'move' ? Math.hypot(deltaPx, event.clientY - active.clientY) : Math.abs(deltaPx)
    if (distance < 3) return
    active.begun = true
    if (active.mode !== 'move') emit('beginClipEdit')
  }
  if (active.mode === 'move') {
    const drop = clipDropAt(event, active)
    clipDropPreview.value = drop ? { drop, clip: active.clip } : null
    return
  }
  emit('updateClipTiming', { trackId: active.trackId, clipId: active.clip.id, patch: timingAtPointer(event, active) })
}

function clearClipPointer() {
  const active = clipEditState
  if (!active) return
  window.removeEventListener('pointermove', handleClipPointerMove)
  window.removeEventListener('pointerup', finishClipPointer)
  window.removeEventListener('pointercancel', finishClipPointer)
  clipEditState = null
  activeClipEditId.value = null
  clipDropPreview.value = null
  clipSnapTime.value = null
  if (active.begun && active.mode !== 'move') emit('commitClipEdit')
}

function finishClipPointer(event: PointerEvent) {
  const active = clipEditState
  if (!active || event.pointerId !== active.pointerId) return
  const drop = active.begun && active.mode === 'move' && event.type === 'pointerup' && !props.editingDisabled
    ? clipDropAt(event, active) : null
  clearClipPointer()
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
  clearClipPointer()
})

watch(() => props.editingDisabled, (disabled) => {
  if (disabled) clearClipPointer()
})

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
  const track = contextTrack.value
  if (!track) return
  if (key === 'reveal') emit('revealTrack', track.id)
  if (key === 'remove' && !props.editingDisabled) emit('removeTrack', track.id)
}

defineExpose({
  focusTimeline,
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
      :style="{ '--lane-width': `${laneWidth}px`, '--lane-step': `${laneStepPx}px` }"
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
              class="chip ruler__tool"
              :class="{ 'chip--active': tool.id === timelineTool }"
              :aria-pressed="tool.id === timelineTool"
              :aria-label="t(tool.label)"
              :aria-keyshortcuts="tool.shortcut"
              :title="t(tool.label) + ' (' + tool.shortcut + ')'"
              @keydown.space.stop
              @click="useTimelineTool(tool.id)"
            >
              <n-icon :component="tool.icon" />
            </button>
            <button
              type="button"
              class="chip ruler__tool"
              :class="{ 'chip--active': snappingEnabled }"
              :aria-pressed="snappingEnabled"
              :aria-label="t('editor.snapToEdges')"
              aria-keyshortcuts="S"
              :title="t('editor.snapToEdges') + ' (S)'"
              @keydown.space.stop
              @click="toggleTimelineSnapping"
            >
              <n-icon :component="MagnetOutline" />
            </button>
          </div>
          <small>{{ tracks.length }}</small>
        </div>
        <div class="ruler__track" :style="{ width: `${laneWidth}px` }" @click="seekFromEvent">
          <div class="ruler__grid">
            <span v-for="tick in rulerTicks" :key="tick.time" class="ruler-tick" :style="{ transform: `translateX(${tick.left}px)` }">
              {{ formatTimecode(tick.time) }}
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
                  'track-wave__clip--moving': clipDropPreview?.drop.clipId === clip.id && !clipDropPreview.drop.copy,
                }"
                :style="clipStyle(clip)"
                tabindex="0"
                role="group"
                :aria-label="sourceForClip(clip)?.name || track.name"
                :title="sourceForClip(clip)?.name || track.name"
                @focusin="emit('selectClip', { trackId: track.id, clipId: clip.id })"
                @pointerdown="beginClipPointer($event, track, clip, 'move')"
                @lostpointercapture="finishClipPointer"
                @click.stop
                @contextmenu.stop.prevent="openClipContextMenu($event, track, clip)"
              >
                <button
                  v-if="timelineTool === 'selection'"
                  class="track-wave__trim track-wave__trim--start"
                  type="button"
                  :aria-label="t('editor.clipTrimStart')"
                  :title="t('editor.clipTrimStart')"
                  @pointerdown.stop="beginClipPointer($event, track, clip, 'trim-start')"
                  @click.stop
                />
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
                  :color="track.color || '#7aa2ff'"
                />
                <div v-else-if="sourceForClip(clip)?.missing" class="track-wave__offline">
                  <strong>{{ t('editor.laneAudioOffline') }}</strong>
                  <span>{{ t('editor.assetMissingHint') }}</span>
                </div>
                <div v-else class="track-wave__empty">{{ t('editor.laneNoAudio') }}</div>
                <button
                  v-if="timelineTool === 'selection'"
                  class="track-wave__trim track-wave__trim--end"
                  type="button"
                  :aria-label="t('editor.clipTrimEnd')"
                  :title="t('editor.clipTrimEnd')"
                  @pointerdown.stop="beginClipPointer($event, track, clip, 'trim-end')"
                  @click.stop
                />
              </div>
              <div
                v-if="clipDropPreview?.drop.toTrackId === track.id"
                class="track-wave__clip track-wave__drop-preview"
                :style="clipStyle(clipDropPreview.clip, clipDropPreview.drop.start)"
                aria-hidden="true"
              >
                <span v-if="clipDropPreview.drop.copy">+</span>
              </div>
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
  height: 28px;
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
  margin-right: 8px;
}

.ruler__tool {
  line-height: 1;
}

.ruler__tool :deep(svg) {
  width: 13px;
  height: 13px;
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
  padding: 7px 0 0 4px;
  border-left: 1px solid var(--outline);
  color: var(--on-surface-muted);
  font-size: 10px;
  white-space: nowrap;
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
  width: max-content;
  min-width: 100%;
  min-height: calc(100% - 28px);
  background-image:
    linear-gradient(90deg, transparent 179px, color-mix(in srgb, var(--outline) 82%, transparent) 179px 180px, transparent 180px),
    linear-gradient(90deg, transparent 0 calc(var(--lane-step) - 1px), color-mix(in srgb, var(--outline) 18%, transparent) calc(var(--lane-step) - 1px) var(--lane-step));
  background-size: 100% 100%, var(--lane-step) 100%;
  background-position: 0 0, 180px 0;
  background-repeat: no-repeat, repeat;
}

.track-row--selected {
  background: color-mix(in srgb, var(--primary-soft) 28%, transparent);
}

.track-row--offline {
  background: color-mix(in srgb, var(--warning) 5%, transparent);
}

.track-row--dimmed .track-row__lane {
  opacity: 0.46;
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
  background: color-mix(in srgb, var(--surface) 76%, var(--surface-1));
  border-left: 1px solid color-mix(in srgb, var(--outline) 44%, transparent);
}

.track-row--offline .track-row__lane {
  background: color-mix(in srgb, var(--warning) 4%, var(--surface));
}

.track-row--selected .track-row__lane {
  background: color-mix(in srgb, var(--primary-soft) 16%, transparent);
}

.track-wave {
  position: relative;
  height: 100%;
  min-width: 100%;
}

.track-wave__clip {
  position: absolute;
  top: 4px;
  left: 0;
  bottom: 4px;
  height: calc(100% - 8px);
  max-width: 100%;
  min-width: 1px;
  overflow: hidden;
  border-radius: 2px;
  border-top: 1px solid color-mix(in srgb, var(--track-color, var(--on-surface-muted)) 22%, transparent);
  border-right: 1px solid color-mix(in srgb, var(--track-color, var(--on-surface-muted)) 22%, transparent);
  border-bottom: 1px solid color-mix(in srgb, var(--track-color, var(--on-surface-muted)) 22%, transparent);
  border-left: 1px solid color-mix(in srgb, var(--track-color, var(--on-surface-muted)) 22%, transparent);
  background:
    linear-gradient(
      180deg,
      color-mix(in srgb, var(--track-color, #7aa2ff) 22%, color-mix(in srgb, var(--surface-1) 52%, var(--surface-2))),
      color-mix(in srgb, var(--track-color, #7aa2ff) 13%, color-mix(in srgb, var(--surface-1) 52%, var(--surface-2)))
    );
  box-shadow: none;
  cursor: grab;
  touch-action: none;
}

.editor-mixer--razor .track-wave__clip {
  cursor: crosshair;
}

.editor-mixer--razor .track-wave__clip--locked {
  cursor: not-allowed;
}

.track-wave__clip--moving {
  opacity: 0.45;
}

.track-wave__drop-preview {
  z-index: 4;
  display: grid;
  place-items: center;
  border: 1px dashed var(--primary);
  background: var(--primary-soft);
  color: var(--primary-strong);
  pointer-events: none;
}

.track-wave__clip--muted {
  opacity: 0.46;
}

.track-wave__clip--editing {
  cursor: grabbing;
}

.track-wave__clip--selected {
  border-color: color-mix(in srgb, var(--track-color, var(--primary)) 58%, var(--outline));
}

.track-wave__clip--selected::before {
  background: color-mix(in srgb, var(--track-color, var(--primary)) 76%, transparent);
}

.track-wave__recording {
  position: absolute;
  z-index: 5;
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
  width: 7px;
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

.track-wave__clip:hover .track-wave__trim,
.track-wave__clip--selected .track-wave__trim {
  opacity: 1;
}

.track-wave__trim::after {
  content: '';
  position: absolute;
  top: 28%;
  bottom: 28%;
  left: 3px;
  width: 1px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--on-surface) 68%, transparent);
}

.track-wave__clip--offline {
  border-top-color: color-mix(in srgb, var(--warning) 28%, transparent);
  border-right-color: color-mix(in srgb, var(--warning) 28%, transparent);
  border-bottom-color: color-mix(in srgb, var(--warning) 28%, transparent);
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

/* Explicit end-cap so the clip terminus is clearly pinned to the right edge. */
.track-wave__clip::before {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  right: 0;
  width: 3px;
  background: color-mix(in srgb, var(--track-color, #7aa2ff) 44%, transparent);
  pointer-events: none;
}

.track-wave__clip::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  top: 50%;
  height: 1px;
  transform: translateY(-50%);
  background: color-mix(in srgb, var(--track-color, #7aa2ff) 30%, transparent);
  opacity: 0.62;
  pointer-events: none;
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
  border-left: 1px dashed var(--on-surface);
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
