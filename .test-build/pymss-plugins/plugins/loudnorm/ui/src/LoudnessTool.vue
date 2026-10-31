<script setup lang="ts">
import { toolRef } from '@/sdk'
import { computed, onMounted, ref, watch } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { CloseCircleOutline, DocumentOutline, FolderOpenOutline, VolumeHighOutline } from '@vicons/ionicons5'
import { useSettingsStore } from '@/stores/settings'
import AudioToolPanel from '@/features/audio-tools/shared/AudioToolPanel.vue'
import AudioToolHeader from '@/features/audio-tools/shared/AudioToolHeader.vue'
import { useAudioToolRuntime } from '@/features/audio-tools/runtime'
import { loadAudioToolsState, updateAudioToolsState } from '@/features/audio-tools/state'

type ScanResult = { files: string[]; warnings: string[] }
type LoudnessMeasurement =
  | 'itu-bs1770-3'
  | 'rms'
  | 'rms-match-file'
  | 'peak'
  | 'true-peak'

const { t } = useI18n()
const message = useMessage()
const settings = useSettingsStore()
const runtime = useAudioToolRuntime()
const state = runtime.stateFor('loudness')
const inputs = toolRef<string[]>('inputs', [])
const outputDir = toolRef('outputDir', settings.outputDir)
const measurement = ref<LoudnessMeasurement>('itu-bs1770-3')
const target = ref<number | null>(-23)
const tolerance = ref<number | null>(0.5)
const maxTruePeak = ref<number | null>(-1)
const useLimiter = ref(true)
const lookahead = ref<number | null>(12)
const release = ref<number | null>(200)
const referencePath = toolRef('referencePath', '')
let restored = false
// Set when the user edits a field before saved state finishes loading, so the
// late restore does not overwrite that edit.
let editedBeforeRestore = false

// Defaults mirror MEASUREMENTS in python/audio_tools/loudness.py.
const measurementDefinitions: Array<{ value: LoudnessMeasurement; labelKey: string; hintKey: string; defaultTarget: number }> = [
  { value: 'itu-bs1770-3', labelKey: 'tools.loudnessMeasurementitu_bs1770_3', hintKey: 'tools.loudnessHintitu_bs1770_3', defaultTarget: -23 },
  { value: 'rms', labelKey: 'tools.loudnessMeasurementrms', hintKey: 'tools.loudnessHintrms', defaultTarget: -23 },
  { value: 'rms-match-file', labelKey: 'tools.loudnessMeasurementrms_match_file', hintKey: 'tools.loudnessHintrms_match_file', defaultTarget: 0 },
  { value: 'peak', labelKey: 'tools.loudnessMeasurementpeak', hintKey: 'tools.loudnessHintpeak', defaultTarget: -1 },
  { value: 'true-peak', labelKey: 'tools.loudnessMeasurementtrue_peak', hintKey: 'tools.loudnessHinttrue_peak', defaultTarget: -1 },
]
// Mirrors MEASUREMENT_ALIASES in python/audio_tools/loudness.py for saved state.
const measurementAliases: Record<string, LoudnessMeasurement> = {
  'atsc-a85': 'itu-bs1770-3',
  'ebu-r128': 'itu-bs1770-3',
  'legacy-loudness': 'rms',
  'legacy-perceived': 'rms',
}
const loudnessPresets = [
  { labelKey: 'tools.loudnessPresetEbu', target: -23 },
  { labelKey: 'tools.loudnessPresetAtsc', target: -24 },
]
const measurementOptions = computed(() => measurementDefinitions.map(item => ({ label: t(item.labelKey), value: item.value })))
const loudnessModes = new Set<LoudnessMeasurement>(['itu-bs1770-3'])
const rmsModes = new Set<LoudnessMeasurement>(['rms', 'rms-match-file'])
const usesTarget = computed(() => measurement.value !== 'rms-match-file')
const showsLimiter = computed(() => loudnessModes.has(measurement.value) || rmsModes.has(measurement.value))
// The ceiling only feeds the limiter (python/audio_tools/loudness.py), so hide it when the limiter is off.
const showsTruePeak = computed(() => showsLimiter.value && useLimiter.value)
const showsReference = computed(() => measurement.value === 'rms-match-file')
const targetUnit = computed(() => loudnessModes.has(measurement.value)
  ? 'LUFS'
  : measurement.value === 'true-peak' ? 'dBTP' : 'dB')
