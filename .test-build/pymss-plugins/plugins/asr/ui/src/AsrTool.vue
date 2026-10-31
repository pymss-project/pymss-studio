<script setup lang="ts">
import { toolRef } from '@/sdk'
import { isStudioHost } from '@/sdk'

import { createLogger } from '@/utils/logger'
import TaskLogConsole from '@/components/TaskLogConsole.vue'
import { normalizeLogRecord } from '@/utils/logRecord'

import { computed, nextTick, onActivated, onMounted, ref, watch } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { open } from '@tauri-apps/plugin-shell'
import { useI18n } from 'vue-i18n'
import { useDialog, useMessage } from 'naive-ui'
import {
  CloudDownloadOutline,
  DocumentOutline,
  FolderOpenOutline,
  MicOutline,
  TerminalOutline,
} from '@vicons/ionicons5'
import { useSettingsStore } from '@/stores/settings'
import AudioToolPanel from '@/features/audio-tools/shared/AudioToolPanel.vue'
import AudioToolHeader from '@/features/audio-tools/shared/AudioToolHeader.vue'
import {
  OptionalPackageCancelledError,
  useOptionalRuntimePackage,
} from '@/features/audio-tools/optionalRuntimePackage'
import { useAudioToolRuntime } from '@/features/audio-tools/runtime'
import { loadAudioToolsState, updateAudioToolsState } from '@/features/audio-tools/state'
import type { AudioToolRecovery } from '@/features/audio-tools/types'
import {
  ASR_LANGUAGE_KEYS,
  ASR_PRESETS,
  DEFAULT_ASR_PRESET,
  findAsrPreset,
  type AsrLanguageCode,
  type AsrPresetId,
} from './profiles'

const studioLog = createLogger('ui.features.audio-tools.tools.asr.AsrTool')

const FUNASR_URL = 'https://github.com/modelscope/FunASR'

type AsrModelMode = 'preset' | 'local'
type ModelTarget = 'model' | 'vad' | 'punc'

const { t, locale } = useI18n()
const dialog = useDialog()
const message = useMessage()
const settings = useSettingsStore()
const runtime = useAudioToolRuntime()
const state = runtime.stateFor('asr')
const funAsrRuntime = useOptionalRuntimePackage('funasr')
const {
  status: funAsrStatus,
  checking: dependencyChecking,
  busy: dependencyBusy,
  error: dependencyError,
  action: dependencyAction,
  cancelling: dependencyCancelling,
  logs: dependencyLogs,
  showLog: showDependencyLog,
} = funAsrRuntime

const inputPath = toolRef('inputPath', '')
const outputDir = toolRef('outputDir', settings.outputDir)
const hotword = toolRef('hotword', '')
const outputFormats = toolRef<string[]>('outputFormats', ['txt', 'json', 'srt'])
const modelMode = ref<AsrModelMode>('preset')
const presetId = ref<AsrPresetId>(DEFAULT_ASR_PRESET)
const language = ref<AsrLanguageCode>('zh')
const modelPath = ref('')
const vadModelPath = ref('')
const puncModelPath = ref('')
const repairingModels = ref(false)
const pendingRecoveryPayload = ref<Record<string, unknown> | null>(null)
const dependencyLogList = ref<HTMLElement | null>(null)
let restored = false
let initialActivation = true

