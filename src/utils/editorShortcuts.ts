import type { EditorPanel, EditorTimelineTool } from '../types/editor'
import { matchEditorClipShortcut, type EditorClipCommand } from './editorClipShortcuts.ts'

export type EditorShortcutHandlers = {
  interceptKeydown?: (event: KeyboardEvent) => boolean
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
  canUseTimeline: () => boolean
  clipCommand: (command: EditorClipCommand) => void
  setTimelineTool: (tool: EditorTimelineTool) => void
  toggleSnapping: () => void
  seekClipEdge: (direction: -1 | 1) => void
  togglePanel: (panel: EditorPanel) => void
  showShortcuts: () => void
}

/** One key press. `mod` is Ctrl on Windows and Linux, Command on macOS. */
export type ShortcutChord = { key: string; mod?: boolean; shift?: boolean }

/**
 * A documented shortcut. `command` names the handler it reaches, with its argument after a
 * colon (`seek:-5`); tests replay every chord against the dispatcher to keep the two in step.
 */
export type EditorShortcut = { command: string; label: string; chords: ShortcutChord[] }
export type EditorShortcutGroup = { title: string; shortcuts: EditorShortcut[] }

export const EDITOR_SHORTCUT_GROUPS: EditorShortcutGroup[] = [
  {
    title: 'editor.shortcuts.groupPlayback',
    shortcuts: [
      { command: 'togglePlay', label: 'editor.shortcuts.togglePlay', chords: [{ key: ' ' }] },
      { command: 'stop', label: 'common.stop', chords: [{ key: 'Escape' }] },
      { command: 'toHome', label: 'editor.shortcuts.toStart', chords: [{ key: 'Home' }] },
      { command: 'seek:-1', label: 'editor.shortcuts.seekBackward', chords: [{ key: 'ArrowLeft' }] },
      { command: 'seek:1', label: 'editor.shortcuts.seekForward', chords: [{ key: 'ArrowRight' }] },
      { command: 'seek:-5', label: 'editor.shortcuts.seekBackwardLong', chords: [{ key: 'ArrowLeft', shift: true }] },
      { command: 'seek:5', label: 'editor.shortcuts.seekForwardLong', chords: [{ key: 'ArrowRight', shift: true }] },
      { command: 'seekClipEdge:-1', label: 'editor.shortcuts.previousClipEdge', chords: [{ key: 'ArrowLeft', mod: true }] },
      { command: 'seekClipEdge:1', label: 'editor.shortcuts.nextClipEdge', chords: [{ key: 'ArrowRight', mod: true }] },
    ],
  },
  {
    title: 'editor.shortcuts.groupEditing',
    shortcuts: [
      { command: 'undo', label: 'common.undo', chords: [{ key: 'z', mod: true }] },
      { command: 'redo', label: 'common.redo', chords: [{ key: 'z', mod: true, shift: true }, { key: 'y', mod: true }] },
      { command: 'save', label: 'editor.save', chords: [{ key: 's', mod: true }] },
      { command: 'clipCommand:cut', label: 'editor.cutClip', chords: [{ key: 'x', mod: true }] },
      { command: 'clipCommand:copy', label: 'editor.copyClip', chords: [{ key: 'c', mod: true }] },
      { command: 'clipCommand:paste', label: 'editor.pasteClip', chords: [{ key: 'v', mod: true }] },
      { command: 'clipCommand:split', label: 'editor.splitClip', chords: [{ key: 'k', mod: true }] },
      { command: 'clipCommand:splitAll', label: 'editor.splitAllClips', chords: [{ key: 'k', mod: true, shift: true }] },
      { command: 'clipCommand:delete', label: 'editor.removeClip', chords: [{ key: 'Delete' }, { key: 'Backspace' }] },
    ],
  },
  {
    title: 'editor.shortcuts.groupTracks',
    shortcuts: [
      { command: 'toggleMute', label: 'editor.shortcuts.toggleMute', chords: [{ key: 'm' }] },
      { command: 'toggleSolo', label: 'editor.shortcuts.toggleSolo', chords: [{ key: 'r' }] },
    ],
  },
  {
    title: 'editor.shortcuts.groupView',
    shortcuts: [
      { command: 'setTimelineTool:selection', label: 'editor.selectionTool', chords: [{ key: 'v' }] },
      { command: 'setTimelineTool:razor', label: 'editor.razorTool', chords: [{ key: 'c' }] },
      { command: 'toggleSnapping', label: 'editor.snapToEdges', chords: [{ key: 's' }] },
      { command: 'zoomIn', label: 'editor.zoomIn', chords: [{ key: '+' }] },
      { command: 'zoomOut', label: 'editor.zoomOut', chords: [{ key: '-' }] },
      { command: 'togglePanel:assets', label: 'editor.shortcuts.toggleAssets', chords: [{ key: '1', mod: true }] },
      { command: 'togglePanel:mixer', label: 'editor.shortcuts.toggleMixer', chords: [{ key: '2', mod: true }] },
      { command: 'togglePanel:inspector', label: 'editor.shortcuts.toggleInspector', chords: [{ key: '3', mod: true }] },
      { command: 'showShortcuts', label: 'editor.shortcuts.title', chords: [{ key: '?' }] },
    ],
  },
]

