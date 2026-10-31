<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch, type Directive } from 'vue'
import { useI18n } from 'vue-i18n'
import { convertFileSrc } from '@tauri-apps/api/core'
import { FolderOutline, OpenOutline, PauseOutline, PlayOutline } from '@vicons/ionicons5'
import { isTauriRuntime } from '@/utils/appStore'
import type { StemOutput } from '@/stores/task'
import { isAudioOutput } from '@/utils/stemOutputs'

const props = defineProps<{
  outputs?: StemOutput[]; directories?: string[]; outputDirectory?: string
  groups?: { id: string; label: string; outputs: StemOutput[] }[]
}>()
const emit = defineEmits<{ reveal: [path: string] }>()
const { t } = useI18n()
// Naive UI applies component attributes to the wrapper; label its focusable handle.
const vSliderLabel: Directive<HTMLElement, string> = (element, { value }) => {
  element.querySelector('[role="slider"]')?.setAttribute('aria-label', value)
}
type Playback = { currentTime: number; duration: number; loading: boolean; failed: boolean }
const playback = ref<Record<string, Playback>>({})
const playingPath = ref('')
const outputGroups = computed(() => props.groups || [{ id: 'outputs', label: '', outputs: props.outputs || [] }])
const outputs = computed(() => outputGroups.value.flatMap(group => group.outputs))
const audioOutputs = computed(() => outputs.value.filter(isAudioOutput))
const directories = computed(() => [...new Set((props.directories?.length ? props.directories : [props.outputDirectory || '']).filter(Boolean))])
let audio: HTMLAudioElement | undefined
let audioPath = ''
let removeListeners: (() => void) | undefined
let playGeneration = 0

function fileName(path: string) { return path.split(/[\\/]/).pop() || path }
function state(path: string): Playback {
  return playback.value[path] || { currentTime: 0, duration: 0, loading: false, failed: false }
}
function update(path: string, patch: Partial<Playback>) {
  playback.value[path] = { ...state(path), ...patch }
}
function time(value: number) {
  const seconds = Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
}
function releaseAudio() {
  playGeneration++
  removeListeners?.()
  removeListeners = undefined
  if (audio) {
    audio.pause()
    audio.removeAttribute('src')
    audio.load()
    update(audioPath, { loading: false })
  }
  audio = undefined
  audioPath = ''
  playingPath.value = ''
}

function prepareAudio(path: string) {
  if (!audioOutputs.value.some(output => output.path === path)) return undefined
  if (audio && audioPath === path && !state(path).failed) return audio
  releaseAudio()
  if (!isTauriRuntime()) return undefined
  const player = new Audio()
  audio = player
  audioPath = path
  player.preload = 'metadata'
  update(path, { loading: true, failed: false })
  const fail = () => {
    if (audio !== player) return
    playingPath.value = ''
    update(path, { loading: false, failed: true })
  }
  const handlers: Record<string, () => void> = {
    loadedmetadata: () => {
      const duration = Number.isFinite(player.duration) ? Math.max(0, player.duration) : 0
      const currentTime = Math.min(state(path).currentTime, duration)
      update(path, { duration, currentTime, loading: false })
      if (currentTime > 0) player.currentTime = currentTime
    },
    timeupdate: () => update(path, { currentTime: player.currentTime }),
    playing: () => { playingPath.value = path; update(path, { loading: false }) },
    pause: () => { if (playingPath.value === path) playingPath.value = '' },
    ended: () => { playingPath.value = ''; player.currentTime = 0; update(path, { currentTime: 0 }) },
    error: fail,
  }
  for (const [event, handler] of Object.entries(handlers)) player.addEventListener(event, handler)
  removeListeners = () => {
    for (const [event, handler] of Object.entries(handlers)) player.removeEventListener(event, handler)
  }
  try {
    player.src = convertFileSrc(path)
    player.load()
  } catch { fail() }
  return player
}

