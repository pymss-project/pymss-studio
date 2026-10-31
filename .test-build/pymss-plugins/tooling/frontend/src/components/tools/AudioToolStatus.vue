<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  AlertCircleOutline,
  CheckmarkCircleOutline,
  FolderOpenOutline,
  TerminalOutline,
  TimeOutline,
} from '@vicons/ionicons5'
import type {
  AudioToolLogEntry,
  AudioToolPhase,
  AudioToolProgress,
  AudioToolResult,
} from '@/features/audio-tools/types'

const props = defineProps<{
  busy: boolean
  cancelling: boolean
  hasResult: boolean
  error: string
  progress: AudioToolProgress
  percentage: number
  result: AudioToolResult | null
  elapsedMs: number
  logs: AudioToolLogEntry[]
  view: 'activity' | 'result'
}>()

const emit = defineEmits<{
  cancel: []
  reveal: [path: string]
}>()

const { t } = useI18n()
const logList = ref<HTMLElement | null>(null)

const outputPath = computed(() => props.result?.outputPath || props.result?.outputDir || '')
const outputs = computed(() => props.result?.outputPaths || (props.result?.outputPath ? [props.result.outputPath] : []))
const failures = computed(() => [...(props.result?.failed || []), ...(props.result?.skipped || [])])
const resultWarnings = computed(() => (props.result?.warnings || []).map((warning) => {
  if (warning === 'no_notes_detected') return t('tools.midiWarningNoNotes')
  if (warning === 'stereo_downmix_fallback') return t('tools.midiWarningStereoFallback')
  if (warning === 'timestamps_unavailable') return t('tools.asrWarningNoTimestamps')
  if (warning === 'loudness_target_not_reached') return t('tools.loudnessTargetNotReached')
  if (warning === 'loudness_true_peak_exceeded') return t('tools.loudnessTruePeakExceeded')
  return warning
}))
const completedCount = computed(() => {
  if (!props.result) return 0
  if (props.result.operation === 'convert') return props.result.succeeded ?? outputs.value.length
  if (props.result.operation === 'merge') return props.result.merged ?? outputs.value.length
  return props.result.succeeded ?? outputs.value.length
})
const determinate = computed(() => props.busy && props.progress.total > 0)
const phaseLabels = computed<Record<AudioToolPhase, string>>(() => ({
  started: t('tools.phaseStarted'),
  preparing: t('tools.phasePreparing'),
  converting: t('tools.phaseConverting'),
  normalizing: t('tools.phaseNormalizing'),
  merging: t('tools.phaseMerging'),
  loading_reference: t('tools.phaseLoadingReference'),
  loading_estimated: t('tools.phaseLoadingEstimated'),
  calculating: t('tools.phaseCalculating'),
  loading_model: t('tools.phaseLoadingModel'),
  loading_audio: t('tools.phaseLoadingAudio'),
  transcribing: t('tools.phaseTranscribing'),
  writing_output: t('tools.phaseWritingOutput'),
  probing: t('tools.phaseProbing'),
  analyzing_silence: t('tools.phaseAnalyzingSilence'),
  writing_segments: t('tools.phaseWritingSegments'),
  loading_asr_model: t('tools.phaseLoadingAsrModel'),
  recognizing_speech: t('tools.phaseRecognizingSpeech'),
  writing_transcript: t('tools.phaseWritingTranscript'),
  analyzing_tempo_key: t('tools.phaseAnalyzingTempoKey'),
  completed: t('tools.phaseCompleted'),
  cancelled: t('tasks.statusCancelled'),
  failed: t('tools.phaseFailed'),
}))
const statusLabel = computed(() => {
  if (props.error) return phaseLabels.value.failed
  if (props.busy) return phaseLabels.value[props.progress.phase]
  if (props.progress.phase === 'cancelled') return phaseLabels.value.cancelled
  if (props.hasResult && props.result) return phaseLabels.value.completed
  return t('tools.statusReady')
})

