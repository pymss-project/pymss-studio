<script setup lang="ts">
import { isTutorialFrame } from '@/features/onboarding/demoContext'
import { computed, nextTick, onScopeDispose, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { useDialog, useMessage } from 'naive-ui'
import { useI18n } from 'vue-i18n'
import { FolderOpenOutline, MusicalNotesOutline, OptionsOutline } from '@vicons/ionicons5'
import { useEditorStore } from '@/stores/editor'
import { currentPluginSession, observePluginTransport } from '@/utils/pluginHost'
import { useTaskStore } from '@/stores/task'
import { useSettingsStore } from '@/stores/settings'
import EditorAssetPanel from '@/components/editor/EditorAssetPanel.vue'
import EditorExportDialog from '@/components/editor/EditorExportDialog.vue'
import EditorInspectorPanel from '@/components/editor/EditorInspectorPanel.vue'
import EditorMixer from '@/components/editor/EditorMixer.vue'
import EditorTransportBar from '@/components/editor/EditorTransportBar.vue'
import { useEditorAssetDrag } from '@/composables/useEditorAssetDrag'
import { useEditorAssets } from '@/composables/useEditorAssets'
import { useEditorExport } from '@/composables/useEditorExport'
import { useEditorLayout } from '@/composables/useEditorLayout'
import { useEditorMixerView } from '@/composables/useEditorMixerView'
import { useEditorPlayback } from '@/composables/useEditorPlayback'
import { useEditorProjectBridge } from '@/composables/useEditorProjectBridge'
import { useEditorRecording } from '@/composables/useEditorRecording'
import { useDiagnosticWindowState } from '@/features/diagnostics/windowState'
import { useEditorShortcuts } from '@/composables/useEditorShortcuts'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const { t, locale } = useI18n()
const editor = useEditorStore()
const task = useTaskStore()
const settings = useSettingsStore()

const MIXER_HEAD_WIDTH = 180
const ASSET_RAIL_WIDTH = 34
const INSPECTOR_RAIL_WIDTH = 34
const ASSET_PANEL_WIDTH = 218
const RESIZER_WIDTH = 10
const hasTauriApis = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
const shellEl = ref<HTMLElement | null>(null)
const mixerScrollEl = ref<HTMLElement | null>(null)
const assetPanelEl = ref<HTMLElement | null>(null)
const mixerRef = ref<InstanceType<typeof EditorMixer> | null>(null)

const session = computed(() => editor.session)
const routeProjectId = computed(() => String(route.query.projectId || ''))
const sessionName = computed(() => session.value?.name || t('editor.fallbackTitle'))
const {
  activeResize,
  assetPanelVisible,
  assetResizerVisible,
  inspectorVisible,
  inspectorPanelVisible,
  inspectorPanelWidth,
  shellStyle,
  startResize,
  toggleAssetPanel,
  toggleInspectorPanel,
} = useEditorLayout({
  shellEl,
  assetRailWidth: ASSET_RAIL_WIDTH,
  inspectorRailWidth: INSPECTOR_RAIL_WIDTH,
  resizerWidth: RESIZER_WIDTH,
  minAssetWidth: 180,
  maxAssetWidth: 320,
  minCenterWidth: 520,
  minInspectorWidth: 240,
  maxInspectorWidth: 340,
  initialAssetWidth: ASSET_PANEL_WIDTH,
  initialInspectorWidth: 268,
})
const {
  draggingSourceName,
  draggingGhost,
  clearAssetPointerDrag,
  handleAssetPointerGrab,
} = useEditorAssetDrag({
  mixerRef,
})
const {
  librarySources,
  addSourceAsReference,
  addTrackFromAsset,
  revealSource,
  relinkSource,
  revealTrackSource,
  openExportDir,
  removeSource,
} = useEditorAssets({
  editor,
  task,
  message,
  dialog,
  t,
  session,
  clearAssetPointerDrag,
})
const { isDraggingExternal, isDraggingExternalOnMixer, isImportingExternal } = useEditorProjectBridge({
  routeProjectId,
  hasTauriApis,
  editor,
  assetPanelEl,
  mixerRef,
  canDropToMixer: () => !recordingBusy.value,
  zoomFactor: computed(() => settings.scaleFactor),
  message,
  t,
})
const {
  showExportDialog,
  exportFormatDraft,
  exportWavBitDepthDraft,
  exportFlacBitDepthDraft,
  exportSampleRateDraft,
  exportPeakProtectionDraft,
  exportFileNameDraft,
  exportDirDraft,
  exportDirPicking,
  exportProgressPercentage,
  exportProgressText,
  openExportDialog,
  setExportDialogVisible,
  setExportFormat,
  setExportWavBitDepth,
  setExportFlacBitDepth,
  setExportSampleRate,
  setExportPeakProtection,
  setExportFileName,
  setExportDir,
  pickExportDir,
  exportMix,
  cancelExport,
} = useEditorExport({
  editor,
  settings,
  message,
  t,
})
const playback = useEditorPlayback({ editor, scrollEl: mixerScrollEl, trackHeaderWidth: MIXER_HEAD_WIDTH })
const {
  transportVisualState,
  transportPendingAction,
  transportCanToggle,
  shouldFollowPlayhead,
  currentTime: playbackCurrentTime,
  loop: playbackLoop,
  trackLevels,
  playbackError,
  stop: playbackStop,
  requestPlay: playbackRequestPlay,
  requestPause: playbackRequestPause,
  toggleTransport,
  seek: playbackSeek,
  followPlayhead: playbackFollowPlayhead,
} = playback
const {
  devices: recordingDevices,
  selectedDeviceId: recordingDeviceId,
  state: recordingState,
  isRecording,
  isBusy: recordingBusy,
  inputLevel: recordingInputLevel,
  elapsed: recordingElapsed,
  startTime: recordingStartTime,
  targetTrackId: recordingTargetTrackId,
  error: recordingError,
  refreshDevices: refreshRecordingDevices,
  selectDevice: selectRecordingDevice,
  toggleRecording,
  cancelRecording,
} = useEditorRecording({
  editor,
  currentTime: playbackCurrentTime,
  requestPlayback: playbackRequestPlay,
  pausePlayback: () => playbackRequestPause(false),
  followPlayhead: playbackFollowPlayhead,
})
const recordingPreview = computed(() => {
  if (!recordingTargetTrackId.value || recordingState.value === 'idle' || recordingState.value === 'preparing') {
    return null
  }
  return {
    trackId: recordingTargetTrackId.value,
    start: recordingStartTime.value,
    duration: Math.max(0.01, recordingElapsed.value),
  }
})
useDiagnosticWindowState('editor', () => ({
  kind: 'editor', projectId: editor.session?.id || '', loading: editor.loading, saving: editor.saving,
  exporting: editor.exporting, exportCancelling: editor.exportCancelling,
  exportPhase: editor.exportProgress?.phase || '', exportCompleted: editor.exportProgress?.completed,
  exportTotal: editor.exportProgress?.total, hasLastExport: Boolean(editor.lastExport), error: editor.lastError,
  lastExportPath: editor.lastExport?.path || '',
  playbackStatus: playback.status.value, playbackError: playbackError.value,
  recordingState: recordingState.value, recordingError: recordingError.value,
  pluginStateDirty: editor.pluginStateDirty, pluginStateError: editor.pluginStateError,
  pluginStateUnavailable: editor.pluginStateUnavailable,
}))
const displayedTimelineDuration = computed(() => (
  recordingPreview.value ? Math.max(editor.duration, playbackCurrentTime.value) : editor.duration
))
const relinkingMissingAssets = ref(false)
const missingSources = computed(() => editor.missingSources())
const missingAssetPreview = computed(() => missingSources.value.slice(0, 3).map((source) => source.name))
const {
  zoomFit,
  zoomAt,
  zoomIn,
  zoomOut,
  updatePlaybackLoop,
  handleMixerScrollReady,
} = useEditorMixerView({
  editor,
  trackHeaderWidth: MIXER_HEAD_WIDTH,
  scrollEl: mixerScrollEl,
  playbackLoop,
})

async function relinkMissingAssets() {
  try {
    relinkingMissingAssets.value = true
    const result = await editor.relinkMissingSources()
    if (!result) return
    if (result.unresolved.length) {
      message.warning(t('editor.assetRelinkPartial', { resolved: result.relinked, unresolved: result.unresolved.length }))
      return
    }
    message.success(t('editor.assetRelinkSuccess', { count: result.relinked }))
  } catch (error) {
    message.error(error instanceof Error ? error.message : t('editor.assetRelinkFailed'))
  } finally {
    relinkingMissingAssets.value = false
  }
}

let pendingInitialZoomFitSessionId = ''
let appliedInitialZoomFitSessionId = ''

function scheduleInitialZoomFit(sessionId: string) {
  if (!sessionId || appliedInitialZoomFitSessionId === sessionId) return
  pendingInitialZoomFitSessionId = sessionId
  void nextTick(() => {
    requestAnimationFrame(() => {
      if (pendingInitialZoomFitSessionId !== sessionId) return
      if (editor.session?.id !== sessionId || editor.duration <= 0 || !mixerScrollEl.value) return
      zoomFit()
      appliedInitialZoomFitSessionId = sessionId
    })
  })
}

async function togglePlayback() {
  if (isRecording.value) return false
  const ok = await toggleTransport()
  if (!ok && playbackError.value) message.error(playbackError.value)
}

let nativeShortcutDisposed = false
let nativeShortcutQueue = Promise.resolve()
const stopNativeShortcuts = observePluginTransport(request => {
  nativeShortcutQueue = nativeShortcutQueue.then(async () => {
    const current = currentPluginSession()
    if (nativeShortcutDisposed || editor.session !== request.project || current !== request.session
      || current.sessionId !== request.sessionId || current.transportOwner !== request.owner) return
    await togglePlayback()
  }).catch(cause => { if (!nativeShortcutDisposed) message.error(String(cause)) })
})
onScopeDispose(() => { nativeShortcutDisposed = true; stopNativeShortcuts() })

async function handleRecordingToggle() {
  if (recordingState.value === 'idle' && isImportingExternal.value) {
    message.warning(t('editor.recordingImportPending'))
    return
  }
  const wasRecording = isRecording.value
  const ok = await toggleRecording()
  if (!ok && recordingError.value) {
    message.error(t('editor.recordingFailed', { detail: recordingError.value }))
    return
  }
  if (wasRecording) message.success(t('editor.recordingSaved'))
}

async function handleRecordingCancel() {
  await cancelRecording()
  message.info(t('editor.recordingCancelled'))
}

async function handleRecordingDevicesRefresh() {
  try {
    await refreshRecordingDevices(true)
  } catch (error) {
    message.error(t('editor.recordingDeviceFailed', {
      detail: error instanceof Error ? error.message : String(error),
    }))
  }
}

function handleAddRecordingTrack() {
  const track = editor.addRecordingTrack()
  message.success(t('editor.recordingTrackAdded', { name: track.name }))
}

function handleTrackMuteRequest(trackId: string) {
  editor.toggleTrackFlag(trackId, 'muted')
}

function handleTrackSoloRequest(trackId: string) {
  editor.toggleTrackFlag(trackId, 'solo')
}

function toggleSelectedTrackFlag(flag: 'muted' | 'solo') {
  if (!editor.selectedTrackId) return
  editor.toggleTrackFlag(editor.selectedTrackId, flag)
}

function removeSelectedTrack() {
  if (recordingBusy.value || !editor.selectedTrackId) return
  editor.removeTrack(editor.selectedTrackId)
}

function removeTrack(trackId: string) {
  if (recordingBusy.value) return
  editor.removeTrack(trackId)
}

function removeClip(payload: { trackId: string; clipId: string }) {
  if (recordingBusy.value) return
  editor.removeClip(payload.trackId, payload.clipId)
}

function undoEditor() {
  if (recordingBusy.value) return
  editor.undo()
}

function redoEditor() {
  if (recordingBusy.value) return
  editor.redo()
}

function openSelectedTrackInspector() {
  toggleInspectorPanel(true)
}

function handleTransportToggleRequest() {
  void togglePlayback()
}

function stopPlayback() {
  playbackStop(false)
}

function stopPlaybackAndReset() {
  playbackStop(true)
}

function resetPlayhead() {
  if (isRecording.value) return
  playbackSeek(0)
}

function seekTimeline(time: number) {
  if (isRecording.value) return
  playbackSeek(time)
}

function seekBy(delta: number) {
  playbackSeek(Math.max(0, playbackCurrentTime.value + delta))
}

function setTrackVolume(trackId: string, value: number) {
  editor.setTrackVolume(trackId, value)
}

function setTrackPan(trackId: string, value: number) {
  editor.setTrackPan(trackId, value)
}

function setMasterVolume(value: number) {
  editor.setMasterVolume(value)
}

function setMasterPan(value: number) {
  editor.setMasterPan(value)
}

function setClipFades(payload: {
  trackId: string
  clipId: string
  patch: { fadeIn?: number; fadeOut?: number }
}) {
  editor.setClipFades(payload.trackId, payload.clipId, payload.patch)
}

function handleSelectClip(payload: { trackId: string; clipId: string }) {
  editor.selectClip(payload.trackId, payload.clipId)
}

function handleClipTiming(payload: {
  trackId: string
  clipId: string
  patch: { start?: number; offset?: number; duration?: number }
}) {
  editor.setClipTiming(payload.trackId, payload.clipId, payload.patch)
}

async function save() {
  try {
    await editor.saveProject()
    message.success(t('editor.saved'))
  } catch (cause) { if (!editor.pluginStateError) message.error(editor.lastError || String(cause)) }
}

watch(() => editor.pluginStateError, error => {
  const host = currentPluginSession()
  if (error && !(host?.project === editor.session && host.interactionRequired)) message.error(error, { duration: 10000 })
})

onBeforeRouteLeave(async () => {
  try {
    if (editor.session) await editor.releasePluginSession(editor.session)
    return true
  } catch (cause) {
    if (!editor.pluginStateError) message.error(editor.lastError || String(cause))
    return false
  }
})

useEditorShortcuts({
  togglePlay: togglePlayback,
  stop: stopPlayback,
  undo: undoEditor,
  redo: redoEditor,
  zoomIn,
  zoomOut,
  save,
  toHome: resetPlayhead,
  seek: seekBy,
  toggleMute: () => toggleSelectedTrackFlag('muted'),
  toggleSolo: () => toggleSelectedTrackFlag('solo'),
  removeTrack: removeSelectedTrack,
})

watch(() => editor.session?.id, stopPlaybackAndReset)
watch(locale, () => editor.localizeDefaultTrackNames(), { immediate: true })
watch(routeProjectId, (value) => {
  if (!value) {
    editor.clearSession()
  }
})
watch(
  () => [editor.session?.id || '', editor.duration, Boolean(mixerScrollEl.value)] as const,
  ([sessionId]) => scheduleInitialZoomFit(sessionId),
  { immediate: true },
)
</script>

<template>
  <div class="editor-view">
    <div
      ref="shellEl"
      class="editor-shell"
      :class="{
        'editor-shell--resizing': Boolean(activeResize),
        'editor-shell--playback-following': shouldFollowPlayhead,
      }"
      :style="shellStyle"
      @contextmenu.prevent
    >
      <EditorTransportBar data-guide="demo-editor-transport"
        :session-name="sessionName"
        :track-count="session?.tracks.length || 0"
        :current-time="playbackCurrentTime"
        :duration="displayedTimelineDuration"
        :transport-visual-state="transportVisualState"
        :transport-pending-action="transportPendingAction"
        :transport-can-toggle="transportCanToggle"
        :loop="playbackLoop"
        :master-volume="editor.masterVolume"
        :master-pan="editor.masterPan"
        :saving="editor.saving"
        :exporting="editor.exporting"
        :can-undo="editor.canUndo"
        :can-redo="editor.canRedo"
        :disabled="!session"
        :missing-asset-count="missingSources.length"
        :missing-asset-preview="missingAssetPreview"
        :relinking-missing-assets="relinkingMissingAssets"
        :recording-state="recordingState"
        :recording-start-disabled="isImportingExternal || isTutorialFrame()"
        :recording-devices="recordingDevices"
        :recording-device-id="recordingDeviceId"
        :recording-input-level="recordingInputLevel"
        :recording-elapsed="recordingElapsed"
        :recording-error="recordingError"
        @reset="resetPlayhead"
        @stop="stopPlaybackAndReset"
        @toggle-transport="handleTransportToggleRequest"
        @update:loop="updatePlaybackLoop"
        @update:master-volume="setMasterVolume"
        @begin-master-volume="editor.beginInteraction"
        @commit-master-volume="editor.commitInteraction"
        @update:master-pan="setMasterPan"
        @begin-master-pan="editor.beginInteraction"
        @commit-master-pan="editor.commitInteraction"
        @undo="undoEditor"
        @redo="redoEditor"
        @save="save"
        @export="openExportDialog"
        @relink-missing-assets="relinkMissingAssets"
        @toggle-recording="handleRecordingToggle"
        @cancel-recording="handleRecordingCancel"
        @refresh-recording-devices="handleRecordingDevicesRefresh"
        @add-recording-track="handleAddRecordingTrack"
        @update:recording-device-id="selectRecordingDevice"
      />

      <div v-if="editor.loading" class="editor-state">{{ t('editor.loading') }}</div>
      <div v-else-if="!routeProjectId" class="editor-empty-state">
        <span class="editor-empty-state__icon">
          <n-icon :component="FolderOpenOutline" />
        </span>
        <strong>{{ t('editor.noProjectSelected') }}</strong>
        <p>{{ t('editor.noProjectSelectedHint') }}</p>
        <div class="editor-empty-state__actions">
          <n-button type="primary" @click="router.push('/results')">
            <template #icon><n-icon :component="MusicalNotesOutline" /></template>
            {{ t('editor.openResultsList') }}
          </n-button>
        </div>
      </div>
      <div v-else-if="!session" class="editor-state">{{ t('editor.notFound') }}</div>
      <template v-else>
        <aside
          class="editor-shell__assets"
          :class="{ 'editor-shell__assets--collapsed': !assetPanelVisible }"
        >
          <div class="editor-shell__asset-rail">
            <button
              type="button"
              class="editor-shell__asset-toggle"
              :aria-pressed="assetPanelVisible"
              :aria-label="assetPanelVisible ? t('common.collapse') : t('editor.assetLibrary')"
              @click="toggleAssetPanel()"
            >
              <span class="editor-shell__asset-toggle-icon">
                <span />
              </span>
              <em>{{ t('editor.assetLibrary') }}</em>
            </button>
          </div>

          <div ref="assetPanelEl" class="editor-shell__asset-panel" :inert="!assetPanelVisible" :aria-hidden="!assetPanelVisible">
            <EditorAssetPanel
              :sources="librarySources"
              :tree="editor.assetTree"
              :external-dragging="isDraggingExternal"
              @source-add="addSourceAsReference"
              @source-pointer-grab="handleAssetPointerGrab"
              @source-reveal="revealSource"
              @source-relink="relinkSource"
              @source-remove="removeSource"
            />
          </div>
        </aside>

        <div
          class="editor-shell__resizer editor-shell__resizer--left"
          :class="{ 'editor-shell__resizer--hidden': !assetResizerVisible }"
          @mousedown="startResize('assets', $event)"
        >
          <span />
        </div>

        <div class="editor-shell__center">
          <EditorMixer data-guide="demo-editor-mixer"
            ref="mixerRef"
            :tracks="session.tracks"
            :source-map="editor.sourceMap"
            :selected-track-id="editor.selectedTrackId"
            :selected-clip-id="editor.selectedClipId"
            :current-time="playbackCurrentTime"
            :duration="displayedTimelineDuration"
            :pixels-per-second="editor.pixelsPerSecond"
            :track-levels="trackLevels"
            :editing-disabled="recordingBusy"
            :external-dragging="isDraggingExternalOnMixer"
            :external-importing="isImportingExternal"
            :recording-preview="recordingPreview"
            @scroll-ready="handleMixerScrollReady"
            @select-track="editor.selectTrack"
            @select-clip="handleSelectClip"
            @begin-clip-edit="editor.beginInteraction"
            @update-clip-timing="handleClipTiming"
            @commit-clip-edit="editor.commitInteraction"
            @toggle-mute="handleTrackMuteRequest"
            @toggle-solo="handleTrackSoloRequest"
            @context-mute="handleTrackMuteRequest"
            @context-solo="handleTrackSoloRequest"
            @seek="seekTimeline"
            @remove-track="removeTrack"
            @remove-clip="removeClip"
            @reveal-track="revealTrackSource"
            @zoom-in="editor.zoomIn"
            @zoom-out="editor.zoomOut"
            @zoom-fit="zoomFit"
            @zoom-at="zoomAt"
            @add-track-from-asset="addTrackFromAsset"
            @show-inspector="openSelectedTrackInspector"
          />
        </div>

        <div
          class="editor-shell__resizer editor-shell__resizer--right"
          :class="{ 'editor-shell__resizer--hidden': !inspectorPanelVisible }"
          @mousedown="startResize('inspector', $event)"
        >
          <span />
        </div>

        <aside
          v-if="inspectorVisible"
          class="editor-shell__inspector"
          :class="{ 'editor-shell__inspector--collapsed': !inspectorPanelVisible }"
        >
          <button
            type="button"
            class="editor-shell__inspector-toggle"
            :aria-pressed="inspectorPanelVisible"
            :aria-label="inspectorPanelVisible ? t('common.collapse') : t('editor.inspectorTitle')"
            @click="toggleInspectorPanel()"
          >
            <n-icon :component="OptionsOutline" />
            <em>{{ t('editor.inspectorTitle') }}</em>
          </button>
          <div class="editor-shell__inspector-panel" :inert="!inspectorPanelVisible" :aria-hidden="!inspectorPanelVisible">
            <EditorInspectorPanel
              :session="session"
              :selected-track-id="editor.selectedTrackId"
              :selected-clip-id="editor.selectedClipId"
              :selected-source="editor.selectedSource"
              :duration="displayedTimelineDuration"
              :last-export-path="editor.lastExport?.path || null"
              :compact="inspectorPanelWidth <= 248"
              @select-track="editor.selectTrack"
              @toggle-playback="togglePlayback"
              @set-master-volume="setMasterVolume"
              @set-master-pan="setMasterPan"
              @set-master-limiter="editor.setMasterLimiter"
              @begin-master-limiter="editor.beginInteraction"
              @commit-master-limiter="editor.commitInteraction"
              @begin-master-volume="editor.beginInteraction"
              @commit-master-volume="editor.commitInteraction"
              @begin-master-pan="editor.beginInteraction"
              @commit-master-pan="editor.commitInteraction"
              @rename-track="editor.renameTrack"
              @toggle-track-flag="(trackId, flag) => editor.toggleTrackFlag(trackId, flag)"
              @set-track-volume="setTrackVolume"
              @set-track-pan="setTrackPan"
              @begin-track-volume="editor.beginInteraction"
              @commit-track-volume="editor.commitInteraction"
              @begin-track-pan="editor.beginInteraction"
              @commit-track-pan="editor.commitInteraction"
              @set-clip-fades="setClipFades"
              @set-clip-timing="handleClipTiming"
              @begin-clip-timing="editor.beginInteraction"
              @commit-clip-timing="editor.commitInteraction"
              @open-location="openExportDir"
              @relink-source="() => editor.selectedSource && relinkSource(editor.selectedSource)"
            />
          </div>
        </aside>
      </template>
    </div>
    <div
      v-if="draggingSourceName && draggingGhost"
      class="editor-drag-ghost"
      :style="{ left: `${draggingGhost.x + 18}px`, top: `${draggingGhost.y + 18}px` }"
    >
      {{ draggingSourceName }}
    </div>

    <EditorExportDialog
      :show="showExportDialog"
      :session-name="sessionName"
      :duration="displayedTimelineDuration"
      :track-count="session?.tracks.length || 0"
      :exporting="editor.exporting"
      :export-cancelling="editor.exportCancelling"
      :export-progress="exportProgressPercentage"
      :export-progress-indeterminate="!editor.exportProgress || (editor.exportProgress.phase === 'preparing' && editor.exportProgress.completed === 0)"
      :export-progress-text="exportProgressText"
      :format="exportFormatDraft"
      :wav-bit-depth="exportWavBitDepthDraft"
      :flac-bit-depth="exportFlacBitDepthDraft"
      :sample-rate="exportSampleRateDraft"
      :peak-protection="exportPeakProtectionDraft"
      :file-name="exportFileNameDraft"
      :export-dir="exportDirDraft"
      :export-dir-resolving="exportDirPicking"
      @update:show="setExportDialogVisible"
      @update:format="setExportFormat"
      @update:wav-bit-depth="setExportWavBitDepth"
      @update:flac-bit-depth="setExportFlacBitDepth"
      @update:sample-rate="setExportSampleRate"
      @update:peak-protection="setExportPeakProtection"
      @update:file-name="setExportFileName"
      @update:export-dir="setExportDir"
      @pick-export-dir="pickExportDir"
      @confirm="exportMix"
      @cancel-export="cancelExport"
    />
  </div>
