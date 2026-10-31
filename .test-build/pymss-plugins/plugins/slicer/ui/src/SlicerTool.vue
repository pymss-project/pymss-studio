<script setup lang="ts">
import { toolRef } from '@/sdk'
import { computed, ref, watch } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { CloseCircleOutline, CutOutline, DocumentOutline, FolderOpenOutline } from '@vicons/ionicons5'
import { useSettingsStore } from '@/stores/settings'
import AudioToolPanel from '@/features/audio-tools/shared/AudioToolPanel.vue'
import AudioToolHeader from '@/features/audio-tools/shared/AudioToolHeader.vue'
import { useAudioToolRuntime } from '@/features/audio-tools/runtime'
type ScanResult = { files: string[]; warnings: string[] }
const { t } = useI18n(); const message = useMessage(); const settings = useSettingsStore(); const runtime = useAudioToolRuntime(); const state = runtime.stateFor('slicer')
const inputs = toolRef<string[]>('inputs', []); const outputDir = toolRef('outputDir', settings.outputDir); const outputFormat = toolRef('outputFormat', 'wav'); const threshold = toolRef<number | null>('threshold', -40); const minLength = toolRef<number | null>('minLength', 5000); const minInterval = toolRef<number | null>('minInterval', 300); const hopSize = toolRef<number | null>('hopSize', 10); const maxSilKept = toolRef<number | null>('maxSilKept', 500)
const formats = ['wav', 'flac', 'mp3'].map(value => ({ label: value.toUpperCase(), value }))
const valid = computed(() => {
  const values = [threshold.value, minLength.value, minInterval.value, hopSize.value, maxSilKept.value]
  if (!values.every((value): value is number => typeof value === 'number' && Number.isFinite(value))) return false
  const [level, length, interval, hop, kept] = values
  return [length, interval, hop, kept].every(Number.isInteger) && length >= interval && interval >= hop && hop > 0 && kept >= hop && level >= -100 && level <= 0
})
watch(() => settings.outputDir, value => { if (!outputDir.value) outputDir.value = value })
function fileName(path: string) { return path.split(/[\\/]/).filter(Boolean).pop() || path }
function add(paths: string[]) { const current = new Set(inputs.value); inputs.value = [...inputs.value, ...paths.filter(path => path && !current.has(path))] }
async function pickFiles() { add(await invoke<string[]>('pick_audio_files') || []) }
async function pickFolder() { const folder = await invoke<string | null>('pick_input_folder'); if (!folder) return; const result = await invoke<ScanResult>('scan_audio_paths_with_options', { paths: [folder], recursive: true, sortFiles: true }); const before = inputs.value.length; add(result.files || []); message.info(t(inputs.value.length > before ? 'tools.folderScanned' : 'tools.folderEmpty', { count: inputs.value.length - before })) }
async function pickOutput() { const path = await invoke<string | null>('pick_output_folder'); if (path) outputDir.value = path }
function run() { if (!valid.value) return void message.error(t('tools.slicerParameterError')); void runtime.execute('slicer', { inputs: inputs.value, outputDir: outputDir.value, outputFormat: outputFormat.value, threshold: threshold.value, minLength: minLength.value, minInterval: minInterval.value, hopSize: hopSize.value, maxSilKept: maxSilKept.value }) }
function seconds(value: number) { return `${value.toFixed(2)} s` }
</script>
<template><AudioToolPanel tool="slicer" :status-title="t('tools.processingResult')" :status-hint="t('tools.slicerHint')">
  <AudioToolHeader :icon="CutOutline" :title="t('tools.slicerTitle')" :description="t('tools.slicerDescription')" />
  <div data-guide="tool-input" class="audio-tool-field"><div class="audio-tool-field-heading"><label>{{ t('tools.inputFiles') }}</label><span>{{ t('tools.selectedCount', { count: inputs.length }) }}</span></div><div class="audio-tool-picker-grid"><button class="audio-tool-picker" :disabled="state.anyBusy" @click="pickFiles"><n-icon :component="DocumentOutline" size="20" />{{ t('tools.chooseFiles') }}</button><button class="audio-tool-picker" :disabled="state.anyBusy" @click="pickFolder"><n-icon :component="FolderOpenOutline" size="20" />{{ t('tools.addInputFolder') }}</button></div><div v-if="inputs.length" class="audio-tool-files"><div v-for="path in inputs" :key="path"><span :title="path">{{ fileName(path) }}</span><button :disabled="state.anyBusy" @click="inputs = inputs.filter(item => item !== path)"><n-icon :component="CloseCircleOutline" /></button></div></div></div>
  <div class="audio-tool-field"><label>{{ t('tools.outputDirectory') }}</label><div class="audio-tool-path"><n-input :value="outputDir" readonly /><n-button secondary :disabled="state.anyBusy" @click="pickOutput"><template #icon><n-icon :component="FolderOpenOutline" /></template>{{ t('common.browse') }}</n-button></div></div>
  <div class="audio-tool-grid audio-tool-grid--three"><div class="audio-tool-field"><label>{{ t('tools.silenceThreshold') }}</label><n-input-number v-model:value="threshold" :min="-100" :max="0"><template #suffix>dB</template></n-input-number></div><div class="audio-tool-field"><label>{{ t('tools.minimumClipLength') }}</label><n-input-number v-model:value="minLength" :min="10"><template #suffix>ms</template></n-input-number></div><div class="audio-tool-field"><label>{{ t('tools.minimumSilence') }}</label><n-input-number v-model:value="minInterval" :min="10"><template #suffix>ms</template></n-input-number></div><div class="audio-tool-field"><label>{{ t('tools.maximumKeptSilence') }}</label><n-input-number v-model:value="maxSilKept" :min="10"><template #suffix>ms</template></n-input-number></div><div class="audio-tool-field"><label>{{ t('tools.analysisHop') }}</label><n-input-number v-model:value="hopSize" :min="1"><template #suffix>ms</template></n-input-number></div><div class="audio-tool-field"><label>{{ t('tools.outputFormat') }}</label><n-select v-model:value="outputFormat" :options="formats" /></div></div>
  <n-alert v-if="!valid" type="error" :bordered="false">{{ t('tools.slicerParameterError') }}</n-alert>
  <div class="audio-tool-actions"><n-button type="primary" :loading="state.busy" :disabled="state.anyBusy || !valid || !inputs.length || !outputDir" @click="run">{{ t('tools.startSlicer') }}</n-button></div>
  <template #result="{ result }"><section v-if="result.operation === 'slicer'" class="tool-result-stack"><div class="tool-result-grid"><div><span>{{ t('tools.sourceFiles') }}</span><strong>{{ result.sourceCount }}</strong></div><div><span>{{ t('tools.generatedClips') }}</span><strong>{{ result.segments.length }}</strong></div><div><span>{{ t('tools.keptDuration') }}</span><strong>{{ seconds(result.keptDuration) }}</strong></div></div><div class="tool-result-list"><div v-for="segment in result.segments.slice(0, 8)" :key="segment.outputPath"><strong>{{ fileName(segment.outputPath) }}</strong><span>{{ seconds(segment.start) }} – {{ seconds(segment.end) }}</span></div><small v-if="result.segments.length > 8">+{{ result.segments.length - 8 }}</small></div><n-button v-if="result.outputDir" secondary size="small" @click="runtime.revealPath(result.outputDir)">{{ t('tools.openOutputLocation') }}</n-button></section></template>
</AudioToolPanel></template>
