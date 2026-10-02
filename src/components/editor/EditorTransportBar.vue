<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  AlertCircleOutline,
  ArrowRedoOutline,
  ArrowUndoOutline,
  ChevronDownOutline,
  DownloadOutline,
  Mic,
  PlaySkipBackOutline,
  StopOutline,
  RefreshOutline,
  RepeatOutline,
  SaveOutline,
  VolumeMediumOutline,
  VolumeMuteOutline,
} from '@vicons/ionicons5'
import { formatTime } from '@/utils/editorTime'
import type { TransportPendingAction, TransportVisualState } from '@/stores/editorPlayback'

const props = defineProps<{
  sessionName: string
  trackCount: number
  currentTime: number
  duration: number
  transportVisualState: TransportVisualState
  transportPendingAction: TransportPendingAction
  transportCanToggle: boolean
  loop: boolean
  masterVolume: number
  masterPan: number
  canUndo: boolean
  canRedo: boolean
  saving: boolean
  exporting: boolean
  disabled: boolean
  missingAssetCount?: number
  missingAssetPreview?: string[]
  relinkingMissingAssets?: boolean
  recordingState?: 'idle' | 'preparing' | 'recording' | 'stopping'
  recordingDevices?: Array<{ deviceId: string; label: string }>
  recordingDeviceId?: string
  recordingInputLevel?: number
  recordingElapsed?: number
  recordingError?: string | null
}>()

const emit = defineEmits<{
  reset: []
  toggleTransport: []
  stop: []
  'update:loop': [value: boolean]
  'update:masterVolume': [value: number]
  beginMasterVolume: []
  commitMasterVolume: []
  'update:masterPan': [value: number]
  beginMasterPan: []
  commitMasterPan: []
  undo: []
  redo: []
  save: []
  export: []
  relinkMissingAssets: []
  toggleRecording: []
  cancelRecording: []
  refreshRecordingDevices: []
  addRecordingTrack: []
  'update:recordingDeviceId': [value: string]
}>()

const { t } = useI18n()

const optimisticVisualState = ref<TransportVisualState | null>(null)
const renderedVisualState = computed<TransportVisualState>(() => optimisticVisualState.value || props.transportVisualState)
const showPauseButton = computed(() => renderedVisualState.value === 'pause')
const isStarting = computed(() => props.transportPendingAction === 'starting')
const isPausing = computed(() => props.transportPendingAction === 'pausing')
const transportLabel = computed(() => (showPauseButton.value ? t('common.pause') : t('common.resume')))
const transportPressed = ref(false)
const sessionMeta = computed(() => `${props.trackCount} ${t('editor.tracks')}`)
const timecode = computed(() => `${formatTime(props.currentTime)} / ${formatTime(props.duration)}`)
const volumeIcon = computed(() => props.masterVolume <= 0.01 ? VolumeMuteOutline : VolumeMediumOutline)
const masterVolumePercent = computed(() => `${Math.round(props.masterVolume * 100)}%`)
const missingPreviewText = computed(() => (props.missingAssetPreview || []).filter(Boolean).join(' · '))
const offlineDetailsOpen = ref(false)
const recordingPopoverOpen = ref(false)
const recordingState = computed(() => props.recordingState || 'idle')
const recordingActive = computed(() => recordingState.value === 'recording')
const recordingPending = computed(() => recordingState.value === 'preparing' || recordingState.value === 'stopping')
const recordingDeviceOptions = computed(() => [
  { label: t('editor.recordingDefaultDevice'), value: '' },
  ...(props.recordingDevices || []).map((device, index) => ({
    label: device.label || t('editor.recordingDeviceFallback', { index: index + 1 }),
    value: device.deviceId,
  })),
])

function formatTrackPan(value: number) {
  const pan = Number(value || 0)
  if (Math.abs(pan) < 0.025) return t('editor.panCenter')
  const amount = Math.round(Math.abs(pan) * 100)
  return pan < 0 ? `${t('editor.panLeft')} ${amount}` : `${t('editor.panRight')} ${amount}`
}

