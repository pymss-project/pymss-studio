<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { AddOutline, ArrowDownOutline, ArrowUpOutline, OptionsOutline, PowerOutline, TrashOutline } from '@vicons/ionicons5'
import { useEditorInsertHost } from '@/composables/useEditorInsertHost'
import type { EditorPluginSlot } from '@/types/editor'
import { pluginDescriptorVariant } from '@/utils/editorPlugins'

const props = defineProps<{
  /** Channel the list acts on; null addresses the master bus. */
  trackId: string | null
  chain: EditorPluginSlot[]
  /** Mixer strips use a dense slot column; the inspector shows the full list. */
  compact?: boolean
  /** Visible slot rows in the compact layout; longer chains scroll. */
  rows?: number
  disabled?: boolean
}>()

const { t } = useI18n()
const hostRef = useEditorInsertHost()
const host = computed(() => hostRef.value)
const enabled = computed(() => Boolean(host.value?.capabilities.value?.enabled))
const busy = computed(() => props.disabled || Boolean(host.value?.busy.value))
const menu = ref<{ slotId: string; x: number; y: number } | null>(null)

// Pointer reordering: Tauri handles OS drag and drop on Windows, so HTML5 drag events never fire.
const DRAG_THRESHOLD = 4
const listEl = ref<HTMLElement | null>(null)
const dragSlotId = ref<string | null>(null)
const dropIndex = ref<number | null>(null)
let pressed: { slotId: string; x: number; y: number } | null = null
let suppressClick = false

function dropIndexAt(clientY: number) {
  const items = Array.from(listEl.value?.querySelectorAll<HTMLElement>('[data-insert-slot]') || [])
  const index = items.findIndex(item => {
    const rect = item.getBoundingClientRect()
    return clientY < rect.top + rect.height / 2
  })
  return index < 0 ? items.length : index
}

function startSlotPointer(event: PointerEvent, slot: EditorPluginSlot) {
  if (event.button !== 0 || busy.value || !host.value || props.chain.length < 2) return
  pressed = { slotId: slot.id, x: event.clientX, y: event.clientY }
  window.addEventListener('pointermove', moveSlotPointer)
  window.addEventListener('pointerup', finishSlotPointer)
  window.addEventListener('pointercancel', cancelSlotPointer)
}

function moveSlotPointer(event: PointerEvent) {
  if (!pressed) return
  if (!dragSlotId.value && Math.hypot(event.clientX - pressed.x, event.clientY - pressed.y) < DRAG_THRESHOLD) return
  dragSlotId.value = pressed.slotId
  dropIndex.value = dropIndexAt(event.clientY)
}

function releaseSlotPointer() {
  window.removeEventListener('pointermove', moveSlotPointer)
  window.removeEventListener('pointerup', finishSlotPointer)
  window.removeEventListener('pointercancel', cancelSlotPointer)
  const dragged = dragSlotId.value
  const target = dropIndex.value
  pressed = null
  dragSlotId.value = null
  dropIndex.value = null
  return { dragged, target }
}

function finishSlotPointer() {
  const { dragged, target } = releaseSlotPointer()
  if (!dragged || target === null) return
  // The release ends a drag, not a click on the slot under the pointer.
  suppressClick = true
  setTimeout(() => { suppressClick = false })
  const from = props.chain.findIndex(slot => slot.id === dragged)
  const to = target > from ? target - 1 : target
  if (from >= 0 && to !== from && host.value) void host.value.move(props.trackId, dragged, to - from)
}

function cancelSlotPointer() {
  releaseSlotPointer()
}

function swallowDragClick(event: MouseEvent) {
  if (!suppressClick) return
  event.preventDefault()
  event.stopPropagation()
}

onBeforeUnmount(releaseSlotPointer)

