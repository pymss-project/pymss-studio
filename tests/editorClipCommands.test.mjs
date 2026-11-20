import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createPinia } from 'pinia'

const vite = await createServer({
  configFile: false,
  server: { middlewareMode: true, watch: null, hmr: false, preTransformRequests: false },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
})
after(() => vite.close())
globalThis.window = { matchMedia: () => ({ matches: false }), addEventListener() {}, removeEventListener() {} }
Object.defineProperty(globalThis, 'navigator', { value: { language: 'en' }, configurable: true })
const { useEditorStore } = await vite.ssrLoadModule('/src/stores/editor.ts')

function environment(t) {
  const source = {
    id: 'source', name: 'Keys', role: 'stem', path: 'keys.wav',
    duration: 12, sampleRate: 48000, channels: 1,
  }
  const clip = {
    id: 'clip', assetId: source.id, start: 2, offset: 1, duration: 6,
    volume: 0.8, fadeIn: 1, fadeOut: 2, muted: false, locked: false,
  }
  let saved
  window.__TAURI_INTERNALS__ = {
    async invoke(command, args) {
      if (command === 'save_editor_project') {
        saved = structuredClone(args.project)
        return saved
      }
      if (command === 'load_editor_project') return structuredClone({ ...saved, id: args.projectId })
      if (command === 'get_audio_metadata') return source
      if (command === 'generate_waveform_peaks') return { ...source, peaks: [0.5] }
      if (command === 'write_frontend_log') return
      throw new Error(`Unexpected command: ${command}`)
    },
  }
  const editor = useEditorStore(createPinia())
  editor.session = {
    id: 'project', name: 'Project', masterVolume: 1, masterPan: 0, sources: [source],
    createdAt: 1, updatedAt: 1,
    tracks: ['a', 'b'].map(id => ({
      id, sourceId: source.id, name: id, role: 'stem', volume: 1, pan: 0, muted: false,
      solo: false, fadeIn: 1, fadeOut: 2, clips: [{ ...clip, id: id === 'a' ? 'clip' : 'clip-b' }],
    })),
  }
  t.after(async () => { await editor.flushSave(); editor.$dispose() })
  return { editor, clip }
}

test('cuts the last clip into the clipboard and keeps the empty track empty after reload', async t => {
  const { editor, clip } = environment(t)
  editor.selectClip('a', clip.id)
  editor.cutSelectedClip()
  assert.equal(editor.session.tracks.length, 2)
  assert.deepEqual(editor.session.tracks[0].clips, [])
  assert.equal(editor.session.tracks[0].sourceId, '')
  assert.equal(editor.selectedClipId, null)
  editor.undo()
  assert.deepEqual(editor.selectedClip, clip)
  editor.redo()
  assert.deepEqual(editor.session.tracks[0].clips, [])

  editor.selectTrack('b', false)
  assert.equal(editor.canCutClip, false)
  const first = editor.pasteClipAt(4)
  const second = editor.pasteClipAt(10)
  assert.deepEqual(first, { ...clip, id: first.id, start: 4 })
  assert.notEqual(second.id, first.id)
  assert.deepEqual(editor.session.tracks[1].clips.map(item => item.id), ['clip-b', first.id, second.id])
  editor.undo()
  assert.deepEqual(editor.session.tracks[1].clips.map(item => item.id), ['clip-b', first.id])

  await editor.flushSave()
  await editor.loadProject('project')
  assert.deepEqual(editor.session.tracks[0].clips, [])
  assert.equal(editor.session.tracks[0].sourceId, '')
  assert.equal(editor.duration, 10)
  assert.equal(editor.canPasteClip, true)
  await editor.loadProject('another-project')
  assert.equal(editor.canPasteClip, false)
})

