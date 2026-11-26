<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  AlertCircleOutline,
  ChevronDownOutline,
  ChevronForwardOutline,
  FolderOutline,
  MusicalNoteOutline,
} from '@vicons/ionicons5'
import type { EditorAssetTreeNode, EditorSource } from '@/types/editor'
import { formatTime } from '@/utils/editorTime'

defineOptions({
  name: 'EditorAssetTreeBranch',
})

const props = withDefaults(defineProps<{
  nodes: EditorAssetTreeNode[]
  expandedKeys: string[]
  trackColors: Record<string, string[]>
  depth?: number
}>(), {
  depth: 0,
})

const emit = defineEmits<{
  toggleFolder: [node: EditorAssetTreeNode]
  sourceAdd: [source: EditorSource]
  sourceContext: [payload: { event: MouseEvent; source: EditorSource }]
  sourcePointerGrab: [payload: { source: EditorSource; x: number; y: number }]
}>()

const { t } = useI18n()

const expandedSet = computed(() => new Set(props.expandedKeys))

function countNodeAssets(node: EditorAssetTreeNode): number {
  return node.assets.length + node.children.reduce((total, child) => total + countNodeAssets(child), 0)
}

function handleAssetPointerDown(event: MouseEvent, source: EditorSource) {
  if (event.button !== 0) return
  emit('sourcePointerGrab', {
    source,
    x: event.clientX,
    y: event.clientY,
  })
}

function formatAssetMeta(source: EditorSource) {
  if (source.missing) return t('editor.assetMissing')
  const duration = formatTime(source.duration)
  const channels = source.channels ? `${source.channels}ch` : '-'
  const sampleRate = source.sampleRate ? `${Math.round(source.sampleRate / 100) / 10}kHz` : '-'
  return `${duration} · ${channels} · ${sampleRate}`
}
</script>

<template>
  <div class="asset-branch">
    <template v-for="node in nodes" :key="node.key">
      <div class="asset-folder" :class="{ 'asset-folder--nested': depth > 0 }">
        <button type="button" class="asset-folder__head" @click="emit('toggleFolder', node)">
          <n-icon :component="expandedSet.has(node.key) ? ChevronDownOutline : ChevronForwardOutline" />
          <n-icon :component="FolderOutline" />
          <strong>{{ node.name }}</strong>
          <span>{{ countNodeAssets(node) }}</span>
        </button>

        <div v-if="expandedSet.has(node.key)" class="asset-folder__body">
          <EditorAssetTreeBranch
            v-if="node.children.length"
            :nodes="node.children"
            :expanded-keys="expandedKeys"
            :track-colors="trackColors"
            :depth="depth + 1"
            @toggle-folder="(child) => emit('toggleFolder', child)"
            @source-add="(source) => emit('sourceAdd', source)"
            @source-context="(payload) => emit('sourceContext', payload)"
            @source-pointer-grab="(payload) => emit('sourcePointerGrab', payload)"
          />

          <div
            v-for="source in node.assets"
            :key="source.id"
            class="asset-row asset-row--nested"
            :class="{ 'asset-row--missing': source.missing, 'asset-row--unused': !source.missing && !trackColors[source.id]?.length }"
            :title="source.path"
            @mousedown="handleAssetPointerDown($event, source)"
            @dblclick="emit('sourceAdd', source)"
            @contextmenu.stop.prevent="emit('sourceContext', { event: $event, source })"
          >
            <span class="asset-row__icon"><n-icon :component="source.missing ? AlertCircleOutline : MusicalNoteOutline" /></span>
            <span class="asset-row__body">
              <strong>{{ source.name }}</strong>
              <small>{{ formatAssetMeta(source) }}</small>
            </span>
            <span v-if="trackColors[source.id]?.length" class="asset-row__usage" :title="t('editor.assetUsedOnTracks', { count: trackColors[source.id].length })">
              <i v-for="(color, index) in trackColors[source.id].slice(0, 3)" :key="index" :style="{ background: color }" />
            </span>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.asset-branch {
  min-width: 0;
  display: grid;
  gap: 6px;
}

.asset-folder {
  min-width: 0;
  display: grid;
  gap: 4px;
}

.asset-folder--nested {
  margin-left: 8px;
}

.asset-folder__head {
  min-width: 0;
  display: grid;
  grid-template-columns: 14px 16px minmax(0, 1fr) auto;
  gap: 5px;
  align-items: center;
  padding: 4px 2px 6px;
  border: 0;
  border-radius: 6px;
  color: color-mix(in srgb, var(--on-surface) 92%, var(--on-surface-muted));
  background: transparent;
  cursor: pointer;
  text-align: left;
  transition: background 140ms ease, color 140ms ease, transform 140ms ease;
}

.asset-folder__head:hover {
  background: color-mix(in srgb, var(--surface-2) 52%, transparent);
  transform: translateX(1px);
}

.asset-folder__head strong {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  font-weight: 600;
}

.asset-folder__head span {
  color: var(--on-surface-muted);
  font-size: 11px;
  min-width: 16px;
  text-align: right;
}

.asset-folder__body {
  min-width: 0;
  display: grid;
  gap: 4px;
  padding-left: 8px;
  border-left: 1px solid color-mix(in srgb, var(--outline) 72%, transparent);
  margin-left: 5px;
}

.asset-row {
  position: relative;
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr) auto;
  gap: 6px;
  align-items: center;
  padding: 5px 6px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: grab;
  transition: border-color 140ms ease, background 140ms ease, transform 140ms ease;
}

.asset-row--nested {
  border-radius: 8px;
}

.asset-row:active {
  cursor: grabbing;
}

.asset-row:hover {
  border-color: color-mix(in srgb, var(--primary) 34%, transparent);
  background: color-mix(in srgb, var(--primary-soft) 45%, var(--surface-2));
  transform: translateX(1px);
}

.asset-row--missing {
  border-color: color-mix(in srgb, var(--warning) 34%, transparent);
  background: color-mix(in srgb, var(--warning) 9%, transparent);
}

.asset-row__icon {
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  border-radius: 6px;
  color: #ff7b54;
  background: rgba(255, 123, 84, 0.08);
}

.asset-row__body {
  min-width: 0;
  display: grid;
  gap: 1px;
}

.asset-row__body strong,
.asset-row__body small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.asset-row__body small {
  color: var(--on-surface-muted);
  font-size: 11px;
}

.asset-row__body strong {
  font-size: 12px;
  font-weight: 600;
}

.asset-row__usage {
  display: flex;
  align-items: center;
  gap: 2px;
}

.asset-row__usage i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}

.asset-row--unused .asset-row__body strong {
  color: var(--on-surface-muted);
  font-weight: 500;
}
</style>