const slotName = (slot: EditorPluginSlot) => slot.name || slot.pluginId
const isOpened = (slot: EditorPluginSlot) => Boolean(host.value?.opened.value.includes(slot.id))
const unavailable = (slot: EditorPluginSlot) => host.value?.unavailablePlugins.value.find(item => item.slotId === slot.id)
function canOpenEditor(slot: EditorPluginSlot) {
  const capabilities = host.value?.capabilities.value
  return Boolean(capabilities?.nativeEditor && (capabilities.nativeEditorFormats || ['vst3']).includes(slot.format) && !unavailable(slot))
}
// Slots without a native editor open their parameters in the plugin manager instead.
function activate(slot: EditorPluginSlot) {
  if (busy.value || !host.value) return
  if (canOpenEditor(slot)) void host.value.toggleEditor(props.trackId, slot.id)
  else host.value.openParameters(props.trackId, slot.id)
}

const menuSlotIndex = computed(() => props.chain.findIndex(slot => slot.id === menu.value?.slotId))
const menuOptions = computed(() => [
  { key: 'parameters', label: t('editor.plugins.parameters') },
  { key: 'up', label: t('editor.plugins.moveUp'), disabled: menuSlotIndex.value <= 0 },
  { key: 'down', label: t('editor.plugins.moveDown'), disabled: menuSlotIndex.value < 0 || menuSlotIndex.value >= props.chain.length - 1 },
  { type: 'divider', key: 'divider' },
  { key: 'remove', label: t('editor.plugins.remove') },
])
async function openMenu(event: MouseEvent, slot: EditorPluginSlot) {
  if (busy.value) return
  menu.value = null
  await nextTick()
  menu.value = { slotId: slot.id, x: event.clientX, y: event.clientY }
}
function runMenuAction(key: string) {
  const slotId = menu.value?.slotId
  menu.value = null
  if (!slotId || !host.value) return
  if (key === 'parameters') host.value.openParameters(props.trackId, slotId)
  else if (key === 'up' || key === 'down') void host.value.move(props.trackId, slotId, key === 'up' ? -1 : 1)
  else if (key === 'remove') void host.value.remove(props.trackId, slotId)
}
</script>