const targetLabel = computed(() => levelLabel(measurement.value))
// Ranges follow FFmpeg's loudnorm (I: -70..-5, TP: -9..0) and alimiter (attack: 0.1..80 ms).
const targetMax = computed(() => loudnessModes.has(measurement.value) ? -5 : 0)
const parametersComplete = computed(() => {
  // Only visible fields can block a run; hidden ones fall back to backend defaults.
  const required: Array<number | null> = []
  if (usesTarget.value) required.push(target.value)
  required.push(tolerance.value)
  if (showsTruePeak.value) required.push(maxTruePeak.value)
  if (showsLimiter.value && useLimiter.value) required.push(lookahead.value, release.value)
  return required.every(value => typeof value === 'number' && Number.isFinite(value))
})
const parameterHint = computed(() => t(measurementDefinitions.find(item => item.value === measurement.value)?.hintKey || 'tools.loudnessHint'))

watch(() => settings.outputDir, value => { if (!outputDir.value) outputDir.value = value })

onMounted(async () => {
  const saved = await loadAudioToolsState()
  if (editedBeforeRestore) {
    restored = true
    persist()
    return
  }
  const savedMeasurement = saved?.loudnessMeasurement ? measurementAliases[saved.loudnessMeasurement] || saved.loudnessMeasurement : ''
  if (savedMeasurement && measurementOptions.value.some(item => item.value === savedMeasurement)) {
    measurement.value = savedMeasurement as LoudnessMeasurement
  }
  // Clamp values saved before the ranges were narrowed to what FFmpeg accepts.
  if (typeof saved?.loudnessTarget === 'number') target.value = clamp(saved.loudnessTarget, -70, targetMax.value)
  if (typeof saved?.loudnessTolerance === 'number') tolerance.value = clamp(saved.loudnessTolerance, 0, 20)
  if (typeof saved?.loudnessMaxTruePeak === 'number') maxTruePeak.value = clamp(saved.loudnessMaxTruePeak, -9, 0)
  if (typeof saved?.loudnessUseLimiter === 'boolean') useLimiter.value = saved.loudnessUseLimiter
  if (typeof saved?.loudnessLookahead === 'number') lookahead.value = clamp(saved.loudnessLookahead, 0.1, 80)
  if (typeof saved?.loudnessRelease === 'number') release.value = clamp(saved.loudnessRelease, 10, 5000)
  restored = true
})

watch([measurement, target, tolerance, maxTruePeak, useLimiter, lookahead, release], () => {
  if (!restored) {
    editedBeforeRestore = true
    return
  }
  persist()
})

function persist() {
  void updateAudioToolsState({
    loudnessMeasurement: measurement.value,
    loudnessTarget: target.value ?? undefined,
    loudnessTolerance: tolerance.value ?? undefined,
    loudnessMaxTruePeak: maxTruePeak.value ?? undefined,
    loudnessUseLimiter: useLimiter.value,
    loudnessLookahead: lookahead.value ?? undefined,
    loudnessRelease: release.value ?? undefined,
  })
}

function clamp(value: number, min: number, max: number) { return Math.min(max, Math.max(min, value)) }
function selectMeasurement(value: LoudnessMeasurement) {
  if (value === measurement.value) return
  measurement.value = value
  const definition = measurementDefinitions.find(item => item.value === value)
  if (definition) target.value = definition.defaultTarget
}
function fileName(path: string) { return path.split(/[\\/]/).filter(Boolean).pop() || path }
function addInputs(paths: string[]) {
  const current = new Set(inputs.value)
  inputs.value = [...inputs.value, ...paths.filter(path => path && !current.has(path))]
}
async function pickFiles() { addInputs(await invoke<string[]>('pick_audio_files') || []) }
async function pickFolder() {
  const folder = await invoke<string | null>('pick_input_folder')
  if (!folder) return
  const result = await invoke<ScanResult>('scan_audio_paths_with_options', { paths: [folder], recursive: true, sortFiles: true })
  const before = inputs.value.length
  addInputs(result.files || [])
  const added = inputs.value.length - before
  if (added) message.success(t('tools.folderScanned', { count: added }))
  else message.info(t(result.files?.length ? 'tools.folderAlreadyAdded' : 'tools.folderEmpty'))
  if (result.warnings?.length) message.warning(t('tools.folderScanWarnings', { count: result.warnings.length }))
}
async function pickOutput() {
  const value = await invoke<string | null>('pick_output_folder')
  if (value) outputDir.value = value
}
async function pickReference() {
  const value = await invoke<string | null>('pick_single_audio_file')
  if (value) referencePath.value = value
}
function run() {
  if ((showsReference.value && !referencePath.value) || !parametersComplete.value) return
  void runtime.execute('loudness', {
    inputs: inputs.value,
    outputDir: outputDir.value,
    measurement: measurement.value,
    target: target.value,
    tolerance: tolerance.value,
    maxTruePeak: maxTruePeak.value,
    useLimiter: useLimiter.value,
    lookahead: lookahead.value,
    release: release.value,
    referencePath: referencePath.value,
  })
}
function measurementLabel(value: string) {
  const resolved = measurementAliases[value] || value
  return t(measurementDefinitions.find(item => item.value === resolved)?.labelKey || 'tools.loudnessMeasurement')
}
function levelLabel(value: string) {
  return t(value === 'true-peak' ? 'tools.loudnessPeakLevel' : value === 'peak' ? 'tools.loudnessPeakAmplitude' : 'tools.loudnessTarget')
}
</script>

