<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { InputInst, InputNumberInst } from 'naive-ui'
import { useI18n } from 'vue-i18n'
import { useNumberInputAccessibility } from '@/composables/useNumberInputAccessibility'
import { PencilOutline } from '@vicons/ionicons5'
import { useEditorPlaybackStore } from '@/stores/editorPlayback'
import type { EditorClip, EditorMasterLimiter as MasterLimiterSettings, EditorSession, EditorSource } from '@/types/editor'
import { formatTime } from '@/utils/editorTime'
import { clipTrimBounds } from '@/utils/editorClips'
import EditorMasterLimiter from '@/components/editor/EditorMasterLimiter.vue'
import EditorParameterSlider from '@/components/editor/EditorParameterSlider.vue'
import EditorPluginPanel from '@/components/editor/EditorPluginPanel.vue'

const props = defineProps<{
  session: EditorSession
  selectedTrackId: string | null
  selectedClipId: string | null
  selectedSource: EditorSource | null
  duration: number
  lastExportPath: string | null
  compact?: boolean
}>()

const emit = defineEmits<{
  selectTrack: [trackId: string | null]
  togglePlayback: []
  setMasterVolume: [value: number]
  setMasterPan: [value: number]
  setMasterLimiter: [patch: Partial<MasterLimiterSettings>]
  beginMasterLimiter: []
  commitMasterLimiter: []
  beginMasterVolume: []
  commitMasterVolume: []
  beginMasterPan: []
  commitMasterPan: []
  renameTrack: [trackId: string, name: string, commit?: boolean]
  toggleTrackFlag: [trackId: string, flag: 'muted' | 'solo']
  setTrackVolume: [trackId: string, value: number]
  setTrackPan: [trackId: string, value: number]
  beginTrackVolume: []
  commitTrackVolume: []
  beginTrackPan: []
  commitTrackPan: []
  setClipFades: [payload: {
    trackId: string
    clipId: string
    patch: { fadeIn?: number; fadeOut?: number }
  }]
  setClipTiming: [payload: {
    trackId: string
    clipId: string
    patch: Partial<Pick<EditorClip, 'start' | 'offset' | 'duration'>>
  }]
  moveClip: [payload: { trackId: string; clipId: string; start: number }]
  beginClipTiming: []
  commitClipTiming: []
  openLocation: []
  relinkSource: []
}>()

const { t } = useI18n()
const vNumberA11y = useNumberInputAccessibility()
const playback = useEditorPlaybackStore()
const CLIP_START_STEP = 0.01

const selectedTrack = computed(() => props.session.tracks.find((track) => track.id === props.selectedTrackId) || null)
const channelOptions = computed(() => [
  { label: t('editor.plugins.masterOutput'), value: 'master' },
  ...props.session.tracks.map(track => ({ label: track.name, value: `track:${track.id}` })),
])
const channelValue = computed(() => selectedTrack.value ? `track:${selectedTrack.value.id}` : 'master')
const channelVolume = computed(() => selectedTrack.value ? selectedTrack.value.volume : props.session.masterVolume)
const channelPan = computed(() => selectedTrack.value ? selectedTrack.value.pan : props.session.masterPan)
const channelVolumeLabel = computed(() => t(selectedTrack.value ? 'editor.trackVolume' : 'editor.masterVolume'))
const channelLevels = computed<[number, number]>(() => {
  const levels = selectedTrack.value ? playback.trackLevels[selectedTrack.value.id] : playback.masterLevel
  return [normalizeLevel(levels?.[0]), normalizeLevel(levels?.[1])]
})
const channelPeak = computed(() => Math.max(...channelLevels.value))
const selectedClip = computed(() => (
  selectedTrack.value?.clips?.find((clip) => clip.id === props.selectedClipId) || null
))
const clipStartInput = ref<InputNumberInst | null>(null)
let cancellingClipStart = false
const renameDraft = ref('')
const editingTrackId = ref<string | null>(null)
const renameInput = ref<InputInst | null>(null)
const nameButton = ref<HTMLButtonElement | null>(null)