<template>
  <section
    v-if="host && enabled"
    class="insert-list"
    :class="{ 'insert-list--compact': compact }"
    :aria-busy="busy"
    :aria-label="t('editor.plugins.inserts')"
    @keydown.stop="host.handleKeydown"
  >
    <header class="insert-list__header">
      <strong>{{ t('editor.plugins.inserts') }}<span>{{ chain.length }}</span></strong>
      <n-button
        v-if="!compact"
        size="tiny"
        quaternary
        :type="host.operationError.value ? 'warning' : 'default'"
        :aria-label="t('editor.plugins.title')"
        aria-haspopup="dialog"
        :aria-expanded="host.managerOpen.value"
        :title="t('editor.plugins.manage')"
        @click="host.openManager(trackId)"
      ><template #icon><n-icon :component="AddOutline" /></template>{{ t('editor.plugins.library') }}</n-button>
      <button
        v-else
        type="button"
        class="insert-list__icon-button"
        :disabled="busy"
        :title="t('editor.plugins.addInsert')"
        :aria-label="t('editor.plugins.addInsert')"
        @click="host.addInsert(trackId)"
      ><n-icon :component="AddOutline" :size="12" /></button>
    </header>

    <ol
      v-if="compact"
      ref="listEl"
      class="insert-list__slots"
      :class="{ 'insert-list--dragging': dragSlotId, 'insert-list--drop-end': dropIndex === chain.length }"
      :style="{ height: `${(rows ?? 4) * 22 - 2}px` }"
      :aria-label="t('editor.plugins.chainTitle')"
      @click.capture="swallowDragClick"
    >
      <li
        v-for="(slot, index) in chain"
        :key="slot.id"
        data-insert-slot
        class="insert-slot"
        :class="{ 'insert-slot--bypassed': slot.bypassed, 'insert-slot--open': isOpened(slot), 'insert-slot--missing': unavailable(slot), 'insert-slot--dragging': dragSlotId === slot.id, 'insert-slot--drop-before': dropIndex === index }"
        @pointerdown="startSlotPointer($event, slot)"
        @contextmenu.prevent="openMenu($event, slot)"
      >
        <button
          type="button"
          class="insert-slot__power"
          :aria-pressed="!slot.bypassed"
          :disabled="busy"
          :title="t('editor.plugins.enableNamed', { name: slotName(slot) })"
          :aria-label="t('editor.plugins.enableNamed', { name: slotName(slot) })"
          @click="host.setBypassed(trackId, slot.id, !slot.bypassed)"
        ><n-icon :component="PowerOutline" :size="10" /></button>
        <button
          type="button"
          class="insert-slot__name"
          data-editor-transport
          :disabled="busy"
          :title="unavailable(slot)?.message || slotName(slot)"
          :aria-label="t(isOpened(slot) ? 'editor.plugins.closeNamedEditor' : 'editor.plugins.openNamedEditor', { name: slotName(slot) })"
          @click="activate(slot)"
        >{{ slotName(slot) }}</button>
      </li>
      <li v-if="!chain.length" class="insert-list__empty">
        <button type="button" :disabled="busy" @click="host.addInsert(trackId)">{{ t('editor.plugins.addInsert') }}</button>
      </li>
    </ol>

    <template v-else>
      <ol
        v-if="chain.length"
        ref="listEl"
        class="insert-list__inserts"
        :class="{ 'insert-list--dragging': dragSlotId, 'insert-list--drop-end': dropIndex === chain.length }"
        :aria-label="t('editor.plugins.chainTitle')"
        @click.capture="swallowDragClick"
      >
        <li
          v-for="(slot, index) in chain"
          :key="slot.id"
          data-insert-slot
          class="plugin-insert"
          :class="{ 'plugin-insert--bypassed': slot.bypassed, 'insert-slot--dragging': dragSlotId === slot.id, 'insert-slot--drop-before': dropIndex === index }"
          @pointerdown="startSlotPointer($event, slot)"
        >
          <div class="plugin-insert__main">
            <span class="plugin-insert__number">{{ index + 1 }}</span>
            <n-button class="plugin-insert__editor" data-editor-transport size="small" text :type="isOpened(slot) ? 'primary' : 'default'" :disabled="busy || !canOpenEditor(slot)" :aria-label="t(isOpened(slot) ? 'editor.plugins.closeNamedEditor' : 'editor.plugins.openNamedEditor', { name: slotName(slot) })" :title="slotName(slot)" @click="host.toggleEditor(trackId, slot.id)">{{ slotName(slot) }}</n-button>
            <n-switch size="small" :value="!slot.bypassed" :disabled="busy" :aria-label="t('editor.plugins.enableNamed', { name: slotName(slot) })" @update:value="(value: boolean) => host?.setBypassed(trackId, slot.id, !value)" />
          </div>
          <div class="plugin-insert__tools">
            <span class="plugin-insert__format">{{ pluginDescriptorVariant({ ...slot, id: slot.pluginId }) }}</span>
            <span v-if="unavailable(slot)" class="plugin-insert__missing" :title="unavailable(slot)?.message">{{ t('editor.plugins.insertUnavailable') }}</span>
            <div>
              <n-button size="tiny" quaternary :disabled="busy || index === 0" :title="t('editor.plugins.moveUp')" :aria-label="t('editor.plugins.moveUp')" @click="host.move(trackId, slot.id, -1)"><template #icon><n-icon :component="ArrowUpOutline" /></template></n-button>
              <n-button size="tiny" quaternary :disabled="busy || index === chain.length - 1" :title="t('editor.plugins.moveDown')" :aria-label="t('editor.plugins.moveDown')" @click="host.move(trackId, slot.id, 1)"><template #icon><n-icon :component="ArrowDownOutline" /></template></n-button>
              <n-button size="tiny" quaternary :disabled="busy" :title="t('editor.plugins.parameters')" :aria-label="t('editor.plugins.parameters')" @click="host.openParameters(trackId, slot.id)"><template #icon><n-icon :component="OptionsOutline" /></template></n-button>
              <n-button size="tiny" quaternary type="error" :disabled="busy" :title="t('editor.plugins.remove')" :aria-label="t('editor.plugins.remove')" @click="host.remove(trackId, slot.id)"><template #icon><n-icon :component="TrashOutline" /></template></n-button>
            </div>
          </div>
        </li>
      </ol>
      <button type="button" class="insert-list__add" :disabled="busy" @click="host.addInsert(trackId)"><n-icon :component="AddOutline" />{{ t('editor.plugins.addInsert') }}</button>
      <button v-if="host.operationError.value" type="button" class="insert-list__attention" :title="host.operationError.value" @click="host.openManager(trackId)">{{ t(host.dialogPending.value ? 'editor.plugins.insertWaiting' : 'editor.plugins.attentionRequired') }}</button>
    </template>

    <n-dropdown
      v-if="compact"
      trigger="manual"
      placement="bottom-start"
      size="small"
      :show="Boolean(menu)"
      :x="menu?.x ?? 0"
      :y="menu?.y ?? 0"
      :options="menuOptions"
      @select="runMenuAction"
      @clickoutside="menu = null"
    />
  </section>
