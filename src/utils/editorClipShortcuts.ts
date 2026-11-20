export type EditorClipCommand = 'cut' | 'copy' | 'paste' | 'split' | 'splitAll' | 'delete'

const modifiedKeys = { cut: 'x', copy: 'c', paste: 'v', split: 'k' } as const

export function editorClipShortcutLabel(command: EditorClipCommand, isMac: boolean) {
  if (command === 'delete') return isMac ? '⌫' : 'Delete'
  if (command === 'splitAll') return isMac ? '⇧⌘K' : 'Ctrl+Shift+K'
  return (isMac ? '⌘' : 'Ctrl+') + modifiedKeys[command].toUpperCase()
}

export function matchEditorClipShortcut(event: KeyboardEvent): EditorClipCommand | null {
  if (event.altKey || (event.ctrlKey && event.metaKey)) return null
  if (event.shiftKey) {
    return (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k' ? 'splitAll' : null
  }
  if (!event.ctrlKey && !event.metaKey) {
    return event.key === 'Delete' || event.key === 'Backspace' ? 'delete' : null
  }
  const key = event.key.toLowerCase()
  for (const command of Object.keys(modifiedKeys) as (keyof typeof modifiedKeys)[]) {
    if (modifiedKeys[command] === key) return command
  }
  return null
}