watch(() => props.logs.length, async () => {
  await nextTick()
  if (logList.value) logList.value.scrollTop = logList.value.scrollHeight
})

function fileName(path: string) {
  return path.split(/[\\/]/).filter(Boolean).pop() || path
}

function phaseLabel(phase: AudioToolPhase) {
  return phaseLabels.value[phase]
}

function formatElapsed(milliseconds: number) {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000))
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const remainder = seconds % 60
  if (hours > 0) {
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`
  }
  return `${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`
}

function formatAudioTime(seconds: number | null | undefined) {
  if (seconds == null || !Number.isFinite(seconds)) return '—'
  const centiseconds = Math.max(0, Math.round(seconds * 100))
  const wholeSeconds = Math.floor(centiseconds / 100)
  const hours = Math.floor(wholeSeconds / 3600)
  const minutes = Math.floor((wholeSeconds % 3600) / 60)
  const remainder = wholeSeconds % 60
  const fraction = centiseconds % 100
  const secondsText = `${String(remainder).padStart(2, '0')}.${String(fraction).padStart(2, '0')}`
  if (hours > 0) {
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${secondsText}`
  }
  return `${String(minutes).padStart(2, '0')}:${secondsText}`
}

function formatLogTime(timestamp: number) {
  return new Date(timestamp).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

function logDuration(entry: AudioToolLogEntry, index: number) {
  const updatedAt = props.busy && index === props.logs.length - 1
    ? Date.now()
    : entry.updatedAt
  const duration = Math.max(0, updatedAt - entry.timestamp)
  return duration >= 1000 ? formatElapsed(duration) : ''
}

function showLogProgress(entry: AudioToolLogEntry) {
  return entry.total > 0 && !['started', 'completed', 'failed'].includes(entry.phase)
}
</script>

<template>
  <div class="audio-tool-status" >
    <section
      v-if="view === 'activity'"
      class="status-overview"
      :class="{
        'status-overview--busy': busy,
        'status-overview--success': hasResult && result && !error && !resultWarnings.length,
        'status-overview--warning': hasResult && result && !error && resultWarnings.length,
        'status-overview--error': error,
        'status-overview--cancelled': !busy && !error && progress.phase === 'cancelled',
      }"
    >
      <header class="status-overview__header">
        <span class="status-state">
          <i aria-hidden="true" />
          <strong>{{ statusLabel }}</strong>
        </span>
        <span class="status-elapsed">
          <n-icon :component="TimeOutline" />
          {{ formatElapsed(elapsedMs) }}
        </span>
      </header>

      <template v-if="busy">
        <div class="progress-heading">
          <span>{{ phaseLabel(progress.phase) }}</span>
          <strong v-if="determinate">{{ percentage }}%</strong>
          <small v-else>{{ t('tools.inProgress') }}</small>
        </div>
        <n-progress
          v-if="determinate"
          type="line"
          :percentage="percentage"
          processing
          :height="10"
          :show-indicator="false"
        />
        <div v-else class="indeterminate-progress">
          <n-spin :size="15" />
          <span>{{ t('tools.waitingForProgress') }}</span>
        </div>
        <div class="progress-meta">
          <span :title="progress.current">{{ progress.current || t('tools.preparingTask') }}</span>
          <strong v-if="progress.total">{{ progress.completed }} / {{ progress.total }}</strong>
        </div>
        <div class="progress-actions">
          <n-button size="small" secondary :loading="cancelling" @click="emit('cancel')">
            {{ t('common.cancel') }}
          </n-button>
        </div>
      </template>

      <div v-else-if="error" class="status-error" role="alert">
        <n-icon :component="AlertCircleOutline" />
        <span>{{ error }}</span>
      </div>

      <div v-else-if="progress.phase === 'cancelled'" class="status-empty status-empty--cancelled">
        {{ t('tasks.statusCancelled') }}
      </div>

      <div v-else-if="!hasResult || !result" class="status-empty">
        {{ t('tools.waiting') }}
      </div>

      <div v-else class="status-complete">
        <n-icon :component="resultWarnings.length ? AlertCircleOutline : CheckmarkCircleOutline" />
        <div>
          <strong v-if="result.operation === 'sdr' || result.operation === 'inspect' || result.operation === 'tempo_key' || (result.operation === 'midi' && result.action === 'detect_bpm')">{{ t('tools.analysisCompleted') }}</strong>
          <strong v-else-if="resultWarnings.length">{{ t('tools.completedWithWarnings') }}</strong>
          <strong v-else>{{ t('tools.outputCount', { count: completedCount }) }}</strong>
          <span>{{ t('tools.completedIn', { time: formatElapsed(elapsedMs) }) }}</span>
        </div>
      </div>
    </section>

    <template v-if="view === 'result'">
    <slot v-if="$slots.result && result && !error" name="result" :result="result" />

    <section v-else-if="result?.operation === 'sdr' && !error" class="metric-grid">
      <div class="metric-item">
        <span>SDR</span>
        <strong>{{ result.averageSdr ?? '—' }} dB</strong>
      </div>
      <div class="metric-item">
        <span>SI-SDR</span>
        <strong>{{ result.averageSiSdr ?? '—' }} dB</strong>
      </div>
      <div class="channel-values">
        <span>SDR: {{ (result.sdr || []).map(value => `${value} dB`).join(' / ') }}</span>
        <span>SI-SDR: {{ (result.siSdr || []).map(value => `${value} dB`).join(' / ') }}</span>
      </div>
    </section>

    <section v-else-if="result && !error" class="result-summary">
      <div v-if="result.operation === 'midi' && result.action === 'detect_bpm'" class="metric-grid">
        <div class="metric-item"><span>{{ t('tools.bpm') }}</span><strong>{{ result.roundedBpm }}</strong></div>
      </div>
      <div v-else-if="result.operation === 'midi'" class="midi-result-metrics">
        <div>
          <span>{{ t('tools.midiNoteCount') }}</span>
          <strong>{{ result.noteCount ?? '—' }}</strong>
        </div>
        <div>
          <span>{{ t('tools.midiFirstNoteAt') }}</span>
          <strong>{{ formatAudioTime(result.firstNoteAt) }}</strong>
        </div>
        <div>
          <span>{{ t('tools.midiLastNoteAt') }}</span>
          <strong>{{ formatAudioTime(result.lastNoteAt) }}</strong>
        </div>
        <div>
          <span>{{ t('tools.midiInputDuration') }}</span>
          <strong>{{ formatAudioTime(result.inputDuration) }}</strong>
        </div>
      </div>
      <div v-for="path in outputs.slice(0, 4)" :key="path" class="output-file" :title="path">
        {{ fileName(path) }}
      </div>
      <small v-if="outputs.length > 4">+{{ outputs.length - 4 }}</small>
      <div v-if="resultWarnings.length" class="status-warning" role="status">
        <strong>{{ t('tools.resultWarnings') }}</strong>
        <span v-for="warning in resultWarnings" :key="warning">{{ warning }}</span>
      </div>
      <div v-if="failures.length" class="status-warning">
        <strong>{{ t('tools.skippedCount', { count: failures.length }) }}</strong>
        <span v-for="failure in failures.slice(0, 3)" :key="failure.path" :title="failure.message">
          {{ fileName(failure.path) }}：{{ failure.message }}
        </span>
      </div>
      <n-button v-if="outputPath" secondary size="small" @click="emit('reveal', outputPath)">
        <template #icon><n-icon :component="FolderOpenOutline" /></template>
        {{ t('tools.openOutputLocation') }}
      </n-button>
    </section>

    </template>

    <section v-if="view === 'activity'" class="activity-panel">
      <header class="activity-panel__header">
        <span>
          <n-icon :component="TerminalOutline" />
          <strong>{{ t('tools.activityLog') }}</strong>
        </span>
        <small>{{ t('tools.logEntries', { count: logs.length }) }}</small>
      </header>
      <div ref="logList" class="activity-list" role="log" aria-live="polite">
        <div v-if="!logs.length" class="activity-empty">{{ t('tools.noActivityLog') }}</div>
        <template v-else>
          <article
            v-for="(entry, index) in logs"
            :key="entry.id"
            class="activity-entry"
            :class="{ 'activity-entry--error': entry.phase === 'failed' }"
          >
            <time>
              <span>{{ formatLogTime(entry.timestamp) }}</span>
              <em v-if="logDuration(entry, index)" :title="t('tools.phaseDuration')">
                +{{ logDuration(entry, index) }}
              </em>
            </time>
            <div>
              <strong>{{ phaseLabel(entry.phase) }}</strong>
              <span v-if="entry.current && entry.phase === 'started'" :title="entry.current">
                {{ entry.current }}
              </span>
              <p>{{ entry.description }}</p>
              <small v-if="entry.detail">{{ entry.detail }}</small>
              <footer v-if="showLogProgress(entry)">
                <span>
                  {{ t('tools.progressDetail', {
                    completed: entry.completed,
                    total: entry.total,
                    percentage: Math.min(100, Math.round((entry.completed / entry.total) * 100)),
                  }) }}
                </span>
              </footer>
            </div>
          </article>
        </template>
      </div>
    </section>
  </div>