</template>

<style scoped>
.insert-list { container: insert-list / inline-size; min-width: 0; }
.insert-list--dragging, .insert-list--dragging * { cursor: grabbing; }
.insert-list--dragging [data-insert-slot] { position: relative; }
.insert-slot--dragging { opacity: 0.5; }
.insert-slot--drop-before::before,
.insert-list--drop-end > [data-insert-slot]:last-child::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  height: 2px;
  border-radius: 1px;
  background: var(--primary);
  pointer-events: none;
  z-index: 1;
}
.insert-slot--drop-before::before { top: -1px; }
.insert-list--drop-end > [data-insert-slot]:last-child::after { bottom: -1px; }
.insert-list:not(.insert-list--compact) { padding: 8px 7px; }
.insert-list__header { display: flex; justify-content: space-between; align-items: center; gap: 6px; margin-bottom: 6px; }
.insert-list__header > strong { display: flex; align-items: baseline; gap: 5px; min-width: 0; font-size: 11px; font-weight: 600; }
.insert-list__header > strong > span { font-size: 10px; font-weight: 400; color: var(--on-surface-muted); font-variant-numeric: tabular-nums; }
.insert-list__header > .n-button { margin-left: auto; }

.insert-list__inserts { display: grid; gap: 3px; margin: 0; padding: 0; list-style: none; }
.plugin-insert { min-width: 0; padding: 3px 4px 2px; border: 1px solid var(--outline); border-left: 3px solid var(--primary); border-radius: 4px; background: var(--surface-1); }
.plugin-insert--bypassed { border-left-color: var(--outline-strong); background: transparent; }
.plugin-insert--bypassed .plugin-insert__editor { opacity: .55; }
.plugin-insert__main { display: flex; align-items: center; gap: 5px; min-width: 0; }
.plugin-insert__number { flex-shrink: 0; width: 10px; font-size: 9px; color: var(--on-surface-muted); font-variant-numeric: tabular-nums; }
.plugin-insert__editor { flex: 1; justify-content: flex-start; overflow: hidden; min-width: 0; font-size: 11px; }
.plugin-insert__editor :deep(.n-button__content) { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.plugin-insert__main > .n-switch { flex-shrink: 0; }
.plugin-insert__tools { display: flex; flex-wrap: wrap; align-items: center; gap: 3px; min-width: 0; padding-left: 15px; opacity: .5; transition: opacity 120ms; }
.plugin-insert:hover .plugin-insert__tools, .plugin-insert:focus-within .plugin-insert__tools { opacity: 1; }
.plugin-insert__format { flex-shrink: 0; padding: 0 4px; border-radius: 4px; background: var(--surface-2); font-size: 9px; line-height: 1.5; }
.plugin-insert__tools > div { display: flex; flex-shrink: 0; gap: 0; margin-left: auto; }
.plugin-insert__tools :deep(.n-button) { width: 20px; padding: 0; }
.plugin-insert__missing { color: var(--warning); font-size: 10px; }
.insert-list__add { display: flex; align-items: center; justify-content: center; gap: 6px; width: 100%; min-height: 28px; margin-top: 3px; border: 1px dashed var(--outline-strong); border-radius: 4px; background: transparent; color: var(--on-surface-muted); cursor: pointer; font: inherit; font-size: 11px; }
.insert-list__add:hover:not(:disabled) { color: var(--primary); border-color: var(--primary-border); background: var(--primary-softer); }
.insert-list__attention { display: block; width: 100%; margin-top: 7px; padding: 0; border: 0; background: transparent; color: var(--warning); text-align: left; font: inherit; font-size: 11px; cursor: pointer; }
.insert-list__add:focus-visible, .insert-list__attention:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }

