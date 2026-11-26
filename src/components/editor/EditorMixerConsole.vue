<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import EditorChannelStrip from '@/components/editor/EditorChannelStrip.vue'
import { useEditorPlaybackStore } from '@/stores/editorPlayback'
import type { EditorPluginSlot, EditorTrack } from '@/types/editor'

const props = defineProps<{
  tracks: EditorTrack[]
  masterVolume: number
  masterPan: number
  masterPluginChain?: EditorPluginSlot[]
  selectedTrackId: string | null
  /** Fixed height after a manual resize; automatic when null. */
  height: number | null
  disabled?: boolean
}>()

const emit = defineEmits<{
  selectTrack: [trackId: string | null]
  toggleTrackFlag: [trackId: string, flag: 'muted' | 'solo']
  setTrackVolume: [trackId: string, value: number]
  setTrackPan: [trackId: string, value: number]
  setMasterVolume: [value: number]
  setMasterPan: [value: number]
  beginEdit: []
  commitEdit: []
}>()

const { t } = useI18n()
const playback = useEditorPlaybackStore()
// Every strip reserves the rows of the longest chain, between one and four, so faders line up.
const MAX_INSERT_ROWS = 4
const insertRows = computed(() => Math.min(MAX_INSERT_ROWS, Math.max(1,
  props.masterPluginChain?.length || 0, ...props.tracks.map(track => track.pluginChain?.length || 0))))
</script>

<template>
  <section class="mixer-console" :aria-label="t('editor.mixerPanel')" :style="height === null ? undefined : { height: `${height}px` }">
    <div class="mixer-console__channels">
      <EditorChannelStrip
        v-for="track in tracks"
        :key="track.id"
        :track-id="track.id"
        :plugin-chain="track.pluginChain"
        :insert-rows="insertRows"
        :name="track.name"
        :color="track.color"
        :volume="track.volume"
        :pan="track.pan"
        :levels="playback.trackLevels[track.id]"
        :muted="track.muted"
        :solo="track.solo"
        :selected="track.id === selectedTrackId"
        :disabled="disabled"
        @select="emit('selectTrack', track.id)"
        @update:volume="(value) => emit('setTrackVolume', track.id, value)"
        @update:pan="(value) => emit('setTrackPan', track.id, value)"
        @begin-volume="emit('beginEdit')"
        @commit-volume="emit('commitEdit')"
        @begin-pan="emit('beginEdit')"
        @commit-pan="emit('commitEdit')"
        @toggle-mute="emit('toggleTrackFlag', track.id, 'muted')"
        @toggle-solo="emit('toggleTrackFlag', track.id, 'solo')"
      />
    </div>
    <div class="mixer-console__master">
      <EditorChannelStrip
        master
        :track-id="null"
        :plugin-chain="masterPluginChain"
        :insert-rows="insertRows"
        :name="t('editor.plugins.masterOutput')"
        :volume="masterVolume"
        :pan="masterPan"
        :levels="playback.masterLevel"
        :selected="selectedTrackId === null"
        :disabled="disabled"
        @select="emit('selectTrack', null)"
        @update:volume="(value) => emit('setMasterVolume', value)"
        @update:pan="(value) => emit('setMasterPan', value)"
        @begin-volume="emit('beginEdit')"
        @commit-volume="emit('commitEdit')"
        @begin-pan="emit('beginEdit')"
        @commit-pan="emit('commitEdit')"
      />
    </div>
  </section>
</template>

<style scoped>
.mixer-console {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  min-height: 0;
  border-top: 1px solid var(--outline);
  background: color-mix(in srgb, var(--surface) 90%, var(--surface-1));
}

.mixer-console__channels {
  display: flex;
  gap: 8px;
  min-width: 0;
  min-height: 0;
  padding: 10px 12px;
  overflow-x: auto;
  overflow-y: hidden;
  scrollbar-width: thin;
}

.mixer-console__master {
  display: flex;
  min-height: 0;
  padding: 10px 12px;
  border-left: 1px solid var(--outline);
}
</style>