</template>

<style scoped>
.editor-view {
  position: relative;
}

.editor-shell {
  --asset-rail-width: 34px;
  --asset-panel-width: 218px;
  --asset-resizer-width: 10px;
  --inspector-rail-width: 34px;
  --inspector-resizer-width: 10px;
  --inspector-width: 268px;
  position: relative;
  height: calc(100vh - 40px);
  height: calc(100dvh - 40px);
  container-type: inline-size;
  container-name: editor-shell;
  display: grid;
  grid-template-columns:
    calc(var(--asset-rail-width) + var(--asset-panel-width))
    var(--asset-resizer-width)
    minmax(0, 1fr)
    var(--inspector-resizer-width)
    calc(var(--inspector-rail-width) + var(--inspector-width));
  grid-template-rows: auto minmax(0, 1fr);
  background: var(--surface);
  color: var(--on-surface);
  overflow: hidden;
  user-select: none;
  -webkit-user-select: none;
  transition: grid-template-columns 180ms ease;
}

.editor-shell :deep(input),
.editor-shell :deep(textarea),
.editor-shell :deep([contenteditable='true']),
.editor-shell :deep(.n-input__input-el),
.editor-shell :deep(.n-base-selection-input) {
  user-select: text;
  -webkit-user-select: text;
}