/* Mixer strip: fixed-height slot column so every strip keeps its fader aligned. */
.insert-list--compact .insert-list__header { margin-bottom: 3px; }
.insert-list--compact .insert-list__header > strong { font-size: 10px; color: var(--on-surface-muted); font-weight: 500; }
.insert-list__icon-button { display: grid; place-items: center; flex: none; width: 18px; height: 18px; padding: 0; border: 0; border-radius: 3px; background: transparent; color: var(--on-surface-muted); cursor: pointer; }
.insert-list__icon-button:hover:not(:disabled) { background: var(--surface-2); color: var(--primary); }
.insert-list__slots { display: grid; align-content: start; gap: 2px; margin: 0; padding: 0; overflow-y: auto; list-style: none; scrollbar-width: none; }
.insert-slot { display: flex; align-items: center; min-width: 0; height: 20px; border: 1px solid var(--outline); border-radius: 3px; background: var(--surface-2); }
.insert-slot--open { border-color: var(--primary-border); }
.insert-slot--missing .insert-slot__name { color: var(--warning); }
.insert-slot--bypassed .insert-slot__name { color: var(--on-surface-muted); text-decoration: line-through; }
.insert-slot__power { display: grid; place-items: center; flex: none; width: 16px; height: 100%; padding: 0; border: 0; border-right: 1px solid var(--outline); background: transparent; color: var(--primary); cursor: pointer; }
.insert-slot--bypassed .insert-slot__power { color: var(--on-surface-muted); }
.insert-slot__name { flex: 1; min-width: 0; height: 100%; padding: 0 4px; overflow: hidden; border: 0; background: transparent; color: var(--on-surface); font: inherit; font-size: 10px; text-align: start; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
.insert-slot__name:hover:not(:disabled) { background: color-mix(in srgb, var(--primary) 10%, transparent); }
.insert-list__empty > button { width: 100%; height: 20px; padding: 0 4px; overflow: hidden; border: 1px dashed var(--outline); border-radius: 3px; background: transparent; color: var(--on-surface-muted); font: inherit; font-size: 10px; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
.insert-list__empty > button:hover:not(:disabled) { border-color: var(--primary-border); color: var(--primary); }
.insert-slot button:focus-visible, .insert-list__icon-button:focus-visible, .insert-list__empty > button:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
.insert-list button:disabled { cursor: default; opacity: .55; }

@container insert-list (max-width: 145px) {
  .insert-list:not(.insert-list--compact) .insert-list__header { gap: 4px; }
  .plugin-insert__tools { padding-left: 0; }
  .plugin-insert__main { gap: 3px; }
}
</style>
