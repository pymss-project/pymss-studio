<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { EditorExportFormat, EditorExportSampleRate } from '@/types/editor'

const props = defineProps<{
  show: boolean
  sessionName: string
  duration: number
  trackCount: number
  exporting: boolean
  exportCancelling: boolean
  exportProgress: number
  exportProgressIndeterminate?: boolean
  exportProgressText: string
  format: EditorExportFormat
  wavBitDepth: string
  flacBitDepth: string
  sampleRate: EditorExportSampleRate
  peakProtection: boolean
  fileName: string
  exportDir: string
  exportDirResolving: boolean
}>()

const emit = defineEmits<{
  'update:show': [value: boolean]
  'update:format': [value: EditorExportFormat]
  'update:wav-bit-depth': [value: string]
  'update:flac-bit-depth': [value: string]
  'update:sample-rate': [value: EditorExportSampleRate]
  'update:peak-protection': [value: boolean]
  'update:file-name': [value: string]
  'update:export-dir': [value: string]
  pickExportDir: []
  confirm: []
  cancelExport: []
}>()

const { t } = useI18n()

const exportFormatOptions = computed(() => [
  { label: 'WAV', value: 'wav' as EditorExportFormat },
  { label: 'FLAC', value: 'flac' as EditorExportFormat },
])

const wavBitDepthOptions = computed(() => [
  { label: 'PCM_16', value: 'PCM_16' },
  { label: 'PCM_24', value: 'PCM_24' },
  { label: 'FLOAT', value: 'FLOAT' },
])

const flacBitDepthOptions = computed(() => [
  { label: 'PCM_16', value: 'PCM_16' },
  { label: 'PCM_24', value: 'PCM_24' },
])

const sampleRateOptions = computed(() => [
  { label: t('editor.exportSampleRateAuto'), value: 'auto' as EditorExportSampleRate },
  { label: '32000 Hz', value: 32000 as EditorExportSampleRate },
  { label: '44100 Hz', value: 44100 as EditorExportSampleRate },
  { label: '48000 Hz', value: 48000 as EditorExportSampleRate },
])

const sampleRateSummary = computed(() => props.sampleRate === 'auto'
  ? t('editor.exportSampleRateStrategyValue')
  : `${props.sampleRate} Hz`)

const exportSummaryRows = computed(() => [
  { label: t('editor.totalDuration'), value: props.duration ? `${Math.round(props.duration * 10) / 10}s` : '0s' },
  { label: t('editor.tracks'), value: String(props.trackCount) },
  { label: t('editor.exportSampleRateStrategy'), value: sampleRateSummary.value },
])

</script>