.editor-shell :deep(.editor-transport) {
  grid-column: 1 / -1;
}

.editor-shell__assets {
  grid-column: 1;
  grid-row: 2;
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-columns: var(--asset-rail-width) minmax(0, var(--asset-panel-width));
  border-right: 1px solid var(--outline);
  background: color-mix(in srgb, var(--surface) 90%, var(--surface-1));
  transition: grid-template-columns 220ms ease, border-color 180ms ease;
}

.editor-shell__assets--collapsed {
  border-right-color: color-mix(in srgb, var(--outline) 46%, transparent);
}

.editor-shell__assets--collapsed .editor-shell__asset-panel {
  visibility: hidden;
  pointer-events: none;
}

.editor-shell__asset-rail {
  min-height: 0;
  padding: 6px 4px;
  border-right: 1px solid var(--outline);
  background: color-mix(in srgb, var(--surface) 88%, var(--surface-1));
}

.editor-shell__asset-toggle {
  width: 100%;
  display: grid;
  justify-items: center;
  gap: 0;
  padding: 4px 0;
  border: 0;
  border-radius: 8px;
  color: var(--on-surface-muted);
  background: transparent;
  cursor: pointer;
  transition: color 180ms ease, background 180ms ease;
}

.editor-shell__asset-toggle:hover {
  color: var(--on-surface);
  background: var(--surface-2);
}