function selectChannel(value: string) {
  if (value === 'master') { emit('selectTrack', null); return }
  const track = props.session.tracks.find(track => `track:${track.id}` === value)
  if (track) emit('selectTrack', track.id)
}
function updateChannelVolume(value: number) {
  if (selectedTrack.value) emit('setTrackVolume', selectedTrack.value.id, value)
  else emit('setMasterVolume', value)
}
function updateChannelPan(value: number) {
  if (selectedTrack.value) emit('setTrackPan', selectedTrack.value.id, value)
  else emit('setMasterPan', value)
}
function beginChannelVolume() {
  if (selectedTrack.value) emit('beginTrackVolume')
  else emit('beginMasterVolume')
}
function commitChannelVolume() {
  if (selectedTrack.value) emit('commitTrackVolume')
  else emit('commitMasterVolume')
}
function beginChannelPan() {
  if (selectedTrack.value) emit('beginTrackPan')
  else emit('beginMasterPan')
}
function commitChannelPan() {
  if (selectedTrack.value) emit('commitTrackPan')
  else emit('commitMasterPan')
}
function toggleTrackFlag(flag: 'muted' | 'solo') {
  if (selectedTrack.value) emit('toggleTrackFlag', selectedTrack.value.id, flag)
}
function normalizeLevel(value: number | undefined) {
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0
}
function levelDb(value: number) {
  return value > 0 ? 20 * Math.log10(value) : -Infinity
}
const meterTicks = [
  { db: 0, position: 1 },
  { db: -3, position: 0.88 },
  { db: -6, position: 0.76 },
  { db: -9, position: 0.64 },
  { db: -12, position: 0.52 },
  { db: -15, position: 0.4 },
  { db: -24, position: 0.24 },
  { db: -30, position: 0.12 },
  { db: -60, position: 0 },
]
function meterValue(value: number) { return Math.max(-60, levelDb(value)) }
function meterDbScale(db: number) {
  if (db >= 0) return 1
  for (let index = 1; index < meterTicks.length; index++) {
    const lower = meterTicks[index]
    const upper = meterTicks[index - 1]
    if (db >= lower.db) return lower.position + (db - lower.db) / (upper.db - lower.db) * (upper.position - lower.position)
  }
  return 0
}
function meterScale(value: number) { return meterDbScale(meterValue(value)) }
function formatLevel(value: number) { return value > 0 ? levelDb(value).toFixed(1) : '−∞' }

watch(
  selectedTrack,
  (track) => {
    editingTrackId.value = null
    renameDraft.value = track?.name || ''
  },
  { immediate: true },
)

async function beginRename() {
  const track = selectedTrack.value
  if (!track) return
  renameDraft.value = track.name
  editingTrackId.value = track.id
  await nextTick()
  if (editingTrackId.value !== track.id) return
  renameInput.value?.focus()
  renameInput.value?.select()
}

function commitRename() {
  const track = props.session.tracks.find(track => track.id === editingTrackId.value)
  const name = renameDraft.value.trim()
  editingTrackId.value = null
  if (track && name && name !== track.name) emit('renameTrack', track.id, name, true)
}

function onRenameKeydown(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229) return
  if (event.key !== 'Enter' && event.key !== 'Escape') return
  event.preventDefault()
  if (event.key === 'Enter') commitRename()
  else editingTrackId.value = null
  void nextTick(() => nameButton.value?.focus({ preventScroll: true }))
}

function shortPath(path: string) {
  if (!path) return ''
  if (path.length <= 74) return path
  return `${path.slice(0, 28)}…${path.slice(-38)}`
}

function numberOrZero(value: number | null) {
  return Number(value || 0)
}

function formatTrackVolume(value: number) {
  const volume = Math.max(0, Number(value || 0))
  const percent = `${Math.round(volume * 100)}%`
  if (volume <= 0.0001) return `${percent} · -∞ dB`
  const db = 20 * Math.log10(volume)
  const normalized = `${db > 0 ? '+' : ''}${db.toFixed(1)} dB`
  return `${percent} · ${normalized}`
}

function formatVolumeDb(value: number) {
  const volume = Math.max(0, Number(value || 0))
  if (volume <= 0.0001) return '−∞ dB'
  const db = 20 * Math.log10(volume)
  return `${db > 0.05 ? '+' : ''}${db.toFixed(1)} dB`
}

function formatPanShort(value: number) {
  const pan = Number(value || 0)
  if (Math.abs(pan) < 0.025) return 'C'
  return `${pan < 0 ? 'L' : 'R'}${Math.round(Math.abs(pan) * 100)}`
}

function formatTrackPan(value: number) {
  const pan = Number(value || 0)
  if (Math.abs(pan) < 0.025) return t('editor.panCenter')
  const amount = Math.round(Math.abs(pan) * 100)
  return pan < 0 ? `${t('editor.panLeft')} ${amount}` : `${t('editor.panRight')} ${amount}`
}

const sourceMeta = computed(() => [
  props.selectedSource ? formatTime(props.selectedSource.duration || 0) : '',
  props.selectedSource ? `${props.selectedSource.channels || 0}ch` : '',
  props.selectedSource ? `${props.selectedSource.sampleRate || 0}` : '',
].filter(Boolean).join(' · '))