const funAsrInstalled = computed(() => Boolean(funAsrStatus.value?.installed))
const funAsrCanUninstall = computed(() => Boolean((funAsrStatus.value?.present ?? funAsrStatus.value?.installed) && funAsrStatus.value?.canUninstall !== false))
const funAsrNeedsRepair = computed(() => Boolean(funAsrStatus.value?.present && !funAsrStatus.value?.installed))
const isAppleSilicon = computed(() => settings.platform === 'darwin' && /arm64|aarch64/i.test(settings.machine))
const availablePresets = computed(() => isAppleSilicon.value ? ASR_PRESETS.filter(preset => ['paraformer-zh', 'sensevoice-small'].includes(preset.id)) : ASR_PRESETS)
const selectedPreset = computed(() => availablePresets.value.find(preset => preset.id === presetId.value) || availablePresets.value[0])
const timestampOutputAvailable = computed(() => !isAppleSilicon.value && (modelMode.value === 'local' || selectedPreset.value.supportsTimestamps))
const formatOptions = computed(() => [
  { label: 'TXT', value: 'txt', disabled: false },
  { label: 'JSON', value: 'json', disabled: false },
  { label: 'SRT', value: 'srt', disabled: !timestampOutputAvailable.value },
])
const presetOptions = computed(() => availablePresets.value.map(preset => ({
  label: `${t(preset.titleKey)} · ${t(preset.scaleKey)}`,
  value: preset.id,
})))
const languageOptions = computed(() => selectedPreset.value.languages.map(value => ({
  label: t(ASR_LANGUAGE_KEYS[value]),
  value,
})))
const supportsHotwords = computed(() => !isAppleSilicon.value && (modelMode.value === 'local' || selectedPreset.value.supportsHotwords))
const localAuxiliaryComplete = computed(() => Boolean(vadModelPath.value && puncModelPath.value))
const modelConfigurationReady = computed(() => modelMode.value === 'preset' || Boolean(modelPath.value))
const canRun = computed(() => (
  funAsrInstalled.value
  && !dependencyChecking.value
  && !dependencyBusy.value
  && !repairingModels.value
  && Boolean(inputPath.value)
  && Boolean(outputDir.value)
  && outputFormats.value.length > 0
  && modelConfigurationReady.value
))

async function pickAudio() {
  const path = await invoke<string | null>('pick_single_audio_file')
  if (path) inputPath.value = path
}

async function pickOutput() {
  const path = await invoke<string | null>('pick_output_folder')
  if (path) outputDir.value = path
}

async function pickModel(target: ModelTarget) {
  const path = await invoke<string | null>('pick_input_folder')
  if (!path) return
  if (target === 'model') modelPath.value = path
  else if (target === 'vad') vadModelPath.value = path
  else puncModelPath.value = path
}

function clearModel(target: ModelTarget) {
  if (target === 'model') modelPath.value = ''
  else if (target === 'vad') vadModelPath.value = ''
  else puncModelPath.value = ''
}

function fileName(path: string) {
  return path.split(/[\\/]/).filter(Boolean).pop() || path
}

function languageLabel(value: unknown) {
  const key = ASR_LANGUAGE_KEYS[String(value || '') as AsrLanguageCode]
  return key ? t(key) : String(value || '—')
}

function resultModelLabel(value: unknown) {
  if (value === 'local') return t('tools.asrResultLocalModel')
  const preset = findAsrPreset(value)
  return preset ? t(preset.titleKey) : String(value || '—')
}

async function openFunAsr() {
  try {
    if (typeof window !== 'undefined' && !(isStudioHost())) {
      window.open(FUNASR_URL, '_blank', 'noopener,noreferrer')
    } else {
      await open(FUNASR_URL)
    }
  } catch (error) {
    message.error(String(error))
  }
}

async function refreshFunAsrStatus() {
  await funAsrRuntime.refresh()
}

async function manageFunAsr(action: 'install' | 'uninstall') {
  try {
    await funAsrRuntime.manage(action, {
      mirror: 'auto',
      locale: locale.value,
      startedMessage: action === 'install'
        ? t('tools.asrRuntimeInstallStarted')
        : t('tools.asrRuntimeUninstallStarted'),
    })
    const successText = action === 'install'
      ? t('tools.asrRuntimeInstallSuccess')
      : t('tools.asrRuntimeUninstallSuccess')
    funAsrRuntime.appendLog(successText, 'success')
    message.success(successText)
    showDependencyLog.value = false
  } catch (error) {
    if (error instanceof OptionalPackageCancelledError) {
      const cancelledText = t('tools.asrRuntimeManageCancelled')
      funAsrRuntime.appendLog(cancelledText, 'stage')
      message.info(cancelledText)
      await refreshFunAsrStatus()
      return
    }
    const detail = String(error)
    message.error(t('tools.asrRuntimeManageFailed', { error: detail }))
    showDependencyLog.value = true
  }
}