.editor-shell__asset-toggle-icon {
  width: 22px;
  height: 22px;
  display: grid;
  place-items: center;
  border-radius: 6px;
  background: color-mix(in srgb, var(--surface-2) 92%, transparent);
  border: 1px solid color-mix(in srgb, var(--outline) 54%, transparent);
}

.editor-shell__asset-toggle-icon span {
  width: 12px;
  height: 10px;
  position: relative;
  display: block;
}

.editor-shell__asset-toggle-icon span::before,
.editor-shell__asset-toggle-icon span::after,
.editor-shell__asset-toggle-icon span {
  border-radius: 999px;
  background: currentColor;
}

.editor-shell__asset-toggle-icon span::before,
.editor-shell__asset-toggle-icon span::after {
  content: '';
  position: absolute;
  left: 0;
  width: 12px;
  height: 2px;
}

.editor-shell__asset-toggle-icon span {
  height: 2px;
}

.editor-shell__asset-toggle-icon span::before {
  top: -5px;
}

.editor-shell__asset-toggle-icon span::after {
  top: 5px;
}

.editor-shell__asset-toggle em {
  display: none;
}

.editor-shell__asset-panel {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}

.editor-shell__center {
  grid-column: 3;
  grid-row: 2;
  min-width: 0;
  min-height: 0;
  display: grid;
  overflow: hidden;
  background: var(--surface-1);
}

