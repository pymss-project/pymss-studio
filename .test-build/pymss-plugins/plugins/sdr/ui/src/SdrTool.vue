<script setup lang="ts">
import { toolRef } from '@/sdk'
import { ref } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { useI18n } from 'vue-i18n'
import { AnalyticsOutline, DocumentOutline } from '@vicons/ionicons5'
import AudioToolPanel from '@/features/audio-tools/shared/AudioToolPanel.vue'
import AudioToolHeader from '@/features/audio-tools/shared/AudioToolHeader.vue'
import { useAudioToolRuntime } from '@/features/audio-tools/runtime'
const { t } = useI18n(); const runtime = useAudioToolRuntime(); const state = runtime.stateFor('sdr')
const referencePath = toolRef('referencePath', ''); const estimatedPath = toolRef('estimatedPath', '')
async function pick(target: 'reference' | 'estimated') { const path = await invoke<string | null>('pick_single_audio_file'); if (!path) return; if (target === 'reference') referencePath.value = path; else estimatedPath.value = path }
function run() { void runtime.execute('sdr', { referencePath: referencePath.value, estimatedPath: estimatedPath.value }) }
</script>
<template><AudioToolPanel tool="sdr" :status-title="t('tools.analysisResult')" :status-hint="t('tools.sdrResultHint')">
  <AudioToolHeader :icon="AnalyticsOutline" :title="t('tools.sdrTitle')" :description="t('tools.sdrDescription')" />
  <div data-guide="tool-input" class="audio-tool-field"><label>{{ t('tools.referenceAudio') }}</label><div class="audio-tool-path"><n-input :value="referencePath" readonly :placeholder="t('tools.chooseReferenceAudio')" /><n-button secondary :disabled="state.anyBusy" @click="pick('reference')"><template #icon><n-icon :component="DocumentOutline" /></template>{{ t('common.browse') }}</n-button></div></div>
  <div class="audio-tool-field"><label>{{ t('tools.estimatedAudio') }}</label><div class="audio-tool-path"><n-input :value="estimatedPath" readonly :placeholder="t('tools.chooseEstimatedAudio')" /><n-button secondary :disabled="state.anyBusy" @click="pick('estimated')"><template #icon><n-icon :component="DocumentOutline" /></template>{{ t('common.browse') }}</n-button></div></div>
  <n-alert type="info" :bordered="false">{{ t('tools.sdrHint') }}</n-alert>
  <div class="audio-tool-actions"><n-button type="primary" :loading="state.busy" :disabled="state.anyBusy || !referencePath || !estimatedPath" @click="run">{{ t('tools.startSdr') }}</n-button></div>
</AudioToolPanel></template>
