import assert from 'node:assert/strict'
import test from 'node:test'
import {
  EDITOR_SHORTCUT_GROUPS,
  createEditorKeydownHandler,
  shortcutKeyCaps,
  type EditorShortcutHandlers,
  type ShortcutChord,
} from '../src/utils/editorShortcuts.ts'

// The dispatcher only reads these DOM globals; node has neither.
globalThis.Element ??= class {} as typeof Element
globalThis.document ??= { querySelector: () => null } as unknown as Document

function recordingHandlers(calls: string[]): EditorShortcutHandlers {
  const record = (name: string) => (argument?: unknown) => { calls.push(argument === undefined ? name : `${name}:${argument}`) }
  return {
    canUseTimeline: () => true,
    togglePlay: record('togglePlay'),
    stop: record('stop'),
    undo: record('undo'),
    redo: record('redo'),
    zoomIn: record('zoomIn'),
    zoomOut: record('zoomOut'),
    save: record('save'),
    toHome: record('toHome'),
    seek: record('seek'),
    toggleMute: record('toggleMute'),
    toggleSolo: record('toggleSolo'),
    clipCommand: record('clipCommand'),
    setTimelineTool: record('setTimelineTool'),
    toggleSnapping: record('toggleSnapping'),
    seekClipEdge: record('seekClipEdge'),
    togglePanel: record('togglePanel'),
    showShortcuts: record('showShortcuts'),
  }
}

function press(chord: ShortcutChord, isMac: boolean) {
  return {
    key: chord.key,
    ctrlKey: Boolean(chord.mod) && !isMac,
    metaKey: Boolean(chord.mod) && isMac,
    shiftKey: Boolean(chord.shift),
    altKey: false,
    repeat: false,
    isComposing: false,
    defaultPrevented: false,
    target: null,
    preventDefault() { this.defaultPrevented = true },
  } as unknown as KeyboardEvent
}

const shortcuts = EDITOR_SHORTCUT_GROUPS.flatMap(group => group.shortcuts)

for (const isMac of [false, true]) {
  test(`every documented shortcut reaches its command (${isMac ? 'macOS' : 'Windows'})`, () => {
    for (const shortcut of shortcuts) {
      for (const chord of shortcut.chords) {
        const calls: string[] = []
        createEditorKeydownHandler(recordingHandlers(calls))(press(chord, isMac))
        assert.deepEqual(calls, [shortcut.command], `${shortcutKeyCaps(chord, isMac).join('+')} → ${shortcut.command}`)
      }
    }
  })
}

test('every editor command is documented', () => {
  const documented = new Set(shortcuts.map(shortcut => shortcut.command.split(':')[0]))
  const commands = Object.keys(recordingHandlers([])).filter(name => name !== 'canUseTimeline')
  assert.deepEqual(commands.filter(name => !documented.has(name)), [])
})

test('key caps follow each platform convention', () => {
  assert.deepEqual(shortcutKeyCaps({ key: 'k', mod: true, shift: true }, false), ['Ctrl', 'Shift', 'K'])
  assert.deepEqual(shortcutKeyCaps({ key: 'k', mod: true, shift: true }, true), ['⇧', '⌘', 'K'])
  assert.deepEqual(shortcutKeyCaps({ key: 'ArrowLeft', shift: true }, false), ['Shift', '←'])
  assert.deepEqual(shortcutKeyCaps({ key: 'Backspace' }, true), ['⌫'])
  assert.deepEqual(shortcutKeyCaps({ key: 'Home' }, false), ['Home'])
})