.editor-shell__inspector {
  grid-column: 5;
  grid-row: 2;
  min-width: 0;
  min-height: 0;
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  border-left: 1px solid var(--outline);
  z-index: 5;
}

.editor-shell__inspector--collapsed {
  border-left-color: color-mix(in srgb, var(--outline) 48%, transparent);
}

.editor-shell__inspector-toggle {
  position: absolute;
  top: 8px;
  right: 8px;
  z-index: 2;
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  gap: 0;
  padding: 0;
  border: 0;
  border-radius: 6px;
  color: var(--on-surface-muted);
  background: transparent;
  cursor: pointer;
  transition: color 180ms ease, background 180ms ease;
}

.editor-shell__inspector-toggle:hover {
  color: var(--on-surface);
  background: var(--surface-2);
}

.editor-shell__inspector-toggle:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--primary) 72%, transparent);
  outline-offset: 2px;
}

.editor-shell__inspector-toggle em {
  display: none;
}

.editor-shell__inspector-panel {
  position: relative;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}

.editor-shell__inspector:not(.editor-shell__inspector--collapsed) :deep(.editor-inspector__titlebar) {
  padding-right: 44px;
}

.editor-shell__inspector--collapsed .editor-shell__inspector-toggle {
  top: 6px;
  right: 4px;
}

