import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { createDemoRuntime } from '../src/features/onboarding/demoRuntime.ts'
import { visibleParameterGuides } from '../src/features/onboarding/parameters.ts'
import { BEGINNER_GUIDE_STEPS, normalizeBeginnerGuideProgress } from '../src/features/onboarding/guide.ts'

const config = { locale: 'en', themeMode: 'dark', themeAccent: 'blue', platform: 'win32', progress: { step: 0, stepId: 'workspace' } }

test('current progress resumes by step ID without migrating obsolete formats', () => {
  for (const stepId of ['modelManage', 'inference', 'resultEditor', 'workflowOutput', 'runtime', 'help']) {
    const progress = normalizeBeginnerGuideProgress({ status: 'active', stepId, step: 0 })
    assert.equal(BEGINNER_GUIDE_STEPS[progress.step].id, stepId)
  }
  const resumed = normalizeBeginnerGuideProgress({ status: 'active', stepId: 'inference', parameter: 'presets' })
  assert.equal(resumed.stepId, 'inference')
  assert.equal(resumed.parameter, 'presets')
  assert.deepEqual(normalizeBeginnerGuideProgress({ status: 'active', step: 3 }), { status: 'active', step: 0, stepId: 'workspace' })
  assert.equal(normalizeBeginnerGuideProgress({ status: 'active', stepId: 'toolConvert', step: 13 }).stepId, 'workspace')
  assert.equal(normalizeBeginnerGuideProgress({ status: 'completed', stepId: 'help' }).status, 'completed')
  for (const stepId of ['modelBatch', 'resultManage', 'pluginsOverview']) {
    assert.ok(BEGINNER_GUIDE_STEPS.some(step => step.id === stepId && step.anchor))
  }
})

test('parameter-source and preset lessons follow the actual model capabilities', () => {
  const context = { hasModel: true, defaults: {}, format: 'wav', canSaveDefaults: false }
  assert.equal(visibleParameterGuides(context)[0].id, 'parameterSource')
  assert.equal(visibleParameterGuides(context).some(item => item.id === 'presets'), false)
  assert.deepEqual(visibleParameterGuides({ ...context, supportsPresets: true }).slice(0, 2).map(item => item.id), ['parameterSource', 'presets'])
  assert.equal(visibleParameterGuides({ ...context, hasModel: false, supportsPresets: true }).some(item => ['parameterSource', 'presets'].includes(item.id)), false)
})

test('plugin sources and tool assets are isolated and use the current plugin SDK contract', async t => {
  const runtime = createDemoRuntime(config), other = createDemoRuntime(config)
  t.after(() => { runtime.dispose(); other.dispose() })
  const initial = await runtime.invoke('plugin_marketplace', { payload: { includeCatalog: true } })
  const contract = JSON.parse(readFileSync(new URL('../python/plugin-sdk.json', import.meta.url), 'utf8'))
  assert.equal(initial.installed[0].contributions.views[0].sdkVersion, contract.version)
  const settings = await runtime.invoke('load_app_store', { name: 'app-settings' })
  await runtime.invoke('save_app_store', { name: 'app-settings', data: { ...settings, pluginSource: 'gitcode' } })
  assert.equal((await runtime.invoke('plugin_marketplace')).catalogSource.provider, 'gitcode')
  assert.equal((await other.invoke('plugin_marketplace')).catalogSource.provider, 'github')
  initial.installed[0].name = 'Changed only in response'
  assert.equal((await runtime.invoke('plugin_marketplace')).installed[0].name, 'pymss.convert')
  const url = runtime.assetUrl('pymss-tutorial-plugin:pymss.convert/tool')
  await runtime.invoke('set_plugin_view_root', { root: initial.pluginsDir })
  await assert.rejects(runtime.invoke('set_plugin_view_root', { root: 'C:/Private/Plugins' }), /sample plugin views/)
  const html = await (await fetch(url)).text()
  assert.match(html, /pymss.ready/)
  assert.doesNotMatch(html, /type=["']file["']/)
  assert.throws(() => runtime.assetUrl('pymss-tutorial-plugin:untrusted/tool'), /sample assets/)
  await assert.rejects(runtime.invoke('manage_plugin', { payload: { action: 'install', source: 'https://example.com' } }), /not available/)
  runtime.dispose()
  await assert.rejects(fetch(url))
})

test('batch download and deletion modify only temporary models and cancelled downloads cannot complete', async t => {
  const runtime = createDemoRuntime(config), other = createDemoRuntime(config)
  t.after(() => { runtime.dispose(); other.dispose() })
  const events = []
  const handler = runtime.callback(event => events.push(event.payload))
  await runtime.invoke('plugin:event|listen', { event: 'pymss://worker-event', handler })
  const available = runtime.models.find(model => !model.downloaded)
  assert.ok(available)
  await runtime.invoke('start_model_download', { payload: { model: available.name, taskId: 'download_cancelled' } })
  await runtime.invoke('cancel_task', { taskId: 'download_cancelled' })
  await new Promise(resolve => setTimeout(resolve, 350))
  assert.equal(available.downloaded, false)
  assert.equal(events.some(event => event.taskId === 'download_cancelled' && event.type === 'download_done'), false)
  await runtime.invoke('start_model_download', { payload: { model: available.name, taskId: 'download_completed' } })
  await new Promise(resolve => setTimeout(resolve, 350))
  assert.equal((await runtime.invoke('list_models')).models.find(model => model.name === available.name).downloaded, true)
  assert.equal(other.models.find(model => model.name === available.name).downloaded, false)
  await runtime.invoke('start_model_delete', { payload: { model: available.name, taskId: 'delete_completed' } })
  await new Promise(resolve => setTimeout(resolve, 350))
  assert.equal(available.downloaded, false)
  assert.ok(events.some(event => event.type === 'model_delete_done' && event.taskId === 'delete_completed'))
  await assert.rejects(runtime.invoke('start_model_download', { payload: { model: 'foreign', taskId: 'download_foreign' } }), /Unknown tutorial model/)
})

test('result file cleanup only accepts the session sample outputs', async t => {
  const runtime = createDemoRuntime(config)
  t.after(() => runtime.dispose())
  const paths = runtime.task.outputs.map(item => item.path)
  assert.deepEqual((await runtime.invoke('move_paths_to_trash', { paths })).trashed, paths)
  await assert.rejects(runtime.invoke('move_paths_to_trash', { paths: ['C:/Private/recording.wav'] }), /sample outputs/)
  await assert.rejects(runtime.invoke('move_paths_to_trash', { paths: [runtime.output + '/../private.wav'] }), /sample outputs/)
})

test('native audio plugins remain disabled in the tutorial', async t => {
  const runtime = createDemoRuntime(config)
  t.after(() => runtime.dispose())
  assert.equal((await runtime.invoke('plugin_host_capabilities')).enabled, false)
  await assert.rejects(runtime.invoke('plugin_host_request', { request: { op: 'pause', projectId: runtime.initialProject.id } }), /unavailable/)
  await assert.rejects(runtime.invoke('plugin_host_request', { request: { op: 'load', path: 'C:/Private/effect.vst3' } }), /unavailable/)
  assert.deepEqual(runtime.unknown, [])
})

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
  const downloaded = runtime.models.filter(model => model.downloaded)
  assert.equal(summary.downloadedCount, downloaded.length)
  assert.equal(summary.totalBytes, downloaded.reduce((total, model) => total + model.sizeBytes, 0))
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