async function cancelFunAsrManagement() {
  try {
    await funAsrRuntime.cancel()
  } catch (error) {
    message.error(t('tools.asrRuntimeManageFailed', { error: String(error) }))
  }
}

function confirmFunAsrUninstall() {
  if (!funAsrCanUninstall.value) return
  dialog.warning({
    title: t('tools.asrRuntimeUninstallTitle'),
    content: t('tools.asrRuntimeUninstallConfirm'),
    positiveText: t('tools.asrRuntimeUninstall'),
    negativeText: t('common.cancel'),
    onPositiveClick: () => manageFunAsr('uninstall'),
  })
}

function buildAsrPayload() {
  const usePreset = modelMode.value === 'preset'
  return {
    inputPath: inputPath.value,
    outputDir: outputDir.value,
    modelDir: settings.modelDir || null,
    hotword: hotword.value,
    outputFormats: outputFormats.value,
    modelPreset: usePreset ? presetId.value : 'local',
    language: usePreset ? language.value : 'auto',
    allowDownload: usePreset || !localAuxiliaryComplete.value,
    modelPath: usePreset ? '' : modelPath.value,
    vadModelPath: usePreset ? '' : vadModelPath.value,
    puncModelPath: usePreset ? '' : puncModelPath.value,
  }
}

function showModelRecoveryDialog(recovery: AudioToolRecovery, payload: Record<string, unknown>) {
  dialog.warning({
    title: t('tools.asrModelRecoveryTitle'),
    content: recovery.reason === 'disk_full'
      ? t('tools.asrModelRecoveryDiskFull')
      : t('tools.asrModelRecoveryIncomplete'),
    positiveText: t('tools.asrModelRedownload'),
    negativeText: t('common.cancel'),
    positiveButtonProps: { type: 'primary' },
    onPositiveClick: () => {
      void redownloadModelsAndRetry(recovery, payload)
    },
  })
}

async function executeAsr(payload: Record<string, unknown>) {
  pendingRecoveryPayload.value = payload
  const result = await runtime.execute('asr', payload)
  if (result) pendingRecoveryPayload.value = null
}

async function redownloadModelsAndRetry(recovery: AudioToolRecovery, payload: Record<string, unknown>) {
  if (repairingModels.value || state.value.anyBusy) return
  repairingModels.value = true
  try {
    for (const modelId of recovery.modelIds) {
      await invoke('delete_tool_model', {
        payload: {
          id: modelId,
          tool: recovery.tool,
          modelDir: recovery.modelDir,
          missingOk: true,
        },
      })
    }
    message.info(t('tools.asrModelRedownloadStarted'))
    await executeAsr({ ...payload, modelDir: recovery.modelDir })
  } catch (error) {
    message.error(t('tools.asrModelRedownloadFailed', { error: String(error) }))
  } finally {
    repairingModels.value = false
  }
}

function run() {
  if (!funAsrInstalled.value) return void message.error(t('tools.asrRuntimeRequired'))
  if (!outputFormats.value.length) return void message.error(t('tools.asrFormatRequired'))
  if (modelMode.value === 'local' && !modelPath.value) {
    return void message.error(t('tools.asrMainModelRequired'))
  }
  void executeAsr(buildAsrPayload())
}

