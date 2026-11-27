<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { EDITOR_SHORTCUT_GROUPS, SPACE_KEY, shortcutKeyCaps, type ShortcutChord } from '@/utils/editorShortcuts'

defineProps<{ show: boolean }>()
const emit = defineEmits<{ 'update:show': [value: boolean] }>()

const { t } = useI18n()
const isMac = typeof navigator !== 'undefined' && /Mac/i.test(navigator.platform)
const groups = ref<HTMLElement | null>(null)

function keyCaps(chord: ShortcutChord) {
  return shortcutKeyCaps(chord, isMac).map(cap => cap === SPACE_KEY ? t('editor.shortcuts.keySpace') : cap)
}
</script>

<template>
  <n-modal
    :show="show" preset="card" class="editor-shortcuts" style="width: min(640px, calc(100vw - 48px))"
    :title="t('editor.shortcuts.title')" :aria-label="t('editor.shortcuts.title')" :bordered="false"
    @update:show="(value: boolean) => emit('update:show', value)" @after-enter="groups?.focus()"
  >
    <!-- The list is the only content: focusing it lets arrow keys scroll it right away. -->
    <div ref="groups" class="editor-shortcuts__groups" tabindex="0" role="region" :aria-label="t('editor.shortcuts.title')">
      <table v-for="group in EDITOR_SHORTCUT_GROUPS" :key="group.title" class="editor-shortcuts__group">
        <caption>{{ t(group.title) }}</caption>
        <tbody>
          <tr v-for="shortcut in group.shortcuts" :key="shortcut.command">
            <th scope="row">{{ t(shortcut.label) }}</th>
            <td>
              <span v-for="(chord, index) in shortcut.chords" :key="index" class="editor-shortcuts__chord">
                <span v-if="index" class="editor-shortcuts__or">{{ t('editor.shortcuts.or') }}</span>
                <kbd v-for="cap in keyCaps(chord)" :key="cap">{{ cap }}</kbd>
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </n-modal>
</template>

<style scoped>
.editor-shortcuts__groups { display: grid; gap: 18px; max-height: min(560px, calc(100dvh - 180px)); overflow-y: auto; }
.editor-shortcuts__group { width: 100%; border-collapse: collapse; font-size: 13px; }
.editor-shortcuts__group caption { padding-bottom: 6px; text-align: left; font-weight: 600; color: var(--on-surface-muted); }
.editor-shortcuts__group th, .editor-shortcuts__group td { padding: 6px 0; border-top: 1px solid var(--outline); text-align: left; vertical-align: middle; }
.editor-shortcuts__group th { font-weight: 400; }
.editor-shortcuts__group td { text-align: right; white-space: nowrap; }
.editor-shortcuts__chord { display: inline-flex; align-items: center; gap: 4px; }
.editor-shortcuts__or { margin: 0 6px; color: var(--on-surface-muted); }
kbd {
  min-width: 22px;
  padding: 1px 6px;
  border: 1px solid var(--outline-strong);
  border-radius: 5px;
  background: var(--surface-2);
  font: inherit;
  font-size: 12px;
  text-align: center;
}
</style>