test('splits only inside an unlocked clip and keeps source timing and outer fades', t => {
  const { editor, clip } = environment(t)
  editor.selectClip('a', clip.id)
  editor.splitSelectedClipAt(clip.start)
  editor.splitSelectedClipAt(clip.start + clip.duration)
  assert.equal(editor.canUndo, false)
  editor.splitSelectedClipAt(4)
  const [left, right] = editor.session.tracks[0].clips
  assert.deepEqual(left, { ...clip, duration: 2, fadeOut: 0 })
  assert.deepEqual(right, { ...clip, id: right.id, start: 4, offset: 3, duration: 4, fadeIn: 0 })
  assert.notEqual(right.id, left.id)
  editor.undo()
  assert.deepEqual(editor.session.tracks[0].clips, [clip])

  editor.selectedClip.locked = true
  editor.cutSelectedClip()
  editor.deleteSelectedClip()
  editor.splitSelectedClipAt(4)
  assert.equal(editor.canUndo, false)
  assert.equal(editor.canCopyClip, true)
  editor.copySelectedClip()
  assert.equal(editor.canPasteClip, true)
})

test('moves and copies clips across tracks without replacing destination audio', t => {
  const { editor, clip } = environment(t)
  editor.selectClip('a', clip.id)
  const drop = { clipId: clip.id, fromTrackId: 'a', toTrackId: 'b', start: 4, copy: false }
  editor.dropClip(drop)
  assert.deepEqual(editor.session.tracks[0].clips, [])
  assert.equal(editor.session.tracks[0].sourceId, '')
  assert.equal(editor.selectedTrackId, 'b')
  assert.deepEqual(editor.selectedClip, { ...clip, start: 4 })
  assert.deepEqual(editor.session.tracks[1].clips.map(item => item.id), ['clip-b', clip.id])
  editor.undo()
  assert.deepEqual(editor.session.tracks[0].clips, [clip])
  assert.equal(editor.canUndo, false)

  editor.dropClip({ ...drop, copy: true })
  const duplicate = editor.selectedClip
  assert.notEqual(duplicate.id, clip.id)
  assert.deepEqual(duplicate, { ...clip, id: duplicate.id, start: 4 })
  assert.deepEqual(editor.session.tracks[0].clips, [clip])
  editor.dropClip({ clipId: duplicate.id, fromTrackId: 'b', toTrackId: 'b', start: 1, copy: false })
  assert.deepEqual(editor.session.tracks[1].clips.map(item => item.id), [duplicate.id, 'clip-b'])
  editor.undo()
  assert.deepEqual(editor.session.tracks[1].clips.map(item => item.id), ['clip-b', duplicate.id])
  editor.undo()
  assert.equal(editor.session.tracks[1].clips.length, 1)
  editor.redo()
  assert.deepEqual(editor.session.tracks[1].clips.map(item => item.id), ['clip-b', duplicate.id])
})

test('splits multiple tracks at one time with one undo and leaves locked clips intact', t => {
  const { editor, clip } = environment(t)
  const [first, second] = editor.session.tracks
  const locked = { ...clip, id: 'locked', locked: true }
  const atBoundary = { ...clip, id: 'boundary', start: 4 }
  first.clips.push(locked, atBoundary)
  editor.selectClip('a', clip.id)
  editor.splitAllClipsAt(2)
  assert.equal(editor.canUndo, false)
  editor.splitAllClipsAt(4)
  assert.equal(first.clips.length, 4)
  assert.equal(second.clips.length, 2)
  assert.deepEqual(first.clips.find(item => item.id === locked.id), locked)
  assert.deepEqual(first.clips.find(item => item.id === atBoundary.id), atBoundary)
  assert.equal(editor.selectedClip.start, 4)
  assert.equal(editor.selectedClip.offset, 3)
  assert.equal(second.clips[1].start, 4)
  editor.undo()
  assert.equal(editor.canUndo, false)
  assert.deepEqual(editor.session.tracks.map(track => track.clips.length), [3, 1])
  assert.equal(editor.selectedClip.id, clip.id)
  editor.redo()
  assert.deepEqual(editor.session.tracks.map(track => track.clips.length), [4, 2])
})