const fadeMax = computed(() => selectedClip.value?.duration || props.selectedSource?.duration || 0)
const usesTrackFadeLabel = computed(() => (selectedTrack.value?.clips?.length || 0) === 1)
const fadeInValue = computed(() => selectedClip.value?.fadeIn || 0)
const fadeOutValue = computed(() => selectedClip.value?.fadeOut || 0)
const clipOffsetMax = computed(() => Math.max(0, Number(props.selectedSource?.duration || 0) - Number(selectedClip.value?.duration || 0)))
const clipDurationMin = computed(() => Math.min(0.01, selectedClip.value?.duration ?? 0.01))
const clipDurationMax = computed(() => {
  const clip = selectedClip.value
  if (!clip) return clipDurationMin.value
  const { end } = clipTrimBounds(clip, selectedTrack.value?.clips || [])
  const sourceDuration = props.selectedSource?.duration || 0
  const availableDuration = sourceDuration > 0 ? sourceDuration - clip.offset : Infinity
  const remaining = Math.min(availableDuration, end - clip.start)
  return Number.isFinite(remaining) ? Math.max(clipDurationMin.value, remaining) : undefined
})

function updateClipTiming(patch: Partial<Pick<EditorClip, 'start' | 'offset' | 'duration'>>) {
  if (!selectedTrack.value || !selectedClip.value) return
  emit('setClipTiming', {
    trackId: selectedTrack.value.id,
    clipId: selectedClip.value.id,
    patch,
  })
}

function updateClipDuration(value: number | null) {
  const duration = Math.max(clipDurationMin.value, Math.min(clipDurationMax.value ?? Infinity, numberOrZero(value)))
  updateClipTiming({ duration })
}

function moveClip(start: number | null) {
  if (cancellingClipStart || !selectedTrack.value || !selectedClip.value || start === null) return
  emit('moveClip', { trackId: selectedTrack.value.id, clipId: selectedClip.value.id, start })
}

function discardClipStartInput() {
  cancellingClipStart = true
  try {
    clipStartInput.value?.blur()
  } finally {
    cancellingClipStart = false
  }
}

function handleClipStartKeydown(event: KeyboardEvent) {
  if (event.isComposing) return
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    discardClipStartInput()
    return
  }
  if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return
  const clip = selectedClip.value
  if (!clip || clip.locked) return
  event.preventDefault()
  event.stopPropagation()
  const draft = event.target instanceof HTMLInputElement ? event.target.value.trim() : ''
  const base = draft ? Number(draft) : clip.start
  if (!Number.isFinite(base)) return
  const step = event.key === 'ArrowUp' ? CLIP_START_STEP : -CLIP_START_STEP
  const start = Math.max(0, Number((base + step).toPrecision(15)))
  if (start === clip.start && base === clip.start) return

  const input = clipStartInput.value
  const unchanged = start === clip.start
  moveClip(start)
  void nextTick(() => {
    if (clipStartInput.value !== input || selectedClip.value?.id !== clip.id) return
    if (unchanged || selectedClip.value.start !== start) {
      discardClipStartInput()
      input?.focus()
    }
  })
}

function updateFades(patch: { fadeIn?: number; fadeOut?: number }) {
  if (!selectedTrack.value || !selectedClip.value) return
  emit('setClipFades', {
    trackId: selectedTrack.value.id,
    clipId: selectedClip.value.id,
    patch,
  })
}

</script>