<template>
  <n-modal :aria-label="t('editor.exportDialogTitle')"
    :show="show"
    preset="card"
    class="editor-export-modal"
    :title="t('editor.exportDialogTitle')"
    :bordered="false"
    size="small"
    :mask-closable="!exporting"
    :close-on-esc="!exporting"
    :closable="!exporting"
    style="width: var(--modal-width-sm)"
    @update:show="(value: boolean) => emit('update:show', value)"
  >
    <div class="export-dialog">
      <div class="export-dialog__intro">
        <strong>{{ sessionName }}</strong>
        <span>{{ t('editor.exportDialogHint') }}</span>
      </div>

      <div v-if="exporting" class="export-dialog__progress">
        <div class="export-dialog__progress-copy">
          <span>{{ exportProgressText || t('editor.exporting') }}</span>
          <strong v-if="!exportProgressIndeterminate">{{ exportProgress }}%</strong>
          <n-spin v-else :size="14" :aria-label="t('editor.exporting')" />
        </div>
        <n-progress :aria-label="t('editor.exporting')" :aria-valuetext="`${exportProgress}%${exportProgressText ? ' · ' + exportProgressText : ''}`" v-if="!exportProgressIndeterminate" type="line" :percentage="exportProgress" :show-indicator="false" status="info" />
        <n-skeleton v-else role="progressbar" :aria-label="t('editor.exporting')" :aria-valuetext="exportProgressText || t('editor.exporting')" :height="8" :sharp="false" />
      </div>

      <div class="export-dialog__grid">
        <section class="export-dialog__section export-dialog__section--stacked">
          <div class="export-dialog__section-title">{{ t('editor.exportParamsSection') }}</div>
          <div class="export-dialog__form">
            <label class="export-dialog__field">
              <span>{{ t('editor.exportFormat') }}</span>
              <AppSelect
                :value="format"
                :label="t('editor.exportFormat')"
                :options="exportFormatOptions"
                size="small"
                :disabled="exporting"
                @update:value="(value: EditorExportFormat) => emit('update:format', value)"
              />
            </label>

            <label class="export-dialog__field" v-if="format === 'wav'">
              <span>{{ t('audio.wavBitDepth') }}</span>
              <AppSelect
                :value="wavBitDepth"
                :label="t('audio.wavBitDepth')"
                :options="wavBitDepthOptions"
                size="small"
                :disabled="exporting"
                @update:value="(value: string) => emit('update:wav-bit-depth', value)"
              />
            </label>

            <label class="export-dialog__field" v-if="format === 'flac'">
              <span>{{ t('audio.flacBitDepth') }}</span>
              <AppSelect
                :value="flacBitDepth"
                :label="t('audio.flacBitDepth')"
                :options="flacBitDepthOptions"
                size="small"
                :disabled="exporting"
                @update:value="(value: string) => emit('update:flac-bit-depth', value)"
              />
            </label>

            <label class="export-dialog__field">
              <span>{{ t('editor.exportSampleRate') }}</span>
              <AppSelect
                :value="sampleRate"
                :label="t('editor.exportSampleRate')"
                :options="sampleRateOptions"
                size="small"
                :disabled="exporting"
                @update:value="(value: EditorExportSampleRate) => emit('update:sample-rate', value)"
              />
            </label>

            <label class="export-dialog__field export-dialog__field--switch">
              <span :title="t('editor.exportPeakProtectionHint')">
                {{ t('editor.exportPeakProtection') }}
              </span>
              <n-switch :aria-label="t('editor.exportPeakProtection')"
                :value="peakProtection"
                :disabled="exporting"
                @update:value="(value: boolean) => emit('update:peak-protection', value)"
              />
            </label>
          </div>
        </section>

        <section class="export-dialog__section export-dialog__section--stacked export-dialog__section--path">
          <div class="export-dialog__section-title">{{ t('editor.exportDirSection') }}</div>
          <div class="export-dir">
            <label class="export-dialog__field">
              <span>{{ t('editor.exportFileName') }}</span>
              <n-input
                :value="fileName" :input-props="{ 'aria-label': t('editor.exportFileName') }"
                size="small"
                :disabled="exporting"
                :placeholder="t('editor.exportFileNamePlaceholder')"
                @update:value="(value: string) => emit('update:file-name', value)"
              />
            </label>
            <div class="export-dir__actions">
              <n-input
                :value="exportDir" :input-props="{ 'aria-label': t('editor.exportDirSection') }"
                size="small"
                clearable
                :disabled="exporting"
                :placeholder="t('editor.exportDirPlaceholder')"
                @update:value="(value: string) => emit('update:export-dir', value)"
              />
              <n-button secondary size="small" :loading="exportDirResolving" :disabled="exporting" @click="emit('pickExportDir')">
                {{ t('editor.exportDirBrowse') }}
              </n-button>
            </div>
          </div>
        </section>

        <section class="export-dialog__section export-dialog__section--compact">
          <div class="export-dialog__section-title">{{ t('editor.exportRenderSummary') }}</div>
          <div class="export-summary">
            <div v-for="row in exportSummaryRows" :key="row.label" class="export-summary__item">
              <span>{{ row.label }}</span>
              <strong>{{ row.value }}</strong>
            </div>
            <div class="export-summary__item export-summary__item--wide">
              <span>{{ t('editor.exportProcessing') }}</span>
              <strong>{{ t('editor.exportProcessingValue') }}</strong>
            </div>
          </div>
        </section>
      </div>
    </div>

    <template #footer>
      <div class="export-dialog__footer">
        <n-button
          v-if="exporting"
          secondary
          type="warning"
          :loading="exportCancelling"
          @click="emit('cancelExport')"
        >
          {{ t('common.cancel') }}
        </n-button>
        <n-button v-else secondary @click="emit('update:show', false)">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" :loading="exporting" @click="emit('confirm')">{{ t('editor.export') }}</n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.editor-export-modal :deep(.n-card) {
  background: linear-gradient(180deg, color-mix(in srgb, var(--surface-1) 96%, transparent), var(--surface));
}