watch(() => props.transportVisualState, (value) => {
  if (optimisticVisualState.value === value) {
    optimisticVisualState.value = null
    return
  }
  if (props.transportPendingAction === null) {
    optimisticVisualState.value = null
  }
})

function handleTransportPointerDown(event: PointerEvent) {
  if (props.disabled || !props.transportCanToggle || event.button !== 0) return
  transportPressed.value = true
}

function clearTransportPressed() {
  transportPressed.value = false
}
</script>

<template>
  <div class="editor-transport-wrap">
    <header class="editor-transport">
      <div class="editor-transport__brand">
        <strong>{{ sessionName }}</strong>
        <span class="editor-transport__brand-meta">{{ sessionMeta }}</span>
      </div>

      <div class="editor-transport__center">
        <div class="transport-history">
          <button
            class="transport-chip"
            type="button"
            :title="t('common.undo')"
            :aria-label="t('common.undo')"
            :disabled="disabled || recordingActive || recordingPending || !canUndo"
            @click="emit('undo')"
          >
            <span class="sr-only">{{ t('common.undo') }}</span>
            <n-icon :component="ArrowUndoOutline" />
          </button>
          <button
            class="transport-chip"
            type="button"
            :title="t('common.redo')"
            :aria-label="t('common.redo')"
            :disabled="disabled || recordingActive || recordingPending || !canRedo"
            @click="emit('redo')"
          >
            <span class="sr-only">{{ t('common.redo') }}</span>
            <n-icon :component="ArrowRedoOutline" />
          </button>
        </div>

        <div class="transport-controls">
          <button
            class="transport-chip"
            type="button"
            :title="t('common.reset')"
            :aria-label="t('common.reset')"
            :disabled="disabled || recordingActive || recordingPending"
            @click="emit('reset')"
          >
            <n-icon :component="PlaySkipBackOutline" />
          </button>
          <button
            class="transport-chip transport-chip--stop"
            type="button"
            :title="t('common.stop')"
            :aria-label="t('common.stop')"
            :disabled="disabled || recordingActive || recordingPending"
            @click="emit('stop')"
          >
            <n-icon :component="StopOutline" />
          </button>
          <button
            class="transport-play"
            type="button"
            :disabled="disabled || recordingActive || recordingPending || !transportCanToggle"
            :data-state="showPauseButton ? 'pause' : 'play'"
            :data-pending="transportPendingAction || undefined"
            :data-pressed="transportPressed ? 'true' : undefined"
            :aria-label="transportLabel"
            :title="transportLabel"
            @pointerdown="handleTransportPointerDown"
            @pointerup="clearTransportPressed"
            @pointercancel="clearTransportPressed"
            @pointerleave="clearTransportPressed"
            @blur="clearTransportPressed"
            @click="emit('toggleTransport')"
          >
            <svg v-if="showPauseButton" viewBox="0 0 24 24" aria-hidden="true">
              <rect x="6" y="5" width="4" height="14" rx="1.2" />
              <rect x="14" y="5" width="4" height="14" rx="1.2" />
            </svg>
            <svg v-else viewBox="0 0 24 24" aria-hidden="true">
              <path d="M8 5.5v13l10-6.5z" />
            </svg>
          </button>
          <n-popover
            trigger="manual"
            placement="bottom"
            :show="recordingPopoverOpen"
            :show-arrow="false"
            :overlap="false"
            @clickoutside="recordingPopoverOpen = false"
          >
            <template #trigger>
              <div class="record-control" :class="{ 'record-control--active': recordingActive }">
                <button
                  class="transport-record"
                  type="button"
                  :class="{ 'transport-record--active': recordingActive }"
                  :disabled="disabled || recordingPending"
                  :aria-label="recordingActive ? t('editor.recordingStop') : t('editor.recordingStart')"
                  :title="recordingActive ? t('editor.recordingStop') : t('editor.recordingStart')"
                  @click.stop="emit('toggleRecording')"
                >
                  <n-icon :component="recordingActive ? StopOutline : Mic" />
                </button>
                <button
                  class="record-control__settings"
                  type="button"
                  :aria-label="t('editor.recordingSettings')"
                  :title="t('editor.recordingSettings')"
                  :disabled="disabled"
                  @click.stop="recordingPopoverOpen = !recordingPopoverOpen"
                >
                  <n-icon :component="ChevronDownOutline" />
                </button>
              </div>
            </template>
            <div class="recording-popover">
              <div class="recording-popover__head">
                <div>
                  <n-icon :component="Mic" />
                  <strong>{{ t('editor.recordingSettings') }}</strong>
                  <span v-if="recordingActive">{{ formatTime(recordingElapsed || 0) }}</span>
                </div>
                <button type="button" :disabled="recordingActive" @click="emit('refreshRecordingDevices')">
                  <n-icon :component="RefreshOutline" />
                  {{ t('common.refresh') }}
                </button>
              </div>
              <n-select
                class="recording-device-select"
                :value="recordingDeviceId || ''"
                :options="recordingDeviceOptions"
                :disabled="recordingActive || recordingPending"
                :virtual-scroll="false"
                :menu-props="{ class: 'recording-device-menu' }"
                size="small"
                @update:value="(value: string) => emit('update:recordingDeviceId', value)"
              />
              <div class="recording-meter" :aria-label="t('editor.recordingInputLevel')">
                <span :style="{ transform: `scaleX(${Math.max(0, Math.min(1, recordingInputLevel || 0))})` }" />
              </div>
              <p v-if="recordingError" class="recording-popover__error">{{ recordingError }}</p>
              <p v-else class="recording-popover__hint">{{ t('editor.recordingHint') }}</p>
              <div class="recording-popover__actions">
                <n-button
                  v-if="!recordingActive"
                  size="tiny"
                  secondary
                  @click="emit('addRecordingTrack'); recordingPopoverOpen = false"
                >
                  {{ t('editor.recordingAddTrack') }}
                </n-button>
                <n-button v-else size="tiny" secondary type="error" @click="emit('cancelRecording')">
                  {{ t('editor.recordingCancel') }}
                </n-button>
              </div>
            </div>
          </n-popover>
          <button
            class="transport-chip"
            type="button"
            :class="{ 'transport-chip--active': loop }"
            :title="t('common.loop')"
            :aria-label="t('common.loop')"
            :aria-pressed="loop"
            :disabled="disabled || recordingActive || recordingPending"
            @click="emit('update:loop', !loop)"
          >
            <n-icon :component="RepeatOutline" />
          </button>
        </div>

        <div class="transport-timecode">
          <code>{{ timecode }}</code>
        </div>
      </div>

      <div class="editor-transport__actions">
        <n-popover trigger="click" placement="bottom-end" :show-arrow="false">
          <template #trigger>
            <button class="master-summary" type="button" :disabled="disabled">
              <n-icon :component="volumeIcon" />
              <span>{{ t('editor.masterVolume') }}</span>
              <strong>{{ masterVolumePercent }}</strong>
            </button>
          </template>
          <div class="master-popover">
            <div class="master-popover__head">
              <strong>{{ t('editor.masterVolume') }}</strong>
              <span>{{ masterVolumePercent }}</span>
            </div>
            <n-slider
              :value="masterVolume"
              :min="0"
              :max="2"
              :step="0.01"
              :tooltip="false"
              :disabled="disabled"
              @update:value="(value: number) => emit('update:masterVolume', value)"
              @dragstart="emit('beginMasterVolume')"
              @dragend="emit('commitMasterVolume')"
            />
            <div class="master-popover__head">
              <strong>{{ t('editor.balanceShort') }}</strong>
              <span>{{ formatTrackPan(masterPan) }}</span>
            </div>
            <n-slider
              :value="masterPan"
              :min="-1"
              :max="1"
              :step="0.01"
              :tooltip="false"
              :disabled="disabled"
              @update:value="(value: number) => emit('update:masterPan', value)"
              @dragstart="emit('beginMasterPan')"
              @dragend="emit('commitMasterPan')"
            />
          </div>
        </n-popover>
        <div class="transport-actions__buttons">
          <n-button secondary size="small" :loading="saving" :disabled="disabled || recordingActive || recordingPending" @click="emit('save')">
            <template #icon><n-icon :component="SaveOutline" /></template>
            {{ t('editor.save') }}
          </n-button>
          <n-button type="primary" size="small" :loading="exporting" :disabled="disabled || recordingActive || recordingPending" @click="emit('export')">
            <template #icon><n-icon :component="DownloadOutline" /></template>
            {{ t('editor.export') }}
          </n-button>
        </div>
      </div>

      <div v-if="(missingAssetCount || 0) > 0" class="editor-offline-banner">
        <span class="editor-offline-banner__icon"><n-icon :component="AlertCircleOutline" /></span>
        <strong>{{ t('editor.offlineBannerTitle', { count: missingAssetCount }) }}</strong>
        <button type="button" class="editor-offline-banner__details" @click="offlineDetailsOpen = !offlineDetailsOpen">
          {{ offlineDetailsOpen ? t('editor.offlineDetailsHide') : t('editor.offlineDetails') }}
        </button>
        <n-button
          size="small"
          type="warning"
          ghost
          :loading="relinkingMissingAssets"
          @click="emit('relinkMissingAssets')"
        >
          {{ t('editor.assetRelink') }}
        </n-button>
        <span v-if="offlineDetailsOpen" class="editor-offline-banner__detail-text">
          {{ missingPreviewText || t('editor.assetMissingHint') }}
        </span>
      </div>
    </header>
  </div>
