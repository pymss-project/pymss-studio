<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import EditorInsertList from '@/components/editor/EditorInsertList.vue'
import EditorPanBar from '@/components/editor/EditorPanBar.vue'
import EditorParameterSlider from '@/components/editor/EditorParameterSlider.vue'
import type { EditorPluginSlot } from '@/types/editor'
import { formatLevel, formatPanShort, formatVolumeDb, METER_TICKS, meterScale, meterValue, normalizeLevel } from '@/utils/editorMeter'

const props = defineProps<{
  name: string
  color?: string | null
  /** Track id for the insert column; null addresses the master bus. */
  trackId: string | null
  pluginChain?: EditorPluginSlot[]
  /** Insert rows shared by every strip in the console so faders stay aligned. */
  insertRows?: number
  volume: number
  pan: number
  levels?: [number, number] | number[]
  muted?: boolean
  solo?: boolean
  master?: boolean
  selected?: boolean
  disabled?: boolean
}>()

const emit = defineEmits<{
  select: []
  'update:volume': [value: number]
  'update:pan': [value: number]
  beginVolume: []
  commitVolume: []
  beginPan: []
  commitPan: []
  toggleMute: []
  toggleSolo: []
}>()

const { t } = useI18n()
const meterLevels = computed<[number, number]>(() => [normalizeLevel(props.levels?.[0]), normalizeLevel(props.levels?.[1])])
const peak = computed(() => Math.max(...meterLevels.value))
const volumeLabel = computed(() => `${props.name} · ${t(props.master ? 'editor.masterVolume' : 'editor.trackVolume')}`)
const panLabel = computed(() => `${props.name} · ${t('editor.balance')}`)
</script>

<template>
  <section
    class="channel-strip"
    :class="{ 'channel-strip--selected': selected, 'channel-strip--master': master, 'channel-strip--dimmed': muted }"
    :aria-label="name"
    @mousedown="emit('select')"
  >
    <header class="channel-strip__name" :title="name">
      <i :style="{ background: color || 'var(--primary)' }" aria-hidden="true" />
      <span>{{ name }}</span>
    </header>

    <EditorInsertList class="channel-strip__inserts" compact :rows="insertRows" :track-id="trackId" :chain="pluginChain || []" :disabled="disabled" />

    <div class="channel-strip__pan" :title="`${t('editor.balance')} · ${formatPanShort(pan)}`">
      <EditorPanBar
        :value="pan"
        :value-text="formatPanShort(pan)"
        :label="panLabel"
        :disabled="disabled"
        @update:value="(value: number) => emit('update:pan', value)"
        @dragstart="emit('beginPan')"
        @dragend="emit('commitPan')"
      />
    </div>

    <div class="channel-strip__flags">
      <template v-if="!master">
        <button type="button" class="channel-strip__flag channel-strip__flag--mute" :class="{ 'channel-strip__flag--active': muted }" :aria-pressed="muted" :title="t(muted ? 'editor.unmuteTrack' : 'editor.muteTrack')" :aria-label="t(muted ? 'editor.unmuteTrack' : 'editor.muteTrack')" :disabled="disabled" @keydown.space.stop @click="emit('toggleMute')">M</button>
        <button type="button" class="channel-strip__flag channel-strip__flag--solo" :class="{ 'channel-strip__flag--active': solo }" :aria-pressed="solo" :title="t(solo ? 'editor.unsoloTrack' : 'editor.soloTrack')" :aria-label="t(solo ? 'editor.unsoloTrack' : 'editor.soloTrack')" :disabled="disabled" @keydown.space.stop @click="emit('toggleSolo')">S</button>
      </template>
    </div>

    <div class="channel-strip__fader-area" :title="t('editor.channelLevelStereo', { left: formatLevel(meterLevels[0]), right: formatLevel(meterLevels[1]) })">
      <div class="channel-strip__fader">
        <EditorParameterSlider
          vertical
          :value="volume" :value-text="formatVolumeDb(volume)"
          :reset-value="1"
          :label="volumeLabel"
          :reset-label="t('editor.sliderDefaultVolume')"
          :min="0"
          :max="2"
          :step="0.01"
          :tooltip="false"
          :disabled="disabled"
          @update:value="(value: number) => emit('update:volume', value)"
          @dragstart="emit('beginVolume')"
          @dragend="emit('commitVolume')"
        />
      </div>
      <div class="channel-strip__meter">
        <div v-for="(value, index) in meterLevels" :key="index" class="channel-strip__meter-bar" role="meter" :aria-label="`${t('editor.channelLevel')} · ${t(index === 0 ? 'editor.panLeft' : 'editor.panRight')}`" :aria-valuemin="-60" :aria-valuemax="0" :aria-valuenow="meterValue(value)" :aria-valuetext="`${formatLevel(value)} dBFS`">
          <span :class="{ 'channel-strip__meter-fill--full': value >= 1 }" :style="{ clipPath: `inset(${(1 - meterScale(value)) * 100}% 0 0)` }" />
        </div>
        <div class="channel-strip__meter-scale" aria-hidden="true">
          <span v-for="tick in METER_TICKS" :key="tick.db" :style="{ top: `${(1 - tick.position) * 100}%` }">{{ tick.db === 0 ? '0' : `−${Math.abs(tick.db)}` }}</span>
        </div>
      </div>
    </div>

    <footer class="channel-strip__readout">
      <strong>{{ formatVolumeDb(volume) }}</strong>
      <small>{{ formatLevel(peak) }} dBFS</small>
    </footer>
  </section>