<template>
  <aside class="editor-inspector" :class="{ 'editor-inspector--compact': compact }">
    <div class="editor-inspector__titlebar">
      <div class="editor-inspector__eyebrow" :title="selectedTrack ? t('editor.trackParams') : t('editor.plugins.masterOutput')">{{ t('editor.channelInspector') }}</div>
      <div class="editor-inspector__channel-select">
        <n-input v-if="selectedTrack && editingTrackId === selectedTrack.id" ref="renameInput" v-model:value="renameDraft" class="editor-inspector__name-input" size="small" :input-props="{ 'aria-label': t('editor.inspectorFieldName') }" @blur="commitRename()" @keydown.stop="onRenameKeydown" />
        <AppSelect v-else class="editor-inspector__channel-picker" size="small" :value="channelValue" :options="channelOptions" :aria-label="t('editor.selectChannel')" @update:value="selectChannel" />
        <button v-if="selectedTrack && editingTrackId !== selectedTrack.id" ref="nameButton" type="button" class="editor-inspector__name" :title="t('editor.renameTrack', { name: selectedTrack.name })" :aria-label="t('editor.renameTrack', { name: selectedTrack.name })" @click="beginRename" @keydown.stop><n-icon :component="PencilOutline" :size="14" /></button>
        <n-button class="editor-inspector__master-button" size="tiny" secondary :type="selectedTrack ? 'default' : 'primary'" :disabled="!selectedTrack" @keydown.space.stop @click="selectChannel('master')">{{ t('editor.plugins.masterOutput') }}</n-button>
      </div>
      <span class="editor-inspector__channel-context" :title="selectedTrack ? sourceMeta : session.name">{{ selectedTrack ? (sourceMeta || t('editor.laneNoAudio')) : session.name }}</span>
    </div>

    <div class="editor-inspector__scroll">
      <section class="inspector-strip" :aria-label="selectedTrack ? selectedTrack.name : t('editor.plugins.masterOutput')">
        <div class="inspector-strip__controls">
          <div class="inspector-strip__pan" :title="`${t('editor.balance')} · ${formatTrackPan(channelPan)}`">
            <div class="inspector-strip__caption"><span>{{ t('editor.balance') }}</span><strong>{{ formatPanShort(channelPan) }}</strong></div>
            <EditorParameterSlider
              :key="channelValue"
              :value="channelPan" :value-text="formatTrackPan(channelPan)"
              :reset-value="0"
              :label="t('editor.balance')"
              :reset-label="t('editor.sliderDefaultCenter')"
              :min="-1"
              :max="1"
              :step="0.01"
              :tooltip="false"
              @update:value="updateChannelPan"
              @dragstart="beginChannelPan"
              @dragend="commitChannelPan"
            />
          </div>

          <div v-if="selectedTrack" class="inspector-strip__flags">
            <button type="button" class="inspector-strip__flag inspector-strip__flag--mute" :class="{ 'inspector-strip__flag--active': selectedTrack.muted }" :aria-pressed="selectedTrack.muted" :title="t(selectedTrack.muted ? 'editor.unmuteTrack' : 'editor.muteTrack')" :aria-label="t(selectedTrack.muted ? 'editor.unmuteTrack' : 'editor.muteTrack')" @keydown.space.stop @click="toggleTrackFlag('muted')">M</button>
            <button type="button" class="inspector-strip__flag inspector-strip__flag--solo" :class="{ 'inspector-strip__flag--active': selectedTrack.solo }" :aria-pressed="selectedTrack.solo" :title="t(selectedTrack.solo ? 'editor.unsoloTrack' : 'editor.soloTrack')" :aria-label="t(selectedTrack.solo ? 'editor.unsoloTrack' : 'editor.soloTrack')" @keydown.space.stop @click="toggleTrackFlag('solo')">S</button>
          </div>

          <div class="inspector-strip__fader-area" :title="t('editor.channelLevelStereo', { left: formatLevel(channelLevels[0]), right: formatLevel(channelLevels[1]) })">
            <div class="inspector-strip__fader">
            <EditorParameterSlider
              :key="channelValue"
              vertical
              :value="channelVolume" :value-text="Math.round(channelVolume * 100) + '%'"
              :reset-value="1"
              :label="channelVolumeLabel"
              :reset-label="t('editor.sliderDefaultVolume')"
              :min="0"
              :max="2"
              :step="0.01"
              :tooltip="false"
              @update:value="updateChannelVolume"
              @dragstart="beginChannelVolume"
              @dragend="commitChannelVolume"
            />
            </div>
            <div class="inspector-strip__meter">
              <div v-for="(value, index) in channelLevels" :key="index" class="inspector-strip__meter-bar" role="meter" :aria-label="`${t('editor.channelLevel')} · ${t(index === 0 ? 'editor.panLeft' : 'editor.panRight')}`" :aria-valuemin="-60" :aria-valuemax="0" :aria-valuenow="meterValue(value)" :aria-valuetext="`${formatLevel(value)} dBFS`">
                <span :class="{ 'inspector-strip__meter-fill--full': value >= 1 }" :style="{ clipPath: `inset(${(1 - meterScale(value)) * 100}% 0 0)` }" />
              </div>
              <div class="inspector-strip__meter-scale" aria-hidden="true">
                <span v-for="tick in meterTicks" :key="tick.db" :class="{ 'inspector-strip__meter-tick--major': tick.db >= -12 }" :style="{ top: `${(1 - tick.position) * 100}%` }">{{ tick.db === 0 ? '0' : `−${Math.abs(tick.db)}` }}</span>
              </div>
            </div>
          </div>

          <div class="inspector-strip__readout" :title="`${channelVolumeLabel} · ${formatTrackVolume(channelVolume)}`">
            <strong>{{ formatVolumeDb(channelVolume) }}</strong>
            <small>{{ formatLevel(channelPeak) }} dBFS</small>
          </div>
        </div>

        <div class="inspector-strip__inserts">
          <EditorPluginPanel :session="session" :selected-track-id="selectedTrackId" @select-target="(id) => emit('selectTrack', id)" @toggle-playback="emit('togglePlayback')" />
        </div>
      </section>

      <section v-if="selectedTrack && selectedClip" class="inspector-group inspector-group--clip">
        <div class="inspector-group__header">
          <strong>{{ t('editor.inspectorSectionClip') }}</strong>
          <span>{{ t('editor.inspectorClipTiming') }}</span>
        </div>
        <div class="inspector-group__body inspector-group__body--dense">
          <div class="dual-fields">
            <label class="panel-field panel-field--compact">
              <span class="panel-field__label">{{ t('editor.clipStart') }}</span>
              <n-input-number v-number-a11y="t('editor.clipStart') + ' (s)'"
                ref="clipStartInput"
                :value="selectedClip.start"
                :disabled="selectedClip.locked"
                :min="0"
                :step="CLIP_START_STEP"
                :update-value-on-input="false"
                size="small"
                @keydown.capture="handleClipStartKeydown"
                @update:value="moveClip"
              />
            </label>
            <label class="panel-field panel-field--compact">
              <span class="panel-field__label">{{ t('editor.clipDuration') }}</span>
              <n-input-number v-number-a11y="t('editor.clipDuration') + ' (s)'"
                :value="selectedClip.duration"
                :disabled="selectedClip.locked"
                :min="clipDurationMin"
                :max="clipDurationMax"
                :step="0.01"
                size="small"
                @focus="emit('beginClipTiming')"
                @blur="emit('commitClipTiming')"
                @update:value="updateClipDuration"
              />
            </label>
          </div>
          <label class="panel-field panel-field--compact">
            <span class="panel-field__label">{{ t('editor.clipSourceOffset') }}</span>
            <n-input-number v-number-a11y="t('editor.clipSourceOffset') + ' (s)'"
              :value="selectedClip.offset"
              :disabled="selectedClip.locked"
              :min="0"
              :max="clipOffsetMax"
              :step="0.01"
              size="small"
              @focus="emit('beginClipTiming')"
              @blur="emit('commitClipTiming')"
              @update:value="(value: number | null) => updateClipTiming({ offset: numberOrZero(value) })"
            />
          </label>
        </div>
      </section>

      <details v-if="selectedTrack && selectedClip" class="inspector-group inspector-group--secondary">
        <summary class="inspector-group__header">
          <strong>{{ t('editor.inspectorSectionAdvanced') }}</strong>
          <span>{{ t(usesTrackFadeLabel ? 'editor.inspectorTrackTuning' : 'editor.inspectorClipTuning') }}</span>
        </summary>

        <div class="inspector-group__body inspector-group__body--dense">
          <div class="dual-fields">
            <label class="panel-field panel-field--compact">
              <span class="panel-field__label">{{ t('editor.fieldFadeIn') }}</span>
              <n-input-number v-number-a11y="t('editor.fieldFadeIn') + ' (s)'"
                :value="fadeInValue"
                :disabled="selectedClip.locked"
                :min="0"
                :max="fadeMax"
                :step="0.1"
                size="small"
                @update:value="(value: number | null) => updateFades({ fadeIn: numberOrZero(value) })"
              />
            </label>
            <label class="panel-field panel-field--compact">
              <span class="panel-field__label">{{ t('editor.fieldFadeOut') }}</span>
              <n-input-number v-number-a11y="t('editor.fieldFadeOut') + ' (s)'"
                :value="fadeOutValue"
                :disabled="selectedClip.locked"
                :min="0"
                :max="fadeMax"
                :step="0.1"
                size="small"
                @update:value="(value: number | null) => updateFades({ fadeOut: numberOrZero(value) })"
              />
            </label>
          </div>
        </div>
      </details>

      <div v-if="selectedTrack && selectedSource?.missing" class="source-missing-card">
        <strong>{{ t('editor.assetMissing') }}</strong>
        <span>{{ t('editor.assetMissingHint') }}</span>
        <n-button size="small" type="warning" ghost @click="emit('relinkSource')">
          {{ t('editor.assetRelink') }}
        </n-button>
      </div>

      <details v-if="selectedTrack" class="inspector-group inspector-group--source" :open="Boolean(selectedSource?.missing)">
        <summary class="inspector-group__header">
          <strong>{{ t('editor.inspectorSectionSource') }}</strong>
          <span>{{ t('editor.inspectorSourceContext') }}</span>
        </summary>

        <div class="inspector-group__body inspector-group__body--dense">
          <label class="panel-field panel-field--compact">
            <span class="panel-field__label">{{ t('editor.trackSourcePath') }}</span>
            <n-input :value="selectedSource?.path || '-'" size="small" readonly />
          </label>

          <dl class="stats-grid" :class="{ 'stats-grid--compact': compact }">
            <div class="meta-cell"><dt>{{ t('editor.trackSourceDuration') }}</dt><dd>{{ formatTime(selectedSource?.duration || 0) }}</dd></div>
            <div class="meta-cell"><dt>{{ t('editor.trackSourceChannels') }}</dt><dd>{{ selectedSource?.channels || 0 }}</dd></div>
            <div class="meta-cell"><dt>{{ t('editor.trackSourceSampleRate') }}</dt><dd>{{ selectedSource?.sampleRate || 0 }}</dd></div>
          </dl>
        </div>
      </details>

      <section v-else class="inspector-group inspector-group--project">
        <div class="inspector-group__header">
          <strong>{{ t('editor.projectInfo') }}</strong>
        </div>

        <div class="inspector-group__body">
          <dl class="stats-grid" :class="{ 'stats-grid--compact': compact }">
            <div class="meta-cell"><dt>{{ t('editor.tracks') }}</dt><dd>{{ session.tracks.length }}</dd></div>
            <div class="meta-cell"><dt>{{ t('editor.assets') }}</dt><dd>{{ session.sources.length }}</dd></div>
            <div class="meta-cell"><dt>{{ t('editor.totalDuration') }}</dt><dd>{{ formatTime(duration) }}</dd></div>
          </dl>

          <div v-if="lastExportPath" class="last-export">
            <span class="panel-field__label">{{ t('editor.inspectorSectionExport') }}</span>
            <strong>{{ t('editor.lastExport') }}</strong>
            <span>{{ shortPath(lastExportPath) }}</span>
            <n-button size="small" secondary @click="emit('openLocation')">{{ t('editor.openLocation') }}</n-button>
          </div>
        </div>
      </section>
      <EditorMasterLimiter
        v-if="!selectedTrack"
        :value="session.masterLimiter"
        @update="emit('setMasterLimiter', $event)"
        @begin="emit('beginMasterLimiter')"
        @commit="emit('commitMasterLimiter')"
      />
    </div>
  </aside>