</template>

<style scoped>
.editor-transport-wrap {
  display: grid;
  gap: 0;
  grid-column: 1 / -1;
  min-width: 0;
  width: 100%;
}

.editor-transport {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  min-height: 50px;
  padding: 4px 12px;
  border-bottom: 1px solid var(--outline);
  background: var(--surface);
}

.editor-offline-banner {
  flex-basis: 100%;
  display: grid;
  grid-template-columns: 20px minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 8px;
  padding: 6px 14px;
  border-top: 1px solid color-mix(in srgb, var(--warning) 26%, transparent);
  background: color-mix(in srgb, var(--warning) 8%, var(--surface));
}

.editor-offline-banner__icon {
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  color: color-mix(in srgb, var(--warning) 78%, var(--primary));
}

.editor-offline-banner > strong,
.editor-offline-banner__detail-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.editor-offline-banner > strong {
  font-size: 12px;
  color: color-mix(in srgb, var(--warning) 84%, var(--on-surface));
}

.editor-offline-banner__detail-text {
  grid-column: 2 / -1;
  font-size: 11px;
  color: var(--on-surface-muted);
}

.editor-offline-banner__details {
  padding: 2px 0;
  border: 0;
  color: color-mix(in srgb, var(--warning) 80%, var(--on-surface));
  background: transparent;
  cursor: pointer;
  font: inherit;
  font-size: 11px;
}