</template>

<style scoped>
.audio-tool-status {
  min-height: 0;
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 12px;
}

.status-overview,
.activity-panel,
.result-summary,
.metric-grid {
  border: 1px solid color-mix(in srgb, var(--outline) 78%, transparent);
  border-radius: 11px;
  background: color-mix(in srgb, var(--surface-2) 34%, var(--surface-1));
}

.status-overview {
  padding: 13px;
}

.status-overview__header,
.progress-heading,
.progress-meta,
.activity-panel__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.progress-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 10px;
}

.status-state,
.status-elapsed,
.activity-panel__header > span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.status-state {
  min-width: 0;
  font-size: 13px;
}

.status-state i {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--on-surface-muted);
}

.status-overview--busy .status-state i {
  background: var(--primary-strong);
}

.status-overview--success .status-state i {
  background: var(--success);
}

.status-overview--warning .status-state i {
  background: var(--warning);
}

.status-overview--warning .status-complete {
  color: var(--warning);
}

.status-overview--error .status-state i {
  background: var(--danger);
}

.status-elapsed {
  flex: 0 0 auto;
  color: var(--on-surface-muted);
  font: 11px/1 var(--font-mono);
  font-variant-numeric: tabular-nums;
}

.progress-heading {
  margin: 15px 0 8px;
  font-size: 12px;
}