</template>

<style scoped>
.editor-inspector {
  container: channel-inspector / inline-size;
  min-height: 0;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  gap: 0;
  padding: 0;
  border-left: 0;
  background: color-mix(in srgb, var(--surface-2) 38%, var(--surface-1));
}

.editor-inspector__titlebar {
  display: grid;
  gap: 3px;
  padding: 9px 12px 8px;
  border-bottom: 1px solid color-mix(in srgb, var(--outline) 76%, transparent);
  background: var(--surface-1);
}

.editor-inspector__eyebrow {
  color: var(--on-surface-muted);
  font-size: 9px;
  line-height: 1;
  letter-spacing: 0.08em;
}

.editor-inspector__channel-select { display: flex; align-items: center; gap: 4px; min-width: 0; }
.editor-inspector__channel-picker, .editor-inspector__name-input { min-width: 0; flex: 1; }
.editor-inspector__master-button { flex-shrink: 0; }
.editor-inspector__channel-context {
  color: var(--on-surface-muted);
  font-size: 10px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.editor-inspector__name {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 22px;
  height: 26px;
  padding: 0;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.editor-inspector__name:hover { background: var(--primary-softer); }
.editor-inspector__name:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.editor-inspector__name-input :deep(input) { font-size: 14px; font-weight: 600; }

.editor-inspector__scroll {
  min-height: 0;
  overflow: auto;
  display: grid;
  align-content: start;
  gap: 7px;
  padding: 7px 12px 9px;
}

.editor-inspector--compact .editor-inspector__scroll {
  padding-inline: 8px;
}

.inspector-group {
  display: grid;
  gap: 8px;
  padding: 9px 10px 10px;
  border: 1px solid color-mix(in srgb, var(--outline) 70%, transparent);
  border-radius: 8px;
  background: var(--surface-1);
  box-shadow: none;
  transition: border-color 160ms ease, background 160ms ease;
}

.inspector-group--primary {
  border-color: color-mix(in srgb, var(--outline) 78%, transparent);
}

/* Channel strip: fader column on the left, insert slots on the right. */
.inspector-strip {
  display: grid;
  grid-template-columns: 74px minmax(0, 1fr);
  height: clamp(216px, 36dvh, 380px);
  min-height: 0;
  border: 1px solid color-mix(in srgb, var(--outline) 78%, transparent);
  border-radius: 8px;
  background: var(--surface-1);
  overflow: hidden;
}
.editor-inspector--compact .inspector-strip { grid-template-columns: 66px minmax(0, 1fr); }
.inspector-strip__controls {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
  min-height: 0;
  padding: 8px 7px;
  border-right: 1px solid color-mix(in srgb, var(--outline) 70%, transparent);
  background: color-mix(in srgb, var(--surface-2) 40%, var(--surface-1));
}
.inspector-strip__caption { display: flex; justify-content: space-between; align-items: baseline; gap: 2px; font-size: 9px; color: var(--on-surface-muted); }
.inspector-strip__caption > span { min-width: 0; overflow-wrap: anywhere; }
.inspector-strip__caption > strong { color: var(--on-surface); font-size: 10px; font-weight: 500; font-variant-numeric: tabular-nums; }
.inspector-strip__pan :deep(.editor-parameter-slider) { gap: 0; }
.inspector-strip__pan :deep(.editor-parameter-slider__reset) { display: none; }
.inspector-strip__flags { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; }
.inspector-strip__flag {
  height: 20px;
  padding: 0;
  border: 1px solid var(--outline);
  border-radius: 4px;
  background: var(--surface-1);
  color: var(--on-surface-muted);
  font: inherit;
  font-size: 10px;
  font-weight: 600;
  cursor: pointer;
}
.inspector-strip__flag:hover { color: var(--on-surface); border-color: var(--outline-strong); }
.inspector-strip__flag:focus-visible { outline: 2px solid var(--primary); outline-offset: 1px; }
.inspector-strip__flag--mute.inspector-strip__flag--active { border-color: transparent; background: var(--warning); color: #1b1b1b; }
.inspector-strip__flag--solo.inspector-strip__flag--active { border-color: transparent; background: var(--primary); color: #fff; }
.inspector-strip__fader-area { position: relative; display: flex; justify-content: center; gap: 5px; flex: 1; min-height: 150px; padding-bottom: 23px; }
.inspector-strip__fader { display: flex; flex-direction: column; }
.inspector-strip__fader :deep(.editor-parameter-slider) { flex-direction: column; flex: 1; min-height: 0; gap: 0; }
.inspector-strip__fader :deep(.editor-parameter-slider__control) { display: flex; justify-content: center; flex: 1; min-height: 0; }
.inspector-strip__fader :deep(.n-slider) { height: 100%; }
.inspector-strip__fader :deep(.editor-parameter-slider__reset) { position: absolute; bottom: 0; left: 50%; transform: translateX(-50%); min-height: 18px; max-width: 100%; padding: 0 3px; font-size: 9px; white-space: normal; }
.inspector-strip__meter { display: grid; grid-template-columns: 5px 5px 16px; gap: 2px; }
.inspector-strip__meter-bar { position: relative; overflow: hidden; background: var(--surface-2); border: 1px solid var(--outline); border-radius: 2px; }
.inspector-strip__meter-bar > span { position: absolute; inset: 0; background: linear-gradient(180deg, rgba(255, 106, 106, 0.95), rgba(255, 170, 102, 0.92) 24%, rgba(92, 214, 132, 0.9) 60%); }
.inspector-strip__meter-bar > .inspector-strip__meter-fill--full { background: var(--error, var(--warning)); }
.inspector-strip__meter-scale { position: relative; color: var(--on-surface-muted); font-size: 8px; line-height: 1; font-variant-numeric: tabular-nums; }
.inspector-strip__meter-scale > span { position: absolute; left: 0; white-space: nowrap; transform: translateY(-50%); }
.inspector-strip__meter-scale > span::before { content: ''; position: absolute; top: 50%; left: -3px; width: 2px; height: 1px; background: var(--outline-strong); }
.inspector-strip__meter-scale > .inspector-strip__meter-tick--major { color: var(--on-surface); font-size: 9px; font-weight: 600; }
.inspector-strip__readout { display: grid; justify-items: center; gap: 1px; padding: 4px 0 1px; border-top: 1px solid color-mix(in srgb, var(--outline) 60%, transparent); font-variant-numeric: tabular-nums; white-space: nowrap; }
.inspector-strip__readout > strong { font-size: 11px; font-weight: 600; }
.inspector-strip__readout > small { color: var(--on-surface-muted); font-size: 8px; }
.inspector-strip__inserts { min-width: 0; min-height: 0; overflow: auto; }

.inspector-group--clip {
  background: color-mix(in srgb, var(--surface-2) 20%, var(--surface-1));
}

.inspector-group--secondary {
  background: color-mix(in srgb, var(--surface-2) 30%, var(--surface-1));
}

.inspector-group--source {
  background: color-mix(in srgb, var(--surface-2) 36%, var(--surface-1));
}

details.inspector-group {
  gap: 0;
}

details.inspector-group[open] {
  gap: 8px;
}

.inspector-group:first-child {
  border-top: 1px solid color-mix(in srgb, var(--outline) 42%, transparent);
}

.inspector-group__header strong {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.02em;
  color: color-mix(in srgb, var(--on-surface) 88%, var(--on-surface-muted));
}

.inspector-group__header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  min-width: 0;
  padding: 4px 6px 6px;
  margin: -4px -6px 1px;
  border-radius: 6px;
  border-bottom: 1px solid color-mix(in srgb, var(--outline) 40%, transparent);
  background: transparent;
}

summary.inspector-group__header {
  min-height: 16px;
  cursor: pointer;
  list-style: none;
  transition: background 160ms ease, color 160ms ease;
}

summary.inspector-group__header:hover {
  background: color-mix(in srgb, var(--surface-2) 56%, transparent);
}

summary.inspector-group__header:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--primary) 72%, transparent);
  outline-offset: 2px;
}

