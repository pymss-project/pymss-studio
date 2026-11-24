import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { createPinia } from 'pinia'
import { createServer } from 'vite'
import { createRenderer, reactive } from 'vue'

const vite = await createServer({
  configFile: false,
  server: { middlewareMode: true, watch: null, hmr: false, preTransformRequests: false },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
})
after(() => vite.close())

globalThis.window = { __TAURI_INTERNALS__: {}, matchMedia: () => ({ matches: false }), addEventListener() {}, removeEventListener() {} }
Object.defineProperty(globalThis, 'navigator', { value: { language: 'en' }, configurable: true })
const { useEditorNativePlayback } = await vite.ssrLoadModule('/src/composables/useEditorNativePlayback.ts')
const { useEditorPlaybackStore } = await vite.ssrLoadModule('/src/stores/editorPlayback.ts')
const { useEditorStore } = await vite.ssrLoadModule('/src/stores/editor.ts')
const { ensurePluginSession, unloadPendingPluginLoad, unloadPluginSession } = await vite.ssrLoadModule('/src/utils/pluginHost.ts')

const renderer = createRenderer({
  patchProp() {}, insert() {}, remove() {}, setText() {}, setElementText() {},
  createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
  parentNode: () => null, nextSibling: () => null,
})
const slot = id => ({ id, format: 'vst3', pluginId: id, name: id, path: `C:/Plugins/${id}.vst3`, bypassed: false })

async function waitFor(condition) {
  const deadline = Date.now() + 3000
  while (!condition() && Date.now() < deadline) await delay(10)
  assert.ok(condition(), 'Expected transport transition did not occur')
}

function setup(t, modalOp, deferredError) {
  const calls = []
  const received = []
  let playing = false
  let position = 0
  let dismissed = false
  let deferred = null
  const project = { id: 'project', sources: [], tracks: [], pluginChain: [slot('first')], masterVolume: 1, masterPan: 0 }
  const execute = request => {
    received.push(request.op)
    if (request.op === 'play') playing = true
    if (request.op === 'pause') playing = false
    if (request.op === 'play' || request.op === 'seek') position = request.position
    return { projectId: project.id, sessionId: 'host', duration: 60, playing, position }
  }
  window.__TAURI_INTERNALS__.invoke = async (command, { request } = {}) => {
    if (command === 'plugin_host_capabilities') return { enabled: true, formats: ['vst3'], recording: true }
    assert.equal(command, 'plugin_host_request')
    calls.push(request.op)
    if (deferred) {
      if (!dismissed) throw 'PLUGIN_HOST_DIALOG_PENDING: plugin dialog'
      const completed = deferred
      deferred = null
      if (deferredError) throw new Error(deferredError)
      execute(completed)
    }
    if (request.op === modalOp && !dismissed) {
      deferred = request
      throw 'PLUGIN_HOST_DIALOG_PENDING: plugin dialog'
    }
    return execute(request)
  }
  const editor = reactive({
    session: project,
    async releasePluginSession() {
      if (!await unloadPendingPluginLoad(project.id)) {
        await unloadPluginSession(project.id, { discardUnsaved: true })
      }
    },
  })
  let native
  const pinia = createPinia()
  const playback = useEditorPlaybackStore(pinia)
  const app = renderer.createApp({
    setup() {
      native = useEditorNativePlayback(editor, () => {})
      return () => null
    },
  })
  app.use(pinia).mount({})
  t.after(async () => {
    dismissed = true
    app.unmount()
    await waitFor(() => received.includes('unload'))
  })
  return { native, editor, playback, calls, received, dismiss: () => { dismissed = true } }
}

test('a seek during startup preserves the play request and uses the new position', async t => {
  const { native, playback, calls, received, dismiss } = setup(t, 'monitor')
  void native.play(1)
  await waitFor(() => calls.includes('monitor'))
  native.seek(9)
  dismiss()
  await waitFor(() => playback.status === 'playing')
  assert.equal(playback.currentTime, 9)
  assert.equal(received.filter(op => op === 'play').length, 1)
})

test('a handoff keeps the current transport playing until the native graph is ready', async t => {
  const { native, editor, playback, calls, received, dismiss } = setup(t, 'monitor')
  const webRequest = playback.beginRequest('play', 'starting')
  playback.setCurrentTime(1)
  playback.finishPlay(webRequest)
  const started = native.play(1, {
    isCurrent: () => playback.requestId === webRequest,
    takePosition: () => playback.currentTime,
  })
  await waitFor(() => calls.includes('monitor'))
  assert.equal(playback.requestId, webRequest)
  assert.equal(playback.status, 'playing')
  assert.ok(!received.includes('play'))
  editor.session.pluginChain.push(slot('second'))
  playback.setCurrentTime(9)
  dismiss()
  assert.equal(await started, true)
  assert.equal(playback.currentTime, 9)
  assert.ok(received.includes('update'))
  assert.ok(received.indexOf('update') < received.indexOf('play'))
})