/** Placeholder for the space bar, which the caller labels in the interface language. */
export const SPACE_KEY = ' '

const KEY_CAPS: Record<string, string> = { ArrowLeft: '←', ArrowRight: '→', Escape: 'Esc' }
const MAC_KEY_CAPS: Record<string, string> = { ...KEY_CAPS, Backspace: '⌫', Delete: '⌦' }

/** Key caps in the platform's order: `Ctrl`, `Shift`, key elsewhere; `⇧`, `⌘`, key on macOS. */
export function shortcutKeyCaps(chord: ShortcutChord, isMac: boolean): string[] {
  const caps = isMac
    ? [chord.shift && '⇧', chord.mod && '⌘']
    : [chord.mod && 'Ctrl', chord.shift && 'Shift']
  const key = (isMac ? MAC_KEY_CAPS : KEY_CAPS)[chord.key] ?? (chord.key.length === 1 ? chord.key.toUpperCase() : chord.key)
  return [...caps.filter((cap): cap is string => Boolean(cap)), key]
}

const PANEL_SHORTCUTS: Record<string, EditorPanel> = { 1: 'assets', 2: 'mixer', 3: 'inspector' }

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false
  return Boolean(target.closest(
    'input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"], '
    + '[role="combobox"], [role="listbox"], [role="option"], [role="slider"], [role="spinbutton"]',
  ))
}

export function createEditorKeydownHandler(handlers: EditorShortcutHandlers) {
  function onTimelineKeydown(event: KeyboardEvent) {
    if (!handlers.canUseTimeline()) return
    if (document.querySelector('[aria-modal="true"], .n-modal-mask')) return
    if (event.target instanceof Element && event.target.closest('.n-popover, .n-dropdown-menu, [role="menu"]')) return
    const command = matchEditorClipShortcut(event)
    if (command) {
      event.preventDefault()
      if (!event.repeat) handlers.clipCommand(command)
      return
    }
    if (event.altKey || event.shiftKey) return
    if (event.ctrlKey !== event.metaKey && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
      event.preventDefault()
      handlers.seekClipEdge(event.key === 'ArrowLeft' ? -1 : 1)
      return
    }
    if (event.ctrlKey || event.metaKey) return
    const key = event.key.toLowerCase()
    if (key !== 'v' && key !== 'c' && key !== 's') return
    event.preventDefault()
    if (event.repeat) return
    if (key === 's') handlers.toggleSnapping()
    else handlers.setTimelineTool(key === 'v' ? 'selection' : 'razor')
  }

  return function onKeydown(event: KeyboardEvent) {
    if (event.defaultPrevented || event.isComposing) return
    if (handlers.interceptKeydown?.(event)) return
    if (event.target instanceof Element && event.target.closest('[role="dialog"], [role="alertdialog"]')) return
    const mod = event.ctrlKey || event.metaKey
    const editable = isEditableTarget(event.target)
    if (!editable) {
      onTimelineKeydown(event)
      if (event.defaultPrevented) return
    }

    // Ctrl/Cmd 组合键：即便在输入框内也允许保存
    if (mod) {
      const key = event.key.toLowerCase()
      if (key === 's') {
        event.preventDefault()
        handlers.save()
        return
      }
      const panel = !event.shiftKey && !event.altKey ? PANEL_SHORTCUTS[event.key] : undefined
      if (panel) {
        event.preventDefault()
        if (!event.repeat) handlers.togglePanel(panel)
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
      case '?':
        event.preventDefault()
        handlers.showShortcuts()
        break
      default:
        break
    }
  }
}