summary.inspector-group__header::-webkit-details-marker {
  display: none;
}

summary.inspector-group__header::after {
  content: '+';
  color: var(--on-surface-muted);
  font-size: 13px;
  line-height: 1;
}

details.inspector-group[open] > summary.inspector-group__header::after {
  content: '−';
}

.inspector-group__header span {
  color: var(--on-surface-muted);
  font-size: 9px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.inspector-group__body {
  display: grid;
  gap: 10px;
}

.inspector-group__body--dense {
  gap: 8px;
}

.panel-field {
  display: grid;
  gap: 4px;
}

.panel-field--compact {
  gap: 3px;
}

.panel-field__label {
  color: color-mix(in srgb, var(--on-surface) 68%, var(--on-surface-muted));
  font-size: 11px;
  letter-spacing: 0;
}


.panel-field__split {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.panel-field__split--compact strong {
  text-align: right;
}

.panel-field__split strong {
  font-size: 11px;
  line-height: 1.2;
  color: var(--on-surface);
}

.dual-fields {
  display: flex;
  gap: 8px;
}

.dual-fields--stacked {
  display: grid;
  grid-template-columns: 1fr;
}

.dual-fields > .panel-field {
  flex: 1;
}

.stats-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 4px;
  margin: 0;
}

.stats-grid--compact {
  grid-template-columns: 1fr;
}

.meta-cell {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  padding: 4px 0;
  border-bottom: 1px solid color-mix(in srgb, var(--outline) 24%, transparent);
  background: transparent;
}

.meta-cell dt {
  color: var(--on-surface-muted);
  font-size: 11px;
  line-height: 1.1;
}

.meta-cell dd {
  margin: 0;
  font-weight: 600;
  font-size: 12px;
  line-height: 1.15;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: right;
}

.meta-cell:last-child {
  border-bottom: 0;
}

.last-export {
  display: grid;
  gap: 5px;
  padding: 6px 0 0;
}

.last-export strong {
  font-size: 11px;
}

.last-export span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--on-surface-muted);
  font-size: 10px;
}

