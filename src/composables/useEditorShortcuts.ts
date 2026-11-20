import { matchEditorClipShortcut, type EditorClipCommand } from '@/utils/editorClipShortcuts'
import { onBeforeUnmount, onMounted } from 'vue'

export type EditorShortcutHandlers = {
  togglePlay: () => void
  stop: () => void
  undo: () => void
  redo: () => void
  zoomIn: () => void
  zoomOut: () => void
  save: () => void
  toHome: () => void
  seek: (deltaSeconds: number) => void
  toggleMute: () => void
  toggleSolo: () => void
  clipCommand: (command: EditorClipCommand) => void
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false
  return Boolean(target.closest(
    'input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"], '
    + '[role="combobox"], [role="listbox"], [role="option"], [role="slider"], [role="spinbutton"]',
  ))
}

export function useEditorShortcuts(handlers: EditorShortcutHandlers) {
  // Clip edits belong to the focused timeline. Dialogs and other panels own their clipboard.
  function onTimelineKeydown(event: KeyboardEvent) {
    if (event.defaultPrevented || event.isComposing || isEditableTarget(event.target)) return
    const command = matchEditorClipShortcut(event)
    if (!command || document.querySelector('[aria-modal="true"], .n-modal-mask')) return
    event.preventDefault()
    if (!event.repeat) handlers.clipCommand(command)
  }

  function onKeydown(event: KeyboardEvent) {
    if (event.defaultPrevented || event.isComposing) return
    if (event.target instanceof Element && event.target.closest('[role="dialog"], [role="alertdialog"]')) return
    const mod = event.ctrlKey || event.metaKey
    const editable = isEditableTarget(event.target)

    // Ctrl/Cmd 组合键：即便在输入框内也允许保存
    if (mod) {
      const key = event.key.toLowerCase()
      if (key === 's') {
        event.preventDefault()
        handlers.save()
        return
      }
      if (key === 'z' && !event.shiftKey) {
        if (editable) return
        event.preventDefault()
        handlers.undo()
        return
      }
      if ((key === 'z' && event.shiftKey) || key === 'y') {
        if (editable) return
        event.preventDefault()
        handlers.redo()
        return
      }
      return
    }

    if (editable) return

    switch (event.key) {
      case ' ':
        event.preventDefault()
        handlers.togglePlay()
        break
      case 'Escape':
        handlers.stop()
        break
      case 'm':
      case 'M':
        handlers.toggleMute()
        break
      case 'r':
      case 'R':
        handlers.toggleSolo()
        break
      case 'Home':
        event.preventDefault()
        handlers.toHome()
        break
      case '+':
      case '=':
        event.preventDefault()
        handlers.zoomIn()
        break
      case '-':
      case '_':
        event.preventDefault()
        handlers.zoomOut()
        break
      case 'ArrowLeft':
        event.preventDefault()
        handlers.seek(event.shiftKey ? -5 : -1)
        break
      case 'ArrowRight':
        event.preventDefault()
        handlers.seek(event.shiftKey ? 5 : 1)
        break
      default:
        break
    }
  }

  onMounted(() => window.addEventListener('keydown', onKeydown))
  onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
  return { onTimelineKeydown }
}