.editor-transport__brand,
.editor-transport__center,
.editor-transport__actions {
  min-width: 0;
}

.editor-transport__brand {
  display: flex;
  flex: 1 0 180px;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.editor-transport__brand strong,
.editor-transport__brand-meta {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.editor-transport__brand strong {
  font-size: 13px;
  font-weight: 600;
}

.editor-transport__brand-meta {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 7px;
  border: 1px solid color-mix(in srgb, var(--outline) 44%, transparent);
  border-radius: 999px;
  background: color-mix(in srgb, var(--surface-2) 80%, transparent);
  color: var(--on-surface-muted);
  font-size: 10px;
}

.editor-transport__center {
  display: grid;
  grid-template-columns: 128px auto max-content;
  align-items: center;
  gap: 12px;
  min-width: 0;
  flex: 0 0 auto;
}

.transport-history,
.transport-controls {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

.transport-history {
  justify-content: flex-end;
}

.transport-chip,
.transport-play,
.transport-record {
  border: 0;
  cursor: pointer;
}

.transport-record {
  width: 24px;
  height: 25px;
  display: grid;
  place-items: center;
  padding: 0;
  border-radius: 6px 0 0 6px;
  color: var(--danger);
  background: transparent;
}

.record-control {
  display: inline-grid;
  grid-template-columns: 24px 15px;
  height: 25px;
  overflow: hidden;
  border: 1px solid color-mix(in srgb, var(--outline) 52%, transparent);
  border-radius: 6px;
  background: color-mix(in srgb, var(--surface-2) 58%, transparent);
}

.record-control--active {
  border-color: color-mix(in srgb, var(--danger) 42%, var(--outline));
  background: color-mix(in srgb, var(--danger) 8%, var(--surface-2));
}

.record-control__settings {
  width: 15px;
  min-width: 0;
  height: 23px;
  display: grid;
  place-items: center;
  padding: 0;
  border: 0;
  border-left: 1px solid color-mix(in srgb, var(--outline) 42%, transparent);
  color: var(--on-surface-muted);
  background: transparent;
  cursor: pointer;
}

.record-control__settings :deep(svg) {
  width: 10px;
  height: 10px;
}

.record-control__settings:hover:not(:disabled) {
  color: var(--on-surface);
  background: color-mix(in srgb, var(--surface-3) 64%, transparent);
}

.record-control__settings:disabled {
  cursor: default;
  opacity: 0.45;
}

.transport-record :deep(svg) {
  width: 13px;
  height: 13px;
}

.transport-record:hover:not(:disabled),
.transport-record--active {
  background: color-mix(in srgb, var(--danger) 13%, transparent);
}

.transport-record--active :deep(svg) {
  width: 12px;
  height: 12px;
}

.transport-record:disabled {
  cursor: default;
  opacity: 0.45;
}

.transport-chip {
  width: 25px;
  height: 25px;
  display: grid;
  place-items: center;
  border-radius: 6px;
  color: var(--on-surface-muted);
  background: transparent;
  transition: color 140ms ease, background 140ms ease;
}

.transport-chip--stop {
  width: 31px;
  height: 31px;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.transport-chip:not(:disabled):hover {
  color: var(--on-surface);
  background: var(--surface-2);
}

.transport-chip:disabled {
  opacity: 0.4;
  cursor: default;
}

.transport-chip--active {
  color: var(--primary-strong);
  background: var(--primary-soft);
}

.transport-play {
  width: 31px;
  height: 31px;
  display: grid;
  place-items: center;
  position: relative;
  border-radius: 8px;
  color: #fff;
  background: var(--primary);
  box-shadow: none;
  padding: 0;
  transition: transform 140ms ease, background 140ms ease, opacity 140ms ease;
}

.transport-play:not(:disabled):hover {
  transform: translateY(-1px);
  background: color-mix(in srgb, var(--primary) 88%, white);
}

.transport-play[data-pressed='true'] {
  transform: translateY(0) scale(0.97);
}

.transport-play:disabled {
  cursor: default;
}

.transport-play[data-pending='starting'] svg {
  transform: scale(0.94);
}

.transport-play[data-pending='pausing'] svg {
  transform: scale(0.88);
  opacity: 0.92;
}

.transport-play svg {
  width: 18px;
  height: 18px;
  fill: currentColor;
  transition: transform 160ms ease, opacity 140ms ease;
}

.transport-timecode {
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: flex-start;
}

.transport-timecode code {
  padding: 4px 9px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--surface-2) 88%, transparent);
  color: color-mix(in srgb, var(--on-surface) 88%, var(--on-surface-muted));
  font-family: 'JetBrains Mono', 'Cascadia Code', ui-monospace, monospace;
  font-size: 11px;
  font-weight: 600;
  white-space: nowrap;
}

.editor-transport__actions {
  display: flex;
  flex: 1 0 auto;
  align-items: center;
  gap: 10px;
  min-width: max-content;
  justify-content: flex-end;
}

.master-summary {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  height: 30px;
  padding: 0 8px;
  border: 1px solid color-mix(in srgb, var(--outline) 44%, transparent);
  border-radius: 7px;
  color: var(--on-surface-muted);
  background: color-mix(in srgb, var(--surface-2) 72%, transparent);
  cursor: pointer;
}

.master-summary:hover:not(:disabled) {
  color: var(--on-surface);
  border-color: color-mix(in srgb, var(--outline) 68%, transparent);
}

.master-summary:disabled {
  opacity: 0.5;
  cursor: default;
}

.master-summary span {
  font-size: 10px;
}

.master-summary strong {
  color: var(--on-surface);
  font-size: 10px;
}

.transport-actions__buttons {
  display: flex;
  align-items: center;
  gap: 8px;
  justify-content: flex-end;
}

.master-popover {
  width: 216px;
  display: grid;
  gap: 8px;
  padding: 2px;
}

.master-popover__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 2px;
}

.master-popover__head strong,
.master-popover__head span {
  color: var(--on-surface-muted);
  font-size: 10px;
}

.recording-popover {
  width: min(360px, calc(100vw - 56px));
  max-width: calc(100vw - 56px);
  display: grid;
  gap: 10px;
  overflow: hidden;
}

.recording-popover__head,
.recording-popover__head > div,
.recording-popover__head button {
  display: flex;
  align-items: center;
}

.recording-popover__head {
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
}

.recording-popover__head > div {
  gap: 8px;
  min-width: 0;
}

.recording-popover__head > div > strong {
  white-space: nowrap;
}

.recording-popover__head span {
  color: var(--danger);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.recording-popover__head button {
  gap: 4px;
  padding: 3px 5px;
  border: 0;
  color: var(--on-surface-muted);
  background: transparent;
  cursor: pointer;
  font: inherit;
  font-size: 10px;
  flex-shrink: 0;
}

.recording-popover__head button:disabled {
  cursor: default;
  opacity: 0.45;
}

.recording-meter {
  height: 5px;
  overflow: hidden;
  border-radius: 999px;
  background: color-mix(in srgb, var(--on-surface-muted) 12%, transparent);
}

.recording-meter span {
  display: block;
  width: 100%;
  height: 100%;
  transform-origin: left center;
  background: color-mix(in srgb, var(--primary) 74%, var(--danger));
  transition: transform 80ms linear;
}

.recording-popover__hint,
.recording-popover__error {
  margin: 0;
  font-size: 10px;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.recording-popover__hint {
  color: var(--on-surface-muted);
}

.recording-popover__error {
  color: var(--danger);
}

.recording-popover__actions {
  display: flex;
  justify-content: flex-end;
  min-width: 0;
}

.recording-popover :deep(.n-base-selection),
.recording-popover :deep(.n-base-selection-label),
.recording-popover :deep(.n-base-selection-input) {
  min-width: 0;
  max-width: 100%;
}

:global(.recording-device-menu) {
  width: min(360px, calc(100vw - 32px)) !important;
  max-width: calc(100vw - 32px) !important;
}

:global(.recording-device-menu .n-base-select-option) {
  height: auto !important;
  min-height: 34px;
  padding-top: 7px;
  padding-bottom: 7px;
}

:global(.recording-device-menu .n-base-select-option__content) {
  overflow: visible !important;
  padding-right: 20px;
  line-height: 1.35;
  white-space: normal !important;
  text-overflow: clip !important;
  overflow-wrap: anywhere;
}

</style>
