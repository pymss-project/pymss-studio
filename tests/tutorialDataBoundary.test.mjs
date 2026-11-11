import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { createDemoRuntime } from '../src/features/onboarding/demoRuntime.ts'
import { visibleParameterGuides } from '../src/features/onboarding/parameters.ts'

const config = { locale: 'en', themeMode: 'dark', themeAccent: 'blue', platform: 'win32', progress: { step: 0, stepId: 'workspace' } }

test('resumed encoding parameters remain available in the matching tutorial output format', async t => {
  for (const [parameter, format] of [['wavBitDepth', 'wav'], ['flacBitDepth', 'flac'], ['mp3BitRate', 'mp3'], ['m4aBitRate', 'm4a'], ['m4aCodec', 'm4a']]) {
    const progress = { status: 'active', step: 11, stepId: 'encoding', parameter }
    const runtime = createDemoRuntime({ ...config, progress })
    t.after(() => runtime.dispose())
    const settings = await runtime.invoke('load_app_store', { name: 'app-settings' })
    assert.equal(settings.defaultFormat, format, parameter)
    assert.deepEqual(settings.beginnerGuideProgress, progress)
    assert.ok(visibleParameterGuides({ hasModel: true, defaults: {}, format: settings.defaultFormat, canSaveDefaults: false })
      .some(item => item.id === parameter), parameter)
  }
})

test('new tutorials and non-encoding progress retain the WAV default', async t => {
  for (const parameter of [undefined, 'batch_size', 'unknown']) {
    const runtime = createDemoRuntime({ ...config, progress: { ...config.progress, parameter } })
    t.after(() => runtime.dispose())
    const settings = await runtime.invoke('load_app_store', { name: 'app-settings' })
    assert.equal(settings.defaultFormat, 'wav')
  }
})

test('tutorial data is private to each session and unknown operations never reach native services', async t => {
  const first = createDemoRuntime(config), second = createDemoRuntime(config)
  t.after(() => { first.dispose(); second.dispose() })
  const settings = await first.invoke('load_app_store', { name: 'app-settings' })
  settings.outputDir = 'Changed/Only/In/Tutorial'
  await first.invoke('save_app_store', { name: 'app-settings', data: settings })
  assert.notEqual((await second.invoke('load_app_store', { name: 'app-settings' })).outputDir, settings.outputDir)
  const loaded = await first.invoke('load_app_store', { name: 'app-settings' })
  loaded.outputDir = 'Not saved'
  assert.equal((await first.invoke('load_app_store', { name: 'app-settings' })).outputDir, settings.outputDir)
  await assert.rejects(first.invoke('delete_runtime', { payload: { backend: 'cpu' } }), /not available/)
  assert.throws(() => first.assetUrl('C:/Users/private/recording.wav'), /sample assets/)
  assert.ok(first.assetUrl(first.input).startsWith('blob:'))
})

test('the real task lifecycle receives mock completion and usable audio data', async t => {
  const runtime = createDemoRuntime(config)
  t.after(() => runtime.dispose())
  const events = []
  const handler = runtime.callback(event => events.push(event.payload))
  await runtime.invoke('plugin:event|listen', { event: 'pymss://worker-event', handler })
  assert.equal((await runtime.invoke('start_separation', { payload: { taskId: 'sample-run' } })).started, true)
  await new Promise(resolve => setTimeout(resolve, 1200))
  const completed = events.find(event => event.type === 'task_done')
  assert.equal(completed.taskId, 'sample-run')
  assert.equal(completed.payload.outputs.length, 2)
  const pcm = await runtime.invoke('editor_preview_chunk', { path: runtime.input, index: 0, sampleRate: 8000, channels: 1 })
  assert.equal(pcm.byteLength, 4 * 8000 * 4)
  assert.equal((await runtime.invoke('editor_preview_chunk', { index: 3, sampleRate: 8000 })).byteLength, 0)
})

test('storage statistics describe sample files without inspecting the real model directory', async t => {
  const runtime = createDemoRuntime(config)
  t.after(() => runtime.dispose())
  const summary = await runtime.invoke('get_model_storage_summary', { payload: { modelDir: 'C:/Private/Models' } })
  assert.equal(summary.downloadedCount, runtime.models.length)
  assert.equal(summary.totalBytes, runtime.models.reduce((total, model) => total + model.sizeBytes, 0))
  assert.ok(summary.modelDir.includes('Pymss-Demo'))
  assert.ok(summary.models.every(model => model.files.every(file => file.path.startsWith(summary.modelDir + '/'))))
  summary.models[0].name = 'Changed only in response'
  assert.notEqual((await runtime.invoke('get_model_storage_summary')).models[0].name, summary.models[0].name)
})