.export-dialog {
  display: grid;
  gap: 12px;
  width: 100%;
}

.export-dialog__grid {
  display: grid;
  gap: 10px;
}

.export-dialog__progress {
  display: grid;
  gap: 6px;
  padding: 9px 10px;
  border-radius: 12px;
  background: color-mix(in srgb, var(--primary) 8%, var(--surface-2));
}

.export-dialog__progress-copy {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  color: var(--on-surface-muted);
  font-size: 11px;
}

.export-dialog__progress-copy strong {
  color: var(--on-surface);
  font-size: 11px;
}

.export-dialog__intro {
  display: grid;
  gap: 4px;
  padding: 2px 2px 0;
}

.export-dialog__intro strong {
  font-size: 13px;
  line-height: 1.1;
}

.export-dialog__intro span {
  color: var(--on-surface-muted);
  font-size: 11px;
  line-height: 1.45;
}

.export-dialog__form {
  display: grid;
  gap: 10px;
}

.export-dialog__field {
  display: grid;
  gap: 6px;
}

.export-dialog__field--switch {
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
}

.export-dialog__field span {
  color: var(--on-surface-muted);
  font-size: 11px;
  line-height: 1.2;
}

.export-dialog__section {
  display: grid;
  gap: 8px;
  padding: 9px 10px;
  border-radius: 12px;
  background: color-mix(in srgb, var(--surface-2) 82%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 74%, transparent);
}

.export-dialog__section--stacked {
  gap: 10px;
}

.export-dialog__section--compact {
  padding: 10px;
}

.export-dialog__section--path {
  gap: 10px;
}

.export-dialog__section-title {
  font-size: 12px;
  font-weight: 600;
  line-height: 1;
}

.export-summary {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
}

.export-summary__item {
  display: grid;
  gap: 3px;
  padding: 8px 10px;
  border-radius: 12px;
  background: color-mix(in srgb, var(--surface-1) 80%, transparent);
}

.export-summary__item:nth-child(n + 3) {
  grid-column: 1 / -1;
}

.export-summary__item span {
  color: var(--on-surface-muted);
  font-size: 10px;
  line-height: 1.2;
}

.export-summary__item strong {
  font-size: 12px;
  line-height: 1.3;
}

.export-dir {
  display: grid;
  gap: 8px;
}

.export-dir__actions {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  align-items: center;
}

.export-dialog__footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.editor-export-modal :deep(.app-select) {
  --app-select-color: color-mix(in srgb, var(--surface-1) 92%, transparent);
  --app-select-border: color-mix(in srgb, var(--outline) 60%, transparent);
  --app-select-border-hover: color-mix(in srgb, var(--outline) 78%, transparent);
  --app-select-border-focus: color-mix(in srgb, var(--primary) 50%, transparent);
  --app-select-focus-shadow: 0 0 0 2px color-mix(in srgb, var(--primary-soft) 30%, transparent);
}
</style>
