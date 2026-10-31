<script setup lang="ts">
import { toolRef } from '@/sdk'
import { writeClipboardText } from '@/sdk'

import { ref } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { CopyOutline, DocumentTextOutline, FolderOpenOutline, InformationCircleOutline, LockClosedOutline } from '@vicons/ionicons5'
import AudioToolPanel from '@/features/audio-tools/shared/AudioToolPanel.vue'
import AudioToolHeader from '@/features/audio-tools/shared/AudioToolHeader.vue'
import { useAudioToolRuntime } from '@/features/audio-tools/runtime'
import type { AudioStreamInfo } from '@/features/audio-tools/types'
const { t } = useI18n(); const message = useMessage(); const runtime = useAudioToolRuntime(); const state = runtime.stateFor('inspect'); const inputPath = toolRef('inputPath', '')
async function inspect(path: string) { await runtime.execute('inspect', { inputPath: path }) }
async function pick() { const path = await invoke<string | null>('pick_single_audio_file'); if (path) { inputPath.value = path; await inspect(path) } }
function run() { void inspect(inputPath.value) }
function size(value: number) { const units = ['B', 'KB', 'MB', 'GB']; let amount = value; let unit = 0; while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1 } return `${amount.toFixed(unit ? 2 : 0)} ${units[unit]}` }
function duration(value: number | null) { if (value == null) return '—'; const seconds = Math.max(0, Math.round(value)); return `${String(Math.floor(seconds / 3600)).padStart(2, '0')}:${String(Math.floor(seconds % 3600 / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}` }
function bitrate(value: number | null) { return value == null ? '—' : `${Math.round(value / 1000)} kbps` }
function streamSummary(stream: AudioStreamInfo) { return [stream.codec.toUpperCase(), stream.sampleRate ? `${stream.sampleRate} Hz` : '', stream.channels ? `${stream.channels} ch` : '', stream.channelLayout].filter(Boolean).join(' · ') }
async function copyRaw(value: unknown) { try { await writeClipboardText(JSON.stringify(value, null, 2)); message.success(t('tools.inspectCopied')) } catch { message.error(t('tools.inspectCopyFailed')) } }
</script>
<template><AudioToolPanel tool="inspect" :status-title="t('tools.analysisResult')" :status-hint="t('tools.inspectHint')">
  <AudioToolHeader :icon="InformationCircleOutline" :title="t('tools.inspectTitle')" :description="t('tools.inspectDescription')" />
  <div data-guide="tool-input" class="audio-tool-field inspect-picker"><label>{{ t('tools.inputFile') }}</label><div class="audio-tool-path"><n-input :value="inputPath" readonly :placeholder="t('tools.chooseAudio')" :title="inputPath" /><n-button secondary :disabled="state.anyBusy" @click="pick"><template #icon><n-icon :component="FolderOpenOutline" /></template>{{ t('common.browse') }}</n-button><n-button type="primary" :loading="state.busy" :disabled="state.anyBusy || !inputPath" @click="run"><template #icon><n-icon :component="DocumentTextOutline" /></template>{{ t('tools.startInspect') }}</n-button></div><small class="inspect-privacy"><n-icon :component="LockClosedOutline" />{{ t('tools.inspectPrivacyHint') }}</small></div>
  <template #result="{ result }"><section v-if="result.operation === 'inspect'" class="tool-result-stack inspect-result">
    <header class="inspect-result__header"><strong :title="result.inputPath">{{ result.fileName }}</strong></header>
    <div class="tool-result-grid"><div><span>{{ t('tools.fileSize') }}</span><strong>{{ size(result.fileSize) }}</strong></div><div><span>{{ t('tools.duration') }}</span><strong>{{ duration(result.format.duration) }}</strong></div><div><span>{{ t('tools.containerFormat') }}</span><strong>{{ result.format.longName || result.format.name || '—' }}</strong></div><div><span>{{ t('tools.overallBitrate') }}</span><strong>{{ bitrate(result.format.bitRate) }}</strong></div></div>
    <div v-for="stream in result.audioStreams" :key="String(stream.index)" class="tool-result-section"><strong>{{ t('tools.audioStream', { index: stream.index ?? 0 }) }}</strong><span>{{ streamSummary(stream) }}</span><small>{{ stream.sampleFormat || '—' }} · {{ stream.bitsPerSample || '—' }} bit · {{ bitrate(stream.bitRate) }}</small></div>
    <n-collapse><n-collapse-item :title="t('tools.metadataTags')" name="tags"><div class="tool-tag-list"><template v-for="(value, key) in result.format.tags" :key="key"><span>{{ key }}</span><strong>{{ value }}</strong></template><small v-if="!Object.keys(result.format.tags).length">{{ t('tools.noMetadata') }}</small></div></n-collapse-item><n-collapse-item :title="t('tools.rawProbeData')" name="raw"><n-button text size="small" @click="copyRaw(result.raw)"><template #icon><n-icon :component="CopyOutline" /></template>{{ t('common.copy') }}</n-button><pre class="tool-result-json">{{ JSON.stringify(result.raw, null, 2) }}</pre></n-collapse-item></n-collapse>
  </section></template>
</AudioToolPanel></template>

<style scoped>
.inspect-picker {
  margin-bottom: 0;
}

.inspect-privacy {
  display: flex;
  align-items: center;
  gap: 5px;
  margin-top: 8px;
  color: var(--on-surface-muted);
  font-size: 11px;
  line-height: 1.5;
}

.inspect-privacy .n-icon {
  flex: 0 0 auto;
}

.inspect-result.tool-result-stack {
  padding: 0;
  border: 0;
  background: transparent;
}

.inspect-result__header {
  min-width: 0;
  padding-bottom: 10px;
  border-bottom: 1px solid color-mix(in srgb, var(--outline) 60%, transparent);
}

.inspect-result__header strong {
  display: block;
  overflow: hidden;
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 680px) {
  .inspect-picker .audio-tool-path {
    flex-wrap: wrap;
  }

  .inspect-picker .audio-tool-path .n-input {
    flex-basis: 100%;
  }
}
</style>
