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
  let exportListener
  const exports = []
  const nativeRenders = []
  window.__TAURI_INTERNALS__ = {
    transformCallback(callback) {
      exportListener = callback
      return 1
    },
    async invoke(command, args) {
      if (command === 'plugin:event|listen') return 1
      if (command === 'plugin_host_capabilities') return { enabled: true, formats: ['vst3'] }
      if (command === 'plugin_host_render') {
        nativeRenders.push(args.project)
        return { path: 'native-mix.wav', duration: 8, sampleRate: 48000, channels: 2 }
      }
      if (command === 'plugin_host_cleanup_render') return
      if (command === 'start_editor_mix_export') {
        exports.push(args.payload)
        exportListener({ payload: {
          type: 'editor_mix_exported', taskId: args.payload.taskId, payload: { path: 'mix.wav' },
        } })
        return { started: true }
      }
      if (command === 'save_editor_project') {
        saved = JSON.parse(JSON.stringify(args.project))
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
      color: id === 'a' ? '#ff6b80' : '#6089ff',
      solo: false, fadeIn: 1, fadeOut: 2, clips: [{ ...clip, id: id === 'a' ? 'clip' : 'clip-b' }],
    })),
  }
  t.after(async () => { await editor.flushSave(); editor.$dispose() })
  return { editor, clip, exports, nativeRenders }
}

test('mutes only the targeted unlocked clip and preserves its state through history and reload', async t => {
  const { editor, clip } = environment(t)
  editor.selectClip('a', clip.id)
  editor.session.tracks[1].clips[0].locked = true
  editor.setClipMuted('b', 'clip-b', true)
  assert.equal(editor.session.tracks[1].clips[0].muted, false)
  assert.equal(editor.canUndo, false)

  editor.setClipMuted('a', clip.id, true)
  assert.deepEqual(editor.selectedClip, { ...clip, muted: true })
  assert.equal(editor.session.tracks[0].muted, false)
  assert.equal(editor.session.tracks[1].clips[0].muted, false)
  assert.equal(editor.duration, 8)
  editor.setClipMuted('a', clip.id, true)
  editor.undo()
  assert.equal(editor.selectedClip.muted, false)
  editor.redo()
  assert.equal(editor.selectedClip.muted, true)

  await editor.flushSave()
  await editor.loadProject('project')
  assert.equal(editor.session.tracks[0].clips[0].muted, true)
  editor.setClipMuted('a', clip.id, false)
  assert.equal(editor.session.tracks[0].clips[0].muted, false)
})

test('export blocks offline clips in the mix and preserves the timing of excluded offline clips', async t => {
  const { editor, clip, exports, nativeRenders } = environment(t)
  const [first, second] = editor.session.tracks
  const source = editor.session.sources[0]
  source.missing = true
  editor.session.sources.push({ ...source, id: 'online', path: 'online.wav', missing: false })
  second.clips[0].assetId = 'online'

  await assert.rejects(editor.exportMix('wav'), /relink/i)
  assert.equal(exports.length, 0)

  editor.setClipMuted(first.id, clip.id, true)
  await editor.exportMix('wav')
  assert.deepEqual(exports[0].project.tracks[0].clips[0], { ...clip, muted: true })

  editor.setClipMuted(first.id, clip.id, false)
  second.solo = true
  await editor.exportMix('wav')
  assert.deepEqual(exports[1].project.tracks[0].clips[0], clip)

  second.solo = false
  first.volume = 0
  await editor.exportMix('wav')
  assert.equal(exports[2].project.tracks[0].volume, 0)

  editor.session.masterLimiter = { enabled: true }
  await editor.exportMix('wav')
  assert.equal(nativeRenders[0].tracks[0].volume, 0)
  assert.deepEqual(nativeRenders[0].tracks[0].clips[0], clip)
})

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

test('splits a targeted clip independently of the current selection', t => {
  const { editor, clip } = environment(t)
  const [first, second] = editor.session.tracks
  editor.selectClip(first.id, clip.id)
  const target = second.clips[0]

  assert.equal(editor.splitClipAt(second.id, 'missing', 4), 'unavailable')
  assert.equal(editor.splitClipAt(second.id, target.id, target.start), 'outside')
  target.locked = true
  assert.equal(editor.splitClipAt(second.id, target.id, 4), 'locked')
  assert.equal(editor.canUndo, false)
  target.locked = false

  assert.equal(editor.splitClipAt(second.id, target.id, 4), 'split')
  assert.deepEqual(first.clips, [clip])
  assert.deepEqual(second.clips.map(({ start, offset, duration }) => [start, offset, duration]), [[2, 1, 2], [4, 3, 4]])
  editor.undo()
  assert.deepEqual(editor.session.tracks[1].clips, [target])
})