.source-missing-card {
  display: grid;
  gap: 5px;
  padding: 9px 10px;
  border-radius: 8px;
  border: 1px solid color-mix(in srgb, var(--warning) 26%, transparent);
  background: color-mix(in srgb, var(--warning) 8%, transparent);
}

.source-missing-card strong {
  font-size: 12px;
  color: color-mix(in srgb, var(--warning) 84%, var(--on-surface));
}

.source-missing-card span {
  font-size: 11px;
  color: var(--on-surface-muted);
  line-height: 1.5;
}

.editor-inspector :deep(.n-input),
.editor-inspector :deep(.n-input-number),
.editor-inspector :deep(.n-slider) {
  --n-color: color-mix(in srgb, var(--surface-1) 82%, var(--surface-2)) !important;
}

.editor-inspector :deep(.app-select) {
  --app-select-color: color-mix(in srgb, var(--surface-1) 82%, var(--surface-2));
}

.editor-inspector :deep(.n-input),
.editor-inspector :deep(.n-input-number .n-input) {
  --n-border: 1px solid color-mix(in srgb, var(--outline) 58%, transparent) !important;
  --n-border-hover: 1px solid color-mix(in srgb, var(--primary-border) 82%, var(--outline)) !important;
  --n-border-focus: 1px solid color-mix(in srgb, var(--primary) 56%, transparent) !important;
  --n-box-shadow-focus: 0 0 0 2px color-mix(in srgb, var(--primary-soft) 34%, transparent) !important;
}