.editor-shell__inspector--collapsed .editor-shell__inspector-panel {
  visibility: hidden;
  pointer-events: none;
}

.editor-shell__resizer {
  position: relative;
  grid-row: 2;
  width: 10px;
  cursor: col-resize;
  background: color-mix(in srgb, var(--surface-2) 86%, transparent);
  user-select: none;
  z-index: 6;
}

.editor-shell__resizer::before {
  content: '';
  position: absolute;
  inset: 0;
  background: color-mix(in srgb, var(--primary-soft) 28%, transparent);
  opacity: 0;
  transition: opacity 140ms ease;
}

.editor-shell__resizer--left {
  grid-column: 2;
  background: transparent;
  border-left: 1px solid transparent;
  border-right: 1px solid transparent;
}

.editor-shell__resizer--right {
  grid-column: 4;
  background: transparent;
  border-left: 1px solid transparent;
  border-right: 1px solid transparent;
}

.editor-shell__resizer--right span {
  width: 2px;
  height: 38px;
  opacity: 0;
  background: color-mix(in srgb, var(--on-surface-muted) 14%, transparent);
}

.editor-shell__resizer--right:hover {
  background: color-mix(in srgb, var(--surface-2) 42%, transparent);
  border-left-color: color-mix(in srgb, var(--outline) 40%, transparent);
  border-right-color: color-mix(in srgb, var(--outline) 40%, transparent);
}

