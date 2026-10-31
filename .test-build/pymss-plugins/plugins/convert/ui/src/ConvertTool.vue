<script setup lang="ts">
import { computed, watch } from 'vue'
import { toolRef } from '@/sdk'
import { invoke } from '@tauri-apps/api/core'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { CloseCircleOutline, DocumentOutline, FolderOpenOutline, SwapHorizontalOutline } from '@vicons/ionicons5'
import { useSettingsStore } from '@/stores/settings'
import AudioToolPanel from '@/features/audio-tools/shared/AudioToolPanel.vue'
import AudioToolHeader from '@/features/audio-tools/shared/AudioToolHeader.vue'
import { useAudioToolRuntime } from '@/features/audio-tools/runtime'

type ScanResult = { files: string[]; warnings: string[] }
const { t } = useI18n()
const message = useMessage()
const settings = useSettingsStore()
const runtime = useAudioToolRuntime()
const state = runtime.stateFor('convert')
const inputs = toolRef<string[]>('inputs', [], 'files')
const outputDir = toolRef('outputDir', settings.outputDir, 'output_dir')
const outputFormat = toolRef('outputFormat', 'wav', 'format')
const sampleRate = toolRef('sampleRate', 44100, 'sample_rate')
const channels = toolRef('channels', 2, 'channels')
const wavBitDepth = toolRef('wavBitDepth', 'PCM-24', 'wav_bit_depth')
const flacBitDepth = toolRef('flacBitDepth', '16-bit', 'flac_bit_depth')
const mp3BitRate = toolRef('mp3BitRate', '320k', 'mp3_bit_rate')
const oggBitRate = toolRef('oggBitRate', '320k', 'ogg_bit_rate')
const formatOptions = ['wav', 'flac', 'mp3', 'ogg'].map(value => ({ label: value.toUpperCase(), value }))
const sampleRateOptions = [32000, 44100, 48000].map(value => ({ label: `${value} Hz`, value }))
const channelOptions = computed(() => [{ label: t('tools.mono'), value: 1 }, { label: t('tools.stereo'), value: 2 }])
const wavOptions = ['PCM-16', 'PCM-24', 'PCM-32'].map(value => ({ label: value, value }))
const flacOptions = ['16-bit', '32-bit'].map(value => ({ label: value, value }))
const mp3Options = ['192k', '256k', '320k'].map(value => ({ label: value, value }))
const oggOptions = ['192k', '256k', '320k', '450k'].map(value => ({ label: value, value }))
watch(() => settings.outputDir, value => { if (!outputDir.value) outputDir.value = value })
function fileName(path: string) { return path.split(/[\\/]/).filter(Boolean).pop() || path }
function addInputs(paths: string[]) {
  const selected = [...new Set([...inputs.value, ...paths.filter(path => path.trim())])]
  if (selected.length > 4096) { message.error(t('tools.convertFileLimit')); return }
  inputs.value = selected
}
async function pickFiles() { addInputs(await invoke<string[]>('pick_audio_files') || []) }
async function pickFolder() {
  const folder = await invoke<string | null>('pick_input_folder'); if (!folder) return
  const result = await invoke<ScanResult>('scan_audio_paths_with_options', { paths: [folder], recursive: true, sortFiles: true })
  const before = inputs.value.length; addInputs(result.files || [])
  const added = inputs.value.length - before
  if (added) message.success(t('tools.folderScanned', { count: added }))
  else message.info(t(result.files?.length ? 'tools.folderAlreadyAdded' : 'tools.folderEmpty'))
  if (result.warnings?.length) message.warning(t('tools.folderScanWarnings', { count: result.warnings.length }))
}
async function pickOutput() { const value = await invoke<string | null>('pick_output_folder'); if (value) outputDir.value = value }
function run() { void runtime.execute('convert', { inputs: inputs.value, outputDir: outputDir.value, outputFormat: outputFormat.value, sampleRate: sampleRate.value, channels: channels.value, wavBitDepth: wavBitDepth.value, flacBitDepth: flacBitDepth.value, mp3BitRate: mp3BitRate.value, oggBitRate: oggBitRate.value }) }
</script>
<template>
  <AudioToolPanel tool="convert" :status-title="t('tools.processingResult')" :status-hint="t('tools.convertHint')">
    <AudioToolHeader :icon="SwapHorizontalOutline" :title="t('tools.convertTitle')" :description="t('tools.convertDescription')" />
    <div data-guide="tool-input" class="audio-tool-field"><div class="audio-tool-field-heading"><label>{{ t('tools.inputFiles') }}</label><span>{{ t('tools.selectedCount', { count: inputs.length }) }}</span></div>
      <div class="audio-tool-picker-grid"><button class="audio-tool-picker" :disabled="state.anyBusy" @click="pickFiles"><n-icon :component="DocumentOutline" size="20" />{{ inputs.length ? t('tools.addFiles') : t('tools.chooseFiles') }}</button><button class="audio-tool-picker" :disabled="state.anyBusy" @click="pickFolder"><n-icon :component="FolderOpenOutline" size="20" />{{ t('tools.addInputFolder') }}</button></div>
      <div v-if="inputs.length" class="audio-tool-files"><div v-for="path in inputs" :key="path"><span :title="path">{{ fileName(path) }}</span><button :title="t('tools.removeFile')" :disabled="state.anyBusy" @click="inputs = inputs.filter(item => item !== path)"><n-icon :component="CloseCircleOutline" /></button></div></div>
    </div>
    <div class="audio-tool-field"><label>{{ t('tools.outputDirectory') }}</label><div class="audio-tool-path"><n-input :value="outputDir" readonly :placeholder="t('tools.chooseOutputDirectory')" /><n-button secondary :disabled="state.anyBusy" @click="pickOutput"><template #icon><n-icon :component="FolderOpenOutline" /></template>{{ t('common.browse') }}</n-button></div></div>
    <div class="audio-tool-grid audio-tool-grid--four"><div class="audio-tool-field"><label>{{ t('tools.outputFormat') }}</label><n-select v-model:value="outputFormat" :options="formatOptions" :disabled="state.anyBusy" /></div><div class="audio-tool-field"><label>{{ t('tools.sampleRate') }}</label><n-select v-model:value="sampleRate" :options="sampleRateOptions" :disabled="state.anyBusy" /></div><div class="audio-tool-field"><label>{{ t('tools.channels') }}</label><n-select v-model:value="channels" :options="channelOptions" :disabled="state.anyBusy" /></div><div class="audio-tool-field"><label>{{ t('tools.quality') }}</label><n-select v-if="outputFormat === 'wav'" v-model:value="wavBitDepth" :options="wavOptions" :disabled="state.anyBusy" /><n-select v-else-if="outputFormat === 'flac'" v-model:value="flacBitDepth" :options="flacOptions" :disabled="state.anyBusy" /><n-select v-else-if="outputFormat === 'mp3'" v-model:value="mp3BitRate" :options="mp3Options" :disabled="state.anyBusy" /><n-select v-else v-model:value="oggBitRate" :options="oggOptions" :disabled="state.anyBusy" /></div></div>
    <n-alert v-if="channels === 2" type="info" :bordered="false">{{ t('tools.stereoFromMonoHint') }}</n-alert>
    <div class="audio-tool-actions"><n-button type="primary" :loading="state.busy" :disabled="state.anyBusy || !inputs.length || !outputDir" @click="run">{{ t('tools.startConvert') }}</n-button></div>
  </AudioToolPanel>
</template>
