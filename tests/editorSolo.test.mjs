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

test('solo switches tracks as one undoable edit and persists without changing manual mute', async t => {
  const { editor, saved } = environment(t)
  editor.toggleTrackFlag('a', 'solo')
  assert.deepEqual(soloIds(editor), ['a'])
  editor.toggleTrackFlag('b', 'solo')
  assert.deepEqual(soloIds(editor), ['b'])
  assert.deepEqual(mutedIds(editor), ['c'])
  editor.undo()
  assert.deepEqual(soloIds(editor), ['a'])
  editor.redo()
  assert.deepEqual(soloIds(editor), ['b'])
  await editor.flushSave()
  assert.deepEqual(saved.at(-1).tracks.filter(track => track.solo).map(track => track.id), ['b'])
  editor.toggleTrackFlag('b', 'solo')
  assert.deepEqual(soloIds(editor), [])
  assert.deepEqual(mutedIds(editor), ['c'])
  editor.toggleTrackFlag('c', 'muted')
  assert.deepEqual(mutedIds(editor), [])
  assert.deepEqual(soloIds(editor), [])
})

test('clicking an active solo exits legacy multi-solo and invalid tracks leave history intact', t => {
  const { editor } = environment(t)
  editor.session.tracks[0].solo = true
  editor.session.tracks[1].solo = true
  editor.toggleTrackFlag('missing', 'solo')
  assert.equal(editor.canUndo, false)
  assert.deepEqual(soloIds(editor), ['a', 'b'])
  editor.toggleTrackFlag('b', 'solo')
  assert.deepEqual(soloIds(editor), [])
  editor.undo()
  assert.deepEqual(soloIds(editor), ['a', 'b'])
  editor.toggleTrackFlag('c', 'solo')
  assert.deepEqual(soloIds(editor), ['c'])
  assert.deepEqual(mutedIds(editor), ['c'])
})
