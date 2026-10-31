<script setup lang="ts">
import { isStudioHost, toolRef } from '@/sdk'

import { computed, ref, watch } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { open } from '@tauri-apps/plugin-shell'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { CloudDownloadOutline, DocumentOutline, FolderOpenOutline, MusicalNoteOutline, PulseOutline } from '@vicons/ionicons5'
import { useSettingsStore } from '@/stores/settings'
import AudioToolPanel from '@/features/audio-tools/shared/AudioToolPanel.vue'
import AudioToolHeader from '@/features/audio-tools/shared/AudioToolHeader.vue'
import { errorMessage, useAudioToolRuntime } from '@/features/audio-tools/runtime'
const GAME_URL = 'https://github.com/openvpi/GAME/releases/tag/v1.0.0'
const { t } = useI18n(); const message = useMessage(); const settings = useSettingsStore(); const runtime = useAudioToolRuntime(); const state = runtime.stateFor('midi')
type Language = '' | 'zh' | 'yue' | 'ja' | 'en'
const inputPath = toolRef('inputPath', '')
const modelPath = toolRef('modelPath', '')
const outputDir = toolRef('outputDir', settings.outputDir)
const bpm = toolRef<number | null>('bpm', 120)
const language = toolRef<Language>('language', '')
const modelAlias = toolRef('modelAlias', 'medium')
const useLocalMlxModel = toolRef('useLocalMlxModel', false)
const isAppleSilicon = computed(() => settings.platform === 'darwin' && ['arm64', 'aarch64'].includes(settings.machine.toLowerCase()))
const modelAliases = ['small', 'medium', 'large'].map(value => ({ label: value, value }))
const detectingBpm = ref(false)
const modelReady = computed(() => isAppleSilicon.value
  ? (useLocalMlxModel.value ? Boolean(modelPath.value) : modelAliases.some(option => option.value === modelAlias.value))
  : modelPath.value.toLowerCase().endsWith('.pt'))
