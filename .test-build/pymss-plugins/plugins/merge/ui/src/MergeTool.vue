<script setup lang="ts">
import { toolRef } from '@/sdk'
import { computed, onMounted, ref, watch } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { useI18n } from 'vue-i18n'
import { FolderOpenOutline, GitMergeOutline } from '@vicons/ionicons5'
import { useSettingsStore } from '@/stores/settings'
import AudioToolPanel from '@/features/audio-tools/shared/AudioToolPanel.vue'
import AudioToolHeader from '@/features/audio-tools/shared/AudioToolHeader.vue'
import { useAudioToolRuntime } from '@/features/audio-tools/runtime'
import { loadAudioToolsState, updateAudioToolsState } from '@/features/audio-tools/state'
import { sameAudioDirectory } from '@/features/audio-tools/paths'

type MergeSortBy = 'name' | 'modified' | 'regex'
type SortDirection = 'asc' | 'desc'

const { t } = useI18n()
const settings = useSettingsStore()
const runtime = useAudioToolRuntime()
const state = runtime.stateFor('merge')
const inputDir = toolRef('inputDir', '')
const outputDir = toolRef('outputDir', settings.outputDir)
const sortBy = ref<MergeSortBy>('name')
const sortDirection = ref<SortDirection>('asc')
const regexPattern = ref('(\\d+)')
let restored = false

const sortOptions = computed(() => [
  { label: t('tools.mergeSortName'), value: 'name' },
  { label: t('tools.mergeSortModified'), value: 'modified' },
  { label: t('tools.mergeSortRegex'), value: 'regex' },
])
const directionOptions = computed(() => [
  { label: t('tools.sortAscending'), value: 'asc' },
  { label: t('tools.sortDescending'), value: 'desc' },
])
const regexError = computed(() => sortBy.value === 'regex' && !regexPattern.value.trim()
  ? t('tools.mergeRegexRequired')
  : '')
const directoryError = computed(() => sameAudioDirectory(inputDir.value, outputDir.value) ? t('tools.mergeSameDirectory') : '')
const orderHint = computed(() => {
  if (sortBy.value === 'modified') return t('tools.mergeModifiedHint')
  if (sortBy.value === 'regex') return t('tools.mergeRegexHint')
  return t('tools.mergeNameHint')
})

watch(() => settings.outputDir, value => {
  if (!outputDir.value) outputDir.value = value
})

onMounted(async () => {
  const saved = await loadAudioToolsState()
  if (saved?.mergeSortBy && ['name', 'modified', 'regex'].includes(saved.mergeSortBy)) {
    sortBy.value = saved.mergeSortBy
  }
  if (saved?.mergeSortDirection && ['asc', 'desc'].includes(saved.mergeSortDirection)) {
    sortDirection.value = saved.mergeSortDirection
  }
  if (typeof saved?.mergeRegex === 'string') regexPattern.value = saved.mergeRegex
  restored = true
})

watch([sortBy, sortDirection, regexPattern], () => {
  if (!restored) return
  void updateAudioToolsState({
    mergeSortBy: sortBy.value,
    mergeSortDirection: sortDirection.value,
    mergeRegex: regexPattern.value,
  })
})

async function pickInput() {
  const path = await invoke<string | null>('pick_input_folder')
  if (path) inputDir.value = path
}

async function pickOutput() {
  const path = await invoke<string | null>('pick_output_folder')
  if (path) outputDir.value = path
}

function run() {
  if (!inputDir.value || !outputDir.value || regexError.value || directoryError.value) return
  void runtime.execute('merge', {
    inputDir: inputDir.value,
    outputDir: outputDir.value,
    sortBy: sortBy.value,
    sortDirection: sortDirection.value,
    regexPattern: sortBy.value === 'regex' ? regexPattern.value : '',
  })
}
</script>

<template>
  <AudioToolPanel tool="merge" :status-title="t('tools.processingResult')" :status-hint="t('tools.mergeHint')">
    <AudioToolHeader :icon="GitMergeOutline" :title="t('tools.mergeTitle')" :description="t('tools.mergeDescription')" />

    <div class="audio-tool-grid">
      <div data-guide="tool-input" class="audio-tool-field">
        <label>{{ t('tools.inputDirectory') }}</label>
        <div class="audio-tool-path">
          <n-input :value="inputDir" readonly :placeholder="t('tools.chooseInputDirectory')" />
          <n-button secondary :disabled="state.anyBusy" @click="pickInput">
            <template #icon><n-icon :component="FolderOpenOutline" /></template>
            {{ t('common.browse') }}
          </n-button>
        </div>
      </div>
      <div class="audio-tool-field">
        <label>{{ t('tools.outputDirectory') }}</label>
        <div class="audio-tool-path">
          <n-input :value="outputDir" readonly :placeholder="t('tools.chooseOutputDirectory')" />
          <n-button secondary :disabled="state.anyBusy" @click="pickOutput">
            <template #icon><n-icon :component="FolderOpenOutline" /></template>
            {{ t('common.browse') }}
          </n-button>
        </div>
      </div>
    </div>

    <n-alert v-if="directoryError" type="error" :bordered="false">{{ directoryError }}</n-alert>
    <div class="audio-tool-field">
      <div class="audio-tool-field-heading">
        <label>{{ t('tools.mergeRules') }}</label>
        <span>{{ t('tools.mergeRulesScope') }}</span>
      </div>
      <div class="audio-tool-grid">
        <div class="audio-tool-field">
          <label>{{ t('tools.mergeSortBy') }}</label>
          <n-select v-model:value="sortBy" :options="sortOptions" :disabled="state.anyBusy" />
        </div>
        <div class="audio-tool-field">
          <label>{{ t('tools.sortDirection') }}</label>
          <n-select v-model:value="sortDirection" :options="directionOptions" :disabled="state.anyBusy" />
        </div>
      </div>
      <div v-if="sortBy === 'regex'" class="audio-tool-field">
        <label>{{ t('tools.mergeRegex') }}</label>
        <n-input
          v-model:value="regexPattern"
          :disabled="state.anyBusy"
          :placeholder="t('tools.mergeRegexPlaceholder')"
          :status="regexError ? 'error' : undefined"
        />
        <small class="audio-tool-hint">{{ regexError || t('tools.mergeRegexHelp') }}</small>
      </div>
      <n-alert type="info" :bordered="false">{{ orderHint }}</n-alert>
    </div>

    <div class="audio-tool-actions">
      <n-button
        type="primary"
        :loading="state.busy"
        :disabled="state.anyBusy || !inputDir || !outputDir || Boolean(regexError) || Boolean(directoryError)"
        @click="run"
      >
        {{ t('tools.startMerge') }}
      </n-button>
    </div>
  </AudioToolPanel>
</template>