onMounted(async () => {
  try {
    await funAsrRuntime.start()
  } catch (error) {
    studioLog.warn('[audio-tools] optional package log listener unavailable', error)
  }
  const saved = await loadAudioToolsState()
  modelPath.value = saved?.asrModelPath || ''
  vadModelPath.value = saved?.asrVadModelPath || ''
  puncModelPath.value = saved?.asrPuncModelPath || ''
  const savedPreset = findAsrPreset(saved?.asrPreset)
  presetId.value = availablePresets.value.some(preset => preset.id === savedPreset?.id) ? savedPreset!.id : DEFAULT_ASR_PRESET
  const savedLanguage = String(saved?.asrLanguage || '') as AsrLanguageCode
  language.value = selectedPreset.value.languages.includes(savedLanguage)
    ? savedLanguage
    : selectedPreset.value.defaultLanguage
  modelMode.value = saved?.asrModelMode === 'local' || saved?.asrModelMode === 'preset'
    ? saved.asrModelMode
    : modelPath.value ? 'local' : 'preset'
  restored = true
  await refreshFunAsrStatus()
})

onActivated(() => {
  if (initialActivation) {
    initialActivation = false
    return
  }
  void refreshFunAsrStatus()
})

watch(() => dependencyLogs.value.at(-1), async () => {
  const target = dependencyLogList.value
  if (target && target.scrollHeight - target.scrollTop - target.clientHeight > 48) return
  await nextTick()
  if (dependencyLogList.value) dependencyLogList.value.scrollTop = dependencyLogList.value.scrollHeight
})

watch([() => state.value.recovery, () => state.value.busy], ([recovery, busy]) => {
  const payload = pendingRecoveryPayload.value
  if (!recovery || busy || !payload) return
  pendingRecoveryPayload.value = null
  showModelRecoveryDialog(recovery, payload)
})

watch(presetId, () => {
  if (!selectedPreset.value.languages.includes(language.value)) {
    language.value = selectedPreset.value.defaultLanguage
  }
})
watch(timestampOutputAvailable, (available) => {
  if (!available && outputFormats.value.includes('srt')) {
    outputFormats.value = outputFormats.value.filter(value => value !== 'srt')
  }
}, { immediate: true })

watch([modelMode, presetId, language, modelPath, vadModelPath, puncModelPath], () => {
  if (!restored) return
  void updateAudioToolsState({
    asrModelMode: modelMode.value,
    asrPreset: presetId.value,
    asrLanguage: language.value,
    asrModelPath: modelPath.value,
    asrVadModelPath: vadModelPath.value,
    asrPuncModelPath: puncModelPath.value,
  })
})
watch(() => settings.outputDir, (value) => {
  if (!outputDir.value) outputDir.value = value
})
</script>