test('moves and copies clips across tracks with overwrite and undo', t => {
  const { editor, clip } = environment(t)
  const [origin, destination] = editor.session.tracks
  editor.selectClip('a', clip.id)
  const drop = { clipId: clip.id, fromTrackId: 'a', toTrackId: 'b', start: 4, copy: false }
  editor.dropClip(drop)
  assert.deepEqual(editor.session.tracks[0].clips, [])
  assert.equal(editor.session.tracks[0].sourceId, '')
  assert.equal(editor.selectedTrackId, 'b')
  assert.deepEqual(editor.selectedClip, { ...clip, start: 4, color: origin.color })
  assert.equal(destination.color, '#6089ff')
  assert.deepEqual(editor.session.tracks[1].clips.map(item => item.id), ['clip-b', clip.id])
  assert.deepEqual(editor.session.tracks[1].clips[0], { ...clip, id: 'clip-b', duration: 2, fadeOut: 0 })
  editor.undo()
  assert.deepEqual(editor.session.tracks[0].clips, [clip])
  assert.equal(editor.canUndo, false)

  editor.dropClip({ ...drop, copy: true })
  const duplicate = editor.selectedClip
  assert.notEqual(duplicate.id, clip.id)
  assert.deepEqual(duplicate, { ...clip, id: duplicate.id, start: 4, color: origin.color })
  assert.deepEqual(editor.session.tracks[0].clips, [clip])
  editor.dropClip({ clipId: duplicate.id, fromTrackId: 'b', toTrackId: 'b', start: 1, copy: false })
  assert.deepEqual(editor.session.tracks[1].clips.map(item => item.id), [duplicate.id])
  assert.equal(editor.selectedClip.color, origin.color)
  editor.undo()
  assert.deepEqual(editor.session.tracks[1].clips.map(item => item.id), ['clip-b', duplicate.id])
  editor.undo()
  assert.equal(editor.session.tracks[1].clips.length, 1)
  editor.redo()
  assert.deepEqual(editor.session.tracks[1].clips.map(item => item.id), ['clip-b', duplicate.id])
})

test('drops into a new track with the original clip timing and a single undo step', async t => {
  const { editor, clip } = environment(t)
  const [origin, second] = editor.session.tracks
  editor.selectClip(origin.id, clip.id)
  editor.dropClip({ fromTrackId: origin.id, toTrackId: null, clipId: clip.id, start: 5, copy: false })

  const created = editor.session.tracks[2]
  assert.equal(editor.session.tracks.length, 3)
  assert.deepEqual(origin.clips, [])
  assert.equal(origin.sourceId, '')
  assert.equal(second.clips.length, 1)
  assert.equal(editor.selectedTrackId, created.id)
  assert.deepEqual(created.clips, [{ ...clip, start: 5, color: origin.color }])
  assert.deepEqual([created.fadeIn, created.fadeOut], [clip.fadeIn, clip.fadeOut])

  editor.undo()
  assert.equal(editor.session.tracks.length, 2)
  assert.deepEqual(editor.session.tracks[0].clips, [clip])
  assert.equal(editor.canUndo, false)
  editor.redo()
  assert.deepEqual(editor.session.tracks[2].clips, created.clips)
  await editor.flushSave()
  await editor.loadProject('project')
  assert.deepEqual(editor.session.tracks[0].clips, [])
  assert.deepEqual(editor.session.tracks[2].clips, created.clips)
})

test('only a copy of a locked clip can be dropped into a new track', t => {
  const { editor, clip } = environment(t)
  const original = editor.session.tracks[0].clips[0]
  original.locked = true
  const drop = { fromTrackId: 'a', toTrackId: null, clipId: clip.id, start: 4, copy: false }
  editor.dropClip(drop)
  assert.equal(editor.session.tracks.length, 2)
  assert.equal(editor.canUndo, false)

  editor.dropClip({ ...drop, copy: true })
  const copied = editor.session.tracks[2].clips[0]
  assert.notEqual(copied.id, original.id)
  assert.equal(copied.locked, false)
  assert.equal(original.locked, true)
  assert.deepEqual(editor.session.tracks[0].clips, [original])
})

