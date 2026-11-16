<script setup lang="ts">
import { computed, ref, useId } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronDownOutline } from '@vicons/ionicons5'
import type { AudioTrackInfo } from '@/stores/task'
import { audioTrackLanguageLabel } from '@/utils/audioTracks'

const props = defineProps<{
  tracks: AudioTrackInfo[]
  modelValue: number[]
  fileName: string
  batch: boolean
  disabled?: boolean
}>()
const emit = defineEmits<{ 'update:modelValue': [value: number[]] }>()
const { t, locale } = useI18n()
const panelId = useId()
const expanded = ref<boolean | null>(null)
const showAll = ref(false)
const isExpanded = computed(() => expanded.value ?? !props.batch)
const selected = computed(() => new Set(props.modelValue))
const selectedCount = computed(() => props.tracks.filter(track => selected.value.has(track.index)).length)
const empty = computed(() => props.tracks.length > 0 && selectedCount.value === 0)
const visibleTracks = computed(() => showAll.value ? props.tracks : props.tracks.slice(0, 5))

function toggleExpanded() {
  expanded.value = !isExpanded.value
  showAll.value = false
}

function selectTrack(index: number, checked: boolean) {
  if (props.disabled) return
  const next = new Set(props.modelValue)
  if (checked) next.add(index)
  else next.delete(index)
  emit('update:modelValue', props.tracks.filter(track => next.has(track.index)).map(track => track.index))
}

function selectAll() {
  if (!props.disabled) emit('update:modelValue', props.tracks.map(track => track.index))
}

function trackTitle(track: AudioTrackInfo) {
  return track.title.trim() || t('separate.audioTrackName', { index: track.index })
}

function trackDetails(track: AudioTrackInfo) {
  const language = audioTrackLanguageLabel(track.language, locale.value, t('separate.audioTrackLanguageUntagged'))
  const channels = track.channels === 1 ? t('separate.audioTrackMono')
    : track.channels === 2 ? t('separate.audioTrackStereo')
      : track.channels > 0 ? t('separate.audioTrackChannels', { count: track.channels }) : ''
  return [language, channels].filter(Boolean).join(' · ')
}
</script>

<template>
  <div
    class="audio-track-picker"
    :class="{ 'audio-track-picker--empty': empty }"
    role="group"
    :aria-label="t('separate.audioTracksForFile', { name: fileName })"
    @pointerdown.stop
    @click.stop
  >
    <div class="audio-track-picker__header">
      <button type="button" class="audio-track-picker__toggle" :aria-expanded="isExpanded" :aria-controls="panelId" @click="toggleExpanded">
        <n-icon class="audio-track-picker__chevron" :class="{ 'audio-track-picker__chevron--expanded': isExpanded }" :component="ChevronDownOutline" />
        <span>{{ t('separate.audioTracks') }}</span>
        <span class="audio-track-picker__count">{{ t('separate.audioTracksSelected', { count: selectedCount, total: tracks.length }) }}</span>
      </button>
      <button type="button" class="audio-track-picker__action" :disabled="disabled || selectedCount === tracks.length" @click="selectAll">
        {{ t('separate.audioTracksSelectAll') }}
      </button>
    </div>
    <div v-show="isExpanded" :id="panelId" class="audio-track-picker__list">
      <template v-if="isExpanded">
        <n-checkbox
          v-for="track in visibleTracks"
          :key="track.index"
          class="audio-track-picker__option"
          :class="{ 'audio-track-picker__option--selected': selected.has(track.index) }"
          :checked="selected.has(track.index)"
          :disabled="disabled"
          size="small"
          @update:checked="(checked: boolean) => selectTrack(track.index, checked)"
        >
          <span class="audio-track-picker__row">
            <span class="audio-track-picker__number">{{ String(track.index).padStart(2, '0') }}</span>
            <span class="audio-track-picker__title" :title="trackTitle(track)">{{ trackTitle(track) }}</span>
            <span class="audio-track-picker__details" :title="track.language || undefined">{{ trackDetails(track) }}</span>
          </span>
        </n-checkbox>
        <button v-if="tracks.length > 5" type="button" class="audio-track-picker__action audio-track-picker__more" @click="showAll = !showAll">
          {{ showAll ? t('separate.audioTracksShowLess') : t('separate.audioTracksShowMore', { count: tracks.length - 5 }) }}
        </button>
      </template>
    </div>
    <p v-if="empty" class="audio-track-picker__warning" role="status">{{ t('separate.audioTracksRequired') }}</p>
  </div>