.editor-shell__resizer--right:hover span,
.editor-shell--resizing .editor-shell__resizer--right span {
  opacity: 1;
  height: 56px;
  background: color-mix(in srgb, var(--primary) 52%, var(--on-surface-muted));
}

.editor-shell__resizer--left span {
  width: 2px;
  height: 38px;
  opacity: 0;
  background: color-mix(in srgb, var(--on-surface-muted) 14%, transparent);
}

.editor-shell__resizer--left:hover {
  background: color-mix(in srgb, var(--surface-2) 42%, transparent);
  border-left-color: color-mix(in srgb, var(--outline) 40%, transparent);
  border-right-color: color-mix(in srgb, var(--outline) 40%, transparent);
}

.editor-shell__resizer--left:hover span,
.editor-shell--resizing .editor-shell__resizer--left span {
  opacity: 1;
  height: 56px;
  background: color-mix(in srgb, var(--primary) 52%, var(--on-surface-muted));
}

.editor-shell__resizer--hidden {
  pointer-events: none;
}

.editor-shell__resizer--hidden span,
.editor-shell__resizer--hidden::before {
  opacity: 0;
}

.editor-shell__resizer span {
  position: absolute;
  top: 50%;
  left: 50%;
  width: 3px;
  height: 48px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--on-surface-muted) 32%, transparent);
  transform: translate(-50%, -50%);
  transition: background 140ms ease, height 140ms ease;
}

