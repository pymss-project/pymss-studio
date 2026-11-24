<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  AlertCircleOutline,
  MusicalNoteOutline,
  SearchOutline,
} from '@vicons/ionicons5'
import type { DropdownOption } from 'naive-ui'
import type { EditorAssetTreeNode, EditorSource } from '@/types/editor'
import { formatTime } from '@/utils/editorTime'
import EditorAssetTreeBranch from '@/components/editor/EditorAssetTreeBranch.vue'

const props = defineProps<{
  sources: EditorSource[]
  tree: EditorAssetTreeNode[]
  externalDragging: boolean
}>()

const emit = defineEmits<{
  sourceAdd: [source: EditorSource]
  sourceReveal: [source: EditorSource]
  sourceRelink: [source: EditorSource]
  sourceRemove: [source: EditorSource]
  sourcePointerGrab: [payload: { source: EditorSource; x: number; y: number }]
}>()

const { t } = useI18n()
const search = ref('')
const expandedKeys = ref<string[]>([])
const menuX = ref(0)
const menuY = ref(0)
const showMenu = ref(false)
const contextSource = ref<EditorSource | null>(null)
const localSources = computed(() => props.sources.filter((source) => source.role === 'stem'))
const externalSources = computed(() => props.sources.filter((source) => source.role === 'reference'))
const totalCount = computed(() => props.sources.length)

const menuOptions = computed<DropdownOption[]>(() => {
  const source = contextSource.value
  if (!source) return []
  const options: DropdownOption[] = [{
    key: 'reveal',
    label: t('editor.menuRevealAsset'),
  }]
  if (source.missing) {
    options.unshift({
      key: 'relink',
      label: t('editor.assetRelink'),
    })
  }
  if (source.role === 'reference') {
    options.push({
      key: 'remove',
      label: t('editor.menuRemoveAsset'),
    })
  }
  return options
})

const filteredLocalSources = computed(() => {
  const query = search.value.trim().toLowerCase()
  if (!query) return localSources.value
  return localSources.value.filter((source) => `${source.name} ${source.path}`.toLowerCase().includes(query))
})

const filteredExternalSources = computed(() => {
  const query = search.value.trim().toLowerCase()
  if (!query) return externalSources.value
  return externalSources.value.filter((source) => `${source.name} ${source.path}`.toLowerCase().includes(query))
})

function createVisibleTree(role: EditorSource['role']) {
  const query = search.value.trim().toLowerCase()
  if (query) return []
  const activeIds = new Set(props.sources.filter((source) => source.role === role).map((source) => source.id))
  const filterNode = (node: EditorAssetTreeNode): EditorAssetTreeNode | null => {
    const children = node.children
      .map(filterNode)
      .filter((child): child is EditorAssetTreeNode => Boolean(child))
    const assets = node.assets.filter((asset) => activeIds.has(asset.id))
    if (!children.length && !assets.length) return null
    return {
      ...node,
      children,
      assets,
    }
  }
  return props.tree
    .map(filterNode)
    .filter((node): node is EditorAssetTreeNode => Boolean(node))
}

const localTree = computed(() => createVisibleTree('stem'))
const externalTree = computed(() => createVisibleTree('reference'))

watch(
  () => props.tree,
  (tree) => {
    const keys = new Set(expandedKeys.value)
    const visit = (node: EditorAssetTreeNode) => {
      if (node.expanded) keys.add(node.key)
      node.children.forEach(visit)
    }
    tree.forEach(visit)
    expandedKeys.value = Array.from(keys)
  },
  { immediate: true },
)

function toggleFolder(node: EditorAssetTreeNode) {
  const keys = new Set(expandedKeys.value)
  if (keys.has(node.key)) keys.delete(node.key)
  else keys.add(node.key)
  expandedKeys.value = Array.from(keys)
}

function openContextMenu(event: MouseEvent, source: EditorSource) {
  contextSource.value = source
  showMenu.value = false
  menuX.value = event.clientX
  menuY.value = event.clientY
  window.requestAnimationFrame(() => {
    showMenu.value = true
  })
}