test('a seek during a deferred pause stays paused and applies the plugin update only once', async t => {
  const { native, editor, playback, calls, received, dismiss } = setup(t, 'update')
  assert.equal(await native.play(1), true)
  editor.session.pluginChain.push(slot('second'))
  await waitFor(() => calls.includes('update'))
  native.pause()
  await waitFor(() => calls.includes('pause'))
  native.seek(9)
  dismiss()
  await waitFor(() => playback.status === 'paused' && received.includes('seek'))
  assert.equal(playback.currentTime, 9)
  assert.ok(received.includes('pause'))
  assert.ok(received.indexOf('pause') < received.indexOf('seek'))
  assert.equal(received.filter(op => op === 'update').length, 1)
})

test('removing the last plugin after cancelling the initial play releases its deferred load', async t => {
  const { native, editor, playback, calls, received, dismiss } = setup(t, 'load')
  const play = native.play(1)
  await waitFor(() => calls.includes('load'))
  native.pause()
  editor.session.pluginChain = []
  await waitFor(() => calls.includes('unload'))
  assert.ok(!received.includes('unload'))
  dismiss()
  await waitFor(() => received.includes('unload'))
  assert.equal(await play, false)
  assert.equal(playback.status, 'paused')
  assert.ok(!received.includes('play'))
})

test('a rejected deferred load does not prevent a replacement chain from loading', async t => {
  const { editor, calls, received, dismiss } = setup(t, 'load', 'Original plugin failed')
  await assert.rejects(ensurePluginSession(editor.session), { name: 'PluginHostDialogPendingError' })
  editor.session.pluginChain = [slot('replacement')]
  dismiss()
  await ensurePluginSession(editor.session)
  assert.equal(calls.filter(op => op === 'load').length, 2)
  assert.equal(received.filter(op => op === 'load').length, 1)
})

test('a rejected deferred load is reported without reopening the same chain', async t => {
  const { editor, calls, dismiss } = setup(t, 'load', 'Original plugin failed')
  editor.session.pluginChain[0].updatedAt = 100
  await assert.rejects(ensurePluginSession(editor.session), { name: 'PluginHostDialogPendingError' })
  editor.session.masterVolume = 0.8
  dismiss()
  try {
    await assert.rejects(ensurePluginSession(editor.session), /Original plugin failed/)
    assert.equal(calls.filter(op => op === 'load').length, 1)
  } finally {
    await ensurePluginSession(editor.session)
  }
})

test('relinking an asset keeps the live project and its unsaved plugin chain', async t => {
  const editor = useEditorStore(createPinia())
  t.after(() => editor.$dispose())
  const source = {
    id: 'asset', role: 'stem', name: 'Audio', path: 'C:/Audio/missing.wav', missing: true,
    duration: 3, sampleRate: 48000, channels: 2, peaks: [0.2], channelPeaks: [[0.2], [0.2]],
  }
  const saved = {
    id: 'project', name: 'Project', masterVolume: 1, masterPan: 0,
    pluginChain: [], sources: [source], tracks: [], createdAt: 1, updatedAt: 1,
  }
  editor.session = { ...saved, pluginChain: [slot('live')] }
  const project = editor.session
  const updatedSource = { ...source, path: 'C:/Audio/relinked.wav', missing: false, originRoot: 'C:/Audio' }
  window.__TAURI_INTERNALS__.invoke = async (command, args) => {
    if (command === 'relink_editor_sources') {
      return { project: { ...saved, sources: [updatedSource] }, relinked: 1, unresolved: [] }
    }
    if (command === 'get_audio_metadata') return { ...updatedSource, name: 'Audio' }
    if (command === 'generate_waveform_peaks') return { ...updatedSource, peaksPath: '' }
    if (command === 'save_editor_project') return args.project
    throw new Error(`Unexpected command: ${command}`)
  }

  await editor.relinkSource(source.id, updatedSource.path)
  assert.equal(editor.session, project)
  assert.equal(project.sources[0].path, updatedSource.path)
  assert.equal(project.sources[0].missing, false)
  assert.deepEqual(project.pluginChain.map(plugin => plugin.id), ['live'])
  await editor.flushSave()
})