.progress-heading span,
.progress-heading small,
.progress-meta,
.status-empty {
  color: var(--on-surface-muted);
}

.progress-heading strong {
  font: 600 12px/1 var(--font-mono);
  font-variant-numeric: tabular-nums;
}

.indeterminate-progress {
  min-height: 28px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 9px;
  border: 1px solid color-mix(in srgb, var(--primary-border) 44%, var(--outline));
  border-radius: 8px;
  color: var(--on-surface-muted);
  font-size: 11px;
}

.progress-meta {
  margin-top: 9px;
  font-size: 11px;
}

.progress-meta span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.progress-meta strong {
  flex: 0 0 auto;
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
}

.status-empty {
  min-height: 76px;
  display: grid;
  place-items: center;
  font-size: 12px;
  text-align: center;
}

.status-error,
.status-complete {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-top: 13px;
}

.status-error {
  color: var(--danger);
  font-size: 12px;
  line-height: 1.55;
  overflow-wrap: anywhere;
}

.status-complete {
  color: var(--success);
}

.status-complete > .n-icon,
.status-error > .n-icon {
  flex: 0 0 auto;
  margin-top: 2px;
}

.status-complete div {
  display: grid;
  gap: 3px;
}

.status-complete strong {
  font-size: 13px;
}

.status-complete span {
  color: var(--on-surface-muted);
  font-size: 11px;
}