function handleMenuSelect(key: string | number) {
  const source = contextSource.value
  showMenu.value = false
  if (!source) return

  if (key === 'relink') emit('sourceRelink', source)
  if (key === 'reveal') emit('sourceReveal', source)
  if (key === 'remove' && source.role === 'reference') emit('sourceRemove', source)
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

function rowClass(source: EditorSource) {
  return {
    'asset-row--missing': Boolean(source.missing),
  }
}

function hasSectionContent(type: 'local' | 'external') {
  if (search.value.trim()) {
    return type === 'local' ? filteredLocalSources.value.length > 0 : filteredExternalSources.value.length > 0
  }
  return type === 'local' ? localSources.value.length > 0 : externalSources.value.length > 0
}
</script>

<template>
  <aside
    class="editor-assets"
    :class="{
      'editor-assets--dragging': externalDragging,
    }"
  >
    <div class="editor-assets__head">
      <strong>{{ t('editor.assetLibrary') }}</strong>
      <span class="editor-assets__summary">{{ t('editor.assetCount', { count: totalCount }) }}</span>
    </div>

    <n-input v-model:value="search" size="small" clearable :placeholder="t('editor.assetSearch')">
      <template #prefix><n-icon :component="SearchOutline" /></template>
    </n-input>

    <div class="editor-assets__list">
      <template v-if="search">
        <div
          v-for="source in filteredLocalSources"
          :key="source.id"
          class="asset-row"
          :class="rowClass(source)"
          :title="source.path"
          @mousedown="handleAssetPointerDown($event, source)"
          @dblclick="emit('sourceAdd', source)"
          @contextmenu.stop.prevent="openContextMenu($event, source)"
        >
          <span class="asset-row__icon"><n-icon :component="source.missing ? AlertCircleOutline : MusicalNoteOutline" /></span>
          <span class="asset-row__body">
            <strong>{{ source.name }}</strong>
            <small>{{ formatAssetMeta(source) }}</small>
          </span>
        </div>

        <div
          v-for="source in filteredExternalSources"
          :key="source.id"
          class="asset-row"
          :class="rowClass(source)"
          :title="source.path"
          @mousedown="handleAssetPointerDown($event, source)"
          @dblclick="emit('sourceAdd', source)"
          @contextmenu.stop.prevent="openContextMenu($event, source)"
        >
          <span class="asset-row__icon"><n-icon :component="source.missing ? AlertCircleOutline : MusicalNoteOutline" /></span>
          <span class="asset-row__body">
            <strong>{{ source.name }}</strong>
            <small>{{ formatAssetMeta(source) }}</small>
          </span>
        </div>
      </template>

      <template v-else>
        <EditorAssetTreeBranch
          v-if="localTree.length"
          :nodes="localTree"
          :expanded-keys="expandedKeys"
          @toggle-folder="toggleFolder"
          @source-add="(source) => emit('sourceAdd', source)"
          @source-context="({ event, source }) => openContextMenu(event, source)"
          @source-pointer-grab="(payload) => emit('sourcePointerGrab', payload)"
        />
        <EditorAssetTreeBranch
          v-if="externalTree.length"
          :nodes="externalTree"
          :expanded-keys="expandedKeys"
          @toggle-folder="toggleFolder"
          @source-add="(source) => emit('sourceAdd', source)"
          @source-context="({ event, source }) => openContextMenu(event, source)"
          @source-pointer-grab="(payload) => emit('sourcePointerGrab', payload)"
        />
      </template>

      <div v-if="!hasSectionContent('local') && !hasSectionContent('external')" class="editor-assets__empty">
        <strong>{{ search.trim() ? t('editor.assetSearchEmptyTitle') : t('editor.importEmpty') }}</strong>
        <span>{{ search.trim() ? t('editor.assetSearchEmptyHint') : t('editor.assetEmptyHint') }}</span>
      </div>
    </div>

    <n-dropdown
      placement="bottom-start"
      trigger="manual"
      :x="menuX"
      :y="menuY"
      :options="menuOptions"
      :show="showMenu"
      @clickoutside="showMenu = false"
      @select="handleMenuSelect"
    />
  </aside>
</template>

<style scoped>
.editor-assets {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 8px 10px;
  border-right: 1px solid var(--outline);
  background: color-mix(in srgb, var(--surface) 90%, var(--surface-1));
  transition: background 180ms ease, border-color 180ms ease;
}

.editor-assets--dragging {
  background: color-mix(in srgb, var(--primary-soft) 16%, var(--surface));
  border-right-color: color-mix(in srgb, var(--primary) 24%, transparent);
}

.editor-assets__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 2px 2px 4px;
  border-bottom: 1px solid color-mix(in srgb, var(--outline) 34%, transparent);
}

.editor-assets__head strong,
.editor-assets__head span,
.asset-row__body strong,
.asset-row__body small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.editor-assets__head strong {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--on-surface-muted);
}

.editor-assets__summary {
  color: var(--on-surface-muted);
  font-size: 10px;
  opacity: 0.8;
}

.editor-assets__list {
  min-height: 0;
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  display: grid;
  align-content: start;
  gap: 6px;
  transition: opacity 160ms ease, transform 160ms ease;
  padding-top: 2px;
}

.editor-assets--dragging .editor-assets__list {
  opacity: 0.94;
  transform: translateY(2px);
}

.editor-assets__empty {
  display: grid;
  gap: 5px;
  padding: 12px;
  border-radius: 12px;
  background: color-mix(in srgb, var(--surface-2) 82%, transparent);
  color: var(--on-surface-muted);
  font-size: 12px;
}

.asset-row {
  position: relative;
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr);
  gap: 6px;
  align-items: center;
  padding: 4px 6px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: grab;
  transition: border-color 140ms ease, background 140ms ease;
}

.asset-row:active {
  cursor: grabbing;
}

.asset-row:hover {
  border-color: color-mix(in srgb, var(--outline) 54%, transparent);
  background: color-mix(in srgb, var(--surface-2) 74%, transparent);
}

.asset-row--missing {
  border-color: color-mix(in srgb, var(--warning) 36%, transparent);
  background: color-mix(in srgb, var(--warning) 9%, transparent);
}

.asset-row--missing .asset-row__icon {
  color: color-mix(in srgb, var(--warning) 78%, var(--primary));
  background: color-mix(in srgb, var(--warning) 12%, transparent);
}

.asset-row--missing .asset-row__body small {
  color: color-mix(in srgb, var(--warning) 68%, var(--on-surface-muted));
}

.asset-row__icon {
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  border-radius: 4px;
  color: color-mix(in srgb, var(--primary) 78%, var(--on-surface-muted));
  background: color-mix(in srgb, var(--surface-2) 82%, transparent);
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

.asset-row__body strong {
  font-size: 11px;
  font-weight: 600;
}

.asset-row__body small {
  color: var(--on-surface-muted);
  font-size: 10px;
}

.asset-row__menu-indicator {
  position: absolute;
  top: 50%;
  right: 6px;
  width: 16px;
  height: 16px;
  display: grid;
  place-items: center;
  color: var(--on-surface-muted);
  opacity: 0.34;
  transform: translateY(-50%);
}

.asset-row__menu-indicator--lock {
  opacity: 0.42;
}
</style>