async function toggle(path: string) {
  const player = prepareAudio(path)
  if (!player) { update(path, { failed: true }); return }
  const generation = ++playGeneration
  if (!player.paused) {
    player.pause()
    update(path, { loading: false })
    return
  }
  update(path, { loading: true, failed: false })
  try {
    await player.play()
    if (audio === player && generation === playGeneration) {
      playingPath.value = path
      update(path, { loading: false })
    }
  } catch {
    if (audio === player && generation === playGeneration) {
      playingPath.value = ''
      update(path, { loading: false, failed: true })
    }
  }
}

function seek(path: string, value: number) {
  if (!Number.isFinite(value) || state(path).duration <= 0) return
  const position = Math.min(state(path).duration, Math.max(0, value))
  const player = prepareAudio(path)
  update(path, { currentTime: position })
  if (player && player.readyState > 0) player.currentTime = position
}

watch(() => audioOutputs.value.map(output => output.path).filter(Boolean), paths => {
  const available = new Set(paths)
  if (!available.has(audioPath)) {
    releaseAudio()
    if (paths[0]) prepareAudio(paths[0])
  }
  for (const path of Object.keys(playback.value)) if (!available.has(path)) delete playback.value[path]
}, { immediate: true })
onBeforeUnmount(releaseAudio)
</script>

<template>
  <section v-if="outputs.length" class="workflow-result-preview">
    <div v-if="directories.length" class="result-folders">
      <div v-for="directory in directories" :key="directory" class="result-folder">
        <n-icon :component="FolderOutline" aria-hidden="true" />
        <code :title="directory">{{ directory }}</code>
        <n-button size="small" quaternary :title="t('separate.openOutput')" :aria-label="t('separate.openOutput') + ' · ' + directory" @click="emit('reveal', directory)">
          <template #icon><n-icon :component="OpenOutline" /></template>{{ t('common.open') }}
        </n-button>
      </div>
    </div>
    <div class="result-preview-heading"><strong>{{ t(outputs.length === audioOutputs.length ? 'separate.previewTitle' : 'results.title') }}</strong><span>{{ outputs.length }} {{ t(outputs.length === audioOutputs.length ? 'separate.previewStemUnit' : 'separate.queueFileUnit') }}</span></div>
    <section v-for="group in outputGroups" :key="group.id" class="result-output-group">
      <h4 v-if="group.label" :title="group.label">{{ group.label }}</h4>
      <div class="result-track-list" role="list">
      <article v-for="output in group.outputs" :key="output.path" class="result-track" :class="{ 'result-track--file': !isAudioOutput(output), 'result-track--playing': playingPath === output.path, 'result-track--error': state(output.path).failed }" :data-output-path="output.path" role="listitem">
        <div class="result-track__title">
          <strong :title="output.path">{{ fileName(output.path) }}</strong>
          <small class="result-track__stem">{{ isAudioOutput(output) ? output.stem : output.mediaType || output.stem }}</small>
          <span v-if="state(output.path).failed" class="result-track__error" role="status">{{ t('separate.previewPlayFailed') }}</span>
        </div>
        <n-button v-if="isAudioOutput(output)" class="result-track__play" circle secondary size="small" :type="playingPath === output.path ? 'primary' : 'default'" :loading="state(output.path).loading" :title="t(playingPath === output.path ? 'common.pause' : 'workbench.playAudio')" :aria-label="t(playingPath === output.path ? 'common.pause' : 'workbench.playAudio') + ' · ' + fileName(output.path)" @click="toggle(output.path)">
          <template #icon><n-icon :component="playingPath === output.path ? PauseOutline : PlayOutline" /></template>
        </n-button>
        <n-slider v-if="isAudioOutput(output)" v-slider-label="t('workbench.playbackPosition') + ' · ' + fileName(output.path)" class="result-track__slider" :value="state(output.path).currentTime" :min="0" :max="Math.max(state(output.path).duration, 1)" :step="0.1" :tooltip="false" :disabled="!state(output.path).duration || state(output.path).failed" @update:value="(value: number) => seek(output.path, value)" />
        <span v-if="isAudioOutput(output)" class="result-track__time">{{ time(state(output.path).currentTime) }} / {{ state(output.path).duration ? time(state(output.path).duration) : '--:--' }}</span>
        <n-button class="result-track__open" circle quaternary size="small" :title="t('common.open') + ' · ' + fileName(output.path)" :aria-label="t('common.open') + ' · ' + fileName(output.path)" @click="emit('reveal', output.path)"><template #icon><n-icon :component="OpenOutline" /></template></n-button>
      </article>
      </div>
    </section>
  </section>
  <n-empty v-else :description="t('workbench.noResultsYet')" />