test('overwrite splits the destination around the new clip and preserves both source offsets on reload', async t => {
  const { editor, clip } = environment(t)
  const [origin, destination] = editor.session.tracks
  origin.clips[0].duration = 1 / 48000
  origin.clips[0].fadeIn = 0
  origin.clips[0].fadeOut = 0
  const original = { ...destination.clips[0] }
  const start = original.start + 0.005
  const end = start + origin.clips[0].duration
  editor.dropClip({ clipId: clip.id, fromTrackId: 'a', toTrackId: 'b', start, copy: false })

  const [left, moved, right] = destination.clips
  assert.deepEqual(left, { ...original, duration: start - original.start, fadeIn: start - original.start, fadeOut: 0 })
  assert.equal(moved.start, start)
  assert.equal(moved.duration, 1 / 48000)
  assert.equal(moved.color, origin.color)
  assert.equal(right.start, end)
  assert.equal(right.offset, original.offset + (end - original.start))
  assert.ok(Math.abs(right.start + right.duration - (original.start + original.duration)) < 1e-8)
  assert.notEqual(right.id, left.id)
  await editor.flushSave()
  await editor.loadProject('project')
  assert.deepEqual(editor.session.tracks[1].clips, [left, moved, right])
  editor.dropClip({ clipId: moved.id, fromTrackId: 'b', toTrackId: 'a', start: 1, copy: true })
  assert.equal(editor.selectedClip.color, origin.color)
})

test('blocks an overwrite of locked clips but allows a drop at their boundary', t => {
  const { editor, clip } = environment(t)
  const [origin, destination] = editor.session.tracks
  destination.clips[0].locked = true
  const locked = { ...destination.clips[0] }
  const drop = { clipId: clip.id, fromTrackId: origin.id, toTrackId: destination.id, start: 4, copy: false }

  assert.equal(editor.dropClip(drop), 'locked')
  assert.equal(editor.dropClip({ ...drop, copy: true }), 'locked')
  assert.deepEqual(origin.clips, [clip])
  assert.deepEqual(destination.clips, [locked])
  assert.equal(editor.canUndo, false)
  editor.dropClip({ ...drop, start: locked.start + locked.duration })
  assert.deepEqual(destination.clips, [locked, { ...clip, start: 8, color: origin.color }])
  editor.undo()
  assert.deepEqual(editor.session.tracks[1].clips, [locked])
})

test('only committed edits change history and cancellation restores timing and fades', t => {
  const { editor, clip } = environment(t)
  editor.selectClip('a', clip.id)
  editor.setClipTiming('a', clip.id, { start: 3 })
  editor.undo()

  editor.beginInteraction()
  editor.commitInteraction()
  assert.equal(editor.canRedo, true)
  assert.equal(editor.canUndo, false)

  editor.beginInteraction()
  editor.setClipTiming('a', clip.id, { duration: 1 })
  editor.undo()
  editor.redo()
  assert.equal(editor.selectedClip.duration, 1)
  assert.equal(editor.selectedClip.fadeOut, 1)
  editor.commitInteraction(true)
  assert.deepEqual(editor.selectedClip, clip)
  assert.equal(editor.canRedo, true)
  assert.equal(editor.canUndo, false)

  editor.beginInteraction()
  editor.setClipTiming('a', clip.id, { start: 4 })
  editor.setClipTiming('a', clip.id, { start: 5 })
  editor.commitInteraction()
  assert.equal(editor.canRedo, false)
  assert.equal(editor.selectedClip.start, 5)
  editor.undo()
  assert.deepEqual(editor.selectedClip, clip)
  assert.equal(editor.canUndo, false)
  editor.redo()
  assert.equal(editor.selectedClip.start, 5)
})

test('each trim preview uses the original fades and returning to the original edge preserves redo', t => {
  const { editor, clip } = environment(t)
  editor.selectClip('a', clip.id)
  editor.setClipTiming('a', clip.id, { start: 3 })
  editor.undo()
  editor.beginInteraction()
  editor.setClipTiming('a', clip.id, { duration: 0.5 })
  assert.equal(editor.selectedClip.fadeIn, 0.5)
  assert.equal(editor.selectedClip.fadeOut, 0.5)
  editor.setClipTiming('a', clip.id, { duration: clip.duration })
  editor.commitInteraction()
  assert.deepEqual(editor.selectedClip, clip)
  assert.equal(editor.canUndo, false)
  assert.equal(editor.canRedo, true)
})

