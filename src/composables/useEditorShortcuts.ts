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
  removeTrack: () => void
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  const tag = target.tagName
  return (
    tag === 'INPUT' ||
    tag === 'TEXTAREA' ||
    tag === 'SELECT' ||
    Boolean(target.closest('[role="combobox"], [role="listbox"], [role="option"]')) ||
    target.isContentEditable
  )
}

/**
 * 编辑器（Stem 混音器）全局键盘快捷键。输入框聚焦时自动忽略（除保存 / 撤销 / 重做）。
 */
export function useEditorShortcuts(handlers: EditorShortcutHandlers) {
  function onKeydown(event: KeyboardEvent) {
    if (event.defaultPrevented || event.isComposing) return
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
      case 'Delete':
      case 'Backspace':
        event.preventDefault()
        handlers.removeTrack()
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
}