<template>
  <AudioToolPanel tool="loudness" :status-title="t('tools.processingResult')" :status-hint="t('tools.loudnessHint')">
    <AudioToolHeader :icon="VolumeHighOutline" :title="t('tools.loudnessTitle')" :description="t('tools.loudnessDescription')" />

    <div data-guide="tool-input" class="audio-tool-field">
      <div class="audio-tool-field-heading"><label>{{ t('tools.inputFiles') }}</label><span>{{ t('tools.selectedCount', { count: inputs.length }) }}</span></div>
      <div class="audio-tool-picker-grid">
        <button class="audio-tool-picker" :disabled="state.anyBusy" @click="pickFiles"><n-icon :component="DocumentOutline" size="20" />{{ inputs.length ? t('tools.addFiles') : t('tools.chooseFiles') }}</button>
        <button class="audio-tool-picker" :disabled="state.anyBusy" @click="pickFolder"><n-icon :component="FolderOpenOutline" size="20" />{{ t('tools.addInputFolder') }}</button>
      </div>
      <div v-if="inputs.length" class="audio-tool-files"><div v-for="path in inputs" :key="path"><span :title="path">{{ fileName(path) }}</span><button :title="t('tools.removeFile')" :disabled="state.anyBusy" @click="inputs = inputs.filter(item => item !== path)"><n-icon :component="CloseCircleOutline" /></button></div></div>
    </div>

    <div class="audio-tool-field"><label>{{ t('tools.outputDirectory') }}</label><div class="audio-tool-path"><n-input :value="outputDir" readonly :placeholder="t('tools.chooseOutputDirectory')" /><n-button secondary :disabled="state.anyBusy" @click="pickOutput"><template #icon><n-icon :component="FolderOpenOutline" /></template>{{ t('common.browse') }}</n-button></div></div>

    <div class="audio-tool-field">
      <label>{{ t('tools.loudnessMeasurement') }}</label>
      <n-select :value="measurement" :options="measurementOptions" :disabled="state.anyBusy" @update:value="selectMeasurement" />
      <small class="audio-tool-hint">{{ parameterHint }}</small>
    </div>

    <div v-if="showsReference" class="audio-tool-field">
      <label>{{ t('tools.loudnessReferenceFile') }}</label>
      <div class="audio-tool-path"><n-input :value="referencePath" readonly :placeholder="t('tools.chooseAudio')" /><n-button secondary :disabled="state.anyBusy" @click="pickReference"><template #icon><n-icon :component="DocumentOutline" /></template>{{ t('common.browse') }}</n-button></div>
    </div>

    <div class="audio-tool-grid audio-tool-grid--three">
      <div v-if="usesTarget" class="audio-tool-field"><label>{{ targetLabel }}</label><n-input-number v-model:value="target" :min="-70" :max="targetMax" :precision="2" :disabled="state.anyBusy"><template #suffix>{{ targetUnit }}</template></n-input-number><div v-if="targetUnit === 'LUFS'" class="loudness-presets"><n-button v-for="preset in loudnessPresets" :key="preset.labelKey" size="tiny" secondary :type="target === preset.target ? 'primary' : 'default'" :disabled="state.anyBusy" @click="target = preset.target">{{ t(preset.labelKey) }}</n-button></div></div>
      <div class="audio-tool-field"><label>{{ t('tools.loudnessTolerance') }}</label><n-input-number v-model:value="tolerance" :min="0" :max="20" :precision="2" :disabled="state.anyBusy"><template #suffix>{{ targetUnit === 'LUFS' ? 'LU' : 'dB' }}</template></n-input-number></div>
      <div v-if="showsTruePeak" class="audio-tool-field"><label>{{ t('tools.loudnessMaxTruePeak') }}</label><n-input-number v-model:value="maxTruePeak" :min="-9" :max="0" :precision="2" :disabled="state.anyBusy"><template #suffix>dBTP</template></n-input-number></div>
    </div>

    <div v-if="showsLimiter" class="audio-tool-field">
      <n-checkbox v-model:checked="useLimiter" :disabled="state.anyBusy">{{ t('tools.loudnessUseLimiter') }}</n-checkbox>
      <div v-if="useLimiter" class="audio-tool-grid audio-tool-grid--two audio-tool-limiter-fields">
        <div class="audio-tool-field"><label>{{ t('tools.loudnessLookahead') }}</label><n-input-number v-model:value="lookahead" :min="0.1" :max="80" :precision="1" :disabled="state.anyBusy"><template #suffix>ms</template></n-input-number></div>
        <div class="audio-tool-field"><label>{{ t('tools.loudnessRelease') }}</label><n-input-number v-model:value="release" :min="10" :max="5000" :precision="0" :disabled="state.anyBusy"><template #suffix>ms</template></n-input-number></div>
      </div>
    </div>

    <n-alert type="info" :bordered="false">{{ t('tools.loudnessOutputHint') }}</n-alert>
    <div class="audio-tool-actions"><n-button type="primary" :loading="state.busy" :disabled="state.anyBusy || !inputs.length || !outputDir || (showsReference && !referencePath) || !parametersComplete" @click="run">{{ t('tools.startLoudness') }}</n-button></div>

    <template #result="{ result }">
      <section v-if="result.operation === 'loudness'" class="tool-result-stack">
        <div class="tool-result-grid"><div><span>{{ t('tools.outputCount') }}</span><strong>{{ result.succeeded ?? 0 }}</strong></div><div><span>{{ t('tools.loudnessMeasurement') }}</span><strong>{{ measurementLabel(result.measurement) }}</strong></div></div>
        <n-alert v-if="result.warnings?.includes('loudness_target_not_reached')" type="warning" :bordered="false" role="status">{{ t('tools.loudnessTargetNotReached') }}</n-alert>
        <n-alert v-if="result.warnings?.includes('loudness_true_peak_exceeded')" type="warning" :bordered="false" role="status">{{ t('tools.loudnessTruePeakExceeded') }}</n-alert>
        <ul v-if="result.measurements?.length" class="loudness-measurements">
          <li v-for="item in result.measurements" :key="item.path" :class="{ 'loudness-measurements--warning': !item.withinTolerance || item.withinTruePeakLimit === false }">
            <strong :title="item.path">{{ fileName(item.path) }}</strong>
            <span>{{ levelLabel(result.measurement) }}: {{ item.target.toFixed(2) }} {{ item.unit }}</span>
            <span>{{ t('tools.loudnessMeasuredOutput') }}: {{ item.outputLevel.toFixed(2) }} {{ item.unit }}</span>
            <span v-if="item.outputTruePeak !== undefined && item.maxTruePeak !== undefined">{{ t('tools.loudnessMeasuredTruePeak') }}: {{ item.outputTruePeak === null ? '−∞' : item.outputTruePeak.toFixed(2) }} dBTP ({{ t('tools.loudnessMaxTruePeak') }}: {{ item.maxTruePeak.toFixed(2) }} dBTP)</span>
          </li>
        </ul>
        <div v-if="result.failed?.length" class="loudness-failures" role="status"><strong>{{ t('tools.skippedCount', { count: result.failed.length }) }}</strong><span v-for="failure in result.failed.slice(0, 5)" :key="failure.path" :title="failure.message">{{ fileName(failure.path) }}：{{ failure.message }}</span></div>
        <n-button v-if="result.outputDir" secondary size="small" @click="runtime.revealPath(result.outputDir)">{{ t('tools.openOutputLocation') }}</n-button>
      </section>
    </template>
  </AudioToolPanel>
</template>

<style scoped>
.audio-tool-limiter-fields { margin-top: 12px; }
.loudness-presets { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
.loudness-measurements { display: grid; gap: 12px; margin: 0; padding: 0; list-style: none; font-size: 12px; }
.loudness-measurements li { display: grid; gap: 4px; overflow-wrap: anywhere; }
.loudness-measurements span { color: var(--on-surface-muted); font-variant-numeric: tabular-nums; }
.loudness-measurements .loudness-measurements--warning span { color: var(--warning); }
.loudness-failures { display: grid; gap: 4px; color: var(--warning); font-size: 11px; }
.loudness-failures span { color: color-mix(in srgb, var(--warning) 65%, var(--on-surface-muted)); line-height: 1.55; overflow-wrap: anywhere; }
</style>
