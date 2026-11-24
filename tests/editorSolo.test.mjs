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
  const saved = []
  window.__TAURI_INTERNALS__ = { async invoke(command, args) {
    if (command === 'save_editor_project') {
      saved.push(JSON.parse(JSON.stringify(args.project)))
      return args.project
    }
    if (command === 'write_frontend_log') return
    throw new Error(`Unexpected command: ${command}`)
  } }
  const editor = useEditorStore(createPinia())
  editor.session = {
    id: 'project', name: 'Project', masterVolume: 1, masterPan: 0, sources: [], createdAt: 1, updatedAt: 1,
    tracks: ['a', 'b', 'c'].map(id => ({
      id, sourceId: id, name: id, role: 'stem', volume: 1, pan: 0,
      muted: id === 'c', solo: false, fadeIn: 0, fadeOut: 0, clips: [],
    })),
  }
  t.after(async () => { await editor.flushSave(); editor.$dispose() })
  return { editor, saved }
}

const soloIds = editor => editor.session.tracks.filter(track => track.solo).map(track => track.id)
const mutedIds = editor => editor.session.tracks.filter(track => track.muted).map(track => track.id)

test('multi-track solo toggles only the target track as undoable, persisted edits', async t => {
  const { editor, saved } = environment(t)
  editor.toggleTrackFlag('a', 'solo')
  editor.toggleTrackFlag('b', 'solo')
  assert.deepEqual(soloIds(editor), ['a', 'b'])
  editor.toggleTrackFlag('a', 'solo')
  assert.deepEqual(soloIds(editor), ['b'])
  assert.deepEqual(mutedIds(editor), ['c'])
  editor.undo()
  assert.deepEqual(soloIds(editor), ['a', 'b'])
  editor.redo()
  assert.deepEqual(soloIds(editor), ['b'])
  editor.toggleTrackFlag('c', 'solo')
  assert.deepEqual(soloIds(editor), ['b', 'c'])
  assert.deepEqual(mutedIds(editor), ['c'])
  await editor.flushSave()
  assert.deepEqual(saved.at(-1).tracks.filter(track => track.solo).map(track => track.id), ['b', 'c'])
  editor.toggleTrackFlag('c', 'muted')
  assert.deepEqual(mutedIds(editor), [])
  assert.deepEqual(soloIds(editor), ['b', 'c'])
  editor.toggleTrackFlag('b', 'solo')
  editor.toggleTrackFlag('c', 'solo')
  assert.deepEqual(soloIds(editor), [])
})

test('existing solo flags are independent and invalid tracks leave history intact', t => {
  const { editor } = environment(t)
  editor.session.tracks[0].solo = true
  editor.session.tracks[1].solo = true
  editor.toggleTrackFlag('missing', 'solo')
  assert.equal(editor.canUndo, false)
  assert.deepEqual(soloIds(editor), ['a', 'b'])
  editor.toggleTrackFlag('b', 'solo')
  assert.deepEqual(soloIds(editor), ['a'])
  editor.undo()
  assert.deepEqual(soloIds(editor), ['a', 'b'])
})