.editor-inspector :deep(.n-slider-rail),
.editor-inspector :deep(.n-slider-rail__fill) {
  border-radius: 999px;
}

@container channel-inspector (max-width: 250px) {
  .editor-inspector__titlebar { padding-inline: 8px; }
  .editor-inspector__channel-select { flex-wrap: wrap; }
  .editor-inspector__channel-picker, .editor-inspector__name-input { flex-basis: calc(100% - 30px); }
  .editor-inspector__master-button { margin-left: auto; }
  .editor-inspector__scroll { padding-inline: 8px; }
  .inspector-strip { grid-template-columns: 66px minmax(0, 1fr); }
  .inspector-group { padding-inline: 8px; }
}
@container channel-inspector (min-width: 300px) {
  .inspector-strip { grid-template-columns: 84px minmax(0, 1fr); }
  .inspector-strip__controls { padding-inline: 8px; }
  .inspector-strip__caption { font-size: 10px; }
  .inspector-strip__readout > strong { font-size: 12px; }
}
@media (max-height: 760px) {
  .inspector-strip__controls { gap: 6px; padding-block: 6px; }
  .inspector-strip__fader-area { min-height: 104px; }
}
@media (max-height: 640px) {
  .editor-inspector__titlebar { gap: 2px; padding-block: 7px; }
  .editor-inspector__scroll { gap: 6px; padding-block: 6px; }
  .inspector-strip { height: clamp(204px, 35dvh, 224px); }
  .inspector-strip__controls { gap: 4px; }
  .inspector-strip__fader-area { min-height: 76px; }
  .inspector-strip__flag { height: 18px; }
  .inspector-strip__readout { padding-top: 2px; }
}
@media (max-height: 520px) {
  .inspector-strip { height: 200px; }
  .inspector-strip__fader-area { min-height: 68px; }
}
</style>
