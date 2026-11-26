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
const { normalizeTrackHeight } = await vite.ssrLoadModule('/src/utils/editorTrackHeight.ts')
const { pluginProjectSignature } = await vite.ssrLoadModule('/src/utils/editorPlugins.ts')

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
    tracks: ['a', 'b'].map(id => ({
      id, sourceId: id, name: id, role: 'stem', volume: 1, pan: 0,
      muted: false, solo: false, fadeIn: 0, fadeOut: 0, clips: [],
    })),
  }
  t.after(async () => { await editor.flushSave(); editor.$dispose() })
  return { editor, saved }
}

const heights = editor => editor.session.tracks.map(track => track.height)

test('stored track heights are bounded and invalid values mean automatic', () => {
  assert.equal(normalizeTrackHeight(120.4), 120)
  assert.equal(normalizeTrackHeight(10), 50)
  assert.equal(normalizeTrackHeight(1000), 280)
  for (const value of [undefined, null, '', 'tall', Number.NaN]) assert.equal(normalizeTrackHeight(value), undefined)
})

test('track height edits are undoable and saved with the project', async t => {
  const { editor, saved } = environment(t)
  editor.setTrackHeights({ a: 120 })
  editor.setTrackHeights({ a: 120 })
  assert.deepEqual(heights(editor), [120, undefined])
  editor.setTrackHeights({ a: 90, b: 150 })
  assert.deepEqual(heights(editor), [90, 150])
  editor.undo()
  assert.deepEqual(heights(editor), [120, undefined])
  editor.undo()
  assert.deepEqual(heights(editor), [undefined, undefined])
  assert.equal(editor.canUndo, false, 'An unchanged height must not add a history step')
  editor.redo()
  editor.setTrackHeights({ a: null })
  assert.deepEqual(heights(editor), [undefined, undefined])
  await editor.flushSave()
  assert.deepEqual(saved.at(-1).tracks.map(track => track.height), [undefined, undefined])
  editor.setTrackHeights({ b: 160 })
  await editor.flushSave()
  assert.equal(saved.at(-1).tracks[1].height, 160)
})

test('a dragged resize records one history step', t => {
  const { editor } = environment(t)
  editor.beginInteraction()
  for (const height of [60, 80, 110]) editor.setTrackHeights({ a: height })
  editor.commitInteraction()
  assert.deepEqual(heights(editor), [110, undefined])
  editor.undo()
  assert.deepEqual(heights(editor), [undefined, undefined])
  assert.equal(editor.canUndo, false)
})

test('track heights do not change the audio host project', t => {
  const { editor } = environment(t)
  const before = pluginProjectSignature(editor.session)
  editor.setTrackHeights({ a: 200 })
  assert.equal(pluginProjectSignature(editor.session), before)
})