<template>
  <AudioToolPanel tool="asr" :status-title="t('tools.processingResult')" :status-hint="t('tools.asrHint')">
    <AudioToolHeader :icon="MicOutline" :title="t('tools.asrTitle')" :description="t('tools.asrDescription')" />

    <section
      v-if="!funAsrInstalled"
      class="audio-tool-dependency" data-guide="tool-input"
      :class="{ 'audio-tool-dependency--error': dependencyError }"
    >
      <div>
        <strong>{{ t('tools.asrRuntimeComponent') }}</strong>
        <span v-if="dependencyChecking">{{ t('tools.asrRuntimeChecking') }}</span>
        <span v-else-if="dependencyError">{{ dependencyError }}</span>
        <span v-else-if="funAsrNeedsRepair">{{ t('tools.asrRuntimeBroken') }}</span>
        <span v-else>{{ t('tools.asrRuntimeMissing') }}</span>
        <small>{{ funAsrNeedsRepair ? t('tools.asrRuntimeRepairHint') : t('tools.asrRuntimeInstallHint') }}</small>
      </div>
      <div class="audio-tool-dependency-actions">
        <n-button
          v-if="dependencyLogs.length && !dependencyBusy"
          text
          size="small"
          :aria-expanded="showDependencyLog"
          @click="showDependencyLog = !showDependencyLog"
        >
          {{ t('tools.asrRuntimeInstallLog') }} · {{ dependencyLogs.length }}
        </n-button>
        <n-button
          v-if="dependencyError && !funAsrStatus"
          secondary
          :loading="dependencyChecking"
          :disabled="state.anyBusy"
          @click="refreshFunAsrStatus"
        >
          {{ t('common.refresh') }}
        </n-button>
        <n-button
          v-else
          class="audio-tool-dependency-button"
          secondary
          :loading="dependencyBusy"
          :disabled="state.anyBusy || dependencyBusy || dependencyChecking || !funAsrStatus"
          @click="manageFunAsr('install')"
        >
          <template #icon><n-icon :component="CloudDownloadOutline" /></template>
          {{ dependencyBusy ? t('tools.asrRuntimeInstalling') : t(funAsrNeedsRepair ? 'tools.asrRuntimeRepair' : 'tools.asrRuntimeInstall') }}
        </n-button>
        <n-button
          v-if="dependencyBusy && dependencyAction === 'install'"
          secondary
          type="error"
          :loading="dependencyCancelling"
          :disabled="dependencyCancelling"
          @click="cancelFunAsrManagement"
        >
          {{ dependencyCancelling ? t('tools.asrRuntimeCancelling') : t('tools.asrRuntimeCancelInstall') }}
        </n-button>
      </div>
    </section>

    <div v-else class="audio-tool-runtime-ready">
      <span><i />{{ t('tools.asrRuntimeInstalled', { version: funAsrStatus?.version || '—' }) }}</span>
      <div class="audio-tool-runtime-actions">
        <n-button
          v-if="dependencyLogs.length && !dependencyBusy"
          text
          size="tiny"
          :aria-expanded="showDependencyLog"
          @click="showDependencyLog = !showDependencyLog"
        >
          {{ t('tools.asrRuntimeInstallLog') }} · {{ dependencyLogs.length }}
        </n-button>
        <n-tag v-if="!funAsrCanUninstall" size="small" :bordered="false">
          {{ t('tools.asrRuntimeProvidedByEnvironment') }}
        </n-tag>
        <n-button v-if="funAsrCanUninstall" text size="tiny" type="error" :loading="dependencyBusy" :disabled="state.anyBusy || dependencyBusy" @click="confirmFunAsrUninstall">
          {{ dependencyBusy ? t('tools.asrRuntimeUninstalling') : t('tools.asrRuntimeUninstall') }}
        </n-button>
        <n-button
          v-if="dependencyBusy && dependencyAction === 'uninstall'"
          text
          size="tiny"
          type="error"
          :loading="dependencyCancelling"
          :disabled="dependencyCancelling"
          @click="cancelFunAsrManagement"
        >
          {{ dependencyCancelling ? t('tools.asrRuntimeCancelling') : t('common.cancel') }}
        </n-button>
      </div>
    </div>

    <section
      v-if="dependencyLogs.length && (dependencyBusy || dependencyError || showDependencyLog)"
      class="audio-tool-install-log"
    >
      <header>
        <span><n-icon :component="TerminalOutline" />{{ t('tools.asrRuntimeInstallLog') }}</span>
        <small>{{ t('tools.logEntries', { count: dependencyLogs.length }) }}</small>
      </header>
      <div ref="dependencyLogList" class="audio-tool-install-log__list" role="log" aria-live="polite">
        <TaskLogConsole :lines="dependencyLogs.map(entry => entry.record || normalizeLogRecord({ message: entry.message, level: entry.level === 'error' ? 'error' : entry.level === 'output' ? null : 'info', source: 'worker', timestamp: new Date(entry.timestamp).toISOString() }))" embedded />
      </div>
    </section>

    <template v-if="funAsrInstalled">
      <div data-guide="tool-input" class="audio-tool-field">
        <label>{{ t('tools.inputFile') }}</label>
        <div class="audio-tool-path">
          <n-input :value="inputPath" readonly :placeholder="t('tools.chooseAudio')" />
          <n-button secondary :disabled="state.anyBusy" @click="pickAudio">
            <template #icon><n-icon :component="DocumentOutline" /></template>
            {{ t('common.browse') }}
          </n-button>
        </div>
      </div>

      <section class="audio-tool-model-section">
        <div class="audio-tool-field-heading">
          <label>{{ t('tools.asrModelSource') }}</label>
          <a :href="FUNASR_URL" target="_blank" rel="noopener noreferrer" @click.prevent="openFunAsr">
            <n-icon :component="CloudDownloadOutline" />{{ t('tools.asrProjectLink') }}
          </a>
        </div>
        <div class="audio-tool-mode-switch" role="radiogroup" :aria-label="t('tools.asrModelSource')">
          <button
            type="button"
            role="radio"
            :aria-checked="modelMode === 'preset'"
            :class="{ active: modelMode === 'preset' }"
            :disabled="state.anyBusy"
            @click="modelMode = 'preset'"
          >
            <strong>{{ t('tools.asrPresetMode') }}</strong>
            <small>{{ t('tools.asrPresetModeDescription') }}</small>
          </button>
          <button
            type="button"
            role="radio"
            :aria-checked="modelMode === 'local'"
            :class="{ active: modelMode === 'local' }"
            :disabled="state.anyBusy"
            @click="modelMode = 'local'"
          >
            <strong>{{ t('tools.asrLocalMode') }}</strong>
            <small>{{ t('tools.asrLocalModeDescription') }}</small>
          </button>
        </div>

        <div v-if="modelMode === 'preset'" class="audio-tool-preset-config">
          <div class="audio-tool-grid">
            <div class="audio-tool-field">
              <label>{{ t('tools.asrRecognitionProfile') }}</label>
              <n-select
                v-model:value="presetId"
                :options="presetOptions"
                :disabled="state.anyBusy"
                filterable
                :consistent-menu-width="false"
              />
            </div>
            <div class="audio-tool-field">
              <label>{{ t('tools.asrRecognitionLanguage') }}</label>
              <n-select v-model:value="language" :options="languageOptions" :disabled="state.anyBusy" filterable />
            </div>
          </div>

          <div class="audio-tool-profile-summary">
            <div class="audio-tool-profile-summary__title">
              <div>
                <strong>{{ t(selectedPreset.titleKey) }}</strong>
                <small>{{ t(selectedPreset.descriptionKey) }}</small>
              </div>
              <code>{{ selectedPreset.modelId }}</code>
            </div>
            <div class="audio-tool-profile-meta">
              <span>{{ t(selectedPreset.scaleKey) }}</span>
              <span>{{ t(selectedPreset.deviceKey) }}</span>
            </div>
            <div class="audio-tool-profile-capabilities">
              <span v-if="isAppleSilicon">MLX</span>
              <template v-else><span v-for="key in selectedPreset.capabilityKeys" :key="key">{{ t(key) }}</span></template>
            </div>
            <small>{{ t('tools.asrPresetDetails') }}</small>
            <small v-if="!timestampOutputAvailable" class="audio-tool-profile-warning">
              {{ t('tools.asrPresetNoTimestamps') }}
            </small>
          </div>
        </div>

        <div v-else class="audio-tool-local-models">
          <div class="audio-tool-field">
            <label>{{ t('tools.asrMainModel') }}</label>
            <button
              class="audio-tool-model-picker"
              :class="{ selected: modelPath }"
              type="button"
              :disabled="state.anyBusy"
              @click="pickModel('model')"
            >
              <n-icon :component="FolderOpenOutline" size="21" />
              <span>
                <strong>{{ modelPath ? fileName(modelPath) : t('tools.chooseAsrModel') }}</strong>
                <small :title="modelPath">{{ modelPath || t('tools.asrModelChooseHint') }}</small>
              </span>
              <em>{{ modelPath ? t('tools.reselectModel') : t('common.browse') }}</em>
            </button>
            <div class="audio-tool-model-meta">
              <small>{{ t('tools.asrMainModelLocalHint') }}</small>
              <n-button v-if="modelPath" text size="tiny" :disabled="state.anyBusy" @click="clearModel('model')">
                {{ t('common.remove') }}
              </n-button>
            </div>
          </div>

          <n-collapse v-if="!isAppleSilicon" class="audio-tool-collapse">
            <n-collapse-item :title="t('tools.asrLocalModels')" name="auxiliary">
              <p class="audio-tool-hint">{{ t('tools.asrLocalModelsHint') }}</p>
              <div class="audio-tool-grid">
                <div v-for="item in [
                  { key: 'vad', label: t('tools.asrVadModel'), value: vadModelPath },
                  { key: 'punc', label: t('tools.asrPuncModel'), value: puncModelPath },
                ]" :key="item.key" class="audio-tool-field">
                  <label>{{ item.label }}</label>
                  <div class="audio-tool-path">
                    <n-input
                      :value="item.value"
                      readonly
                      clearable
                      :placeholder="t('tools.asrOptionalModelPlaceholder')"
                      :disabled="state.anyBusy"
                      @clear="clearModel(item.key as ModelTarget)"
                    />
                    <n-button secondary :disabled="state.anyBusy" @click="pickModel(item.key as ModelTarget)">
                      <template #icon><n-icon :component="FolderOpenOutline" /></template>
                      {{ t('common.browse') }}
                    </n-button>
                  </div>
                </div>
              </div>
              <small class="audio-tool-auxiliary-state">
                {{ localAuxiliaryComplete ? t('tools.asrAuxFullyLocal') : t('tools.asrAuxUsesPreset') }}
              </small>
            </n-collapse-item>
          </n-collapse>
        </div>
      </section>

      <div class="audio-tool-grid">
        <div class="audio-tool-field">
          <label>{{ t('tools.hotwords') }}</label>
          <n-input
            v-model:value="hotword"
            :disabled="state.anyBusy || !supportsHotwords"
            :placeholder="supportsHotwords ? t('tools.hotwordsPlaceholder') : t('tools.asrHotwordsUnavailable')"
          />
        </div>
        <div class="audio-tool-field">
          <label>{{ t('tools.outputFormats') }}</label>
          <n-checkbox-group v-model:value="outputFormats" :disabled="state.anyBusy">
            <n-space><n-checkbox v-for="option in formatOptions" :key="option.value" :value="option.value" :label="option.label" :disabled="option.disabled" /></n-space>
          </n-checkbox-group>
        </div>
      </div>

      <div class="audio-tool-field">
        <label>{{ t('tools.outputDirectory') }}</label>
        <div class="audio-tool-path">
          <n-input :value="outputDir" readonly />
          <n-button secondary :disabled="state.anyBusy" @click="pickOutput">
            <template #icon><n-icon :component="FolderOpenOutline" /></template>
            {{ t('common.browse') }}
          </n-button>
        </div>
      </div>

      <div class="audio-tool-actions">
        <n-button type="primary" :loading="state.busy || repairingModels" :disabled="state.anyBusy || repairingModels || !canRun" @click="run">
          {{ t('tools.startAsr') }}
        </n-button>
      </div>
    </template>

    <template #result="{ result }">
      <section v-if="result.operation === 'asr'" class="tool-result-stack">
        <div class="tool-result-grid">
          <div><span>{{ t('tools.asrResultModel') }}</span><strong>{{ resultModelLabel(result.modelPreset) }}</strong></div>
          <div>
            <span>{{ t('tools.asrResultLanguage') }}</span>
            <strong>{{ languageLabel(result.detectedLanguage || result.requestedLanguage) }}</strong>
          </div>
          <div><span>{{ t('tools.asrSegments') }}</span><strong>{{ result.segmentCount }}</strong></div>
          <div><span>{{ t('tools.outputCountLabel') }}</span><strong>{{ result.outputPaths?.length || 0 }}</strong></div>
        </div>
        <div class="tool-transcript"><strong>{{ t('tools.transcriptPreview') }}</strong><p>{{ result.text }}</p></div>
        <n-button v-if="result.outputDir" secondary size="small" @click="runtime.revealPath(result.outputDir)">
          {{ t('tools.openOutputLocation') }}
        </n-button>
      </section>
    </template>
  </AudioToolPanel>
</template>