</template>

<style scoped>
.audio-track-picker {
  min-width: 0;
  margin-top: 5px;
  padding-top: 5px;
  border-top: 1px solid color-mix(in srgb, var(--outline) 60%, transparent);
  container-type: inline-size;
  cursor: default;
}
.audio-track-picker__header { display: flex; align-items: center; gap: 8px; min-width: 0; }
.audio-track-picker__toggle,
.audio-track-picker__action {
  border: 0;
  background: transparent;
  font: inherit;
  cursor: pointer;
  border-radius: 4px;
}
.audio-track-picker__toggle {
  display: flex;
  flex: 1;
  align-items: center;
  min-width: 0;
  gap: 5px;
  padding: 4px 0;
  color: var(--on-surface-muted);
  font-size: 11px;
  text-align: left;
}
.audio-track-picker__chevron { flex: 0 0 auto; transform: rotate(-90deg); transition: transform 150ms ease; }
.audio-track-picker__chevron--expanded { transform: rotate(0); }
.audio-track-picker__count { margin-left: auto; font-variant-numeric: tabular-nums; white-space: nowrap; }
.audio-track-picker__action { flex: 0 0 auto; padding: 4px; color: var(--primary-strong); font-size: 11px; }
.audio-track-picker__action:disabled { opacity: 0.45; cursor: default; }
.audio-track-picker__toggle:hover,
.audio-track-picker__action:hover:not(:disabled) { background: color-mix(in srgb, var(--primary-soft) 35%, transparent); }
.audio-track-picker__toggle:focus-visible,
.audio-track-picker__action:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.audio-track-picker__list { display: grid; gap: 2px; margin-top: 2px; }
.audio-track-picker__option {
  display: flex;
  align-items: center;
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  padding: 5px 6px;
  border-radius: 5px;
  font-size: 11px;
}
.audio-track-picker__option--selected { background: color-mix(in srgb, var(--primary-soft) 22%, transparent); }
.audio-track-picker__option:hover:not(.n-checkbox--disabled) { background: color-mix(in srgb, var(--primary-soft) 42%, transparent); }
.audio-track-picker__option :deep(.n-checkbox__label) { flex: 1; min-width: 0; font-size: inherit; }
.audio-track-picker__row { display: grid; grid-template-columns: 1.4rem minmax(0, 1fr) auto; align-items: center; gap: 5px; line-height: 1.4; }
.audio-track-picker__number { color: var(--on-surface-muted); font-size: 10px; font-variant-numeric: tabular-nums; }
.audio-track-picker__title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--on-surface); }
.audio-track-picker__details { max-width: 9rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--on-surface-muted); font-size: 10px; }
.audio-track-picker__more { justify-self: start; margin-left: 23px; }
.audio-track-picker--empty .audio-track-picker__count,
.audio-track-picker__warning { color: var(--danger); }
.audio-track-picker__warning { margin: 4px 0 0; font-size: 11px; line-height: 1.5; }
@container (max-width: 320px) {
  .audio-track-picker__row { grid-template-columns: 1.4rem minmax(0, 1fr); row-gap: 1px; }
  .audio-track-picker__details { grid-column: 2; max-width: 100%; }
}
@media (prefers-reduced-motion: reduce) {
  .audio-track-picker__chevron { transition: none; }
}
</style>