.editor-shell__resizer:hover::before,
.editor-shell--resizing .editor-shell__resizer::before {
  opacity: 1;
}

.editor-shell__resizer:hover span,
.editor-shell--resizing .editor-shell__resizer span {
  height: 72px;
  background: color-mix(in srgb, var(--primary) 66%, var(--on-surface-muted));
}

.editor-state {
  grid-column: 1 / -1;
  grid-row: 2;
  display: grid;
  place-items: center;
  color: var(--on-surface-muted);
}

.editor-empty-state {
  grid-column: 1 / -1;
  grid-row: 2;
  height: 100%;
  display: grid;
  place-items: center;
  align-content: center;
  gap: 12px;
  padding: 24px;
  color: var(--on-surface-muted);
  text-align: center;
}

.editor-empty-state__icon {
  width: 62px;
  height: 62px;
  display: grid;
  place-items: center;
  border-radius: 18px;
  font-size: 28px;
  color: var(--primary-strong);
  background: var(--primary-soft);
}

.editor-empty-state strong {
  color: var(--on-surface);
  font-size: 18px;
}

.editor-empty-state p {
  margin: 0;
  max-width: 460px;
  font-size: 13px;
  line-height: 1.7;
}

.editor-empty-state__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: center;
}

.editor-shell--resizing {
  cursor: col-resize;
  /* Disable the column animation while dragging so the divider stays flush with
     the cursor instead of lerping and momentarily overlapping the mixer. */
  transition: none;
}

.editor-shell--playback-following .editor-shell__center {
  overflow: hidden;
}

.editor-drag-ghost {
  position: fixed;
  z-index: 9999;
  pointer-events: none;
  max-width: 260px;
  padding: 8px 12px;
  border: 1px solid color-mix(in srgb, var(--primary) 28%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, var(--surface-2) 96%, transparent);
  color: var(--on-surface);
  font-size: 12px;
  line-height: 1.2;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

@media (max-width: 920px) {
  .editor-shell__assets {
    grid-template-columns: var(--asset-rail-width);
  }

  .editor-shell__asset-panel {
    display: none;
  }
}
</style>