const languages = computed(() => [{ label: t('tools.midiLanguageAuto'), value: '' }, { label: t('tools.midiLanguageChinese'), value: 'zh' }, { label: t('tools.midiLanguageCantonese'), value: 'yue' }, { label: t('tools.midiLanguageJapanese'), value: 'ja' }, { label: t('tools.midiLanguageEnglish'), value: 'en' }])
const validBpm = computed(() => typeof bpm.value === 'number' && Number.isFinite(bpm.value) && bpm.value >= 30 && bpm.value <= 300)
const looksLikeAccompaniment = computed(() => /(?:instrumental|accompaniment|no[_ -]?vocals?|伴奏|无人声)/i.test(fileName(inputPath.value)))
function fileName(path: string) { return path.split(/[\\/]/).filter(Boolean).pop() || path }
async function pickAudio() { const path = await invoke<string | null>('pick_single_audio_file'); if (path) inputPath.value = path }
async function pickOutput() { const path = await invoke<string | null>('pick_output_folder'); if (path) outputDir.value = path }
async function pickModel() {
  const path = await invoke<string | null>(isAppleSilicon.value ? 'pick_input_folder' : 'pick_model_weights_file', { title: t('tools.midiModelPickerTitle') })
  if (!path) return
  if (!isAppleSilicon.value && !path.toLowerCase().endsWith('.pt')) return void message.error(t('tools.midiModelExtensionError'))
  modelPath.value = path
}
async function openRelease() { try { if (typeof window !== 'undefined' && !(isStudioHost())) window.open(GAME_URL, '_blank', 'noopener,noreferrer'); else await open(GAME_URL) } catch (error) { message.error(errorMessage(error)) } }
async function detectBpm() {
  const path = inputPath.value
  if (!path || state.value.anyBusy || detectingBpm.value) return
  detectingBpm.value = true
  try {
    const result = await runtime.execute('midi', { action: 'detect_bpm', inputs: [path] })
    if (inputPath.value === path && result?.operation === 'midi' && result.action === 'detect_bpm') bpm.value = result.roundedBpm
  } finally {
    detectingBpm.value = false
  }
}
function run() {
  if (!validBpm.value || !modelReady.value || detectingBpm.value) return
  void runtime.execute('midi', {
    inputPath: inputPath.value, modelPath: isAppleSilicon.value && !useLocalMlxModel.value ? '' : modelPath.value, outputDir: outputDir.value,
    modelAlias: modelAlias.value,
    bpm: bpm.value, language: language.value,
  })
}
watch(() => settings.outputDir, value => { if (!outputDir.value) outputDir.value = value })
</script>
<template><AudioToolPanel tool="midi" :status-title="t('tools.processingResult')" :status-hint="t('tools.midiHint')">
  <AudioToolHeader :icon="MusicalNoteOutline" :title="t('tools.midiTitle')" :description="t('tools.midiDescription')" />
  <div data-guide="tool-input" class="audio-tool-field"><label>{{ t('tools.vocalAudio') }}</label><div class="audio-tool-path"><n-input :value="inputPath" readonly :placeholder="t('tools.chooseVocalAudio')" /><n-button secondary :disabled="state.anyBusy || detectingBpm" @click="pickAudio"><template #icon><n-icon :component="DocumentOutline" /></template>{{ t('common.browse') }}</n-button></div><n-alert v-if="looksLikeAccompaniment" class="audio-tool-inline-alert" type="warning" :bordered="false">{{ t('tools.midiInputLooksLikeAccompaniment') }}</n-alert></div>
  <div class="audio-tool-field">
    <div class="audio-tool-field-heading">
      <label>{{ t('tools.midiModel') }}</label>
      <a v-if="!isAppleSilicon" :href="GAME_URL" target="_blank" rel="noopener noreferrer" @click.prevent="openRelease"><n-icon :component="CloudDownloadOutline" />{{ t('tools.midiModelDownload') }}</a>
    </div>
    <template v-if="isAppleSilicon">
      <n-checkbox v-model:checked="useLocalMlxModel" :disabled="state.anyBusy">{{ t('tools.midiMlxLocalModel') }}</n-checkbox>
      <n-select v-if="!useLocalMlxModel" v-model:value="modelAlias" :options="modelAliases" :disabled="state.anyBusy" />
      <small class="audio-tool-hint">{{ t('tools.midiMlxModelHint') }}</small>
    </template>
    <button v-if="!isAppleSilicon || useLocalMlxModel" class="audio-tool-model-picker" :class="{ selected: modelReady }" :disabled="state.anyBusy" @click="pickModel">
      <n-icon :component="DocumentOutline" size="21" />
      <span>
        <strong>{{ modelPath ? fileName(modelPath) : t(isAppleSilicon ? 'tools.midiMlxChooseDirectory' : 'tools.chooseMidiModel') }}</strong>
        <small :title="modelPath">{{ modelPath || t(isAppleSilicon ? 'tools.midiMlxChooseDirectory' : 'tools.midiModelChooseHint') }}</small>
      </span>
      <em>{{ modelPath ? t('tools.reselectModel') : t('common.browse') }}</em>
    </button>
    <small v-if="!isAppleSilicon" class="audio-tool-hint">{{ t('tools.midiModelHint') }}</small>
  </div>
  <div class="audio-tool-grid"><div class="audio-tool-field"><label>{{ t('tools.bpm') }}</label><div class="audio-tool-path"><n-input-number v-model:value="bpm" :min="30" :max="300" :disabled="state.anyBusy || detectingBpm" /><n-button secondary :loading="detectingBpm" :disabled="state.anyBusy || !inputPath" :title="t('tools.bpmDetectHint')" @click="detectBpm"><template #icon><n-icon :component="PulseOutline" /></template>{{ t('tools.bpmDetect') }}</n-button></div></div><div class="audio-tool-field"><label>{{ t('tools.midiLanguage') }}</label><n-select v-model:value="language" :options="languages" :disabled="state.anyBusy" /></div></div>
  <div class="audio-tool-field"><label>{{ t('tools.outputDirectory') }}</label><div class="audio-tool-path"><n-input :value="outputDir" readonly /><n-button secondary :disabled="state.anyBusy" @click="pickOutput"><template #icon><n-icon :component="FolderOpenOutline" /></template>{{ t('common.browse') }}</n-button></div></div>
  <n-alert type="warning" :bordered="false">{{ t('tools.midiAccuracyHint') }}</n-alert>
  <div class="audio-tool-actions"><n-button type="primary" :loading="state.busy" :disabled="state.anyBusy || detectingBpm || !inputPath || !modelReady || !outputDir || !validBpm" @click="run">{{ t('tools.startMidi') }}</n-button></div>
</AudioToolPanel></template>