.result-summary {
  display: grid;
  gap: 8px;
  padding: 11px;
}

.output-file {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--on-surface-muted);
  font-size: 12px;
}

.result-summary > small {
  color: var(--on-surface-muted);
}

.midi-result-metrics {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 7px;
}

.midi-result-metrics > div {
  display: grid;
  gap: 2px;
  min-width: 0;
  padding: 8px 9px;
  border: 1px solid color-mix(in srgb, var(--outline) 72%, transparent);
  border-radius: 8px;
}

.midi-result-metrics span {
  color: var(--on-surface-muted);
  font-size: 10px;
}

.midi-result-metrics strong {
  color: var(--on-surface);
  font-size: 12px;
}

.status-warning {
  display: grid;
  gap: 4px;
  padding: 9px 10px;
  border: 1px solid color-mix(in srgb, var(--warning) 30%, var(--outline));
  border-radius: 9px;
  color: var(--warning);
  font-size: 11px;
}

.status-warning span {
  color: color-mix(in srgb, var(--warning) 65%, var(--on-surface-muted));
  line-height: 1.55;
  overflow-wrap: anywhere;
}

.activity-panel {
  min-height: 180px;
  display: flex;
  flex: 1;
  flex-direction: column;
  overflow: hidden;
}

.activity-panel__header {
  min-height: 38px;
  padding: 0 11px;
  border-bottom: 1px solid color-mix(in srgb, var(--outline) 68%, transparent);
}

.activity-panel__header > span {
  font-size: 12px;
}

.activity-panel__header small {
  color: var(--on-surface-muted);
  font-size: 10px;
}

.activity-list {
  min-height: 140px;
  max-height: 230px;
  overflow: auto;
  padding: 7px 9px 9px;
  scrollbar-width: thin;
}

.activity-empty {
  min-height: 130px;
  display: grid;
  place-items: center;
  color: var(--on-surface-muted);
  font-size: 11px;
}

.activity-entry {
  display: grid;
  grid-template-columns: 54px minmax(0, 1fr);
  gap: 7px;
  padding: 6px 2px;
  border-bottom: 1px solid color-mix(in srgb, var(--outline) 48%, transparent);
}

.activity-entry:last-child {
  border-bottom: 0;
}

.activity-entry time {
  display: grid;
  align-content: start;
  gap: 3px;
  padding-top: 1px;
  color: var(--on-surface-muted);
  font: 10px/1.4 var(--font-mono);
  font-variant-numeric: tabular-nums;
}

.activity-entry time em {
  color: color-mix(in srgb, var(--on-surface-muted) 78%, transparent);
  font-size: 9px;
  font-style: normal;
}

.activity-entry > div {
  min-width: 0;
  display: grid;
  gap: 2px;
}

.activity-entry strong {
  font-size: 11px;
  font-weight: 600;
}

.activity-entry span,
.activity-entry small,
.activity-entry p {
  overflow: hidden;
  color: var(--on-surface-muted);
  font-size: 10px;
  line-height: 1.45;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.activity-entry p {
  margin: 1px 0 0;
  white-space: normal;
}

.activity-entry small {
  white-space: normal;
  overflow-wrap: anywhere;
}

.activity-entry footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 3px;
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
}

.activity-entry--error strong,
.activity-entry--error small {
  color: var(--danger);
}

.metric-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  padding: 10px;
}

.metric-item {
  display: grid;
  gap: 4px;
  padding: 10px;
  border: 1px solid var(--outline);
  border-radius: 9px;
  background: var(--surface-1);
}

.metric-item span {
  color: var(--on-surface-muted);
  font-size: 11px;
}

.metric-item strong {
  font-size: 16px;
}

.channel-values {
  grid-column: 1 / -1;
  display: grid;
  gap: 5px;
  color: var(--on-surface-muted);
  font: 10px/1.5 var(--font-mono);
  overflow-wrap: anywhere;
}
</style>
