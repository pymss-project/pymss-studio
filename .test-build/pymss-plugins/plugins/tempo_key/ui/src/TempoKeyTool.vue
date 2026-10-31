<script setup lang="ts">
import { toolRef } from '@/sdk'
import { writeClipboardText } from '@/sdk'

import { ref } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { CloseCircleOutline, CopyOutline, DocumentOutline, FolderOpenOutline, PulseOutline } from '@vicons/ionicons5'
import AudioToolPanel from '@/features/audio-tools/shared/AudioToolPanel.vue'
import AudioToolHeader from '@/features/audio-tools/shared/AudioToolHeader.vue'
import { useAudioToolRuntime } from '@/features/audio-tools/runtime'
import type { TempoKeyItem } from '@/features/audio-tools/types'

type ScanResult = { files: string[]; warnings: string[] }
const { t } = useI18n()
const message = useMessage()
const runtime = useAudioToolRuntime()
const state = runtime.stateFor('tempo_key')
const inputs = toolRef<string[]>('inputs', [])

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
function run() { void runtime.execute('tempo_key', { inputs: inputs.value }) }
// rba reports libKeyFinder names ("Bb minor"); "silence" means no tonal content.
function keyLabel(key: string) { return key === 'silence' ? t('tools.tempo_keySilence') : key }
function duration(value: number) {
  const seconds = Math.max(0, Math.round(value))
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}
async function copyItems(items: TempoKeyItem[]) {
  const text = items.map(item => `${item.fileName}\t${item.bpm.toFixed(2)}\t${keyLabel(item.key)}`).join('\n')
  try { await writeClipboardText(text); message.success(t('tools.tempo_keyCopied')) } catch { message.error(t('tools.inspectCopyFailed')) }
}
</script>

<template>
  <AudioToolPanel tool="tempo_key" :status-title="t('tools.analysisResult')" :status-hint="t('tools.tempo_keyHint')">
    <AudioToolHeader :icon="PulseOutline" :title="t('tools.tempo_keyTitle')" :description="t('tools.tempo_keyDescription')" />

    <div data-guide="tool-input" class="audio-tool-field">
      <div class="audio-tool-field-heading"><label>{{ t('tools.inputFiles') }}</label><span>{{ t('tools.selectedCount', { count: inputs.length }) }}</span></div>
      <div class="audio-tool-picker-grid">
        <button class="audio-tool-picker" :disabled="state.anyBusy" @click="pickFiles"><n-icon :component="DocumentOutline" size="20" />{{ inputs.length ? t('tools.addFiles') : t('tools.chooseFiles') }}</button>
        <button class="audio-tool-picker" :disabled="state.anyBusy" @click="pickFolder"><n-icon :component="FolderOpenOutline" size="20" />{{ t('tools.addInputFolder') }}</button>
      </div>
      <div v-if="inputs.length" class="audio-tool-files"><div v-for="path in inputs" :key="path"><span :title="path">{{ fileName(path) }}</span><button :title="t('tools.removeFile')" :disabled="state.anyBusy" @click="inputs = inputs.filter(item => item !== path)"><n-icon :component="CloseCircleOutline" /></button></div></div>
    </div>

    <small class="audio-tool-hint">{{ t('tools.tempo_keyEngineHint') }}</small>
    <div class="audio-tool-actions"><n-button type="primary" :loading="state.busy" :disabled="state.anyBusy || !inputs.length" @click="run">{{ t('tools.tempo_keyStart') }}</n-button></div>

    <template #result="{ result }">
      <section v-if="result.operation === 'tempo_key'" class="tool-result-stack">
        <div class="tempo-key-table" role="table">
          <div class="tempo-key-row tempo-key-row--head" role="row"><span>{{ t('tools.inputFile') }}</span><span>BPM</span><span>{{ t('tools.tempo_keyKey') }}</span></div>
          <div v-for="item in result.items" :key="item.path" class="tempo-key-row" role="row">
            <span :title="item.path">{{ item.fileName }}<small>{{ duration(item.duration) }}</small></span>
            <strong>{{ item.bpm.toFixed(2) }}</strong>
            <strong>{{ keyLabel(item.key) }}</strong>
          </div>
        </div>
        <n-button text size="small" @click="copyItems(result.items)"><template #icon><n-icon :component="CopyOutline" /></template>{{ t('common.copy') }}</n-button>
        <div v-if="result.failed?.length" class="tempo-key-failures" role="status"><strong>{{ t('tools.skippedCount', { count: result.failed.length }) }}</strong><span v-for="failure in result.failed.slice(0, 5)" :key="failure.path">{{ fileName(failure.path) }}: {{ failure.message }}</span></div>
      </section>
    </template>
  </AudioToolPanel>
</template>

<style scoped>
.tempo-key-table { display: grid; gap: 2px; font-size: 12px; }
.tempo-key-row { display: grid; grid-template-columns: minmax(0, 1fr) 64px 96px; gap: 8px; align-items: center; padding: 6px 0; border-bottom: 1px solid color-mix(in srgb, var(--outline) 45%, transparent); }
.tempo-key-row--head { color: var(--on-surface-muted); font-size: 11px; }
.tempo-key-row span { display: flex; min-width: 0; gap: 6px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tempo-key-row small { color: var(--on-surface-muted); }
.tempo-key-row strong { font-variant-numeric: tabular-nums; }
.tempo-key-failures { display: grid; gap: 4px; color: var(--warning); font-size: 11px; }
.tempo-key-failures span { overflow-wrap: anywhere; }
</style>