test('locked originals stay locked while pasted and Alt-dragged copies can be edited', async t => {
  const { editor, clip } = environment(t)
  editor.selectClip('a', clip.id)
  editor.setClipLocked('a', clip.id, true)
  assert.equal(editor.canCutClip, false)
  editor.copySelectedClip()
  editor.selectTrack('b')
  assert.equal(editor.pasteClipAt(8).locked, false)
  assert.equal(editor.canCutClip, true)
  editor.dropClip({ clipId: clip.id, fromTrackId: 'a', toTrackId: 'b', start: 14, copy: true })
  assert.equal(editor.selectedClip.locked, false)
  assert.equal(editor.canCutClip, true)
  assert.equal(editor.session.tracks[0].clips[0].locked, true)

  editor.setClipLocked('a', clip.id, false)
  editor.undo()
  assert.equal(editor.session.tracks[0].clips[0].locked, true)
  editor.redo()
  await editor.flushSave()
  await editor.loadProject('project')
  assert.equal(editor.session.tracks[0].clips[0].locked, false)
})

test('a recording that overlaps a locked clip is saved on a new track in one undo step', async t => {
  const { editor, clip } = environment(t)
  const target = editor.session.tracks[0]
  target.role = 'recording'
  target.type = 'recording'
  target.clips[0].locked = true
  target.clips.push({ ...clip, id: 'unlocked', start: 4 })
  const originalTrack = JSON.parse(JSON.stringify(target))
  const metadata = { path: 'take.wav', name: 'take.wav', duration: 3, sampleRate: 48000, channels: 1 }

  editor.selectClip(target.id, clip.id)
  const { source, lockedOverlap } = await editor.addRecordingClip(target.id, metadata, 3)
  assert.equal(lockedOverlap, true)
  assert.deepEqual(editor.session.tracks[0], originalTrack)
  assert.equal(editor.session.tracks.length, 3)
  assert.equal(editor.selectedTrack.role, 'recording')
  assert.equal(editor.selectedTrack.sourceId, source.id)
  assert.equal(editor.selectedClip.assetId, source.id)
  assert.equal(editor.selectedClip.start, 3)
  assert.equal(editor.selectedClip.duration, 3)

  editor.undo()
  assert.equal(editor.session.tracks.length, 2)
  assert.equal(editor.selectedClipId, clip.id)
  assert.equal(editor.canUndo, false)
  editor.redo()
  assert.equal(editor.session.tracks.length, 3)
  await editor.flushSave()
  await editor.loadProject('project')
  assert.deepEqual(editor.session.tracks[0].clips, originalTrack.clips)
  assert.equal(editor.session.tracks[2].clips[0].assetId, source.id)
  assert.equal(editor.sourceMap.get(source.id).path, metadata.path)
})

test('a recording can touch a locked clip and still overwrite unlocked audio on the target track', async t => {
  const { editor, clip } = environment(t)
  const target = editor.session.tracks[0]
  target.role = 'recording'
  target.type = 'recording'
  target.clips[0].locked = true
  target.clips.push({ ...clip, id: 'unlocked', start: 8 })
  const metadata = { path: 'take.wav', name: 'take.wav', duration: 3, sampleRate: 48000, channels: 1 }

  const { source, lockedOverlap } = await editor.addRecordingClip(target.id, metadata, 8)
  assert.equal(lockedOverlap, false)
  assert.equal(editor.session.tracks.length, 2)
  assert.deepEqual(target.clips[0], { ...clip, locked: true })
  assert.equal(target.clips[1].assetId, source.id)
  assert.deepEqual(target.clips[2], { ...clip, id: 'unlocked', start: 11, offset: 4, duration: 3, fadeIn: 0 })
})

test('inspector timing and fades preserve locked clips', t => {
  const { editor, clip } = environment(t)
  editor.selectClip('a', clip.id)
  editor.selectedClip.locked = true
  editor.setClipTiming('a', clip.id, { start: 4, offset: 2, duration: 3 })
  editor.setClipFades('a', clip.id, { fadeIn: 2, fadeOut: 0 })
  assert.deepEqual(editor.selectedClip, { ...clip, locked: true })
  assert.equal(editor.canUndo, false)
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
