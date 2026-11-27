import { onBeforeUnmount, onMounted } from 'vue'
import { createEditorKeydownHandler, type EditorShortcutHandlers } from '@/utils/editorShortcuts'

export type { EditorShortcutHandlers }

export function useEditorShortcuts(handlers: EditorShortcutHandlers) {
  const onKeydown = createEditorKeydownHandler(handlers)
  onMounted(() => window.addEventListener('keydown', onKeydown))
  onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
}