</template>

<style scoped>
.workflow-result-preview { display: grid; gap: 14px; min-width: 0; padding: 2px 0; }
.result-folders { display: grid; gap: 8px; }
.result-folder { display: flex; align-items: center; gap: 10px; min-width: 0; padding: 8px 12px; border-radius: 12px; background: color-mix(in srgb, var(--surface-2) 48%, transparent); }
.result-folder > .n-icon { flex: 0 0 auto; font-size: 18px; color: var(--primary-strong); }
.result-folder code { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; color: var(--on-surface-muted); }
.result-preview-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.result-preview-heading strong { font-size: 14px; font-weight: 600; }
.result-preview-heading > span { color: var(--on-surface-muted); font-size: 11px; }
.result-track-list { display: grid; gap: 8px; min-width: 0; }
.result-output-group { display: grid; gap: 8px; min-width: 0; }
.result-output-group h4 { margin: 0; color: var(--on-surface-muted); font-size: 12px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.result-track { display: grid; grid-template-columns: minmax(180px, 1.15fr) 28px minmax(120px, 1fr) 102px 28px; align-items: center; gap: 12px; padding: 12px 14px; border: 1px solid color-mix(in srgb, var(--outline) 78%, transparent); border-radius: 14px; background: color-mix(in srgb, var(--surface-2) 44%, transparent); transition: border-color 160ms, background-color 160ms; }
.result-track--playing { border-color: color-mix(in srgb, var(--primary) 40%, var(--outline)); background: color-mix(in srgb, var(--primary-soft) 30%, var(--surface)); }
.result-track--file { grid-template-columns: minmax(0, 1fr) 28px; }
.result-track--error { border-color: color-mix(in srgb, var(--danger) 35%, var(--outline)); }
.result-track__title { display: grid; gap: 5px; min-width: 0; }
.result-track__title strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; font-weight: 600; }
.result-track__stem { width: fit-content; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding: 2px 6px; border-radius: 999px; background: color-mix(in srgb, var(--primary-soft) 58%, transparent); color: var(--primary-strong); font-size: 10px; font-weight: 600; }
.result-track__slider { min-width: 0; }
.result-track__time { font-size: 11px; font-variant-numeric: tabular-nums; color: var(--on-surface-muted); white-space: nowrap; text-align: right; }
.result-track__error { color: var(--danger); font-size: 11px; }
@media (max-width: 760px) {
  .result-track { grid-template-columns: 28px minmax(0, 1fr) auto 28px; gap: 8px 10px; }
  .result-track__title { grid-column: 1 / 4; }
  .result-track__open { grid-column: 4; grid-row: 1; }
  .result-track__play { grid-column: 1; grid-row: 2; }
  .result-track__slider { grid-column: 2; grid-row: 2; }
  .result-track__time { grid-column: 3 / 5; grid-row: 2; }
  .result-track--file { grid-template-columns: minmax(0, 1fr) 28px; }
  .result-track--file .result-track__title { grid-column: 1; }
  .result-track--file .result-track__open { grid-column: 2; }
}
</style>