</template>

<style scoped>
.channel-strip {
  display: grid;
  grid-template-rows: auto auto auto auto minmax(0, 1fr) auto;
  gap: 6px;
  width: 100px;
  min-height: 0;
  padding: 8px 7px 6px;
  border: 1px solid color-mix(in srgb, var(--outline) 72%, transparent);
  border-radius: 8px;
  background: var(--surface-1);
}

.channel-strip--selected {
  border-color: var(--primary);
  box-shadow: inset 0 0 0 1px var(--primary);
}

.channel-strip--master {
  background: color-mix(in srgb, var(--surface-2) 50%, var(--surface-1));
}

.channel-strip--dimmed .channel-strip__fader-area {
  opacity: 0.55;
}

.channel-strip__name {
  display: flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
  font-size: 12px;
  font-weight: 600;
}

.channel-strip__name i {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.channel-strip__name span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.channel-strip__inserts {
  padding-bottom: 6px;
  border-bottom: 1px solid color-mix(in srgb, var(--outline) 60%, transparent);
}

.channel-strip__pan {
  min-width: 0;
}

.channel-strip__flags {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px;
  min-height: 20px;
}

.channel-strip__flag {
  height: 20px;
  padding: 0;
  border: 1px solid var(--outline);
  border-radius: 4px;
  background: var(--surface-1);
  color: var(--on-surface-muted);
  font: inherit;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
}

.channel-strip__flag:hover:not(:disabled) { color: var(--on-surface); border-color: var(--outline-strong); }
.channel-strip__flag:focus-visible { outline: 2px solid var(--primary); outline-offset: 1px; }
.channel-strip__flag--mute.channel-strip__flag--active { border-color: transparent; background: var(--warning); color: #1b1b1b; }
.channel-strip__flag--solo.channel-strip__flag--active { border-color: transparent; background: var(--primary); color: #fff; }

.channel-strip__fader-area {
  position: relative;
  display: flex;
  justify-content: center;
  gap: 6px;
  min-height: 120px;
}

.channel-strip__fader { display: flex; flex-direction: column; }
.channel-strip__fader :deep(.editor-parameter-slider) { flex-direction: column; flex: 1; min-height: 0; gap: 0; }
.channel-strip__fader :deep(.editor-parameter-slider__control) { display: flex; justify-content: center; flex: 1; min-height: 0; }
.channel-strip__fader :deep(.n-slider) { height: 100%; }
/* Double-click or Alt+click on the fader handle resets it; the strip has no room for a reset button. */
.channel-strip__fader :deep(.editor-parameter-slider__reset) { display: none; }

.channel-strip__meter { display: grid; grid-template-columns: 5px 5px 18px; gap: 2px; }
.channel-strip__meter-bar { position: relative; overflow: hidden; background: var(--surface-2); border: 1px solid var(--outline); border-radius: 2px; }
.channel-strip__meter-bar > span { position: absolute; inset: 0; background: linear-gradient(180deg, rgba(255, 106, 106, 0.95), rgba(255, 170, 102, 0.92) 24%, rgba(92, 214, 132, 0.9) 60%); }
.channel-strip__meter-bar > .channel-strip__meter-fill--full { background: var(--error, var(--warning)); }
.channel-strip__meter-scale { position: relative; color: var(--on-surface-muted); font-size: 9px; line-height: 1; font-variant-numeric: tabular-nums; }
.channel-strip__meter-scale > span { position: absolute; left: 2px; white-space: nowrap; transform: translateY(-50%); }

.channel-strip__readout {
  display: grid;
  justify-items: center;
  gap: 1px;
  padding-top: 4px;
  border-top: 1px solid color-mix(in srgb, var(--outline) 60%, transparent);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.channel-strip__readout > strong { font-size: 12px; font-weight: 600; }
.channel-strip__readout > small { color: var(--on-surface-muted); font-size: 10px; }
</style>