test('mix export completes through the tutorial runtime', async t => {
  for (const platform of ['win32', 'darwin']) {
    const runtime = createDemoRuntime({ ...config, platform })
    t.after(() => runtime.dispose())
    const events = []
    const handler = runtime.callback(event => events.push(event.payload))
    await runtime.invoke('plugin:event|listen', { event: 'pymss://worker-event', handler })
    assert.equal((await runtime.invoke('start_editor_mix_export', {
      payload: { taskId: 'editor_export_sample', format: 'wav' },
    })).started, true)
    await new Promise(resolve => setTimeout(resolve, 350))
    assert.ok(events.some(event => event.type === 'editor_mix_exported' && event.taskId === 'editor_export_sample'))
    assert.deepEqual(runtime.unknown, [])
  }
})

test('window commands stay inside the tutorial and workflow writes use its private store', async t => {
  const runtime = createDemoRuntime(config)
  t.after(() => runtime.dispose())
  const opened = []
  runtime.connectWindows(path => opened.push(path), () => opened.push('closed'))
  await runtime.invoke('open_editor_window', { payload: { projectId: 'sample' } })
  assert.equal(opened[0], '/editor?projectId=sample')
  await runtime.invoke('plugin:window|close')
  assert.equal(opened[1], 'closed')
  await runtime.invoke('open_workflow_node_editor_window', { payload: { newWorkflow: true } })
  assert.equal(opened[2], '/workflow-node-editor?new=1')
  const original = await runtime.invoke('load_app_store', { name: 'workflow-state' })
  const entry = { ...original.workflows[0], name: 'Updated in tutorial' }
  await runtime.invoke('mutate_workflow_store', { payload: { action: 'upsert', entry } })
  assert.equal((await runtime.invoke('load_app_store', { name: 'workflow-state' })).workflows[0].name, entry.name)
  const metadata = { guid: '0d971bdd-6b5b-4245-8dd0-aad662b657c9', author: 'Author', version: '1.0.0' }
  const published = await runtime.invoke('mutate_workflow_store', { payload: { action: 'metadata', workflowId: entry.id, metadata } })
  assert.deepEqual(published.state.workflows[0].metadata, metadata)
  assert.deepEqual(published.state.workflows[0].definition, entry.definition)
  await assert.rejects(runtime.invoke('mutate_workflow_store', { payload: {
    action: 'metadata', workflowId: entry.id, requireMatchingGuid: true,
    metadata: { ...metadata, guid: '33333333-3333-4333-8333-333333333333' },
  } }), /WORKFLOW_METADATA_GUID_CONFLICT/)
  assert.deepEqual((await runtime.invoke('load_app_store', { name: 'workflow-state' })).workflows[0].metadata, metadata)
  const saved = await runtime.invoke('mutate_workflow_store', { payload: { action: 'upsert', entry } })
  assert.deepEqual(saved.state.workflows[0].metadata, metadata)
})

test('the native boundary forwards ordinary calls unchanged and never falls back in a tutorial', async t => {
  const source = readFileSync(new URL('../src/utils/tauriCore.ts', import.meta.url), 'utf8')
  const file = ts.createSourceFile('tauriCore.ts', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  const javascript = ts.transpileModule(file.statements.filter(node => !ts.isImportDeclaration(node) && !ts.isExportDeclaration(node))
    .map(node => node.getText(file)).join('\n'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  let frame = false
  const calls = []
  class Resource { constructor(rid) { this.rid = rid } close() { calls.push('close'); return Promise.resolve() } }
  class PluginListener { constructor(plugin, event, channelId) { Object.assign(this, { plugin, event, channelId }) } unregister() { calls.push('unregister'); return Promise.resolve() } }
  const native = {
    Resource, PluginListener,
    invoke: (...args) => { calls.push(args); return Promise.resolve('native-result') },
    convertFileSrc: (...args) => { calls.push(args); return 'native-asset' },
    transformCallback: () => 99,
  }
  const runtime = createDemoRuntime(config)
  t.after(() => runtime.dispose())
  const exports = {}
  vm.runInNewContext(javascript, { exports, native, isTutorialFrame: () => frame, tutorialAdapter: () => runtime })
  const args = { payload: { source: 'real' } }, options = { headers: { trace: 'test' } }
  assert.equal(await exports.invoke('ordinary-command', args, options), 'native-result')
  assert.equal(calls[0][0], 'ordinary-command')
  assert.equal(calls[0][1], args)
  assert.equal(calls[0][2], options)
  frame = true
  const before = calls.length
  await assert.rejects(exports.invoke('ordinary-command', args, options), /not available/)
  assert.ok(exports.convertFileSrc(runtime.input).startsWith('blob:'))
  await new exports.Resource(5).close()
  await assert.rejects(new exports.PluginListener('unknown', 'event', 3).unregister(), /not available/)
  assert.equal(calls.length, before)
})
